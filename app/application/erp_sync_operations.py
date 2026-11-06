from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from .erp_sync_scheduler import (
    ERP_SYNC_PROTOCOL_VERSION,
    ErpSyncAutomaticSafetyCapability,
    ErpSyncSchedulerSettings,
    resolve_scheduled_instant,
    scheduler_decision,
)


@dataclass(frozen=True, slots=True)
class ErpSyncAttemptOperations:
    id: str
    occurrence_id: str
    status: str
    phase: str
    trigger_kind: str
    generation: int
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    duration_ms: int | None
    projects_received: int
    projects_created: int
    projects_updated: int
    projects_unchanged: int
    task_projects_total: int | None
    task_projects_synchronized: int | None
    task_projects_rejected: int | None
    task_rows_received: int | None
    tasks_received: int | None
    tasks_created: int | None
    tasks_updated: int | None
    tasks_unchanged: int | None
    tasks_deactivated: int | None
    tasks_rejected: int | None
    error_code: str | None
    diagnostic: str | None


@dataclass(frozen=True, slots=True)
class ErpSyncProjectOperations:
    project_number: str
    result_status: str | None
    quality_state: str
    source_observed_at: datetime | None
    validated_at: datetime | None
    reason_code: str | None
    error_code: str | None
    producer_attempt_id: str | None
    producer_generation: int | None


@dataclass(frozen=True, slots=True)
class ErpSyncRecoveryOperations:
    state: str
    attempt_id: str | None
    reason_code: str | None
    pinned: bool


@dataclass(frozen=True, slots=True)
class ErpSyncEvidenceOperations:
    status: str
    manifests_total: int
    source_manifests: int
    checkpoint_manifests: int
    manifests_ready: int
    manifests_expired: int
    manifests_pinned: int
    active_reservations: int
    reserved_bytes: int
    used_bytes: int
    budget_bytes: int | None
    pressure_ratio: float | None


@dataclass(frozen=True, slots=True)
class ErpSyncCompatibilityOperations:
    status: str
    protocol_compatible: bool | None
    configuration_compatible: bool | None


@dataclass(frozen=True, slots=True)
class ErpSyncOperationsData:
    now_utc: datetime
    control_generation: int
    owner_attempt_id: str | None
    lease_expires_at: datetime | None
    owner_deadline_at: datetime | None
    control_heartbeat_at: datetime | None
    scheduler_observed_at: datetime | None
    control_updated_at: datetime | None
    last_evaluated_business_date: date | None
    control_protocol_version: int
    control_config_fingerprint: str | None
    current_daily_occurrence_status: str | None
    attempts: tuple[ErpSyncAttemptOperations, ...]
    last_complete_success: ErpSyncAttemptOperations | None
    projects: tuple[ErpSyncProjectOperations, ...]
    recovery: ErpSyncRecoveryOperations
    evidence: ErpSyncEvidenceOperations


class ErpSyncOperationsReader(Protocol):
    def read(self, *, evidence_budget_bytes: int | None) -> ErpSyncOperationsData: ...


