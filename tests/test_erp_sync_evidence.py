from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.application.erp_sync_evidence import (
    ErpSyncEvidenceService,
    ErpSyncEvidenceSettings,
)
from app.application.erp_sync_scheduler import ErpSyncSchedulerSettings
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    Base,
    ErpSyncAttempt,
    ErpSyncEvidenceChunk,
    ErpSyncEvidenceManifest,
    ErpSyncControl,
    Project,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.erp_sync_control_repository import (
    SqlErpSyncControlRepository,
)
from app.server.erp_sync_orchestration import ErpSyncOrchestrator


class ProjectSource:
    def __init__(self, rows) -> None:
        self.rows = tuple(rows)
        self.calls = 0

    def list_projects(self):
        self.calls += 1
        return self.rows


class PortfolioTaskSource:
    def __init__(self, snapshots) -> None:
        self.snapshots = dict(snapshots)
        self.calls = 0

    def fetch_project_snapshots(self, *, project_targets):
        self.calls += 1
        snapshots = tuple(self.snapshots[number] for _external_id, number in project_targets)
        return TaskCatalogPortfolioSnapshot(
            source_rows=sum(snapshot.source_rows for snapshot in snapshots),
            source_pages=1,
            snapshots=snapshots,
        )


def project(number: str, external_id: str = "101") -> ExternalProjectRecord:
    return ExternalProjectRecord(
        external_id=external_id,
        number=number,
        name=f"Projet {number}",
        status="Actif",
    )


def snapshot(number: str, task_id: str = "9001") -> TaskCatalogProjectSnapshot:
    return TaskCatalogProjectSnapshot(
        project_number=number,
        source_rows=1,
        rejected_rows=0,
        items=(
            TaskCatalogItem(
                project_number=number,
                code="100",
                label="Task evidence",
                status="Actif",
                active=True,
                erp_task_id=task_id,
                account_group="DEPMO",
            ),
        ),
    )


class ErpSyncEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "evidence.db"
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(ErpSyncControl(id="ERP_PROJECTS_TASKS", generation=0))
        self.control_settings = ErpSyncSchedulerSettings(enabled=False)
        self.service = ErpSyncEvidenceService(
            self.factory,
            cipher=FernetSecretCipher(Fernet.generate_key().decode("ascii")),
            settings=ErpSyncEvidenceSettings(
                max_evidence_mib=16,
                evidence_budget_mib=32,
            ),
            control_settings=self.control_settings,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def _attempt(self, key: str):
        control = SqlErpSyncControlRepository(self.factory)
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key=key,
            settings=self.control_settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(attempt)
        self.assertTrue(
            control.start_attempt(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.control_settings,
            )
        )
        return control, occurrence, attempt

    def test_source_payload_is_encrypted_and_integrity_checked(self) -> None:
        _control, _occurrence, attempt = self._attempt("encrypted-source")
        payload = [{"external_id": "101", "name": "SENSITIVE-EVIDENCE-MARKER"}]

        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=payload,
            row_count=1,
        )

        with self.factory() as session:
            manifest = session.get(ErpSyncEvidenceManifest, captured.manifest_id)
            chunks = tuple(
                session.scalars(
                    select(ErpSyncEvidenceChunk).where(
                        ErpSyncEvidenceChunk.manifest_id == captured.manifest_id
                    )
                )
            )
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.state, "SEALED")
        self.assertEqual(len(chunks), 1)
        self.assertNotIn("SENSITIVE-EVIDENCE-MARKER", chunks[0].encrypted_payload)
        self.assertEqual(self.service.load_source(manifest_id=captured.manifest_id), payload)

    def test_missing_crypto_fails_closed_before_evidence_write(self) -> None:
        _control, _occurrence, attempt = self._attempt("missing-crypto")
        unavailable = ErpSyncEvidenceService(
            self.factory,
            cipher=None,
            settings=ErpSyncEvidenceSettings(
                max_evidence_mib=16,
                evidence_budget_mib=32,
            ),
            control_settings=self.control_settings,
        )
        with self.assertRaisesRegex(Exception, "chiffrement"):
            unavailable.capture_source(
                attempt_id=attempt.id,
                source_type="PROJECTS",
                profile_fingerprint=self.control_settings.config_fingerprint,
                payload=[{"external_id": "101"}],
                row_count=1,
            )
        self.assertFalse(
            unavailable.automatic_execution_available(
                profile_fingerprint=self.control_settings.config_fingerprint
            )
        )

    def test_full_run_requires_human_baseline_then_retry_reads_sources_again(self) -> None:
        projects = ProjectSource((project("P-643B"),))
        tasks = PortfolioTaskSource({"P-643B": snapshot("P-643B")})
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.control_settings,
            evidence_service=self.service,
        )
        control = SqlErpSyncControlRepository(self.factory)

        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="bootstrap-baseline",
            settings=self.control_settings,
        )
        first = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(first)
        orchestrator._execute_attempt(first.id, first.generation)

        failed = control.get_attempt(first.id)
        self.assertEqual(failed.status, "FAILED")
        with self.factory() as session:
            self.assertIsNone(
                session.scalar(select(Project).where(Project.number == "P-643B"))
            )
            manifests = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.attempt_id == first.id
                    )
                )
            )
        self.assertEqual({row.source_type for row in manifests}, {"PROJECTS", "TASKS"})
        for manifest in manifests:
            self.service.approve_baseline(
                manifest_id=manifest.id,
                actor_name="admin",
                reason="Bootstrap reviewed against complete source",
            )

        retry = control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.control_settings,
            command_identity="retry:643b-bootstrap",
        )
        self.assertIsNotNone(retry)
        orchestrator._execute_attempt(retry.id, retry.generation)

        completed = control.get_attempt(retry.id)
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertEqual(projects.calls, 2)
        self.assertEqual(tasks.calls, 2)
        with self.factory() as session:
            self.assertIsNotNone(
                session.scalar(select(Project).where(Project.number == "P-643B"))
            )
            self.assertIsNotNone(
                session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == "P-643B",
                        TaskCatalogEntry.erp_task_id == "9001",
                    )
                )
            )

    def test_exact_source_plausibility_exception_does_not_bypass_other_source(self) -> None:
        _control, _occurrence, baseline_attempt = self._attempt("baseline-large")
        baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=[{"number": str(index)} for index in range(20)],
            row_count=20,
        )
        self.service.approve_baseline(
            manifest_id=baseline.manifest_id,
            actor_name="admin",
            reason="Known complete baseline",
        )
        SqlErpSyncControlRepository(self.factory).finish(
            attempt_id=baseline_attempt.id,
            generation=baseline_attempt.generation,
            status="FAILED",
            phase="FAILED",
            settings=self.control_settings,
        )

        _control, _occurrence, attempt = self._attempt("small-source")
        small = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=[{"number": "1"}],
            row_count=1,
        )
        rejected = self.service.validate_plausibility(small)
        self.assertFalse(rejected.accepted)

        self.service.approve_plausibility_exception(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            source_fingerprint=small.source_fingerprint,
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Confirmed planned source reduction",
        )
        self.assertTrue(self.service.validate_plausibility(small).accepted)

        different = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=[{"different": True}],
            row_count=0,
        )
        self.assertFalse(
            self.service.validate_plausibility(
                different,
                active_task_targets=1,
            ).accepted
        )


if __name__ == "__main__":
    unittest.main()
