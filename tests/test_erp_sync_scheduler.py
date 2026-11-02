from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
    UnavailableErpSyncAutomaticSafetyCapability,
    resolve_scheduled_instant,
    scheduler_decision,
    validate_timezone,
)
from app.infrastructure.sql import (
    Base,
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_PENDING,
    ERP_OCCURRENCE_DISABLED,
    ErpSyncControl,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.erp_sync_control_repository import (
    ErpSyncFenceLost,
    SqlErpSyncControlRepository,
)


class ErpSyncCalendarTests(unittest.TestCase):
    def test_toronto_midnight_and_0037_catchup(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        midnight = scheduler_decision(
            datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc),
            settings,
        )
        self.assertEqual(midnight.business_date, date(2026, 1, 15))
        self.assertEqual(
            midnight.scheduled_at_utc,
            datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(midnight.state, "DUE")
        self.assertEqual(midnight.trigger, "SCHEDULED")

        catchup = scheduler_decision(
            datetime(2026, 1, 15, 5, 37, tzinfo=timezone.utc),
            settings,
        )
        self.assertEqual(catchup.state, "DUE")
        self.assertEqual(catchup.trigger, "CATCHUP")

    def test_catchup_deadline_is_exclusive_and_no_historical_backlog_is_selected(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True, catchup_hours=4)
        decision = scheduler_decision(
            datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
            settings,
        )
        self.assertEqual(decision.business_date, date(2026, 1, 15))
        self.assertEqual(decision.state, "MISSED")

    def test_spring_gap_advances_to_first_valid_local_instant(self) -> None:
        settings = ErpSyncSchedulerSettings(
            enabled=True,
            local_time=time(2, 30),
            timezone_name="America/Toronto",
        )
        self.assertEqual(
            resolve_scheduled_instant(date(2026, 3, 8), settings),
            datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
        )

    def test_fall_repeated_hour_uses_first_fold(self) -> None:
        settings = ErpSyncSchedulerSettings(
            enabled=True,
            local_time=time(1, 30),
            timezone_name="America/Toronto",
        )
        self.assertEqual(
            resolve_scheduled_instant(date(2026, 11, 1), settings),
            datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        )

    def test_invalid_timezone_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            validate_timezone("Not/A-Timezone")


class ErpSyncControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "erp-sync.db"
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(ErpSyncControl(id="ERP_PROJECTS_TASKS", generation=0))
        self.repository = SqlErpSyncControlRepository(self.factory)
        self.settings = ErpSyncSchedulerSettings(enabled=True)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    @staticmethod
    def fixed_now() -> datetime:
        return datetime(2026, 1, 15, 5, 37, tzinfo=timezone.utc)

    def test_daily_occurrence_is_unique_when_clock_moves_backward(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            first = self.repository.record_daily_decision(self.settings, capability)
            second = self.repository.record_daily_decision(self.settings, capability)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.business_date, date(2026, 1, 15))

    def test_disabled_scheduler_creates_no_automatic_occurrence(self) -> None:
        disabled = ErpSyncSchedulerSettings(enabled=False)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                disabled,
                TestErpSyncAutomaticSafetyCapability(),
            )
        self.assertIsNone(occurrence)
        with self.factory() as session:
            from app.infrastructure.sql import ErpSyncOccurrence
            self.assertEqual(
                session.query(ErpSyncOccurrence).count(),
                0,
            )

    def test_late_restart_marks_only_current_business_date_missed(self) -> None:
        late = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=late,
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                TestErpSyncAutomaticSafetyCapability(),
            )
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.business_date, date(2026, 1, 15))
        self.assertEqual(occurrence.status, "MISSED")
        with self.factory() as session:
            from app.infrastructure.sql import ErpSyncOccurrence
            rows = tuple(session.query(ErpSyncOccurrence).all())
        self.assertEqual(len(rows), 1)

    def test_enabled_scheduler_is_blocked_without_643b_capability(self) -> None:
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                UnavailableErpSyncAutomaticSafetyCapability(),
            )
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.status, ERP_OCCURRENCE_DISABLED)
        self.assertEqual(
            occurrence.diagnostic,
            "erp_sync_643b_safety_capability_unavailable",
        )

    def test_manual_acceptance_is_idempotent(self) -> None:
        first = self.repository.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="same-command",
            settings=self.settings,
        )
        second = self.repository.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="same-command",
            settings=self.settings,
        )
        self.assertEqual(first.id, second.id)

    def test_healthy_peer_restart_does_not_interrupt_owner(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="healthy",
            settings=self.settings,
        )
        attempt = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        self.assertEqual(self.repository.reconcile_expired_owner(), 0)
        self.assertEqual(
            self.repository.get_attempt(attempt.id).status,
            ERP_ATTEMPT_PENDING,
        )

    def test_expired_owner_is_interrupted_and_new_generation_fences_stale_worker(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="takeover",
            settings=self.settings,
        )
        first = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(first)

        with transactional_session(self.factory) as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertIsNotNone(control)
            control.lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

        second = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.settings,
            command_identity="retry:takeover",
        )
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)
        self.assertEqual(
            self.repository.get_attempt(first.id).status,
            ERP_ATTEMPT_INTERRUPTED,
        )

        with self.factory.begin() as session:
            with self.assertRaises(ErpSyncFenceLost):
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=first.id,
                    generation=first.generation,
                )

    def test_automatic_retries_are_bounded(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="scheduler",
            idempotency_key="auto-bounded",
            settings=self.settings,
        )
        attempts = []
        for index in range(self.settings.max_automatic_attempts):
            attempt = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP" if index == 0 else "RETRY",
                actor_name="scheduler",
                settings=self.settings,
                automatic=True,
                command_identity=f"auto:{index}",
            )
            self.assertIsNotNone(attempt)
            attempts.append(attempt)
            with transactional_session(self.factory) as session:
                control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
                self.assertIsNotNone(control)
                control.lease_expires_at = datetime(
                    2000, 1, 1, tzinfo=timezone.utc
                )

        blocked = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="scheduler",
            settings=self.settings,
            automatic=True,
            command_identity="auto:blocked",
        )
        self.assertIsNone(blocked)
        self.assertEqual(len(attempts), self.settings.max_automatic_attempts)

    def test_retry_claim_is_idempotent(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="retry-base",
            settings=self.settings,
        )
        first = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(first)
        with transactional_session(self.factory) as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertIsNotNone(control)
            control.lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

        retry_one = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.settings,
            command_identity="retry:same",
        )
        retry_two = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name="operator",
            settings=self.settings,
            command_identity="retry:same",
        )
        self.assertIsNotNone(retry_one)
        self.assertIsNotNone(retry_two)
        self.assertEqual(retry_one.id, retry_two.id)


if __name__ == "__main__":
    unittest.main()
