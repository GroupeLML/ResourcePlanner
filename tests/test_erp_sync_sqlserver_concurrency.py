from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import os
from threading import Barrier
import unittest

from sqlalchemy import func, select, text

from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
    resolve_scheduled_instant,
)
from app.infrastructure.sql import (
    AcumaticaProjectTaskSyncRun,
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_PENDING,
    ERP_OCCURRENCE_DAILY,
    ERP_OCCURRENCE_PENDING,
    ERP_SYNC_CONTROL_ROW_ID,
    GLOBAL_SYNC_RUN_KEY,
    SYNC_RUN_INTERRUPTED,
    SYNC_RUN_RUNNING,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncOccurrence,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server.erp_sync_orchestration import ErpSyncOrchestrator
from app.infrastructure.sql.erp_sync_control_repository import (
    ErpSyncFenceLost,
    SqlErpSyncControlRepository,
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
        occurrence = self._daily_occurrence()
        repository = SqlErpSyncControlRepository(self.factory)
        first = repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="backend-A",
            settings=self.settings,
        )
        self.assertIsNotNone(first)

        with transactional_session(self.factory) as session:
            child = AcumaticaProjectTaskSyncRun(
                id="SQL-CHILD-GEN-N",
                active_key=GLOBAL_SYNC_RUN_KEY,
                status=SYNC_RUN_RUNNING,
                parent_attempt_id=first.id,
                erp_generation=first.generation,
                started_at=datetime.now(timezone.utc),
            )
            session.add(child)
            stored_attempt = session.get(ErpSyncAttempt, first.id)
            self.assertIsNotNone(stored_attempt)
            stored_attempt.task_sync_run_id = child.id
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
                "SQL-CHILD-GEN-N",
            )
            self.assertIsNotNone(stale_child)
            self.assertEqual(stale_child.status, SYNC_RUN_INTERRUPTED)
            self.assertIsNone(stale_child.active_key)

        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=None,
            settings=self.settings,
        )
        replacement_child_id = orchestrator._create_task_run(
            second.id,
            second.generation,
        )
        with self.factory() as session:
            replacement = session.get(
                AcumaticaProjectTaskSyncRun,
                replacement_child_id,
            )
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.active_key, GLOBAL_SYNC_RUN_KEY)
            self.assertEqual(replacement.erp_generation, second.generation)

        with self.factory.begin() as stale_session:
            with self.assertRaises(ErpSyncFenceLost):
                SqlErpSyncControlRepository.guard_session(
                    stale_session,
                    attempt_id=first.id,
                    generation=first.generation,
                )
        with self.factory.begin() as stale_progress:
            with self.assertRaises(ErpSyncFenceLost):
                SqlErpSyncControlRepository.update_phase_in_session(
                    stale_progress,
                    attempt_id=first.id,
                    generation=first.generation,
                    phase="STALE_PROGRESS",
                )
        self.assertFalse(
            repository.finish(
                attempt_id=first.id,
                generation=first.generation,
                status="SUCCEEDED",
                phase="PUBLICATION_COMMITTED",
            )
        )
        orchestrator.shutdown()


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
