from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import os
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

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
from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
    UnavailableErpSyncAutomaticSafetyCapability,
    resolve_scheduled_instant,
)
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    AcumaticaProjectTaskSyncProjectResult,
    AcumaticaProjectTaskSyncRun,
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_PENDING,
    ERP_OCCURRENCE_DAILY,
    ERP_OCCURRENCE_PENDING,
    ERP_SYNC_CONTROL_ROW_ID,
    GLOBAL_SYNC_RUN_KEY,
    SYNC_RUN_COMPLETED,
    SYNC_RUN_INTERRUPTED,
    SYNC_RUN_RUNNING,
    ErpSyncAttempt,
    ErpSyncBaseline,
    ErpSyncCompensationReceipt,
    ErpSyncControl,
    ErpSyncEvidenceChunk,
    ErpSyncEvidenceManifest,
    ErpSyncEvidenceReservation,
    ErpSyncMutationEvidence,
    ErpSyncPlausibilityException,
    ErpSyncRecoveryState,
    ErpSyncOccurrence,
    Project,
    ResourceClassConfig,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    TaskCatalogEntry,
    TaskCatalogProjectSyncState,
    TaskClassStandard,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server.erp_sync_orchestration import ErpSyncOrchestrator
from app.infrastructure.sql.erp_sync_control_repository import (
    ErpSyncFenceLost,
    SqlErpSyncControlRepository,
)


class SqlServerProjectSource:
    def __init__(self, rows: tuple[ExternalProjectRecord, ...]) -> None:
        self.rows = rows
        self.calls = 0

    def list_projects(self):
        self.calls += 1
        return self.rows


class SqlServerPortfolioTaskSource:
    def __init__(
        self,
        snapshots: dict[str, TaskCatalogProjectSnapshot],
    ) -> None:
        self.snapshots = snapshots
        self.batch_calls = 0

    def fetch_project_snapshots(
        self,
        *,
        project_targets: tuple[tuple[str, str], ...],
    ) -> TaskCatalogPortfolioSnapshot:
        self.batch_calls += 1
        snapshots = tuple(
            self.snapshots.get(
                number,
                TaskCatalogProjectSnapshot(
                    project_number=number,
                    source_rows=0,
                    rejected_rows=0,
                    items=(),
                ),
            )
            for _external_id, number in project_targets
        )
        return TaskCatalogPortfolioSnapshot(
            source_rows=sum(snapshot.source_rows for snapshot in snapshots),
            source_pages=1 if project_targets else 0,
            snapshots=snapshots,
        )


def _sql_project(number: str, name: str) -> ExternalProjectRecord:
    return ExternalProjectRecord(
        external_id=f"ERP-{number}",
        number=number,
        name=name,
        status="Actif",
    )


