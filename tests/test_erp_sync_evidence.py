from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
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
    ErpSyncEvidenceService,
    ErpSyncEvidenceSettings,
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
        original_validate = orchestrator._validate_full_publication

        def corrupt_then_validate(*, attempt_id: str, generation: int) -> None:
            attempt = control.get_attempt(attempt_id)
            self.assertIsNotNone(attempt.task_sync_run_id)
            with transactional_session(self.factory) as session:
                run = session.get(
                    AcumaticaProjectTaskSyncRun,
                    attempt.task_sync_run_id,
                )
                self.assertIsNotNone(run)
                run.projects_processed += 1
            original_validate(
                attempt_id=attempt_id,
                generation=generation,
            )

        with patch.object(
            orchestrator,
            "_validate_full_publication",
            side_effect=corrupt_then_validate,
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
        self.assertFalse(self.service.validate_plausibility(reviewed).accepted)
        self.service.approve_plausibility_exception(
            attempt_id=retry_one.id,
            source_type="PROJECTS",
            source_fingerprint=reviewed.source_fingerprint,
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Reviewed planned reduction",
        )
        self.assertTrue(self.service.validate_plausibility(reviewed).accepted)
        self.assertTrue(
            control.finish(
                attempt_id=retry_one.id,
                generation=retry_one.generation,
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
        self.assertFalse(self.service.validate_plausibility(identical).accepted)
        self.service.approve_plausibility_exception(
            attempt_id=retry_two.id,
            source_type="PROJECTS",
            source_fingerprint=identical.source_fingerprint,
            threshold_scope="GLOBAL",
            actor_name="admin",
            reason="Fresh retry requires its own explicit approval",
        )
        self.assertTrue(self.service.validate_plausibility(identical).accepted)
        self.assertTrue(
            control.finish(
                attempt_id=retry_two.id,
                generation=retry_two.generation,
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