@dataclass(frozen=True, slots=True)
class ErpSyncOperationsSnapshot:
    automation_enabled: bool
    automatic_execution_available: bool
    scheduler_status: str
    scheduler_observation_status: str
    scheduler_observed_at: datetime | None
    scheduler_observation_age_seconds: int | None
    next_scheduled_run: datetime | None
    business_timezone: str
    current_business_date: date
    last_attempt: ErpSyncAttemptOperations | None
    last_complete_success: ErpSyncAttemptOperations | None
    last_partial_or_failed_attempt: ErpSyncAttemptOperations | None
    current_phase: str | None
    current_attempt_id: str | None
    current_generation: int
    current_owner_attempt_id: str | None
    lease_expires_at: datetime | None
    owner_deadline_at: datetime | None
    recovery_state: ErpSyncRecoveryOperations
    freshness_status: str
    freshness_age_seconds: int | None
    consecutive_failures: int
    repeated_failure: bool
    projects: tuple[ErpSyncProjectOperations, ...]
    evidence: ErpSyncEvidenceOperations
    compatibility: ErpSyncCompatibilityOperations
    readiness_blockers: tuple[str, ...]
    safe_to_operate: bool


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ErpSyncOperationsService:
    def __init__(
        self,
        reader: ErpSyncOperationsReader,
        *,
        settings: ErpSyncSchedulerSettings,
        automatic_safety: ErpSyncAutomaticSafetyCapability,
        freshness_sla_hours: int = 30,
    ) -> None:
        self._reader = reader
        self._settings = settings
        self._automatic_safety = automatic_safety
        self._freshness_sla_hours = int(freshness_sla_hours)

    def snapshot(
        self,
        *,
        evidence_budget_bytes: int | None = None,
    ) -> ErpSyncOperationsSnapshot:
        data = self._reader.read(evidence_budget_bytes=evidence_budget_bytes)
        now = data.now_utc
        zone = self._settings.timezone()
        business_date = now.astimezone(zone).date()
        decision = scheduler_decision(now, self._settings)

        current_occurrence_exists = data.current_daily_occurrence_status is not None
        if not self._settings.enabled:
            next_run = None
        elif decision.state == "NOT_DUE":
            next_run = decision.scheduled_at_utc
        elif decision.state == "DUE" and not current_occurrence_exists:
            next_run = decision.scheduled_at_utc
        else:
            next_run = resolve_scheduled_instant(
                business_date + timedelta(days=1),
                self._settings,
            )

        current_now = _utc(now)
        assert current_now is not None
        observed_at: datetime | None = None
        if not self._settings.enabled:
            observation_status = "DISABLED"
        else:
            observed_at = _utc(data.scheduler_observed_at)
            if observed_at is None:
                observation_status = "MISSING"
            elif (
                current_now - observed_at
            ).total_seconds() > max(self._settings.heartbeat_seconds * 2, 1):
                observation_status = "STALE"
            else:
                observation_status = "OBSERVED"
        observation_age_seconds = (
            max(0, int((current_now - observed_at).total_seconds()))
            if observed_at is not None
            else None
        )

        owner_heartbeat_stale = False
        if data.owner_attempt_id:
            owner_heartbeat = _utc(data.control_heartbeat_at)
            owner_lease = _utc(data.lease_expires_at)
            owner_heartbeat_stale = (
                owner_heartbeat is None
                or (current_now - owner_heartbeat).total_seconds()
                > max(self._settings.heartbeat_seconds * 2, 1)
                or owner_lease is None
                or owner_lease <= current_now
            )

        attempts = data.attempts
        last_attempt = attempts[0] if attempts else None
        last_partial_or_failed = next(
            (
                row
                for row in attempts
                if row.status in {"SUCCEEDED_WITH_ERRORS", "FAILED", "INTERRUPTED"}
            ),
            None,
        )

        failures = 0
        for row in attempts:
            if row.status in {"FAILED", "INTERRUPTED", "SUCCEEDED_WITH_ERRORS"}:
                failures += 1
                continue
            if row.status == "SUCCEEDED":
                break
            if row.status in {"RUNNING", "PENDING"}:
                continue
            break

        success = data.last_complete_success
        if success is None or success.finished_at is None:
            freshness_status = "UNKNOWN"
            freshness_age_seconds = None
        else:
            success_at = _utc(success.finished_at)
            current_at = _utc(now)
            assert success_at is not None and current_at is not None
            age = max(0, int((current_at - success_at).total_seconds()))
            freshness_age_seconds = age
            freshness_status = (
                "STALE"
                if age > self._freshness_sla_hours * 3600
                else "FRESH"
            )

        quality_states = {row.quality_state for row in data.projects}
        has_quarantine = "QUARANTINED" in quality_states
        has_project_stale = "STALE" in quality_states
        has_required_recovery = data.recovery.state == "REQUIRED"
        automatic_available = bool(
            self._automatic_safety.automatic_execution_available()
        )

        protocol_ok = data.control_protocol_version == ERP_SYNC_PROTOCOL_VERSION
        config_known = bool(data.control_config_fingerprint)
        config_ok = (
            data.control_config_fingerprint == self._settings.config_fingerprint
            if config_known
            else None
        )
        compatibility_status = (
            "INCOMPATIBLE"
            if not protocol_ok or config_ok is False
            else "COMPATIBLE"
            if config_ok is True
            else "UNKNOWN"
        )
        compatibility = ErpSyncCompatibilityOperations(
            status=compatibility_status,
            protocol_compatible=protocol_ok,
            configuration_compatible=config_ok,
        )

        blockers: list[str] = []
        if not automatic_available:
            blockers.append(self._automatic_safety.diagnostic())
        if compatibility_status == "INCOMPATIBLE":
            blockers.append("erp_sync_runtime_compatibility_incompatible")
        if has_required_recovery:
            blockers.append(data.recovery.reason_code or "erp_sync_recovery_required")
        if data.evidence.status in {"UNAVAILABLE", "PRESSURE"}:
            blockers.append("erp_sync_evidence_" + data.evidence.status.casefold())
        blockers = list(dict.fromkeys(blockers))

        current = next(
            (row for row in attempts if row.id == data.owner_attempt_id),
            None,
        )
        if current is None:
            current = next(
                (
                    row
                    for row in attempts
                    if row.status in {"RUNNING", "PENDING"}
                ),
                None,
            )
        if not self._settings.enabled:
            scheduler_status = "DISABLED"
        elif has_required_recovery or has_quarantine:
            scheduler_status = "QUARANTINED"
        elif observation_status == "STALE" or owner_heartbeat_stale:
            scheduler_status = "STALE"
        elif current is not None and current.status in {"RUNNING", "PENDING"}:
            scheduler_status = "IN_PROGRESS"
        elif observation_status == "MISSING" or (
            decision.state == "MISSED"
            and not current_occurrence_exists
        ) or data.current_daily_occurrence_status == "MISSED":
            scheduler_status = "MISSED"
        elif data.current_daily_occurrence_status == "DISABLED":
            scheduler_status = "BLOCKED"
        elif last_attempt is not None and last_attempt.status in {"FAILED", "INTERRUPTED"}:
            scheduler_status = "FAILED"
        elif (
            last_attempt is not None
            and last_attempt.status == "SUCCEEDED_WITH_ERRORS"
        ) or has_project_stale:
            scheduler_status = "PARTIAL"
        elif freshness_status == "STALE":
            scheduler_status = "STALE"
        elif not automatic_available or compatibility_status == "INCOMPATIBLE":
            scheduler_status = "BLOCKED"
        else:
            scheduler_status = "HEALTHY"

        if has_project_stale and freshness_status == "FRESH":
            freshness_status = "PARTIAL"
        if has_quarantine:
            freshness_status = "PARTIAL"

        return ErpSyncOperationsSnapshot(
            automation_enabled=self._settings.enabled,
            automatic_execution_available=automatic_available,
            scheduler_status=scheduler_status,
            scheduler_observation_status=observation_status,
            scheduler_observed_at=observed_at,
            scheduler_observation_age_seconds=observation_age_seconds,
            next_scheduled_run=next_run,
            business_timezone=self._settings.timezone_name,
            current_business_date=business_date,
            last_attempt=last_attempt,
            last_complete_success=success,
            last_partial_or_failed_attempt=last_partial_or_failed,
            current_phase=current.phase if current is not None else None,
            current_attempt_id=current.id if current is not None else None,
            current_generation=data.control_generation,
            current_owner_attempt_id=data.owner_attempt_id,
            lease_expires_at=data.lease_expires_at,
            owner_deadline_at=data.owner_deadline_at,
            recovery_state=data.recovery,
            freshness_status=freshness_status,
            freshness_age_seconds=freshness_age_seconds,
            consecutive_failures=failures,
            repeated_failure=failures >= 3,
            projects=data.projects,
            evidence=data.evidence,
            compatibility=compatibility,
            readiness_blockers=tuple(blockers),
            safe_to_operate=(
                automatic_available
                and not has_required_recovery
                and not has_quarantine
                and compatibility_status != "INCOMPATIBLE"
                and data.evidence.status not in {"UNAVAILABLE", "PRESSURE"}
            ),
        )
