from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ERP_SYNC_DAILY_POLICY = "ERP_PROJECTS_TASKS_DAILY"
ERP_SYNC_CONTROL_KEY = "ERP_PROJECTS_TASKS"


class ErpSyncScheduleConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ErpSyncSchedulerSettings:
    enabled: bool = False
    local_time: time = time(0, 0)
    timezone_name: str = "America/Toronto"
    catchup_hours: int = 4
    poll_seconds: int = 30
    retry_minutes: int = 15
    max_automatic_attempts: int = 3
    lease_seconds: int = 300
    heartbeat_seconds: int = 60
    max_attempt_seconds: int = 7200

    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ErpSyncScheduleConfigurationError(
                f"Fuseau IANA introuvable: {self.timezone_name}"
            ) from exc

    @property
    def config_fingerprint(self) -> str:
        payload = {
            "policy": ERP_SYNC_DAILY_POLICY,
            "time": self.local_time.strftime("%H:%M"),
            "timezone": self.timezone_name,
            "catchup_hours": self.catchup_hours,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ErpSyncScheduleDecision:
    business_date: date
    scheduled_at_utc: datetime
    state: str
    trigger: str | None = None


class ErpSyncAutomaticSafetyCapability(Protocol):
    def automatic_execution_available(self) -> bool: ...

    def diagnostic(self) -> str: ...


@dataclass(frozen=True, slots=True)
class UnavailableErpSyncAutomaticSafetyCapability:
    """643A production default: 643B validation/checkpoint safety is absent."""

    reason: str = "erp_sync_643b_safety_capability_unavailable"

    def automatic_execution_available(self) -> bool:
        return False

    def diagnostic(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class TestErpSyncAutomaticSafetyCapability:
    """Explicit test-only capability for scheduler mechanics."""

    def automatic_execution_available(self) -> bool:
        return True

    def diagnostic(self) -> str:
        return "test_capability_available"


def parse_scheduler_time(value: str) -> time:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ErpSyncScheduleConfigurationError(
            "RESOURCEPLANNER_ERP_SYNC_TIME doit respecter HH:MM."
        )
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ErpSyncScheduleConfigurationError(
            "RESOURCEPLANNER_ERP_SYNC_TIME doit respecter HH:MM."
        ) from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ErpSyncScheduleConfigurationError(
            "RESOURCEPLANNER_ERP_SYNC_TIME doit respecter HH:MM."
        )
    return time(hour, minute)


def validate_timezone(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ErpSyncScheduleConfigurationError(
            "RESOURCEPLANNER_ERP_SYNC_TIMEZONE est requis."
        )
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ErpSyncScheduleConfigurationError(
            f"Fuseau IANA introuvable: {normalized}"
        ) from exc
    return normalized


def _roundtrip_matches(local_naive: datetime, aware: datetime, zone: ZoneInfo) -> bool:
    roundtrip = aware.astimezone(timezone.utc).astimezone(zone)
    return roundtrip.replace(tzinfo=None) == local_naive


def resolve_scheduled_instant(
    business_date: date,
    settings: ErpSyncSchedulerSettings,
) -> datetime:
    """Resolve one business-date schedule using ADR-033 DST semantics.

    Ambiguous local time uses fold=0. A nonexistent local time advances minute by
    minute to the first local instant that survives a UTC round trip.
    """

    zone = settings.timezone()
    candidate = datetime.combine(business_date, settings.local_time)
    for _ in range(181):
        aware = candidate.replace(tzinfo=zone, fold=0)
        if _roundtrip_matches(candidate, aware, zone):
            return aware.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    raise ErpSyncScheduleConfigurationError(
        "Impossible de résoudre l'heure locale configurée dans le fuseau IANA."
    )


def scheduler_decision(
    now_utc: datetime,
    settings: ErpSyncSchedulerSettings,
) -> ErpSyncScheduleDecision:
    if now_utc.tzinfo is None:
        now = now_utc.replace(tzinfo=timezone.utc)
    else:
        now = now_utc.astimezone(timezone.utc)

    zone = settings.timezone()
    local_now = now.astimezone(zone)
    business_date = local_now.date()
    scheduled = resolve_scheduled_instant(business_date, settings)

    if not settings.enabled:
        return ErpSyncScheduleDecision(
            business_date=business_date,
            scheduled_at_utc=scheduled,
            state="DISABLED",
        )
    if now < scheduled:
        return ErpSyncScheduleDecision(
            business_date=business_date,
            scheduled_at_utc=scheduled,
            state="NOT_DUE",
        )

    catchup_deadline = scheduled + timedelta(hours=settings.catchup_hours)
    # ADR-033 bounds automatic starts to the current business date even when a
    # configured late schedule would otherwise extend the duration window.
    next_local_midnight = datetime.combine(
        business_date + timedelta(days=1),
        time(0, 0),
        tzinfo=zone,
    ).astimezone(timezone.utc)
    deadline = min(catchup_deadline, next_local_midnight)
    if now >= deadline:
        return ErpSyncScheduleDecision(
            business_date=business_date,
            scheduled_at_utc=scheduled,
            state="MISSED",
        )

    return ErpSyncScheduleDecision(
        business_date=business_date,
        scheduled_at_utc=scheduled,
        state="DUE",
        trigger="SCHEDULED" if now == scheduled else "CATCHUP",
    )
