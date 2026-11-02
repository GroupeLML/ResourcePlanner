from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import os
from threading import Barrier
import unittest

from sqlalchemy import func, select, text

from app.application.erp_sync_scheduler import ErpSyncSchedulerSettings
from app.infrastructure.sql import (
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_PENDING,
    ERP_OCCURRENCE_DAILY,
    ERP_OCCURRENCE_PENDING,
    ERP_SYNC_CONTROL_ROW_ID,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncOccurrence,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
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
        cls.settings = ErpSyncSchedulerSettings(enabled=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with transactional_session(self.factory) as session:
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

    def _daily_occurrence(self) -> ErpSyncOccurrence:
        with transactional_session(self.factory) as session:
            row = ErpSyncOccurrence(
                occurrence_key="ERP_PROJECTS_TASKS_DAILY:2026-11-02",
                occurrence_type=ERP_OCCURRENCE_DAILY,
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=date(2026, 11, 2),
                scheduled_at=datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc),
                timezone_name="America/Toronto",
                scheduled_local_time="00:00",
                config_fingerprint="sqlserver-concurrency",
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
        self.assertEqual(count, 1)
        self.assertIsNotNone(control)
        self.assertEqual(control.owner_attempt_id, attempts[0].id)

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

    def test_generation_takeover_fences_stale_worker(self) -> None:
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

        second = repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="backend-B",
            settings=self.settings,
            command_identity="retry:sqlserver-takeover",
        )
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)

        with self.factory.begin() as stale_session:
            with self.assertRaises(ErpSyncFenceLost):
                SqlErpSyncControlRepository.guard_session(
                    stale_session,
                    attempt_id=first.id,
                    generation=first.generation,
                )

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