def _sql_snapshot(number: str, code: str, label: str) -> TaskCatalogProjectSnapshot:
    return TaskCatalogProjectSnapshot(
        project_number=number,
        source_rows=1,
        rejected_rows=0,
        items=(
            TaskCatalogItem(
                project_number=number,
                code=code,
                label=label,
                status="Actif",
                active=True,
                erp_task_id=f"ERP-{number}-{code}",
                account_group="DEPMO",
            ),
        ),
    )


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(IS_MSSQL, "requires RESOURCEPLANNER_SQLSERVER_TEST_URL")
class ErpSyncSqlServerConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_sql_engine(DATABASE_URL)
        cls.factory = create_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    TEST_PROJECT_PREFIX = "SQL643"
    TEST_RESOURCE_CLASS = "SQL643_TEST"
    TEST_TASK_CODES = (
        "SQL643-100",
        "SQL643-200",
        "SQL643-300",
        "SQL643-400",
        "SQL643-643",
    )

    def _cleanup_test_business_rows(self) -> None:
        with transactional_session(self.factory) as session:
            test_numbers = tuple(
                session.scalars(
                    select(Project.number).where(
                        Project.number.like(f"{self.TEST_PROJECT_PREFIX}%")
                    )
                )
            )
            if test_numbers:
                session.execute(
                    delete(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number.in_(test_numbers)
                    )
                )
                session.execute(
                    delete(TaskCatalogProjectSyncState).where(
                        TaskCatalogProjectSyncState.project_number.in_(test_numbers)
                    )
                )
                session.execute(
                    delete(Project).where(Project.number.in_(test_numbers))
                )

    def _ensure_test_workforce_policy(self) -> None:
        with transactional_session(self.factory) as session:
            resource_class = session.get(
                ResourceClassConfig,
                self.TEST_RESOURCE_CLASS,
            )
            if resource_class is None:
                session.add(
                    ResourceClassConfig(
                        code=self.TEST_RESOURCE_CLASS,
                        label="SQL 643 test class",
                        active=True,
                        version=1,
                    )
                )
            else:
                resource_class.active = True
            for task_code in self.TEST_TASK_CODES:
                standard = session.get(TaskClassStandard, task_code)
                if standard is None:
                    session.add(
                        TaskClassStandard(
                            task_code=task_code,
                            resource_class_code=self.TEST_RESOURCE_CLASS,
                            active=True,
                            version=1,
                        )
                    )
                else:
                    self.assertEqual(
                        standard.resource_class_code,
                        self.TEST_RESOURCE_CLASS,
                        f"test-owned task standard unexpectedly reused: {task_code}",
                    )
                    self.assertTrue(standard.active)

    def setUp(self) -> None:
        with transactional_session(self.factory) as session:
            session.execute(
                text("DELETE FROM acumatica_project_task_sync_project_results")
            )
            session.execute(text("DELETE FROM acumatica_project_task_sync_runs"))
            session.execute(text("DELETE FROM erp_sync_compensation_receipts"))
            session.execute(text("DELETE FROM erp_sync_plausibility_exceptions"))
            session.execute(text("DELETE FROM erp_sync_recovery_state"))
            session.execute(text("DELETE FROM erp_sync_project_quality"))
            session.execute(text("DELETE FROM erp_sync_mutation_evidence"))
            session.execute(text("DELETE FROM erp_sync_baseline_approval_audit"))
            session.execute(text("DELETE FROM erp_sync_evidence_chunks"))
            session.execute(text("DELETE FROM erp_sync_evidence_manifests"))
            session.execute(text("DELETE FROM erp_sync_evidence_reservations"))
            session.execute(text("DELETE FROM erp_sync_baselines"))
            session.execute(text("DELETE FROM erp_sync_attempts"))
            session.execute(text("DELETE FROM erp_sync_occurrences"))
            control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            if control is None:
                control = ErpSyncControl(
                    id=ERP_SYNC_CONTROL_ROW_ID,
                    generation=0,
                    protocol_version=1,
                )
                session.add(control)
            control.generation = 0
            control.owner_attempt_id = None
            control.lease_expires_at = None
            control.owner_deadline_at = None
            control.heartbeat_at = None
            control.last_evaluated_business_date = None
            control.last_success_attempt_id = None
            control.config_fingerprint = None

        self._cleanup_test_business_rows()
        self._ensure_test_workforce_policy()

        sql_now = SqlErpSyncControlRepository(self.factory).current_sql_time()
        zone_settings = ErpSyncSchedulerSettings(enabled=True)
        local_now = sql_now.astimezone(zone_settings.timezone())
        self.settings = ErpSyncSchedulerSettings(
            enabled=True,
            local_time=local_now.time().replace(second=0, microsecond=0),
            timezone_name=zone_settings.timezone_name,
        )
        self.automatic_safety = TestErpSyncAutomaticSafetyCapability()
        self.evidence = ErpSyncEvidenceService(
            self.factory,
            cipher=FernetSecretCipher(Fernet.generate_key().decode("ascii")),
            settings=ErpSyncEvidenceSettings(
                max_evidence_mib=16,
                evidence_budget_mib=32,
            ),
            control_settings=self.settings,
        )

    def test_sqlserver_fixture_cleanup_is_scoped_and_idempotent(self) -> None:
        sentinel_number = "SENTINEL-NON-643"
        owned_number = "SQL643-CLEANUP-PROBE"
        with transactional_session(self.factory) as session:
            sentinel = session.scalar(
                select(Project).where(Project.number == sentinel_number)
            )
            if sentinel is None:
                session.add(
                    Project(
                        id="SQL643-SENTINEL-ID",
                        number=sentinel_number,
                        name="Unrelated sentinel",
                        status="Actif",
                    )
                )
            session.add(
                Project(
                    id="SQL643-CLEANUP-ID",
                    erp_external_id="ERP-SQL643-CLEANUP",
                    number=owned_number,
                    name="Owned cleanup probe",
                    status="Actif",
                )
            )
            session.add(
                TaskCatalogEntry(
                    project_number=owned_number,
                    task_code="CLEAN",
                    label="Owned cleanup task",
                    active=True,
                )
            )
            session.add(
                TaskCatalogProjectSyncState(
                    project_number=owned_number,
                    last_attempt_at=datetime.now(timezone.utc),
                    last_success_at=datetime.now(timezone.utc),
                    source_rows=1,
                    task_count=1,
                    rejected_rows=0,
                )
            )

        self._cleanup_test_business_rows()
        self._cleanup_test_business_rows()

        with self.factory() as session:
            self.assertIsNotNone(
                session.scalar(
                    select(Project).where(Project.number == sentinel_number)
                )
            )
            self.assertIsNone(
                session.scalar(
                    select(Project).where(Project.number == owned_number)
                )
            )
            self.assertIsNone(
                session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == owned_number
                    )
                )
            )
            self.assertIsNone(
                session.get(TaskCatalogProjectSyncState, owned_number)
            )

    def _task_run_diagnostic(self, run_id: str | None) -> str:
        if not run_id:
            return "task_sync_run_id=<none>"
        with self.factory() as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            results = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncProjectResult)
                    .where(AcumaticaProjectTaskSyncProjectResult.run_id == run_id)
                    .order_by(AcumaticaProjectTaskSyncProjectResult.project_number)
                )
            )
        return repr(
            {
                "run_status": getattr(run, "status", None),
                "run_error_code": getattr(run, "error_code", None),
                "projects_total": getattr(run, "projects_total", None),
                "projects_processed": getattr(run, "projects_processed", None),
                "projects_rejected": getattr(run, "projects_rejected", None),
                "results": [
                    {
                        "project_number": row.project_number,
                        "status": row.status,
                        "error_code": row.error_code,
                        "reason_code": row.reason_code,
                        "source_rows": row.source_rows,
                        "created": row.created,
                        "updated": row.updated,
                        "unchanged": row.unchanged,
                        "deactivated": row.deactivated,
                        "rejected": row.rejected,
                    }
                    for row in results
                ],
            }
        )

    def _daily_occurrence(self) -> ErpSyncOccurrence:
        now = SqlErpSyncControlRepository(self.factory).current_sql_time()
        business_date = now.astimezone(self.settings.timezone()).date()
        with transactional_session(self.factory) as session:
            row = ErpSyncOccurrence(
                occurrence_key=(
                    "ERP_PROJECTS_TASKS_DAILY:" + business_date.isoformat()
                ),
                occurrence_type=ERP_OCCURRENCE_DAILY,
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=business_date,
                scheduled_at=resolve_scheduled_instant(
                    business_date,
                    self.settings,
                ),
                timezone_name=self.settings.timezone_name,
                scheduled_local_time=self.settings.local_time.strftime("%H:%M"),
                config_fingerprint=self.settings.config_fingerprint,
                status=ERP_OCCURRENCE_PENDING,
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            return row

    def test_same_daily_occurrence_two_connections_converge_on_one_owner(self) -> None:
        occurrence = self._daily_occurrence()
        barrier = Barrier(2)

        def claim(actor: str):
            barrier.wait(timeout=5)
            return SqlErpSyncControlRepository(self.factory).claim(
                occurrence_id=occurrence.id,
                trigger="SCHEDULED",
                actor_name=actor,
                settings=self.settings,
                automatic=True,
                automatic_safety=self.automatic_safety,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = tuple(pool.map(claim, ("backend-A", "backend-B")))

        self.assertTrue(all(attempt is not None for attempt in attempts))
        self.assertEqual({attempt.id for attempt in attempts if attempt}, {attempts[0].id})
        with self.factory() as session:
            count = session.scalar(
                select(func.count(ErpSyncAttempt.id)).where(
                    ErpSyncAttempt.occurrence_id == occurrence.id
                )
            )
            control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            self.assertIsNotNone(control)
            owner_attempt_id = control.owner_attempt_id
        self.assertEqual(count, 1)
        self.assertEqual(owner_attempt_id, attempts[0].id)

    def test_concurrent_identical_manual_command_has_one_occurrence_and_attempt(self) -> None:
        barrier = Barrier(2)

        def accept(_index: int):
            barrier.wait(timeout=5)
            repository = SqlErpSyncControlRepository(self.factory)
            occurrence = repository.accept_manual(
                intent="PROJECTS_AND_ACTIVE_TASKS",
                actor_name="operator",
                idempotency_key="same-manual-command",
                settings=self.settings,
            )
            attempt = repository.claim(
                occurrence_id=occurrence.id,
                trigger="MANUAL",
                actor_name="operator",
                settings=self.settings,
            )
            return occurrence.id, attempt.id if attempt else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(accept, (1, 2)))

        self.assertEqual(len({row[0] for row in results}), 1)
        self.assertEqual(len({row[1] for row in results}), 1)
        with self.factory() as session:
            occurrence_count = session.scalar(select(func.count(ErpSyncOccurrence.id)))
            attempt_count = session.scalar(select(func.count(ErpSyncAttempt.id)))
        self.assertEqual(occurrence_count, 1)
        self.assertEqual(attempt_count, 1)

    def test_evidence_and_business_mutation_rollback_together_on_sqlserver(self) -> None:
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-evidence",
            idempotency_key="sql-evidence-rollback",
            settings=self.settings,
        )
        attempt = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-evidence",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        self.assertTrue(
            repository.start_attempt(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )
        self.evidence.reserve_attempt(attempt_id=attempt.id)

        project_number = "SQL643B-ROLLBACK"
        with transactional_session(self.factory) as session:
            session.execute(delete(Project).where(Project.number == project_number))

        with self.assertRaises(RuntimeError):
            with transactional_session(self.factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    settings=self.settings,
                )
                evidence_id = self.evidence.record_before_in_session(
                    session,
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    unit_type="PROJECT_SNAPSHOT",
                    entity_type="PROJECT",
                    entity_key="ERP-SQL643B-ROLLBACK",
                    before=None,
                )
                row = Project(
                    id="SQL643B-ROLLBACK-ID",
                    erp_external_id="ERP-SQL643B-ROLLBACK",
                    number=project_number,
                    name="Must rollback",
                    status="Actif",
                )
                session.add(row)
                session.flush()
                self.evidence.record_after_in_session(
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
                raise RuntimeError("force rollback")

        with self.factory() as session:
            self.assertIsNone(
                session.scalar(select(Project).where(Project.number == project_number))
            )
            evidence_count = int(
                session.scalar(
                    select(func.count(ErpSyncMutationEvidence.id)).where(
                        ErpSyncMutationEvidence.attempt_id == attempt.id
                    )
                )
                or 0
            )
        self.assertEqual(evidence_count, 0)

    def test_sqlserver_evidence_reservation_is_idempotent_and_recovery_refuses_active_owner(self) -> None:
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-evidence",
            idempotency_key="sql-evidence-reservation",
            settings=self.settings,
        )
        attempt = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-evidence",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(
                pool.map(
                    lambda _index: self.evidence.reserve_attempt(
                        attempt_id=attempt.id
                    ),
                    (1, 2),
                )
            )

        with self.factory() as session:
            reservations = int(
                session.scalar(
                    select(func.count(ErpSyncEvidenceReservation.id)).where(
                        ErpSyncEvidenceReservation.attempt_id == attempt.id
                    )
                )
                or 0
            )
        self.assertEqual(reservations, 1)

        # Recovery preview shares the same SQL control authority and cannot race
        # an active writer.
        with self.assertRaises(Exception) as raised:
            self.evidence.compensation_preview(attempt_id=attempt.id)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_recovery_owner_active",
        )

    def test_full_sqlserver_import_commits_task_result_and_evidence_atomically(self) -> None:
        project_number = "SQL643B-ATOMIC"
        with transactional_session(self.factory) as session:
            session.execute(
                delete(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_number
                )
            )
            session.execute(delete(Project).where(Project.number == project_number))

        projects = SqlServerProjectSource(
            (_sql_project(project_number, "643B atomic evidence"),)
        )
        tasks = SqlServerPortfolioTaskSource(
            {
                project_number: _sql_snapshot(
                    project_number,
                    "SQL643-643",
                    "643B task",
                )
            }
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            automatic_safety=self.automatic_safety,
            evidence_service=self.evidence,
        )
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="sql-evidence",
            idempotency_key="sql-643b-bootstrap",
            settings=self.settings,
        )
        first = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-evidence",
            settings=self.settings,
        )
        self.assertIsNotNone(first)
        orchestrator._execute_attempt(first.id, first.generation)
        self.assertEqual(repository.get_attempt(first.id).status, "FAILED")

        manifests = tuple(
            row
            for row in self.evidence.list_attempt_sources(attempt_id=first.id)
            if row["proof_type"] == "SOURCE"
        )
        self.assertEqual({row["source_type"] for row in manifests}, {"PROJECTS", "TASKS"})
        for manifest in manifests:
            self.evidence.approve_baseline(
                manifest_id=str(manifest["manifest_id"]),
                actor_name="sql-admin",
                reason="Reviewed SQL Server bootstrap source",
            )

        retry = repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="sql-evidence",
            settings=self.settings,
            command_identity="retry:sql-643b-atomic",
        )
        self.assertIsNotNone(retry)
        orchestrator._execute_attempt(retry.id, retry.generation)
        completed = repository.get_attempt(retry.id)
        self.assertEqual(
            completed.status,
            "SUCCEEDED",
            self._task_run_diagnostic(completed.task_sync_run_id),
        )
        self.assertIsNotNone(completed.task_sync_run_id)

        with self.factory() as session:
            task_row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_number,
                    TaskCatalogEntry.task_code == "SQL643-643",
                )
            )
            project_result = session.scalar(
                select(AcumaticaProjectTaskSyncProjectResult).where(
                    AcumaticaProjectTaskSyncProjectResult.run_id
                    == completed.task_sync_run_id,
                    AcumaticaProjectTaskSyncProjectResult.project_number
                    == project_number,
                )
            )
            evidence_rows = tuple(
                session.scalars(
                    select(ErpSyncMutationEvidence).where(
                        ErpSyncMutationEvidence.attempt_id == retry.id
                    )
                )
            )
        self.assertIsNotNone(task_row)
        self.assertIsNotNone(project_result)
        self.assertEqual(project_result.status, "synchronized")
        self.assertTrue(
            any(
                row.entity_type == "TASK"
                and row.after_sha256
                and row.committed_at is not None
                for row in evidence_rows
            )
        )
        self.assertTrue(
            any(
                row.entity_type == "TASK_SYNC_STATE"
                and row.after_sha256
                and row.committed_at is not None
                for row in evidence_rows
            )
        )
        orchestrator.shutdown()

    def test_sqlserver_stale_generation_cannot_write_mutation_evidence(self) -> None:
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-evidence",
            idempotency_key="sql-evidence-stale-generation",
            settings=self.settings,
        )
        first = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-evidence",
            settings=self.settings,
        )
        self.assertIsNotNone(first)
        self.assertTrue(
            repository.start_attempt(
                attempt_id=first.id,
                generation=first.generation,
                settings=self.settings,
            )
        )
        self.evidence.reserve_attempt(attempt_id=first.id)

        with transactional_session(self.factory) as session:
            session.execute(
                text(
                    "UPDATE erp_sync_control "
                    "SET lease_expires_at = DATEADD(second, -10, SYSUTCDATETIME()) "
                    "WHERE id = :id"
                ),
                {"id": ERP_SYNC_CONTROL_ROW_ID},
            )

        second = repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="sql-evidence-replacement",
            settings=self.settings,
            command_identity="retry:sql-evidence-stale-generation",
        )
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)

        with self.assertRaises(ErpSyncFenceLost):
            with transactional_session(self.factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=first.id,
                    generation=first.generation,
                    settings=self.settings,
                )
                self.evidence.record_before_in_session(
                    session,
                    attempt_id=first.id,
                    generation=first.generation,
                    unit_type="PROJECT_SNAPSHOT",
                    entity_type="PROJECT",
                    entity_key="STALE-SQL-EVIDENCE",
                    before=None,
                )

        with self.factory() as session:
            stale_rows = int(
                session.scalar(
                    select(func.count(ErpSyncMutationEvidence.id)).where(
                        ErpSyncMutationEvidence.attempt_id == first.id
                    )
                )
                or 0
            )
        self.assertEqual(stale_rows, 0)

    def test_sqlserver_compensation_preview_is_invalidated_by_new_generation(self) -> None:
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-evidence",
            idempotency_key="sql-compensation-generation",
            settings=self.settings,
        )
        attempt = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-evidence",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        self.assertTrue(
            repository.start_attempt(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )
        self.evidence.reserve_attempt(attempt_id=attempt.id)

        project_id = "SQL643B-COMP-GEN"
        with transactional_session(self.factory) as session:
            session.execute(delete(Project).where(Project.id == project_id))
            row = Project(
                id=project_id,
                erp_external_id="ERP-SQL643B-COMP-GEN",
                number="SQL643B-COMP-GEN",
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
            evidence_id = self.evidence.record_before_in_session(
                session,
                attempt_id=attempt.id,
                generation=attempt.generation,
                unit_type="PROJECT_SNAPSHOT",
                entity_type="PROJECT",
                entity_key=row.erp_external_id,
                before=before,
            )
            row.name = "After"
            self.evidence.record_after_in_session(
                session,
                evidence_id=evidence_id,
                attempt_id=attempt.id,
                generation=attempt.generation,
                after={**before, "name": "After"},
            )
        self.assertTrue(
            repository.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.settings,
            )
        )
        self.evidence.mark_recovery_required(
            attempt_id=attempt.id,
            reason_code="global_validation_failed",
        )
        preview = self.evidence.compensation_preview(attempt_id=attempt.id)
        self.assertTrue(preview["can_confirm"])

        # A separately authorized control-plane command changes generation after
        # preview. Even if the ERP-owned row itself is unchanged, confirmation
        # must refuse the stale preview.
        other_occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-other",
            idempotency_key="sql-compensation-generation-other",
            settings=self.settings,
        )
        other = repository.claim(
            occurrence_id=other_occurrence.id,
            trigger="MANUAL",
            actor_name="sql-other",
            settings=self.settings,
        )
        self.assertIsNotNone(other)
        self.assertTrue(
            repository.start_attempt(
                attempt_id=other.id,
                generation=other.generation,
                settings=self.settings,
            )
        )
        self.assertTrue(
            repository.finish(
                attempt_id=other.id,
                generation=other.generation,
                status="FAILED",
                phase="FAILED",
                settings=self.settings,
            )
        )

        with self.assertRaises(Exception) as raised:
            self.evidence.confirm_compensation(
                attempt_id=attempt.id,
                preview_hash=preview["preview_hash"],
                command_identity="sql-compensation-generation-confirm",
                actor_name="sql-admin",
                reason="Must reject stale generation",
            )
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "erp_sync_compensation_preview_stale",
        )

    def test_sqlserver_evidence_unique_and_foreign_key_constraints_are_enforced(self) -> None:
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="sql-constraints",
            idempotency_key="sql-evidence-constraints",
            settings=self.settings,
        )
        attempt = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="sql-constraints",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)

        with self.assertRaises(IntegrityError):
            with transactional_session(self.factory) as session:
                session.add_all(
                    [
                        ErpSyncEvidenceReservation(
                            attempt_id=attempt.id,
                            reserved_bytes=1024,
                            used_bytes=0,
                            reserved_plaintext_bytes=1024,
                            used_plaintext_bytes=0,
                        ),
                        ErpSyncEvidenceReservation(
                            attempt_id=attempt.id,
                            reserved_bytes=1024,
                            used_bytes=0,
                            reserved_plaintext_bytes=1024,
                            used_plaintext_bytes=0,
                        ),
                    ]
                )

        with self.assertRaises(IntegrityError):
            with transactional_session(self.factory) as session:
                session.add(
                    ErpSyncEvidenceManifest(
                        id="SQL643B-MISSING-FK",
                        attempt_id="MISSING-ATTEMPT",
                        protocol_version=1,
                        source_type="PROJECTS",
                        proof_type="SOURCE",
                        profile_fingerprint="f" * 64,
                        observed_at=datetime.now(timezone.utc),
                        row_count=0,
                        chunk_count=0,
                        plaintext_bytes=0,
                        ciphertext_bytes=0,
                        state="SEALED",
                    )
                )

    def test_terminal_run_now_replay_uses_one_sqlserver_attempt_and_execution(self) -> None:
        project_number = "SQL643A-R"
        with transactional_session(self.factory) as session:
            session.execute(
                delete(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_number
                )
            )
            session.execute(
                delete(Project).where(Project.number == project_number)
            )

        projects = SqlServerProjectSource(
            (_sql_project(project_number, "Terminal replay authority"),)
        )
        tasks = SqlServerPortfolioTaskSource(
            {
                project_number: _sql_snapshot(
                    project_number,
                    "SQL643-400",
                    "Terminal replay task",
                )
            }
        )
        backend_a = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            automatic_safety=self.automatic_safety,
            system_actor="backend-A",
        )
        backend_b = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            automatic_safety=self.automatic_safety,
            system_actor="backend-B",
        )
        repository = SqlErpSyncControlRepository(self.factory)

        accepted_by_b = Event()
        release_b = Event()
        completed_a = Event()
        result_b: list[dict[str, object]] = []
        errors_b: list[BaseException] = []
        original_accept_b = backend_b._control.accept_manual
        original_execute_a = backend_a._execute_attempt

        def delayed_accept_b(*args, **kwargs):
            occurrence = original_accept_b(*args, **kwargs)
            accepted_by_b.set()
            if not release_b.wait(timeout=10):
                raise RuntimeError("SQL terminal replay release timeout")
            return occurrence

        def observed_execute_a(*args, **kwargs):
            try:
                return original_execute_a(*args, **kwargs)
            finally:
                completed_a.set()

        def invoke_b() -> None:
            try:
                result_b.append(
                    backend_b.run_now(
                        intent="PROJECTS_AND_ACTIVE_TASKS",
                        actor_name="operator",
                        idempotency_key="sql-terminal-replay",
                    )
                )
            except BaseException as exc:
                errors_b.append(exc)

        try:
            with patch.object(
                backend_b._control,
                "accept_manual",
                side_effect=delayed_accept_b,
            ), patch.object(
                backend_a,
                "_execute_attempt",
                side_effect=observed_execute_a,
            ), patch.object(
                backend_a,
                "_submit",
                wraps=backend_a._submit,
            ) as submit_a, patch.object(
                backend_b,
                "_submit",
                wraps=backend_b._submit,
            ) as submit_b:
                delayed = Thread(target=invoke_b, name="sql-stale-run-now")
                delayed.start()
                self.assertTrue(accepted_by_b.wait(timeout=5))

                result_a = backend_a.run_now(
                    intent="PROJECTS_AND_ACTIVE_TASKS",
                    actor_name="operator",
                    idempotency_key="sql-terminal-replay",
                )
                self.assertTrue(completed_a.wait(timeout=20))
                completed = repository.get_attempt(str(result_a["run_id"]))
                self.assertEqual(completed.status, "SUCCEEDED")

                release_b.set()
                delayed.join(timeout=10)
                self.assertFalse(delayed.is_alive())

                self.assertEqual(errors_b, [])
                self.assertEqual(len(result_b), 1)
                self.assertEqual(result_b[0]["run_id"], result_a["run_id"])
                self.assertEqual(submit_a.call_count, 1)
                submit_b.assert_not_called()

            with self.factory() as session:
                attempts = tuple(
                    session.scalars(
                        select(ErpSyncAttempt).where(
                            ErpSyncAttempt.occurrence_id
                            == str(result_a["occurrence_id"])
                        )
                    )
                )
                task_row = session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == project_number,
                        TaskCatalogEntry.task_code == "SQL643-400",
                    )
                )
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].id, result_a["run_id"])
            self.assertEqual(attempts[0].trigger_kind, "MANUAL")
            self.assertEqual(projects.calls, 1)
            self.assertEqual(tasks.batch_calls, 1)
            self.assertIsNotNone(
                task_row,
                self._task_run_diagnostic(completed.task_sync_run_id),
            )
        finally:
            release_b.set()
            backend_a.shutdown()
            backend_b.shutdown()

    def test_manual_owner_is_only_observed_by_automatic_scheduler(self) -> None:
        unavailable = UnavailableErpSyncAutomaticSafetyCapability()
        repository = SqlErpSyncControlRepository(self.factory)
        occurrence = repository.record_daily_decision(
            self.settings,
            unavailable,
        )
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.status, "DISABLED")

        manual = repository.claim_outcome(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="manual-backend",
            settings=self.settings,
        )
        self.assertTrue(manual.admitted)
        self.assertIsNotNone(manual.attempt)

        project_number = "SQL643A-O"
        with transactional_session(self.factory) as session:
            session.execute(
                delete(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_number
                )
            )
            session.execute(
                delete(Project).where(Project.number == project_number)
            )

        projects = SqlServerProjectSource(
            (_sql_project(project_number, "Manual observer authority"),)
        )
        tasks = SqlServerPortfolioTaskSource(
            {
                project_number: _sql_snapshot(
                    project_number,
                    "SQL643-300",
                    "Manual observer task",
                )
            }
        )
        backend_a = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            automatic_safety=unavailable,
            system_actor="manual-backend",
        )
        backend_b = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            automatic_safety=unavailable,
            system_actor="automatic-observer",
        )

        worker_entered = Event()
        release_worker = Event()
        original_execute = backend_a._execute_attempt

        def paused_execute(*args, **kwargs):
            worker_entered.set()
            if not release_worker.wait(timeout=10):
                raise RuntimeError("SQL Server manual worker release timeout")
            return original_execute(*args, **kwargs)

        try:
            with patch.object(backend_a, "_execute_attempt", side_effect=paused_execute):
                future = backend_a._submit(
                    manual.attempt.id,
                    manual.attempt.generation,
                )
                self.assertTrue(worker_entered.wait(timeout=5))
                self.assertEqual(
                    repository.get_attempt(manual.attempt.id).status,
                    ERP_ATTEMPT_PENDING,
                )

                with patch.object(
                    backend_b,
                    "_submit",
                    wraps=backend_b._submit,
                ) as observer_submit:
                    observed = backend_b.scheduler_tick()

                self.assertIsNotNone(observed)
                self.assertEqual(observed["attempt_id"], manual.attempt.id)
                self.assertEqual(observed["claim_outcome"], "OBSERVED")
                self.assertEqual(observed["trigger"], "RETRY")
                self.assertEqual(observed["status"], ERP_ATTEMPT_PENDING)
                observer_submit.assert_not_called()
                self.assertEqual(
                    repository.get_attempt(manual.attempt.id).status,
                    ERP_ATTEMPT_PENDING,
                )

                release_worker.set()
                future.result(timeout=20)

            completed = repository.get_attempt(manual.attempt.id)
            self.assertEqual(completed.status, "SUCCEEDED")
            self.assertEqual(completed.trigger_kind, "RETRY")
            self.assertEqual(projects.calls, 1)
            self.assertEqual(tasks.batch_calls, 1)

            with self.factory() as session:
                attempts = tuple(
                    session.scalars(
                        select(ErpSyncAttempt).where(
                            ErpSyncAttempt.occurrence_id == occurrence.id
                        )
                    )
                )
                control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
                task_row = session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == project_number,
                        TaskCatalogEntry.task_code == "SQL643-300",
                    )
                )
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].id, manual.attempt.id)
            self.assertEqual(attempts[0].trigger_kind, "RETRY")
            self.assertIsNotNone(control)
            self.assertIsNone(control.owner_attempt_id)
            self.assertIsNotNone(
                task_row,
                self._task_run_diagnostic(completed.task_sync_run_id),
            )
            self.assertEqual(
                repository.get_occurrence(occurrence.id).success_attempt_id,
                manual.attempt.id,
            )
        finally:
            release_worker.set()
            backend_a.shutdown()
            backend_b.shutdown()

    def test_generation_takeover_releases_child_and_fences_all_stale_writes(self) -> None:
        project_a = "SQL643A-A"
        project_b = "SQL643A-B"
        with transactional_session(self.factory) as session:
            session.execute(
                delete(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number.in_((project_a, project_b))
                )
            )
            session.execute(
                delete(Project).where(Project.number.in_((project_a, project_b)))
            )

        first_projects = SqlServerProjectSource(
            (_sql_project(project_a, "Generation N committed"),)
        )
        first_tasks = SqlServerPortfolioTaskSource(
            {project_a: _sql_snapshot(project_a, "SQL643-100", "Task generation N")}
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=first_projects,
            project_task_source=first_tasks,
            settings=self.settings,
        )
        occurrence = self._daily_occurrence()
        repository = SqlErpSyncControlRepository(self.factory)
        first = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="backend-A",
            settings=self.settings,
        )
        self.assertIsNotNone(first)
        self.assertTrue(
            repository.start_attempt(
                attempt_id=first.id,
                generation=first.generation,
                settings=self.settings,
            )
        )

        # Generation N executes real production project and task mutation logic.
        orchestrator._execute_projects(first.id, first.generation)
        with self.factory() as discovery:
            projected = tuple(
                SqlPlannerQueryRepositoryWithLoadProfiles(discovery).list_projects(
                    active_only=False
                )
            )
        projected_a = next(row for row in projected if row.number == project_a)
        first_child_id = orchestrator._create_task_run(
            first.id,
            first.generation,
        )
        with transactional_session(self.factory) as session:
            child = session.get(AcumaticaProjectTaskSyncRun, first_child_id)
            self.assertIsNotNone(child)
            child.projects_total = 1
        orchestrator._process_task_project(
            first_child_id,
            first.id,
            first.generation,
            projected_a,
            {project_a: first_tasks.snapshots[project_a]},
        )

        with self.factory() as session:
            committed_a = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_a,
                    TaskCatalogEntry.task_code == "SQL643-100",
                )
            )
            self.assertIsNotNone(
                committed_a,
                self._task_run_diagnostic(first_child_id),
            )
            committed_a_id = committed_a.id
            first_results = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncProjectResult).where(
                        AcumaticaProjectTaskSyncProjectResult.run_id
                        == first_child_id
                    )
                )
            )
            self.assertEqual(len(first_results), 1)
            self.assertEqual(first_results[0].status, "synchronized")
            live_child = session.get(AcumaticaProjectTaskSyncRun, first_child_id)
            self.assertIsNotNone(live_child)
            self.assertEqual(live_child.status, SYNC_RUN_RUNNING)
            self.assertEqual(live_child.active_key, GLOBAL_SYNC_RUN_KEY)

        with transactional_session(self.factory) as session:
            session.execute(
                text(
                    "UPDATE erp_sync_control "
                    "SET lease_expires_at = DATEADD(second, -10, SYSUTCDATETIME()) "
                    "WHERE id = :id"
                ),
                {"id": ERP_SYNC_CONTROL_ROW_ID},
            )

        second = repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="backend-B",
            settings=self.settings,
            command_identity="retry:sqlserver-child-takeover",
        )
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)

        with self.factory() as session:
            stale_child = session.get(
                AcumaticaProjectTaskSyncRun,
                first_child_id,
            )
            self.assertIsNotNone(stale_child)
            self.assertEqual(stale_child.status, SYNC_RUN_INTERRUPTED)
            self.assertIsNone(stale_child.active_key)

        # A stale generation invokes the real project business path. The fence
        # rejects it before any domain/progress mutation can commit.
        stale_projects = SqlServerProjectSource(
            (_sql_project(project_a, "STALE GENERATION MUST NOT COMMIT"),)
        )
        with self.assertRaises(ErpSyncFenceLost):
            orchestrator._execute_projects(
                first.id,
                first.generation,
                project_source=stale_projects,
            )
        self.assertFalse(
            repository.finish(
                attempt_id=first.id,
                generation=first.generation,
                status="SUCCEEDED",
                phase="PUBLICATION_COMMITTED",
                settings=self.settings,
            )
        )

        with self.factory() as session:
            unchanged_project = session.scalar(
                select(Project).where(Project.number == project_a)
            )
            unchanged_task = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project_a,
                    TaskCatalogEntry.task_code == "SQL643-100",
                )
            )
            self.assertIsNotNone(unchanged_project)
            self.assertIsNotNone(unchanged_task)
            self.assertEqual(unchanged_project.name, "Generation N committed")
            self.assertEqual(unchanged_task.id, committed_a_id)
            self.assertEqual(
                repository.get_attempt(first.id).status,
                ERP_ATTEMPT_INTERRUPTED,
            )

        # Generation N+1 runs the actual full production orchestration. Its child
        # lifecycle and terminal state are produced by application code, not test
        # state manipulation.
        replacement_projects = SqlServerProjectSource(
            (
                _sql_project(project_a, "Generation N+1 committed"),
                _sql_project(project_b, "Generation N+1 new"),
            )
        )
        replacement_tasks = SqlServerPortfolioTaskSource(
            {
                project_a: _sql_snapshot(
                    project_a,
                    "SQL643-100",
                    "Task generation N+1",
                ),
                project_b: _sql_snapshot(
                    project_b,
                    "SQL643-200",
                    "Replacement task",
                ),
            }
        )
        replacement_orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=replacement_projects,
            project_task_source=replacement_tasks,
            settings=self.settings,
        )
        replacement_orchestrator._execute_attempt(
            second.id,
            second.generation,
        )

        completed = repository.get_attempt(second.id)
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertIsNotNone(completed.task_sync_run_id)
        with self.factory() as session:
            replacement_child = session.get(
                AcumaticaProjectTaskSyncRun,
                completed.task_sync_run_id,
            )
            self.assertIsNotNone(replacement_child)
            self.assertEqual(replacement_child.status, SYNC_RUN_COMPLETED)
            self.assertIsNone(replacement_child.active_key)

            rows = tuple(
                session.scalars(
                    select(TaskCatalogEntry)
                    .where(TaskCatalogEntry.project_number.in_((project_a, project_b)))
                    .order_by(TaskCatalogEntry.project_number)
                )
            )
            self.assertEqual(
                [(row.project_number, row.task_code) for row in rows],
                [(project_a, "SQL643-100"), (project_b, "SQL643-200")],
            )
            surviving_a = next(row for row in rows if row.project_number == project_a)
            self.assertEqual(surviving_a.id, committed_a_id)

            project_rows = tuple(
                session.scalars(
                    select(Project)
                    .where(Project.number.in_((project_a, project_b)))
                    .order_by(Project.number)
                )
            )
            self.assertEqual(
                [(row.number, row.name) for row in project_rows],
                [
                    (project_a, "Generation N+1 committed"),
                    (project_b, "Generation N+1 new"),
                ],
            )
            replacement_results = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncProjectResult).where(
                        AcumaticaProjectTaskSyncProjectResult.run_id
                        == completed.task_sync_run_id
                    )
                )
            )
            replacement_by_number = {
                row.project_number: row for row in replacement_results
            }
            self.assertEqual(
                replacement_by_number[project_a].status,
                "synchronized",
            )
            self.assertEqual(
                replacement_by_number[project_b].status,
                "synchronized",
            )

        self.assertEqual(
            repository.get_occurrence(occurrence.id).success_attempt_id,
            second.id,
        )
        orchestrator.shutdown()
        replacement_orchestrator.shutdown()


    def test_healthy_peer_startup_does_not_interrupt_live_owner(self) -> None:
        occurrence = self._daily_occurrence()
        owner = SqlErpSyncControlRepository(self.factory).claim(
            occurrence_id=occurrence.id,
            trigger="SCHEDULED",
            actor_name="backend-A",
            settings=self.settings,
        )
        self.assertIsNotNone(owner)

        reconciled = SqlErpSyncControlRepository(
            self.factory
        ).reconcile_expired_owner()

        self.assertEqual(reconciled, 0)
        self.assertEqual(
            SqlErpSyncControlRepository(self.factory).get_attempt(owner.id).status,
            ERP_ATTEMPT_PENDING,
        )

    def test_expired_owner_is_reconciled_then_new_generation_can_claim(self) -> None:
        occurrence = self._daily_occurrence()
        repository = SqlErpSyncControlRepository(self.factory)
        first = repository.claim(
            occurrence_id=occurrence.id,
            trigger="SCHEDULED",
            actor_name="backend-A",
            settings=self.settings,
        )
        self.assertIsNotNone(first)
        with transactional_session(self.factory) as session:
            session.execute(
                text(
                    "UPDATE erp_sync_control "
                    "SET lease_expires_at = DATEADD(second, -10, SYSUTCDATETIME()) "
                    "WHERE id = :id"
                ),
                {"id": ERP_SYNC_CONTROL_ROW_ID},
            )

        self.assertEqual(repository.reconcile_expired_owner(), 1)
        self.assertEqual(
            repository.get_attempt(first.id).status,
            ERP_ATTEMPT_INTERRUPTED,
        )

        second = repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="backend-B",
            settings=self.settings,
            command_identity="retry:after-expiry",
        )
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)


if __name__ == "__main__":
    unittest.main()
