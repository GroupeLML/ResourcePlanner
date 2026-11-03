from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import os
from threading import Barrier
import unittest

from sqlalchemy import delete, func, select, text

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
    resolve_scheduled_instant,
)
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
    ErpSyncControl,
    ErpSyncOccurrence,
    Project,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    TaskCatalogEntry,
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

    def list_projects(self):
        return self.rows


class SqlServerPortfolioTaskSource:
    def __init__(
        self,
        snapshots: dict[str, TaskCatalogProjectSnapshot],
    ) -> None:
        self.snapshots = snapshots

    def fetch_project_snapshots(
        self,
        *,
        project_targets: tuple[tuple[str, str], ...],
    ) -> TaskCatalogPortfolioSnapshot:
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

    def setUp(self) -> None:
        with transactional_session(self.factory) as session:
            session.execute(
                text("DELETE FROM acumatica_project_task_sync_project_results")
            )
            session.execute(text("DELETE FROM acumatica_project_task_sync_runs"))
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

        sql_now = SqlErpSyncControlRepository(self.factory).current_sql_time()
        zone_settings = ErpSyncSchedulerSettings(enabled=True)
        local_now = sql_now.astimezone(zone_settings.timezone())
        self.settings = ErpSyncSchedulerSettings(
            enabled=True,
            local_time=local_now.time().replace(second=0, microsecond=0),
            timezone_name=zone_settings.timezone_name,
        )
        self.automatic_safety = TestErpSyncAutomaticSafetyCapability()

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
            {project_a: _sql_snapshot(project_a, "100", "Task generation N")}
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
                    TaskCatalogEntry.task_code == "100",
                )
            )
            self.assertIsNotNone(committed_a)
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
                    TaskCatalogEntry.task_code == "100",
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
                    "100",
                    "Task generation N+1",
                ),
                project_b: _sql_snapshot(
                    project_b,
                    "200",
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
                [(project_a, "100"), (project_b, "200")],
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
