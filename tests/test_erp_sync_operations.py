from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.erp_sync_operations import (
    ErpSyncAttemptOperations,
    ErpSyncEvidenceOperations,
    ErpSyncOperationsData,
    ErpSyncOperationsService,
    ErpSyncProjectOperations,
    ErpSyncRecoveryOperations,
)
from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
)
from app.application.security import (
    AuthPrincipal,
    PERMISSION_READ,
    PERMISSION_SYNC_PROJECTS,
)
from app.infrastructure.sql import (
    Base,
    ERP_SYNC_CONTROL_ROW_ID,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncEvidenceManifest,
    ErpSyncEvidenceReservation,
    ErpSyncOccurrence,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.erp_sync_operations_repository import (
    SqlErpSyncOperationsRepository,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


class Capability:
    def __init__(self, available: bool) -> None:
        self.available = available

    def automatic_execution_available(self) -> bool:
        return self.available

    def diagnostic(self) -> str:
        return "erp_sync_test_safety_not_ready"


class Reader:
    def __init__(self, data: ErpSyncOperationsData) -> None:
        self.data = data

    def read(self, *, evidence_budget_bytes: int | None) -> ErpSyncOperationsData:
        return replace(
            self.data,
            evidence=replace(
                self.data.evidence,
                budget_bytes=evidence_budget_bytes,
            ),
        )


def attempt(
    id: str,
    *,
    status: str,
    finished_at: datetime | None = None,
    phase: str = "COMPLETE",
    generation: int = 1,
) -> ErpSyncAttemptOperations:
    return ErpSyncAttemptOperations(
        id=id,
        occurrence_id=f"O-{id}",
        status=status,
        phase=phase,
        trigger_kind="SCHEDULED",
        generation=generation,
        started_at=finished_at - timedelta(minutes=5) if finished_at else None,
        finished_at=finished_at,
        heartbeat_at=None,
        duration_ms=None,
        projects_received=0,
        projects_created=0,
        projects_updated=0,
        projects_unchanged=0,
        task_projects_total=None,
        task_projects_synchronized=None,
        task_projects_rejected=None,
        task_rows_received=None,
        tasks_received=None,
        tasks_created=None,
        tasks_updated=None,
        tasks_unchanged=None,
        tasks_deactivated=None,
        tasks_rejected=None,
        error_code="test_failure" if status == "FAILED" else None,
        diagnostic=None,
    )


def project(
    number: str,
    quality: str,
    *,
    attempt_id: str = "A-1",
) -> ErpSyncProjectOperations:
    return ErpSyncProjectOperations(
        project_number=number,
        result_status="rejected" if quality in {"STALE", "QUARANTINED"} else "synchronized",
        quality_state=quality,
        source_observed_at=datetime(2026, 11, 5, 4, tzinfo=timezone.utc),
        validated_at=datetime(2026, 11, 5, 4, 5, tzinfo=timezone.utc),
        reason_code="quality_test" if quality != "VALID" else None,
        error_code="project_test_failure" if quality != "VALID" else None,
        producer_attempt_id=attempt_id,
        producer_generation=1,
    )


def evidence(status: str = "AVAILABLE") -> ErpSyncEvidenceOperations:
    return ErpSyncEvidenceOperations(
        status=status,
        manifests_total=2,
        source_manifests=1,
        checkpoint_manifests=1,
        manifests_ready=2,
        manifests_expired=0,
        manifests_pinned=1 if status == "PINNED" else 0,
        active_reservations=1 if status == "PRESSURE" else 0,
        reserved_bytes=90 if status == "PRESSURE" else 0,
        used_bytes=80 if status == "PRESSURE" else 0,
        budget_bytes=100,
        pressure_ratio=0.9 if status == "PRESSURE" else 0.0,
    )


def data(
    settings: ErpSyncSchedulerSettings,
    *,
    now: datetime,
    attempts: tuple[ErpSyncAttemptOperations, ...] = (),
    success: ErpSyncAttemptOperations | None = None,
    projects: tuple[ErpSyncProjectOperations, ...] = (),
    recovery: ErpSyncRecoveryOperations | None = None,
    current_daily_occurrence_status: str | None = "SUCCEEDED",
    last_evaluated_business_date: date | None = None,
    evidence_state: ErpSyncEvidenceOperations | None = None,
) -> ErpSyncOperationsData:
    business_date = now.astimezone(settings.timezone()).date()
    return ErpSyncOperationsData(
        now_utc=now,
        control_generation=4,
        owner_attempt_id=None,
        lease_expires_at=None,
        owner_deadline_at=None,
        control_heartbeat_at=None,
        scheduler_observed_at=now - timedelta(seconds=30),
        control_updated_at=now - timedelta(minutes=1),
        last_evaluated_business_date=(
            business_date
            if last_evaluated_business_date is None
            else last_evaluated_business_date
        ),
        control_protocol_version=1,
        control_config_fingerprint=settings.config_fingerprint,
        current_daily_occurrence_status=current_daily_occurrence_status,
        attempts=attempts,
        last_complete_success=success,
        projects=projects,
        recovery=recovery
        or ErpSyncRecoveryOperations(
            state="NONE",
            attempt_id=None,
            reason_code=None,
            pinned=False,
        ),
        evidence=evidence_state or evidence(),
    )


class ErpSyncOperationsServiceTests(unittest.TestCase):
    def service(
        self,
        settings: ErpSyncSchedulerSettings,
        value: ErpSyncOperationsData,
        *,
        available: bool = True,
    ) -> ErpSyncOperationsService:
        return ErpSyncOperationsService(
            Reader(value),
            settings=settings,
            automatic_safety=Capability(available),
            freshness_sla_hours=settings.freshness_sla_hours,
        )

    def test_initial_state_has_unknown_freshness_without_fabricated_success(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=False)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        snapshot = self.service(settings, data(settings, now=now)).snapshot(
            evidence_budget_bytes=100
        )
        self.assertEqual(snapshot.scheduler_status, "DISABLED")
        self.assertIsNone(snapshot.last_attempt)
        self.assertIsNone(snapshot.last_complete_success)
        self.assertEqual(snapshot.freshness_status, "UNKNOWN")
        self.assertIsNone(snapshot.freshness_age_seconds)
        self.assertIsNone(snapshot.next_scheduled_run)

    def test_failed_attempt_does_not_replace_prior_complete_success(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        good = attempt("GOOD", status="SUCCEEDED", finished_at=now - timedelta(hours=20))
        failed = attempt("FAILED", status="FAILED", finished_at=now - timedelta(hours=1))
        snapshot = self.service(
            settings,
            data(settings, now=now, attempts=(failed, good), success=good),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.last_attempt.id, "FAILED")
        self.assertEqual(snapshot.last_complete_success.id, "GOOD")
        self.assertEqual(snapshot.scheduler_status, "FAILED")
        self.assertEqual(snapshot.freshness_status, "FRESH")

    def test_partial_result_preserves_project_quality_and_global_success_pointer(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        good = attempt("GOOD", status="SUCCEEDED", finished_at=now - timedelta(hours=20))
        partial = attempt(
            "PARTIAL",
            status="SUCCEEDED_WITH_ERRORS",
            finished_at=now - timedelta(hours=1),
        )
        snapshot = self.service(
            settings,
            data(
                settings,
                now=now,
                attempts=(partial, good),
                success=good,
                projects=(
                    project("A", "VALID", attempt_id="PARTIAL"),
                    project("B", "STALE", attempt_id="PARTIAL"),
                    project("C", "VALID", attempt_id="PARTIAL"),
                ),
            ),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.scheduler_status, "PARTIAL")
        self.assertEqual(snapshot.last_complete_success.id, "GOOD")
        self.assertEqual(snapshot.freshness_status, "PARTIAL")
        states = {row.project_number: row.quality_state for row in snapshot.projects}
        self.assertEqual(states, {"A": "VALID", "B": "STALE", "C": "VALID"})

    def test_enabled_scheduler_missing_observation_is_not_disabled(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        value = data(
            settings,
            now=now,
            current_daily_occurrence_status=None,
            last_evaluated_business_date=date(2026, 11, 4),
        )
        snapshot = self.service(settings, value).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.scheduler_observation_status, "MISSING")
        self.assertEqual(snapshot.scheduler_status, "MISSED")

    def test_stopped_idle_scheduler_observation_is_stale(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True, heartbeat_seconds=60)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        value = replace(
            data(settings, now=now),
            scheduler_observed_at=now - timedelta(minutes=3),
        )
        snapshot = self.service(settings, value).snapshot(
            evidence_budget_bytes=100
        )
        self.assertEqual(snapshot.scheduler_observation_status, "STALE")
        self.assertEqual(snapshot.scheduler_status, "STALE")

    def test_active_owner_with_stale_heartbeat_is_not_reported_healthy(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True, heartbeat_seconds=60)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        running = attempt(
            "RUNNING",
            status="RUNNING",
            phase="TASKS",
        )
        value = replace(
            data(settings, now=now, attempts=(running,)),
            owner_attempt_id=running.id,
            lease_expires_at=now + timedelta(minutes=2),
            control_heartbeat_at=now - timedelta(minutes=3),
        )
        snapshot = self.service(settings, value).snapshot(
            evidence_budget_bytes=100
        )
        self.assertEqual(snapshot.scheduler_observation_status, "OBSERVED")
        self.assertEqual(snapshot.scheduler_status, "STALE")
        self.assertEqual(snapshot.current_attempt_id, running.id)
        self.assertEqual(snapshot.scheduler_observation_age_seconds, 180)

    def test_pending_review_remains_current_without_being_lease_owner(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        pending = attempt(
            "REVIEW",
            status="PENDING",
            phase="PLAUSIBILITY_REVIEW_REQUIRED",
        )
        snapshot = self.service(
            settings,
            data(settings, now=now, attempts=(pending,)),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.current_attempt_id, "REVIEW")
        self.assertEqual(
            snapshot.current_phase,
            "PLAUSIBILITY_REVIEW_REQUIRED",
        )
        self.assertIsNone(snapshot.current_owner_attempt_id)

    def test_dst_next_run_reuses_scheduler_calendar_resolution(self) -> None:
        settings = ErpSyncSchedulerSettings(
            enabled=True,
            local_time=time(2, 30),
            timezone_name="America/Toronto",
        )
        now = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
        value = data(
            settings,
            now=now,
            current_daily_occurrence_status=None,
        )
        snapshot = self.service(settings, value).snapshot(evidence_budget_bytes=100)
        self.assertEqual(
            snapshot.next_scheduled_run,
            datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
        )

    def test_stale_complete_success_classifies_without_rewriting_attempt(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True, freshness_sla_hours=30)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        old = attempt("OLD", status="SUCCEEDED", finished_at=now - timedelta(hours=31))
        snapshot = self.service(
            settings,
            data(settings, now=now, attempts=(old,), success=old),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.scheduler_status, "STALE")
        self.assertEqual(snapshot.freshness_status, "STALE")
        self.assertEqual(snapshot.last_attempt.status, "SUCCEEDED")

    def test_quarantine_and_required_recovery_are_visible_and_blocking(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        required = ErpSyncRecoveryOperations(
            state="REQUIRED",
            attempt_id="A-RECOVERY",
            reason_code="proof_invalid",
            pinned=True,
        )
        snapshot = self.service(
            settings,
            data(
                settings,
                now=now,
                projects=(project("Q", "QUARANTINED"),),
                recovery=required,
            ),
            available=False,
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.scheduler_status, "QUARANTINED")
        self.assertEqual(snapshot.recovery_state.state, "REQUIRED")
        self.assertIn("proof_invalid", snapshot.readiness_blockers)
        self.assertFalse(snapshot.automatic_execution_available)
        self.assertFalse(snapshot.safe_to_operate)

    def test_evidence_pressure_and_capability_failure_are_safe_blockers(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 3, tzinfo=timezone.utc)
        snapshot = self.service(
            settings,
            data(settings, now=now, evidence_state=evidence("PRESSURE")),
            available=False,
        ).snapshot(evidence_budget_bytes=100)
        self.assertFalse(snapshot.automatic_execution_available)
        self.assertIn("erp_sync_test_safety_not_ready", snapshot.readiness_blockers)
        self.assertIn("erp_sync_evidence_pressure", snapshot.readiness_blockers)
        self.assertEqual(snapshot.evidence.pressure_ratio, 0.9)
        self.assertFalse(snapshot.safe_to_operate)


    def test_explicit_missed_occurrence_preserves_bounded_catchup_classification(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True, catchup_hours=4)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        snapshot = self.service(
            settings,
            data(
                settings,
                now=now,
                current_daily_occurrence_status="MISSED",
            ),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.scheduler_status, "MISSED")
        self.assertEqual(snapshot.scheduler_observation_status, "OBSERVED")
        self.assertEqual(
            snapshot.next_scheduled_run,
            datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc),
        )

    def test_safety_disabled_daily_occurrence_is_blocked_not_intentionally_disabled(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        snapshot = self.service(
            settings,
            data(
                settings,
                now=now,
                current_daily_occurrence_status="DISABLED",
            ),
            available=True,
        ).snapshot(evidence_budget_bytes=100)
        self.assertTrue(snapshot.automation_enabled)
        self.assertEqual(snapshot.scheduler_status, "BLOCKED")
        self.assertNotEqual(snapshot.scheduler_status, "DISABLED")

    def test_incompatible_control_profile_is_visible_and_blocks_operation(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        value = replace(
            data(settings, now=now),
            control_config_fingerprint="different-profile",
        )
        snapshot = self.service(settings, value).snapshot(
            evidence_budget_bytes=100
        )
        self.assertEqual(snapshot.compatibility.status, "INCOMPATIBLE")
        self.assertFalse(snapshot.compatibility.configuration_compatible)
        self.assertIn(
            "erp_sync_runtime_compatibility_incompatible",
            snapshot.readiness_blockers,
        )
        self.assertFalse(snapshot.safe_to_operate)

    def test_repeated_failure_signal_counts_terminal_failures_since_success(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        previous = attempt(
            "PREVIOUS",
            status="SUCCEEDED",
            finished_at=now - timedelta(hours=5),
        )
        first = attempt(
            "FAIL-1",
            status="FAILED",
            finished_at=now - timedelta(hours=2),
        )
        second = attempt(
            "FAIL-2",
            status="FAILED",
            finished_at=now - timedelta(hours=1),
        )
        snapshot = self.service(
            settings,
            data(
                settings,
                now=now,
                attempts=(second, first, previous),
                success=previous,
            ),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(snapshot.consecutive_failures, 2)
        self.assertFalse(snapshot.repeated_failure)

        third = attempt(
            "FAIL-3",
            status="FAILED",
            finished_at=now - timedelta(minutes=30),
        )
        three = self.service(
            settings,
            data(
                settings,
                now=now,
                attempts=(third, second, first, previous),
                success=previous,
            ),
        ).snapshot(evidence_budget_bytes=100)
        self.assertEqual(three.consecutive_failures, 3)
        self.assertTrue(three.repeated_failure)

class ErpSyncOperationsSqlAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "operations.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        self.settings = ErpSyncSchedulerSettings(enabled=False)
        with transactional_session(self.factory) as session:
            session.add(
                ErpSyncControl(
                    id=ERP_SYNC_CONTROL_ROW_ID,
                    generation=0,
                    protocol_version=1,
                    config_fingerprint=self.settings.config_fingerprint,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def test_sql_projection_uses_durable_last_success_pointer_after_failure(self) -> None:
        now = datetime.now(timezone.utc)
        with transactional_session(self.factory) as session:
            success_occurrence = ErpSyncOccurrence(
                occurrence_key="MANUAL:success",
                occurrence_type="MANUAL",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=now.date(),
                scheduled_at=None,
                timezone_name="America/Toronto",
                scheduled_local_time="00:00",
                config_fingerprint=self.settings.config_fingerprint,
                status="SUCCEEDED",
            )
            failed_occurrence = ErpSyncOccurrence(
                occurrence_key="MANUAL:failed",
                occurrence_type="MANUAL",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=now.date(),
                scheduled_at=None,
                timezone_name="America/Toronto",
                scheduled_local_time="00:00",
                config_fingerprint=self.settings.config_fingerprint,
                status="FAILED",
            )
            session.add_all((success_occurrence, failed_occurrence))
            session.flush()
            success = ErpSyncAttempt(
                occurrence_id=success_occurrence.id,
                attempt_number=1,
                trigger_kind="MANUAL",
                actor_name="operator",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                generation=1,
                status="SUCCEEDED",
                phase="COMPLETE",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1),
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
            )
            failed = ErpSyncAttempt(
                occurrence_id=failed_occurrence.id,
                attempt_number=1,
                trigger_kind="MANUAL",
                actor_name="operator",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                generation=2,
                status="FAILED",
                phase="FAILED",
                started_at=now - timedelta(minutes=30),
                finished_at=now - timedelta(minutes=20),
                error_code="test_failure",
                created_at=now - timedelta(minutes=30),
                updated_at=now - timedelta(minutes=20),
            )
            session.add_all((success, failed))
            session.flush()
            control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            control.last_success_attempt_id = success.id
        value = SqlErpSyncOperationsRepository(
            self.factory,
            timezone_name="America/Toronto",
        ).read(evidence_budget_bytes=1024)
        self.assertEqual(value.attempts[0].status, "FAILED")
        self.assertEqual(value.last_complete_success.status, "SUCCEEDED")

    def _add_evidence_reservation(
        self,
        *,
        key: str,
        reserved_bytes: int,
        used_bytes: int,
        released_at: datetime | None,
    ) -> str:
        now = datetime.now(timezone.utc)
        with transactional_session(self.factory) as session:
            occurrence = ErpSyncOccurrence(
                occurrence_key=f"MANUAL:evidence-{key}",
                occurrence_type="MANUAL",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=now.date(),
                scheduled_at=None,
                timezone_name="America/Toronto",
                scheduled_local_time="00:00",
                config_fingerprint=self.settings.config_fingerprint,
                status="FAILED",
            )
            session.add(occurrence)
            session.flush()
            attempt_row = ErpSyncAttempt(
                occurrence_id=occurrence.id,
                attempt_number=1,
                trigger_kind="MANUAL",
                actor_name="operator",
                intent="PROJECTS_AND_ACTIVE_TASKS",
                generation=1,
                status="FAILED",
                phase="FAILED",
                started_at=now,
                finished_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(attempt_row)
            session.flush()
            session.add(
                ErpSyncEvidenceReservation(
                    attempt_id=attempt_row.id,
                    reserved_bytes=reserved_bytes,
                    used_bytes=used_bytes,
                    reserved_plaintext_bytes=reserved_bytes,
                    used_plaintext_bytes=used_bytes,
                    released_at=released_at,
                )
            )
            attempt_id = attempt_row.id
        return attempt_id

    def test_elapsed_expiry_is_projected_before_physical_cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        attempt_id = self._add_evidence_reservation(
            key="elapsed-expiry",
            reserved_bytes=500,
            used_bytes=400,
            released_at=now,
        )
        with transactional_session(self.factory) as session:
            session.add(
                ErpSyncEvidenceManifest(
                    attempt_id=attempt_id,
                    protocol_version=1,
                    source_type="PROJECTS",
                    proof_type="SOURCE",
                    profile_fingerprint="f" * 64,
                    source_fingerprint="s" * 64,
                    observed_at=now - timedelta(days=20),
                    row_count=1,
                    chunk_count=1,
                    plaintext_bytes=100,
                    ciphertext_bytes=400,
                    content_sha256="c" * 64,
                    state="READY",
                    pinned=False,
                    expires_at=now - timedelta(seconds=1),
                )
            )
        value = SqlErpSyncOperationsRepository(
            self.factory,
            timezone_name="America/Toronto",
        ).read(evidence_budget_bytes=1000)
        self.assertEqual(value.evidence.manifests_expired, 1)
        self.assertEqual(value.evidence.status, "EXPIRED")
        self.assertEqual(value.evidence.used_bytes, 400)

    def test_released_retained_evidence_remains_charged_in_operations_projection(self) -> None:
        now = datetime.now(timezone.utc)
        self._add_evidence_reservation(
            key="retained",
            reserved_bytes=500,
            used_bytes=400,
            released_at=now,
        )
        value = SqlErpSyncOperationsRepository(
            self.factory,
            timezone_name="America/Toronto",
        ).read(evidence_budget_bytes=1000)
        self.assertEqual(value.evidence.reserved_bytes, 0)
        self.assertEqual(value.evidence.used_bytes, 400)
        self.assertEqual(value.evidence.pressure_ratio, 0.4)

    def test_mixed_active_and_retained_evidence_uses_canonical_charged_usage(self) -> None:
        now = datetime.now(timezone.utc)
        self._add_evidence_reservation(
            key="retained-mixed",
            reserved_bytes=500,
            used_bytes=400,
            released_at=now,
        )
        self._add_evidence_reservation(
            key="active-mixed",
            reserved_bytes=300,
            used_bytes=100,
            released_at=None,
        )
        value = SqlErpSyncOperationsRepository(
            self.factory,
            timezone_name="America/Toronto",
        ).read(evidence_budget_bytes=1000)
        self.assertEqual(value.evidence.reserved_bytes, 300)
        self.assertEqual(value.evidence.used_bytes, 500)
        self.assertEqual(value.evidence.pressure_ratio, 0.7)

    def _principal(self, permissions: tuple[str, ...]) -> AuthPrincipal:
        return AuthPrincipal(
            local_user_id="OBS",
            issuer="urn:test",
            subject="observer",
            display_name="ERP Observer",
            email=None,
            employee_external_id=None,
            roles=(),
            permissions=permissions,
            auth_mode="test",
        )

    def test_operations_api_is_protected_and_remains_available_without_erp_source(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(
                self._principal((PERMISSION_SYNC_PROJECTS,))
            ),
            erp_sync_settings=self.settings,
        )
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/integrations/acumatica/erp-sync/operations"
            )
            sensitive = client.post(
                "/api/v1/integrations/acumatica/erp-sync/evidence/fake/approve-baseline",
                json={"reason": "should not be authorized"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["automation_enabled"])
        self.assertEqual(payload["scheduler_status"], "DISABLED")
        self.assertEqual(payload["freshness_status"], "UNKNOWN")
        self.assertFalse(payload["automatic_execution_available"])
        self.assertEqual(payload["evidence"]["status"], "UNAVAILABLE")
        self.assertIn(
            "erp_sync_643b_safety_capability_unavailable",
            payload["readiness_blockers"],
        )
        self.assertIn(
            "erp_sync_evidence_unavailable",
            payload["readiness_blockers"],
        )
        self.assertEqual(sensitive.status_code, 403, sensitive.text)

        denied_app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(
                self._principal((PERMISSION_READ,))
            ),
            erp_sync_settings=self.settings,
        )
        with TestClient(denied_app) as client:
            denied = client.get(
                "/api/v1/integrations/acumatica/erp-sync/operations"
            )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_sql_unavailable_returns_safe_503_without_fabricating_status(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(
                self._principal((PERMISSION_SYNC_PROJECTS,))
            ),
            erp_sync_settings=self.settings,
            erp_sync_automatic_safety=TestErpSyncAutomaticSafetyCapability(),
        )
        with self.factory() as session:
            before_attempts = tuple(session.scalars(select(ErpSyncAttempt)))
        with patch(
            "app.server.routes_integrations.SqlErpSyncOperationsRepository.read",
            side_effect=RuntimeError("private database failure"),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/integrations/acumatica/erp-sync/operations"
                )
        with self.factory() as session:
            after_attempts = tuple(session.scalars(select(ErpSyncAttempt)))
        self.assertEqual(
            [row.id for row in after_attempts],
            [row.id for row in before_attempts],
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "erp_sync_operations_database_unavailable",
        )
        self.assertNotIn("private database failure", response.text)

        healthy_app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(
                self._principal((PERMISSION_SYNC_PROJECTS,))
            ),
            erp_sync_settings=self.settings,
            erp_sync_automatic_safety=TestErpSyncAutomaticSafetyCapability(),
        )
        with patch(
            "app.server.routes_integrations.SqlErpSyncOperationsRepository.read",
            side_effect=RuntimeError("private database failure"),
        ):
            with TestClient(healthy_app) as client:
                self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
