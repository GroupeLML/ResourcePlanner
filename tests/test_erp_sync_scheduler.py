from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app.application.errors import ApplicationConflictError
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
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_SUCCEEDED,
    ERP_OCCURRENCE_DISABLED,
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

    def test_catchup_above_twelve_hours_is_rejected_even_when_injected(self) -> None:
        with self.assertRaises(ValueError):
            ErpSyncSchedulerSettings(enabled=True, catchup_hours=13)


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
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
            self.assertIsNotNone(occurrence)
            attempts = []
            for index in range(self.settings.max_automatic_attempts):
                attempt = self.repository.claim(
                    occurrence_id=occurrence.id,
                    trigger="CATCHUP" if index == 0 else "RETRY",
                    actor_name="scheduler",
                    settings=self.settings,
                    automatic=True,
                    command_identity=f"auto:{index}",
                    automatic_safety=capability,
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
                automatic_safety=capability,
            )
        self.assertIsNone(blocked)
        self.assertEqual(len(attempts), self.settings.max_automatic_attempts)

    def test_disabled_occurrence_manual_failure_cannot_bypass_automatic_safety(self) -> None:
        unavailable = UnavailableErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                unavailable,
            )
            self.assertIsNotNone(occurrence)
            manual = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="RETRY",
                actor_name="operator",
                settings=self.settings,
            )
            self.assertIsNotNone(manual)
            self.repository.start_attempt(
                attempt_id=manual.id,
                generation=manual.generation,
            )
            self.repository.finish(
                attempt_id=manual.id,
                generation=manual.generation,
                status=ERP_ATTEMPT_FAILED,
                phase="FAILED",
                error_code="acumatica_project_read_failed",
            )

        later = datetime(2026, 1, 15, 5, 53, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=later,
        ):
            with self.assertRaises(ApplicationConflictError) as caught:
                self.repository.claim(
                    occurrence_id=occurrence.id,
                    trigger="RETRY",
                    actor_name="scheduler",
                    settings=self.settings,
                    automatic=True,
                    automatic_safety=unavailable,
                )
        self.assertEqual(caught.exception.code, "erp_sync_automatic_safety_unavailable")
        with self.factory() as session:
            attempts = tuple(
                session.scalars(
                    select(ErpSyncAttempt).where(
                        ErpSyncAttempt.occurrence_id == occurrence.id
                    )
                )
            )
        self.assertEqual(len(attempts), 1)

    def test_delayed_automatic_claim_after_frozen_deadline_is_rejected(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
        self.assertIsNotNone(occurrence)

        late = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=late,
        ):
            rejected = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP",
                actor_name="scheduler",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )
        self.assertIsNone(rejected)
        self.assertEqual(
            self.repository.get_occurrence(occurrence.id).status,
            "MISSED",
        )

    def test_delayed_automatic_claim_preserves_healthy_running_owner(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
        self.assertIsNotNone(occurrence)

        before_deadline = datetime(2026, 1, 15, 8, 59, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=before_deadline,
        ):
            owner = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP",
                actor_name="scheduler-A",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )
            self.assertIsNotNone(owner)
            self.assertTrue(
                self.repository.start_attempt(
                    attempt_id=owner.id,
                    generation=owner.generation,
                    settings=self.settings,
                )
            )

        after_deadline = datetime(2026, 1, 15, 9, 1, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=after_deadline,
        ):
            observed = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="RETRY",
                actor_name="scheduler-B",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )

        self.assertIsNotNone(observed)
        self.assertEqual(observed.id, owner.id)
        persisted = self.repository.get_occurrence(occurrence.id)
        self.assertEqual(persisted.status, "PENDING")
        self.assertIsNone(persisted.success_attempt_id)

    def test_delayed_automatic_claim_preserves_completed_success(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
        self.assertIsNotNone(occurrence)

        before_deadline = datetime(2026, 1, 15, 8, 59, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=before_deadline,
        ):
            owner = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP",
                actor_name="scheduler-A",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )
            self.assertIsNotNone(owner)
            self.assertTrue(
                self.repository.start_attempt(
                    attempt_id=owner.id,
                    generation=owner.generation,
                    settings=self.settings,
                )
            )
            self.assertTrue(
                self.repository.finish(
                    attempt_id=owner.id,
                    generation=owner.generation,
                    status=ERP_ATTEMPT_SUCCEEDED,
                    phase="PUBLICATION_COMMITTED",
                    settings=self.settings,
                )
            )

        after_deadline = datetime(2026, 1, 15, 9, 1, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=after_deadline,
        ):
            observed = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="RETRY",
                actor_name="scheduler-B",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )

        self.assertIsNone(observed)
        persisted = self.repository.get_occurrence(occurrence.id)
        self.assertEqual(persisted.status, "SUCCEEDED")
        self.assertEqual(persisted.success_attempt_id, owner.id)

    def test_automatic_retry_after_deadline_is_rejected(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            occurrence = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
            first = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP",
                actor_name="scheduler",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )
            self.assertIsNotNone(first)
            self.repository.start_attempt(
                attempt_id=first.id,
                generation=first.generation,
            )
            self.repository.finish(
                attempt_id=first.id,
                generation=first.generation,
                status=ERP_ATTEMPT_FAILED,
                phase="FAILED",
                error_code="acumatica_project_read_failed",
            )

        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        ):
            rejected = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="RETRY",
                actor_name="scheduler",
                settings=self.settings,
                automatic=True,
                automatic_safety=capability,
            )
        self.assertIsNone(rejected)
        self.assertEqual(
            self.repository.get_occurrence(occurrence.id).status,
            "MISSED",
        )

    def test_business_date_watermark_never_moves_backward(self) -> None:
        capability = TestErpSyncAutomaticSafetyCapability()
        nov3 = datetime(2026, 11, 3, 5, 37, tzinfo=timezone.utc)
        nov2 = datetime(2026, 11, 2, 5, 37, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=nov3,
        ):
            newer = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
        self.assertIsNotNone(newer)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=nov2,
        ):
            older = self.repository.record_daily_decision(
                self.settings,
                capability,
            )
        self.assertIsNone(older)
        with self.factory() as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertEqual(
                control.last_evaluated_business_date,
                date(2026, 11, 3),
            )
            dates = tuple(
                session.scalars(
                    select(ErpSyncOccurrence.business_date).order_by(
                        ErpSyncOccurrence.business_date
                    )
                )
            )
        self.assertEqual(dates, (date(2026, 11, 3),))

    def test_same_boundary_configuration_change_is_not_adopted_without_owner(self) -> None:
        original = ErpSyncSchedulerSettings(
            enabled=False,
            source_identity="acumatica:boundary-original",
        )
        same_day = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=same_day,
        ):
            occurrence = self.repository.accept_manual(
                intent="PROJECTS_ONLY",
                actor_name="operator",
                idempotency_key="boundary-original",
                settings=original,
            )
            attempt = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="MANUAL",
                actor_name="operator",
                settings=original,
            )
            self.assertIsNotNone(attempt)
            self.repository.start_attempt(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=original,
            )
            self.repository.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status=ERP_ATTEMPT_SUCCEEDED,
                phase="PUBLICATION_COMMITTED",
                settings=original,
            )

            changed = ErpSyncSchedulerSettings(
                enabled=False,
                source_identity="acumatica:boundary-changed",
            )
            with self.assertRaises(ApplicationConflictError) as reconcile_error:
                self.repository.reconcile_configuration(changed)
            self.assertEqual(
                reconcile_error.exception.code,
                "erp_sync_configuration_incompatible",
            )
            with self.assertRaises(ApplicationConflictError) as manual_error:
                self.repository.accept_manual(
                    intent="PROJECTS_ONLY",
                    actor_name="operator",
                    idempotency_key="boundary-changed",
                    settings=changed,
                )
            self.assertEqual(
                manual_error.exception.code,
                "erp_sync_configuration_incompatible",
            )

        with self.factory() as session:
            authority = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertIsNotNone(authority)
            self.assertEqual(
                authority.config_fingerprint,
                original.config_fingerprint,
            )

    def test_incompatible_peer_cannot_overwrite_active_fingerprint(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="config-owner",
            settings=self.settings,
        )
        owner = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(owner)
        incompatible = ErpSyncSchedulerSettings(
            enabled=True,
            source_identity="acumatica:other-site",
        )
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=self.fixed_now(),
        ):
            with self.assertRaises(ApplicationConflictError) as caught:
                self.repository.record_daily_decision(
                    incompatible,
                    TestErpSyncAutomaticSafetyCapability(),
                )
        self.assertEqual(caught.exception.code, "erp_sync_configuration_incompatible")
        with self.factory() as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertEqual(control.config_fingerprint, self.settings.config_fingerprint)

        with self.assertRaises(ApplicationConflictError) as claim_error:
            self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="RETRY",
                actor_name="incompatible-peer",
                settings=incompatible,
            )
        self.assertEqual(
            claim_error.exception.code,
            "erp_sync_configuration_incompatible",
        )
        with self.factory() as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertEqual(control.owner_attempt_id, owner.id)
            self.assertEqual(
                control.config_fingerprint,
                self.settings.config_fingerprint,
            )

    def test_incompatible_local_settings_are_rejected_before_guarded_write(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="local-config-owner",
            settings=self.settings,
        )
        owner = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(owner)
        incompatible = ErpSyncSchedulerSettings(
            enabled=True,
            source_identity="acumatica:other-site",
        )
        with self.factory.begin() as session:
            with self.assertRaises(ErpSyncFenceLost) as caught:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=owner.id,
                    generation=owner.generation,
                    settings=incompatible,
                )
        self.assertEqual(
            caught.exception.code,
            "erp_sync_configuration_incompatible",
        )

    def test_unsupported_protocol_blocks_claim_and_guarded_write(self) -> None:
        occurrence = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="protocol",
            settings=self.settings,
        )
        owner = self.repository.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(owner)
        with transactional_session(self.factory) as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            control.protocol_version = 999
        with self.assertRaises(ErpSyncFenceLost) as guarded:
            with self.factory.begin() as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=owner.id,
                    generation=owner.generation,
                )
        self.assertEqual(guarded.exception.code, "erp_sync_protocol_incompatible")

        with transactional_session(self.factory) as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            control.protocol_version = 1
            control.owner_attempt_id = None
            control.lease_expires_at = None
            control.owner_deadline_at = None
            control.heartbeat_at = None
        another = self.repository.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="operator",
            idempotency_key="protocol-claim",
            settings=self.settings,
        )
        with transactional_session(self.factory) as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            control.protocol_version = 999
        with self.assertRaises(ApplicationConflictError) as caught:
            self.repository.claim(
                occurrence_id=another.id,
                trigger="MANUAL",
                actor_name="operator",
                settings=self.settings,
            )
        self.assertEqual(caught.exception.code, "erp_sync_protocol_incompatible")

    def test_only_complete_global_success_advances_global_success_pointer(self) -> None:
        global_occurrence = self.repository.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="operator",
            idempotency_key="global-success",
            settings=self.settings,
        )
        global_attempt = self.repository.claim(
            occurrence_id=global_occurrence.id,
            trigger="MANUAL",
            actor_name="operator",
            settings=self.settings,
        )
        self.assertIsNotNone(global_attempt)
        self.repository.start_attempt(
            attempt_id=global_attempt.id,
            generation=global_attempt.generation,
        )
        self.repository.finish(
            attempt_id=global_attempt.id,
            generation=global_attempt.generation,
            status=ERP_ATTEMPT_SUCCEEDED,
            phase="PUBLICATION_COMMITTED",
        )

        with self.factory() as session:
            control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertEqual(control.last_success_attempt_id, global_attempt.id)

        for index, intent in enumerate(
            ("PROJECTS_ONLY", "TARGETED_PROJECT_TASKS:PROJECT-1"),
            start=1,
        ):
            occurrence = self.repository.accept_manual(
                intent=intent,
                actor_name="operator",
                idempotency_key=f"scope-success-{index}",
                settings=self.settings,
            )
            attempt = self.repository.claim(
                occurrence_id=occurrence.id,
                trigger="MANUAL",
                actor_name="operator",
                settings=self.settings,
            )
            self.assertIsNotNone(attempt)
            self.repository.start_attempt(
                attempt_id=attempt.id,
                generation=attempt.generation,
            )
            self.repository.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status=ERP_ATTEMPT_SUCCEEDED,
                phase="PUBLICATION_COMMITTED",
            )
            self.assertEqual(
                self.repository.get_occurrence(occurrence.id).success_attempt_id,
                attempt.id,
            )
            with self.factory() as session:
                control = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
                self.assertEqual(
                    control.last_success_attempt_id,
                    global_attempt.id,
                )

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
