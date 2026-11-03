from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.server.erp_sync_evidence import (
    ERP_EVIDENCE_CODEC_VERSION,
    ErpSyncEvidenceService,
    ErpSyncEvidenceSettings,
    canonical_json_bytes,
    decode_canonical_json_bytes,
)
from app.application.erp_sync_scheduler import ErpSyncSchedulerSettings
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    AcumaticaProjectTaskSyncRun,
    Base,
    ErpSyncAttempt,
    ErpSyncEvidenceChunk,
    ErpSyncEvidenceManifest,
    ErpSyncEvidenceReservation,
    ErpSyncMutationEvidence,
    ErpSyncBaselineApprovalAudit,
    ErpSyncProjectQuality,
    ErpSyncRecoveryState,
    ErpSyncControl,
    Project,
    ResourceClassConfig,
    TaskCatalogEntry,
    TaskClassStandard,
    WorkPackage,
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
            session.add_all(
                [
                    ErpSyncControl(id="ERP_PROJECTS_TASKS", generation=0),
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        average_hourly_cost_cad=Decimal("125.0000"),
                        active=True,
                        version=1,
                    ),
                    TaskClassStandard(
                        task_code="100",
                        resource_class_code="PROGRAMMEUR",
                        active=True,
                        version=1,
                    ),
                ]
            )
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

    def test_typed_codec_round_trips_datetime_decimal_and_multibyte_payload(self) -> None:
        observed = datetime(2026, 11, 3, 4, 59, 59, tzinfo=timezone.utc)
        payload = {
            "observed_at": observed,
            "amount": Decimal("125.0000"),
            "accented": "é" * (530 * 1024),
            "flags": [True, False, None, 7, "texte"],
        }
        decoded = decode_canonical_json_bytes(canonical_json_bytes(payload))
        self.assertEqual(decoded["observed_at"], observed)
        self.assertEqual(decoded["amount"], Decimal("125.0000"))
        self.assertEqual(decoded["accented"], payload["accented"])

        _control, _occurrence, attempt = self._attempt("multibyte-codec")
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=payload,
            row_count=1,
        )
        loaded = self.service.load_source(manifest_id=captured.manifest_id)
        self.assertEqual(loaded["observed_at"], observed)
        self.assertEqual(loaded["amount"], Decimal("125.0000"))
        self.assertEqual(loaded["accented"], payload["accented"])
        with self.factory() as session:
            manifest = session.get(ErpSyncEvidenceManifest, captured.manifest_id)
            self.assertGreater(int(manifest.chunk_count), 1)

    def test_unknown_typed_codec_version_fails_closed(self) -> None:
        payload = (
            '{"codec_version":'
            + str(ERP_EVIDENCE_CODEC_VERSION + 1)
            + ',"payload":{"value":"future"}}'
        ).encode("utf-8")
        with self.assertRaises(Exception) as raised:
            decode_canonical_json_bytes(payload)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_evidence_codec_incompatible",
        )

    def test_future_evidence_protocol_is_persistable_but_current_reader_fails_closed(self) -> None:
        _control, _occurrence, attempt = self._attempt("future-evidence-protocol")
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "P-FUTURE"}]},
            row_count=1,
        )
        with transactional_session(self.factory) as session:
            session.get(
                ErpSyncEvidenceManifest,
                captured.manifest_id,
            ).protocol_version = 2

        with self.assertRaises(Exception) as raised:
            self.service.load_source(manifest_id=captured.manifest_id)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_evidence_protocol_incompatible",
        )

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

    def test_duplicate_project_identity_blocks_before_any_business_mutation(self) -> None:
        projects = ProjectSource(
            (
                project("DUP-1", external_id="5001"),
                project("DUP-2", external_id="5001"),
            )
        )
        tasks = PortfolioTaskSource({})
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
            idempotency_key="duplicate-project-source",
            settings=self.control_settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(attempt)

        orchestrator._execute_attempt(attempt.id, attempt.generation)

        failed = control.get_attempt(attempt.id)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.error_code, "erp_sync_project_source_duplicate_identity")
        self.assertEqual(tasks.calls, 0)
        with self.factory() as session:
            self.assertEqual(
                int(session.scalar(select(func.count(Project.id))) or 0),
                0,
            )
        orchestrator.shutdown()

    def test_malformed_task_source_blocks_before_project_mutation(self) -> None:
        projects = ProjectSource((project("P-MAL", external_id="5002"),))
        malformed = TaskCatalogProjectSnapshot(
            project_number="P-MAL",
            source_rows=1,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="P-MAL",
                    code="100",
                    label="Missing TaskID",
                    status="Actif",
                    active=True,
                    erp_task_id=None,
                    account_group="DEPMO",
                ),
            ),
        )
        tasks = PortfolioTaskSource({"P-MAL": malformed})
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
            idempotency_key="malformed-task-source",
            settings=self.control_settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(attempt)

        orchestrator._execute_attempt(attempt.id, attempt.generation)

        failed = control.get_attempt(attempt.id)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.error_code, "erp_sync_task_source_identity_missing")
        with self.factory() as session:
            self.assertIsNone(
                session.scalar(select(Project).where(Project.number == "P-MAL"))
            )
        self.assertEqual(projects.calls, 1)
        self.assertEqual(tasks.calls, 1)
        orchestrator.shutdown()

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
                        ErpSyncEvidenceManifest.attempt_id == first.id,
                        ErpSyncEvidenceManifest.proof_type == "SOURCE",
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
        with self.factory() as session:
            approvals = tuple(session.scalars(select(ErpSyncBaselineApprovalAudit)))
        self.assertEqual(len(approvals), 2)
        self.assertTrue(all(row.actor_name == "admin" for row in approvals))

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

    def test_task_unit_commits_eligible_row_skips_new_standard_exclusion_and_uses_source_time(self) -> None:
        control, _occurrence, attempt = self._attempt("task-exclusion-source-time")
        self.service.reserve_attempt(attempt_id=attempt.id)
        self.service.bind_attempt_policy(
            attempt_id=attempt.id,
            generation=attempt.generation,
        )
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="P-EXCLUSION-ID",
                    erp_external_id="8101",
                    number="P-EXCLUSION",
                    name="Project exclusion",
                    status="Actif",
                )
            )

        eligible = TaskCatalogItem(
            project_number="P-EXCLUSION",
            code="100",
            label="Eligible",
            status="Actif",
            active=True,
            erp_task_id="98100",
            account_group="DEPMO",
        )
        excluded = TaskCatalogItem(
            project_number="P-EXCLUSION",
            code="999",
            label="Outside Standards",
            status="Actif",
            active=True,
            erp_task_id="98999",
            account_group="DEPMO",
        )
        task_snapshot = TaskCatalogProjectSnapshot(
            project_number="P-EXCLUSION",
            source_rows=2,
            rejected_rows=0,
            items=(eligible, excluded),
        )
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=2,
                    source_pages=1,
                    snapshots=(task_snapshot,),
                )
            },
            row_count=2,
            project_counts={"P-EXCLUSION": 2},
        )
        observed = datetime(2026, 11, 3, 4, 59, 59, tzinfo=timezone.utc)
        with transactional_session(self.factory) as session:
            session.get(ErpSyncEvidenceManifest, captured.manifest_id).observed_at = observed

        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=PortfolioTaskSource({"P-EXCLUSION": task_snapshot}),
            settings=self.control_settings,
            evidence_service=self.service,
        )
        run_id = orchestrator._create_task_run(attempt.id, attempt.generation)
        result = orchestrator._process_task_project(
            run_id,
            attempt.id,
            attempt.generation,
            SimpleNamespace(
                id="P-EXCLUSION-ID",
                number="P-EXCLUSION",
                active=True,
                erp_external_id="8101",
            ),
            {"P-EXCLUSION": task_snapshot},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.ignored, 1)

        with self.factory() as session:
            eligible_row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "98100"
                )
            )
            excluded_row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "98999"
                )
            )
            evidence_rows = tuple(
                session.scalars(
                    select(ErpSyncMutationEvidence).where(
                        ErpSyncMutationEvidence.attempt_id == attempt.id,
                        ErpSyncMutationEvidence.entity_type == "TASK",
                    )
                )
            )
        self.assertIsNotNone(eligible_row)
        self.assertEqual(eligible_row.erp_budget_last_success_at, observed)
        self.assertIsNone(excluded_row)
        self.assertEqual(len(evidence_rows), 1)
        self.assertEqual(evidence_rows[0].entity_key, "98100")
        self.assertIsNotNone(evidence_rows[0].after_sha256)
        self.assertIsNotNone(evidence_rows[0].committed_at)
        orchestrator.shutdown()

    def test_existing_task_becoming_standard_excluded_records_real_transition(self) -> None:
        control, _occurrence, attempt = self._attempt("existing-task-excluded")
        self.service.reserve_attempt(attempt_id=attempt.id)
        self.service.bind_attempt_policy(
            attempt_id=attempt.id,
            generation=attempt.generation,
        )
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="EXISTING-EXCLUDED-PROJECT",
                    erp_external_id="8111",
                    number="EXISTING-EXCLUDED",
                    name="Existing excluded",
                    status="Actif",
                )
            )
            session.add(
                TaskCatalogEntry(
                    project_number="EXISTING-EXCLUDED",
                    task_code="999",
                    label="Previously eligible",
                    status="Actif",
                    active=True,
                    erp_task_id="98998",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    resource_class_code="PROGRAMMEUR",
                    average_hourly_cost_cad=Decimal("125.0000"),
                )
            )
        excluded_item = TaskCatalogItem(
            project_number="EXISTING-EXCLUDED",
            code="999",
            label="Now outside Standards",
            status="Actif",
            active=True,
            erp_task_id="98998",
            account_group="DEPMO",
        )
        excluded_snapshot = TaskCatalogProjectSnapshot(
            project_number="EXISTING-EXCLUDED",
            source_rows=1,
            rejected_rows=0,
            items=(excluded_item,),
        )
        self.service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=1,
                    source_pages=1,
                    snapshots=(excluded_snapshot,),
                )
            },
            row_count=1,
            project_counts={"EXISTING-EXCLUDED": 1},
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=PortfolioTaskSource(
                {"EXISTING-EXCLUDED": excluded_snapshot}
            ),
            settings=self.control_settings,
            evidence_service=self.service,
        )
        run_id = orchestrator._create_task_run(attempt.id, attempt.generation)
        result = orchestrator._process_task_project(
            run_id,
            attempt.id,
            attempt.generation,
            SimpleNamespace(
                id="EXISTING-EXCLUDED-PROJECT",
                number="EXISTING-EXCLUDED",
                active=True,
                erp_external_id="8111",
            ),
            {"EXISTING-EXCLUDED": excluded_snapshot},
        )
        self.assertIsNotNone(result)
        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "98998"
                )
            )
            evidence = session.scalar(
                select(ErpSyncMutationEvidence).where(
                    ErpSyncMutationEvidence.attempt_id == attempt.id,
                    ErpSyncMutationEvidence.entity_type == "TASK",
                    ErpSyncMutationEvidence.entity_key == "98998",
                )
            )
        self.assertIsNotNone(row)
        self.assertFalse(row.workforce_eligible)
        self.assertIsNone(row.resource_class_code)
        self.assertIsNotNone(evidence)
        self.assertTrue(evidence.before_present)
        self.assertIsNotNone(evidence.after_sha256)
        self.assertIsNotNone(evidence.committed_at)
        orchestrator.shutdown()

    def test_policy_change_between_project_units_preserves_first_and_blocks_second(self) -> None:
        control, _occurrence, attempt = self._attempt("policy-change-between-projects")
        self.service.reserve_attempt(attempt_id=attempt.id)
        self.service.bind_attempt_policy(
            attempt_id=attempt.id,
            generation=attempt.generation,
        )
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    Project(
                        id="POLICY-A-ID",
                        erp_external_id="8201",
                        number="POLICY-A",
                        name="Policy A",
                        status="Actif",
                    ),
                    Project(
                        id="POLICY-B-ID",
                        erp_external_id="8202",
                        number="POLICY-B",
                        name="Policy B",
                        status="Actif",
                    ),
                ]
            )
        snapshot_a = snapshot("POLICY-A", task_id="98201")
        snapshot_b = snapshot("POLICY-B", task_id="98202")
        self.service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=2,
                    source_pages=1,
                    snapshots=(snapshot_a, snapshot_b),
                )
            },
            row_count=2,
            project_counts={"POLICY-A": 1, "POLICY-B": 1},
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=PortfolioTaskSource(
                {"POLICY-A": snapshot_a, "POLICY-B": snapshot_b}
            ),
            settings=self.control_settings,
            evidence_service=self.service,
        )
        run_id = orchestrator._create_task_run(attempt.id, attempt.generation)
        with transactional_session(self.factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            run.projects_total = 2

        result_a = orchestrator._process_task_project(
            run_id,
            attempt.id,
            attempt.generation,
            SimpleNamespace(
                id="POLICY-A-ID",
                number="POLICY-A",
                active=True,
                erp_external_id="8201",
            ),
            {"POLICY-A": snapshot_a, "POLICY-B": snapshot_b},
        )
        self.assertIsNotNone(result_a)
        with transactional_session(self.factory) as session:
            session.get(
                ResourceClassConfig,
                "PROGRAMMEUR",
            ).average_hourly_cost_cad = Decimal("250.0000")

        result_b = orchestrator._process_task_project(
            run_id,
            attempt.id,
            attempt.generation,
            SimpleNamespace(
                id="POLICY-B-ID",
                number="POLICY-B",
                active=True,
                erp_external_id="8202",
            ),
            {"POLICY-A": snapshot_a, "POLICY-B": snapshot_b},
        )
        self.assertIsNone(result_b)
        with self.factory() as session:
            task_a = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "98201"
                )
            )
            task_b = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "98202"
                )
            )
            receipts = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncRun)
                    .where(AcumaticaProjectTaskSyncRun.id == run_id)
                )
            )
            from app.infrastructure.sql import AcumaticaProjectTaskSyncProjectResult
            project_results = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncProjectResult)
                    .where(AcumaticaProjectTaskSyncProjectResult.run_id == run_id)
                    .order_by(AcumaticaProjectTaskSyncProjectResult.project_number)
                )
            )
        self.assertIsNotNone(task_a)
        self.assertEqual(
            task_a.average_hourly_cost_cad,
            Decimal("125.0000"),
        )
        self.assertIsNone(task_b)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            [(row.project_number, row.status, row.error_code) for row in project_results],
            [
                ("POLICY-A", "synchronized", None),
                ("POLICY-B", "rejected", "erp_sync_policy_fingerprint_changed"),
            ],
        )
        orchestrator.shutdown()

    def test_global_hard_validation_failure_quarantines_provenance_without_rollback(self) -> None:
        projects = ProjectSource((project("P-GLOBAL", external_id="6001"),))
        tasks = PortfolioTaskSource({"P-GLOBAL": snapshot("P-GLOBAL", task_id="96001")})
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
            idempotency_key="global-hard-validation",
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
        self.assertEqual(control.get_attempt(first.id).status, "FAILED")
        with self.factory() as session:
            source_manifests = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.attempt_id == first.id,
                        ErpSyncEvidenceManifest.proof_type == "SOURCE",
                    )
                )
            )
        for manifest in source_manifests:
            self.service.approve_baseline(
                manifest_id=manifest.id,
                actor_name="admin",
                reason="Reviewed bootstrap for global hard validation",
            )

        retry = control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.control_settings,
            command_identity="retry:global-hard-validation",
        )
        self.assertIsNotNone(retry)
        original_publish = orchestrator._publish_full_success

        def corrupt_then_publish(*, attempt_id: str, generation: int) -> None:
            attempt = control.get_attempt(attempt_id)
            self.assertIsNotNone(attempt.task_sync_run_id)
            with transactional_session(self.factory) as session:
                quality = session.get(ErpSyncProjectQuality, "P-GLOBAL")
                self.assertIsNotNone(quality)
                quality.quality_state = "QUARANTINED"
                quality.reason_code = "injected_prepublication_quarantine"
            original_publish(
                attempt_id=attempt_id,
                generation=generation,
            )

        with patch.object(
            orchestrator,
            "_publish_full_success",
            side_effect=corrupt_then_publish,
        ):
            orchestrator._execute_attempt(retry.id, retry.generation)

        failed = control.get_attempt(retry.id)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.error_code, "erp_sync_global_validation_failed")
        self.assertIsNone(control.get_occurrence(occurrence.id).success_attempt_id)
        with self.factory() as session:
            self.assertIsNotNone(
                session.scalar(select(Project).where(Project.number == "P-GLOBAL"))
            )
            self.assertIsNotNone(
                session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == "P-GLOBAL",
                        TaskCatalogEntry.erp_task_id == "96001",
                    )
                )
            )
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == retry.id
                )
            )
            quality = session.get(ErpSyncProjectQuality, "P-GLOBAL")
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.state, "REQUIRED")
            self.assertTrue(recovery.pinned)
            self.assertIsNotNone(quality)
            self.assertEqual(quality.quality_state, "QUARANTINED")
            self.assertEqual(quality.producer_attempt_id, retry.id)
        with self.assertRaisesRegex(Exception, "récupération ERP requise"):
            orchestrator.run_now(
                intent="PROJECTS_AND_ACTIVE_TASKS",
                actor_name="operator",
                idempotency_key="blocked-after-recovery",
            )
        orchestrator.shutdown()

    def test_plausibility_exception_is_attempt_scoped_even_for_identical_retry_source(self) -> None:
        control, occurrence, first = self._attempt("exception-source-first")
        baseline = self.service.capture_source(
            attempt_id=first.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": str(index)} for index in range(20)]},
            row_count=20,
        )
        self.service.approve_baseline(
            manifest_id=baseline.manifest_id,
            actor_name="admin",
            reason="Approved comparable population",
        )
        self.assertTrue(
            control.finish(
                attempt_id=first.id,
                generation=first.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(attempt_id=first.id, successful=False)

        retry_one = control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.control_settings,
            command_identity="retry:exception-one",
        )
        self.assertIsNotNone(retry_one)
        reviewed = self.service.capture_source(
            attempt_id=retry_one.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "1"}]},
            row_count=1,
        )
        self.service.bind_attempt_policy(
            attempt_id=retry_one.id,
            generation=retry_one.generation,
        )
        self.assertFalse(self.service.validate_plausibility(reviewed).accepted)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=retry_one.id,
                generation=retry_one.generation,
                settings=self.control_settings,
            )
        )
        self.service.approve_plausibility_exception(
            attempt_id=retry_one.id,
            source_type="PROJECTS",
            source_fingerprint=reviewed.source_fingerprint,
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Reviewed planned reduction",
        )
        self.assertTrue(self.service.validate_plausibility(reviewed).accepted)
        resumed_one = control.resume_review_attempt(
            attempt_id=retry_one.id,
            settings=self.control_settings,
        )
        self.assertEqual(resumed_one.id, retry_one.id)
        self.assertTrue(
            control.finish(
                attempt_id=retry_one.id,
                generation=resumed_one.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(attempt_id=retry_one.id, successful=False)

        retry_two = control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.control_settings,
            command_identity="retry:exception-two",
        )
        self.assertIsNotNone(retry_two)
        identical = self.service.capture_source(
            attempt_id=retry_two.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "1"}]},
            row_count=1,
        )
        self.assertEqual(identical.source_fingerprint, reviewed.source_fingerprint)
        self.service.bind_attempt_policy(
            attempt_id=retry_two.id,
            generation=retry_two.generation,
        )
        self.assertFalse(self.service.validate_plausibility(identical).accepted)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=retry_two.id,
                generation=retry_two.generation,
                settings=self.control_settings,
            )
        )
        self.service.approve_plausibility_exception(
            attempt_id=retry_two.id,
            source_type="PROJECTS",
            source_fingerprint=identical.source_fingerprint,
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Fresh retry requires its own explicit approval",
        )
        self.assertTrue(self.service.validate_plausibility(identical).accepted)
        resumed_two = control.resume_review_attempt(
            attempt_id=retry_two.id,
            settings=self.control_settings,
        )
        self.assertTrue(
            control.finish(
                attempt_id=retry_two.id,
                generation=resumed_two.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(attempt_id=retry_two.id, successful=False)

        retry_three = control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.control_settings,
            command_identity="retry:exception-three",
        )
        self.assertIsNotNone(retry_three)
        changed = self.service.capture_source(
            attempt_id=retry_three.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "2"}]},
            row_count=1,
        )
        self.assertNotEqual(changed.source_fingerprint, reviewed.source_fingerprint)
        self.assertFalse(self.service.validate_plausibility(changed).accepted)

    def test_project_local_plausibility_drop_rejects_only_that_project(self) -> None:
        control, _baseline_occurrence, baseline_attempt = self._attempt(
            "plausibility-local-baseline"
        )
        projects_baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "records": [
                    {
                        "external_id": "8301",
                        "number": "PL-A",
                        "name": "A",
                        "status": "Actif",
                    },
                    {
                        "external_id": "8302",
                        "number": "PL-B",
                        "name": "B",
                        "status": "Actif",
                    },
                ]
            },
            row_count=2,
        )
        baseline_a = TaskCatalogProjectSnapshot(
            project_number="PL-A",
            source_rows=100,
            rejected_rows=0,
            items=(snapshot("PL-A", task_id="98301").items[0],),
        )
        baseline_b = TaskCatalogProjectSnapshot(
            project_number="PL-B",
            source_rows=100,
            rejected_rows=0,
            items=(snapshot("PL-B", task_id="98302").items[0],),
        )
        tasks_baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=200,
                    source_pages=1,
                    snapshots=(baseline_a, baseline_b),
                )
            },
            row_count=200,
            project_counts={"PL-A": 100, "PL-B": 100},
        )
        for captured in (projects_baseline, tasks_baseline):
            self.service.approve_baseline(
                manifest_id=captured.manifest_id,
                actor_name="admin",
                reason="Approved balanced project baseline",
            )
        self.assertTrue(
            control.finish(
                attempt_id=baseline_attempt.id,
                generation=baseline_attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(
            attempt_id=baseline_attempt.id,
            successful=False,
        )

        current_a = TaskCatalogProjectSnapshot(
            project_number="PL-A",
            source_rows=1,
            rejected_rows=0,
            items=(snapshot("PL-A", task_id="98401").items[0],),
        )
        current_b = TaskCatalogProjectSnapshot(
            project_number="PL-B",
            source_rows=199,
            rejected_rows=0,
            items=(snapshot("PL-B", task_id="98402").items[0],),
        )
        projects = ProjectSource(
            (
                project("PL-A", external_id="8301"),
                project("PL-B", external_id="8302"),
            )
        )
        tasks = PortfolioTaskSource({"PL-A": current_a, "PL-B": current_b})
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.control_settings,
            evidence_service=self.service,
        )
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="plausibility-local-current",
            settings=self.control_settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(attempt)
        orchestrator._execute_attempt(attempt.id, attempt.generation)

        completed = control.get_attempt(attempt.id)
        self.assertEqual(completed.status, "SUCCEEDED_WITH_ERRORS")
        with self.factory() as session:
            task_a = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "PL-A",
                    TaskCatalogEntry.erp_task_id == "98401",
                )
            )
            task_b = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "PL-B",
                    TaskCatalogEntry.erp_task_id == "98402",
                )
            )
            quality_a = session.get(ErpSyncProjectQuality, "PL-A")
            quality_b = session.get(ErpSyncProjectQuality, "PL-B")
        self.assertIsNone(task_a)
        self.assertIsNotNone(task_b)
        self.assertIsNotNone(quality_a)
        self.assertEqual(quality_a.quality_state, "STALE")
        self.assertEqual(quality_a.producer_attempt_id, attempt.id)
        self.assertIsNone(quality_a.source_observed_at)
        self.assertEqual(
            quality_a.reason_code,
            "erp_sync_project_plausibility_rejected",
        )
        self.assertIsNotNone(quality_b)
        self.assertEqual(quality_b.quality_state, "VALID")
        self.assertEqual(quality_b.producer_attempt_id, attempt.id)
        orchestrator.shutdown()

    def test_intentionally_empty_task_target_set_does_not_fail_positive_baseline(self) -> None:
        control, _baseline_occurrence, baseline_attempt = self._attempt(
            "empty-target-baseline"
        )
        baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=100,
                    source_pages=1,
                    snapshots=(
                        TaskCatalogProjectSnapshot(
                            project_number="EMPTY-OLD",
                            source_rows=100,
                            rejected_rows=0,
                            items=(),
                        ),
                    ),
                )
            },
            row_count=100,
            project_counts={"EMPTY-OLD": 100},
        )
        self.service.approve_baseline(
            manifest_id=baseline.manifest_id,
            actor_name="admin",
            reason="Approved historical positive task baseline",
        )
        self.assertTrue(
            control.finish(
                attempt_id=baseline_attempt.id,
                generation=baseline_attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(
            attempt_id=baseline_attempt.id,
            successful=False,
        )

        _control, _occurrence, current_attempt = self._attempt(
            "empty-target-current"
        )
        current = self.service.capture_source(
            attempt_id=current_attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "portfolio": TaskCatalogPortfolioSnapshot(
                    source_rows=0,
                    source_pages=0,
                    snapshots=(),
                )
            },
            row_count=0,
            project_counts={},
        )
        result = self.service.validate_plausibility(
            current,
            active_task_targets=0,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.rejected_projects, ())

    def test_plausibility_review_continues_same_attempt_after_restart_without_reread(self) -> None:
        control, _baseline_occurrence, baseline_attempt = self._attempt(
            "review-baseline"
        )
        project_baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={
                "records": [
                    {
                        "external_id": str(7000 + index),
                        "number": f"B-{index}",
                        "name": f"Baseline {index}",
                        "status": "Actif",
                    }
                    for index in range(20)
                ]
            },
            row_count=20,
        )
        baseline_portfolio = TaskCatalogPortfolioSnapshot(
            source_rows=20,
            source_pages=1,
            snapshots=(
                TaskCatalogProjectSnapshot(
                    project_number="P-REVIEW",
                    source_rows=20,
                    rejected_rows=0,
                    items=(snapshot("P-REVIEW").items[0],),
                ),
            ),
        )
        task_baseline = self.service.capture_source(
            attempt_id=baseline_attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"portfolio": baseline_portfolio},
            row_count=20,
            project_counts={"P-REVIEW": 20},
        )
        for captured in (project_baseline, task_baseline):
            self.service.approve_baseline(
                manifest_id=captured.manifest_id,
                actor_name="admin",
                reason="Reviewed complete reference population",
            )
        self.assertTrue(
            control.finish(
                attempt_id=baseline_attempt.id,
                generation=baseline_attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(
            attempt_id=baseline_attempt.id,
            successful=False,
        )

        projects = ProjectSource((project("P-REVIEW", external_id="7101"),))
        tasks = PortfolioTaskSource({"P-REVIEW": snapshot("P-REVIEW", task_id="97101")})
        first_process = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.control_settings,
            evidence_service=self.service,
        )
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="review-continuation",
            settings=self.control_settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.control_settings,
        )
        self.assertIsNotNone(attempt)
        first_process._execute_attempt(attempt.id, attempt.generation)
        paused = control.get_attempt(attempt.id)
        self.assertEqual(paused.status, "PENDING")
        self.assertEqual(paused.phase, "PLAUSIBILITY_REVIEW_REQUIRED")
        self.assertEqual(projects.calls, 1)
        self.assertEqual(tasks.calls, 1)
        with self.factory() as session:
            self.assertIsNone(
                session.scalar(select(Project).where(Project.number == "P-REVIEW"))
            )
        first_process.shutdown()

        manifests = {
            row["source_type"]: row
            for row in self.service.list_attempt_sources(attempt_id=attempt.id)
            if row["proof_type"] == "SOURCE"
        }
        self.service.approve_plausibility_exception(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            source_fingerprint=str(manifests["PROJECTS"]["source_fingerprint"]),
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Reviewed planned project population reduction",
        )
        self.service.approve_plausibility_exception(
            attempt_id=attempt.id,
            source_type="TASKS",
            source_fingerprint=str(manifests["TASKS"]["source_fingerprint"]),
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Reviewed planned task population reduction",
        )
        self.service.approve_plausibility_exception(
            attempt_id=attempt.id,
            source_type="TASKS",
            source_fingerprint=str(manifests["TASKS"]["source_fingerprint"]),
            threshold_scope="P-REVIEW",
            actor_name="admin",
            reason="Reviewed project-local task reduction",
        )

        second_process = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.control_settings,
            evidence_service=self.service,
        )
        resumed = second_process.continue_plausibility_review(
            attempt_id=attempt.id,
            actor_name="admin",
        )
        self.assertEqual(resumed["attempt_id"], attempt.id)
        second_process.shutdown()

        completed = control.get_attempt(attempt.id)
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertEqual(projects.calls, 1)
        self.assertEqual(tasks.calls, 1)
        with self.factory() as session:
            self.assertIsNotNone(
                session.scalar(select(Project).where(Project.number == "P-REVIEW"))
            )
            self.assertIsNotNone(
                session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == "P-REVIEW",
                        TaskCatalogEntry.erp_task_id == "97101",
                    )
                )
            )
            attempts = tuple(
                session.scalars(
                    select(ErpSyncAttempt).where(
                        ErpSyncAttempt.occurrence_id == occurrence.id
                    )
                )
            )
        self.assertEqual(len(attempts), 1)

    def test_task_project_mutation_unit_identity_accepts_uuid_scope_and_remains_unique(self) -> None:
        control, _occurrence, attempt = self._attempt("task-project-unit-identity")
        self.service.reserve_attempt(attempt_id=attempt.id)
        project_id = str(uuid4())
        unit_type = f"TASK_PROJECT:{project_id}"
        self.assertEqual(len(project_id), 36)
        self.assertLessEqual(len(unit_type), 64)

        with transactional_session(self.factory) as session:
            first_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type=unit_type,
                entity_type="TASK",
                entity_key="ERP-TASK-UUID",
                before=None,
            )
            replay_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type=unit_type,
                entity_type="TASK",
                entity_key="ERP-TASK-UUID",
                before=None,
            )
            second_project_unit = f"TASK_PROJECT:{uuid4()}"
            second_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type=second_project_unit,
                entity_type="TASK",
                entity_key="ERP-TASK-UUID",
                before=None,
            )

        self.assertEqual(first_id, replay_id)
        self.assertNotEqual(first_id, second_id)
        with self.factory() as session:
            rows = tuple(
                session.scalars(
                    select(ErpSyncMutationEvidence)
                    .where(ErpSyncMutationEvidence.attempt_id == attempt.id)
                    .order_by(ErpSyncMutationEvidence.unit_type)
                )
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.unit_type for row in rows},
            {unit_type, second_project_unit},
        )

    def test_compensation_restores_only_erp_owned_fields_and_is_idempotent(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation")
        self.service.reserve_attempt(attempt_id=attempt.id)
        with transactional_session(self.factory) as session:
            row = Project(
                id="COMP-PROJECT",
                erp_external_id="7001",
                number="COMP-1",
                name="Avant",
                client="Client A",
                status="Actif",
                co_managers_version=7,
            )
            session.add(row)
            session.flush()
            before = {
                "erp_external_id": row.erp_external_id,
                "number": row.number,
                "name": row.name,
                "client": row.client,
                "project_manager_external_id": row.project_manager_external_id,
                "project_manager_name": row.project_manager_name,
                "status": row.status,
            }
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key="7001",
                before=before,
            )
            row.name = "Après"
            row.client = "Client B"
            row.status = "Terminé"
            after = {
                "erp_external_id": row.erp_external_id,
                "number": row.number,
                "name": row.name,
                "client": row.client,
                "project_manager_external_id": row.project_manager_external_id,
                "project_manager_name": row.project_manager_name,
                "status": row.status,
            }
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after=after,
            )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="global_validation_failed",
        )

        preview = self.service.compensation_preview(attempt_id=attempt.id)
        self.assertTrue(preview["can_confirm"])
        self.assertEqual(preview["conflict_count"], 0)

        first = self.service.confirm_compensation(
            attempt_id=attempt.id,
            preview_hash=preview["preview_hash"],
            command_identity="compensation:one",
            actor_name="admin",
            reason="Restore accepted ERP fields",
        )
        second = self.service.confirm_compensation(
            attempt_id=attempt.id,
            preview_hash=preview["preview_hash"],
            command_identity="compensation:one",
            actor_name="admin",
            reason="Replay",
        )

        self.assertEqual(first, second)
        self.assertEqual(first["affected_records"], 1)
        with self.factory() as session:
            restored = session.get(Project, "COMP-PROJECT")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.name, "Avant")
            self.assertEqual(restored.client, "Client A")
            self.assertEqual(restored.status, "Actif")
            self.assertEqual(restored.co_managers_version, 7)

    def test_compensation_rejects_elapsed_expiration_before_purge(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-expired")
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "EXP"}]},
            row_count=1,
        )
        with transactional_session(self.factory) as session:
            row = Project(
                id="COMP-EXPIRED",
                erp_external_id="EXP-1",
                number="COMP-EXP",
                name="Expired",
                status="Actif",
            )
            session.add(row)
            session.flush()
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key="EXP-1",
                before=None,
            )
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after={
                    "erp_external_id": row.erp_external_id,
                    "number": row.number,
                    "name": row.name,
                    "client": row.client,
                    "project_manager_external_id": row.project_manager_external_id,
                    "project_manager_name": row.project_manager_name,
                    "status": row.status,
                },
            )
            session.get(
                ErpSyncEvidenceManifest,
                captured.manifest_id,
            ).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        with self.assertRaises(Exception) as raised:
            self.service.compensation_preview(attempt_id=attempt.id)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_compensation_evidence_expired",
        )

    def test_compensation_rejects_tampered_before_ciphertext(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-before-tamper")
        self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "TAMPER"}]},
            row_count=1,
        )
        with transactional_session(self.factory) as session:
            row = Project(
                id="COMP-TAMPER",
                erp_external_id="TAMPER-1",
                number="COMP-TAMPER",
                name="Before",
                status="Actif",
            )
            session.add(row)
            session.flush()
            before = {
                "erp_external_id": row.erp_external_id,
                "number": row.number,
                "name": row.name,
                "client": row.client,
                "project_manager_external_id": row.project_manager_external_id,
                "project_manager_name": row.project_manager_name,
                "status": row.status,
            }
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key=row.erp_external_id,
                before=before,
            )
            row.name = "After"
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after={**before, "name": "After"},
            )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        with transactional_session(self.factory) as session:
            evidence = session.get(ErpSyncMutationEvidence, evidence_id)
            evidence.encrypted_before = self.service._encrypt_bytes(
                canonical_json_bytes({**before, "name": "Forged"})
            )
        with self.assertRaises(Exception) as raised:
            self.service.compensation_preview(attempt_id=attempt.id)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_checkpoint_before_integrity_invalid",
        )

    def test_multi_record_compensation_preview_and_confirmation_are_order_independent(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-multi-record")
        self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "CMP-A"}, {"number": "CMP-B"}]},
            row_count=2,
        )
        with transactional_session(self.factory) as session:
            for suffix in ("A", "B"):
                row = Project(
                    id=f"COMP-MULTI-{suffix}",
                    erp_external_id=f"CMP-{suffix}",
                    number=f"CMP-{suffix}",
                    name=f"Before {suffix}",
                    status="Actif",
                )
                session.add(row)
                session.flush()
                before = {
                    "erp_external_id": row.erp_external_id,
                    "number": row.number,
                    "name": row.name,
                    "client": row.client,
                    "project_manager_external_id": row.project_manager_external_id,
                    "project_manager_name": row.project_manager_name,
                    "status": row.status,
                }
                evidence_id = self.service.record_before_in_session(
                    session,
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    unit_type="PROJECT_SNAPSHOT",
                    entity_type="PROJECT",
                    entity_key=row.erp_external_id,
                    before=before,
                )
                row.name = f"After {suffix}"
                self.service.record_after_in_session(
                    session,
                    evidence_id=evidence_id,
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    after={**before, "name": f"After {suffix}"},
                )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="multi_record_compensation",
        )
        preview = self.service.compensation_preview(attempt_id=attempt.id)
        self.assertTrue(preview["can_confirm"])
        self.assertEqual(preview["eligible_records"], 2)
        result = self.service.confirm_compensation(
            attempt_id=attempt.id,
            preview_hash=preview["preview_hash"],
            command_identity="compensation:multi-record",
            actor_name="admin",
            reason="Restore both project records",
        )
        self.assertEqual(result["affected_records"], 2)
        with self.factory() as session:
            self.assertEqual(session.get(Project, "COMP-MULTI-A").name, "Before A")
            self.assertEqual(session.get(Project, "COMP-MULTI-B").name, "Before B")

    def test_compensation_refuses_changed_local_principal_and_recomputes_derived_cost(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-task-principal")
        self.service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"portfolio": {"source_rows": 1, "source_pages": 1, "snapshots": []}},
            row_count=1,
        )
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="COMP-TASK-PROJECT",
                    erp_external_id="COMP-TASK-PROJECT-ERP",
                    number="COMP-TASK",
                    name="Compensation task project",
                    status="Actif",
                )
            )
            task = TaskCatalogEntry(
                project_number="COMP-TASK",
                task_code="100",
                label="Before",
                status="Actif",
                active=True,
                erp_task_id="COMP-TASK-ERP",
                account_group="DEPMO",
                workforce_eligible=True,
                resource_class_code="PROGRAMMEUR",
                average_hourly_cost_cad=Decimal("125.0000"),
                preferred_resource_version=1,
            )
            session.add(task)
            session.flush()
            before = ErpSyncOrchestrator._task_owned_state(task)
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type=f"TASK_PROJECT:{uuid4()}",
                entity_type="TASK",
                entity_key=task.erp_task_id,
                before=before,
            )
            task.label = "After"
            after = ErpSyncOrchestrator._task_owned_state(task)
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after=after,
            )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="task_compensation",
        )

        with transactional_session(self.factory) as session:
            task = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "COMP-TASK-ERP"
                )
            )
            task.preferred_resource_version = 2
        principal_preview = self.service.compensation_preview(
            attempt_id=attempt.id
        )
        self.assertFalse(principal_preview["can_confirm"])
        self.assertEqual(principal_preview["conflict_count"], 1)

        with transactional_session(self.factory) as session:
            task = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "COMP-TASK-ERP"
                )
            )
            task.preferred_resource_version = 1
            session.get(
                ResourceClassConfig,
                "PROGRAMMEUR",
            ).average_hourly_cost_cad = Decimal("250.0000")
        preview = self.service.compensation_preview(attempt_id=attempt.id)
        self.assertTrue(preview["can_confirm"])
        self.service.confirm_compensation(
            attempt_id=attempt.id,
            preview_hash=preview["preview_hash"],
            command_identity="compensation:task-derived",
            actor_name="admin",
            reason="Restore ERP-owned fields and recalculate derived state",
        )
        with self.factory() as session:
            task = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "COMP-TASK-ERP"
                )
            )
        self.assertEqual(task.label, "Before")
        self.assertEqual(
            task.average_hourly_cost_cad,
            Decimal("250.0000"),
        )
        self.assertEqual(task.preferred_resource_version, 1)

    def test_compensation_preview_refuses_changed_current_erp_state(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-conflict")
        self.service.reserve_attempt(attempt_id=attempt.id)
        with transactional_session(self.factory) as session:
            row = Project(
                id="COMP-CONFLICT",
                erp_external_id="7002",
                number="COMP-2",
                name="Avant",
                status="Actif",
            )
            session.add(row)
            session.flush()
            before = {
                "erp_external_id": row.erp_external_id,
                "number": row.number,
                "name": row.name,
                "client": row.client,
                "project_manager_external_id": row.project_manager_external_id,
                "project_manager_name": row.project_manager_name,
                "status": row.status,
            }
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key="7002",
                before=before,
            )
            row.name = "Après"
            after = {**before, "name": "Après"}
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after=after,
            )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="global_validation_failed",
        )
        with transactional_session(self.factory) as session:
            session.get(Project, "COMP-CONFLICT").name = "Modification locale plus récente"

        preview = self.service.compensation_preview(attempt_id=attempt.id)
        self.assertFalse(preview["can_confirm"])
        self.assertEqual(preview["conflict_count"], 1)
        with self.assertRaisesRegex(Exception, "correspond plus"):
            self.service.confirm_compensation(
                attempt_id=attempt.id,
                preview_hash=preview["preview_hash"],
                command_identity="compensation:conflict",
                actor_name="admin",
                reason="Must refuse stale state",
            )

    def test_compensation_preview_refuses_new_local_dependency(self) -> None:
        control, _occurrence, attempt = self._attempt("compensation-dependency")
        self.service.reserve_attempt(attempt_id=attempt.id)
        with transactional_session(self.factory) as session:
            row = Project(
                id="COMP-DEPENDENCY",
                erp_external_id="7003",
                number="COMP-3",
                name="Imported",
                status="Actif",
            )
            session.add(row)
            session.flush()
            evidence_id = self.service.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key="7003",
                before=None,
            )
            after = {
                "erp_external_id": row.erp_external_id,
                "number": row.number,
                "name": row.name,
                "client": row.client,
                "project_manager_external_id": row.project_manager_external_id,
                "project_manager_name": row.project_manager_name,
                "status": row.status,
            }
            self.service.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after=after,
            )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="global_validation_failed",
        )
        with transactional_session(self.factory) as session:
            session.add(
                WorkPackage(
                    project_id="COMP-DEPENDENCY",
                    code="WP-LOCAL",
                    name="Local dependency",
                    status="planned",
                )
            )

        preview = self.service.compensation_preview(attempt_id=attempt.id)
        self.assertFalse(preview["can_confirm"])
        self.assertEqual(preview["conflict_count"], 1)
        self.assertTrue(
            any(
                value.startswith("dependency:PROJECT:")
                for value in preview["conflicts"]
            )
        )
        with self.assertRaises(Exception) as raised:
            self.service.confirm_compensation(
                attempt_id=attempt.id,
                preview_hash=preview["preview_hash"],
                command_identity="compensation:dependency",
                actor_name="admin",
                reason="Must refuse dependency",
            )
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_compensation_dependency_changed",
        )

    def test_required_recovery_pins_evidence_and_blocks_new_imports(self) -> None:
        control, _occurrence, attempt = self._attempt("recovery-pin")
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "P"}]},
            row_count=1,
        )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.control_settings,
            )
        )
        self.service.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="global_validation_failed",
        )
        self.service.finalize_attempt(attempt_id=attempt.id, successful=False)

        with self.assertRaisesRegex(Exception, "récupération ERP requise"):
            self.service.assert_no_required_recovery()
        with self.factory() as session:
            manifest = session.get(ErpSyncEvidenceManifest, captured.manifest_id)
            self.assertTrue(manifest.pinned)
            self.assertIsNone(manifest.expires_at)

    def test_terminal_attempts_release_reserved_capacity_but_keep_used_evidence_accounted(self) -> None:
        terminal_ids: list[str] = []
        retained_used: list[int] = []
        for index, status in enumerate(
            ("FAILED", "SUCCEEDED", "SUCCEEDED_WITH_ERRORS", "INTERRUPTED"),
            start=1,
        ):
            control, _occurrence, attempt = self._attempt(f"reservation-terminal-{index}")
            self.service.capture_source(
                attempt_id=attempt.id,
                source_type="PROJECTS",
                profile_fingerprint=self.control_settings.config_fingerprint,
                payload={"records": [{"number": f"P-{index}"}]},
                row_count=1,
            )
            self.assertTrue(
                control.finish(
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    status=status,
                    phase=(
                        "PUBLICATION_COMMITTED"
                        if status in {"SUCCEEDED", "SUCCEEDED_WITH_ERRORS"}
                        else "FAILED"
                    ),
                    settings=self.control_settings,
                )
            )
            self.service.finalize_attempt(
                attempt_id=attempt.id,
                successful=status == "SUCCEEDED",
            )
            terminal_ids.append(attempt.id)
            with self.factory() as session:
                reservation = session.scalar(
                    select(ErpSyncEvidenceReservation).where(
                        ErpSyncEvidenceReservation.attempt_id == attempt.id
                    )
                )
                self.assertIsNotNone(reservation)
                self.assertIsNotNone(reservation.released_at)
                self.assertGreater(reservation.used_bytes, 0)
                retained_used.append(int(reservation.used_bytes))

        with self.factory() as session:
            active_reserved = int(
                session.scalar(
                    select(func.coalesce(func.sum(ErpSyncEvidenceReservation.reserved_bytes), 0))
                    .where(ErpSyncEvidenceReservation.released_at.is_(None))
                )
                or 0
            )
            released_used = int(
                session.scalar(
                    select(func.coalesce(func.sum(ErpSyncEvidenceReservation.used_bytes), 0))
                    .where(ErpSyncEvidenceReservation.released_at.is_not(None))
                )
                or 0
            )
        self.assertEqual(active_reserved, 0)
        self.assertEqual(released_used, sum(retained_used))
        self.assertEqual(len(terminal_ids), 4)

    def test_retention_purges_unpinned_payload_and_releases_accounted_bytes(self) -> None:
        control, _occurrence, attempt = self._attempt("retention")
        captured = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload={"records": [{"number": "P-RET"}]},
            row_count=1,
        )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="SUCCEEDED",
                phase="PUBLICATION_COMMITTED",
                settings=self.control_settings,
            )
        )
        self.service.finalize_attempt(attempt_id=attempt.id, successful=True)
        with transactional_session(self.factory) as session:
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt.id
                )
            )
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.state, "NONE")
            manifests = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.attempt_id == attempt.id
                    )
                )
            )
            for manifest in manifests:
                manifest.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        self.assertGreaterEqual(self.service.purge_expired(), 1)
        with self.factory() as session:
            manifest = session.get(ErpSyncEvidenceManifest, captured.manifest_id)
            reservation = session.scalar(
                select(ErpSyncEvidenceReservation).where(
                    ErpSyncEvidenceReservation.attempt_id == attempt.id
                )
            )
            chunks = tuple(
                session.scalars(
                    select(ErpSyncEvidenceChunk).where(
                        ErpSyncEvidenceChunk.manifest_id == captured.manifest_id
                    )
                )
            )
        self.assertEqual(manifest.state, "PURGED")
        self.assertEqual(chunks, ())
        self.assertEqual(reservation.used_bytes, 0)
        self.assertEqual(reservation.used_plaintext_bytes, 0)
        with self.factory() as session:
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt.id
                )
            )
        self.assertEqual(recovery.state, "EXPIRED")

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
        self.service.finalize_attempt(
            attempt_id=baseline_attempt.id,
            successful=False,
        )

        _control, _occurrence, attempt = self._attempt("small-source")
        small = self.service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.control_settings.config_fingerprint,
            payload=[{"number": "1"}],
            row_count=1,
        )
        self.service.bind_attempt_policy(
            attempt_id=attempt.id,
            generation=attempt.generation,
        )
        rejected = self.service.validate_plausibility(small)
        self.assertFalse(rejected.accepted)
        self.assertTrue(
            _control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.control_settings,
            )
        )

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
