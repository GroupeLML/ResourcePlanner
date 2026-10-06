from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN

from .errors import ApplicationValidationError


HOUR_QUANTUM = Decimal("0.01")

LOAD_INTERVAL_ORIGIN_MANUAL = "MANUAL"
LOAD_INTERVAL_ORIGIN_LEGACY_AUTO = "LEGACY_AUTO"
LOAD_INTERVAL_ORIGIN_LEGACY_MANUAL = "LEGACY_MANUAL"
LOAD_INTERVAL_ORIGINS = frozenset(
    {
        LOAD_INTERVAL_ORIGIN_MANUAL,
        LOAD_INTERVAL_ORIGIN_LEGACY_AUTO,
        LOAD_INTERVAL_ORIGIN_LEGACY_MANUAL,
    }
)

LOAD_DIAGNOSTIC_DATES_MISSING = "WORK_PACKAGE_LOAD_DATES_MISSING"
LOAD_DIAGNOSTIC_TOTAL_UNKNOWN = "WORK_PACKAGE_LOAD_TOTAL_UNKNOWN"
LOAD_DIAGNOSTIC_INCONSISTENT = "WORK_PACKAGE_LOAD_INCONSISTENT"
STATUS_DIAGNOSTIC_UNKNOWN_LEGACY = "WORK_PACKAGE_STATUS_UNKNOWN_LEGACY"

TERMINAL_CLOSED = "closed"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({TERMINAL_CLOSED, TERMINAL_CANCELLED})

_CLOSED_ALIASES = frozenset(
    {"closed", "completed", "complete", "termine", "terminé", "ferme", "fermé"}
)
_CANCELLED_ALIASES = frozenset(
    {"cancelled", "canceled", "annule", "annulé", "annulee", "annulée"}
)
_OPEN_ALIASES = frozenset(
    {"", "planned", "planifie", "planifié", "planifiee", "planifiée", "active", "actif", "actifs"}
)
_KNOWN_LEGACY_STATUSES = _OPEN_ALIASES | _CLOSED_ALIASES | _CANCELLED_ALIASES


def decimal_hours(value: object, *, field: str = "hours") -> Decimal:
    try:
        hours = Decimal(str(value))
    except Exception as exc:
        raise ApplicationValidationError(
            "Les heures doivent être numériques.",
            code="work_package_load_hours_invalid",
            context={"field": field, "hours": str(value)},
        ) from exc
    if not hours.is_finite() or hours < 0:
        raise ApplicationValidationError(
            "Les heures ne peuvent pas être négatives.",
            code="work_package_load_hours_invalid",
            context={"field": field, "hours": str(value)},
        )
    quantized = hours.quantize(HOUR_QUANTUM)
    if hours != quantized:
        raise ApplicationValidationError(
            "Les heures doivent respecter une précision de 0,01 h.",
            code="work_package_load_precision_invalid",
            context={"field": field, "hours": str(value)},
        )
    return quantized


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


@dataclass(frozen=True, slots=True)
class WorkPackageLoadIntervalValue:
    start_date: date
    end_date: date
    hours: Decimal
    id: str | None = None
    origin: str = LOAD_INTERVAL_ORIGIN_MANUAL


@dataclass(frozen=True, slots=True)
class WorkPackageLoadState:
    reference: str
    version: int
    start_date: date | None
    end_date: date | None
    planned_hours: Decimal | None
    legacy_status: str | None
    terminal_status: str | None
    intervals: tuple[WorkPackageLoadIntervalValue, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkPackageProjectedDay:
    day: date
    explicit_hours: Decimal
    automatic_hours: Decimal

    @property
    def hours(self) -> Decimal:
        return self.explicit_hours + self.automatic_hours


@dataclass(frozen=True, slots=True)
class WorkPackageProjectedWeek:
    week_start: date
    explicit_hours: Decimal
    automatic_hours: Decimal

    @property
    def hours(self) -> Decimal:
        return self.explicit_hours + self.automatic_hours


def normalize_legacy_status(value: object) -> str:
    return str(value or "").strip().casefold()


def terminal_status_from_legacy(value: object) -> str | None:
    normalized = normalize_legacy_status(value)
    if normalized in _CLOSED_ALIASES:
        return TERMINAL_CLOSED
    if normalized in _CANCELLED_ALIASES:
        return TERMINAL_CANCELLED
    return None


def work_package_status(
    *,
    start_date: date | None,
    terminal_status: str | None,
    legacy_status: object = None,
    today: date | None = None,
) -> tuple[str, str | None]:
    normalized_terminal = str(terminal_status or "").strip().casefold() or None
    if normalized_terminal in TERMINAL_STATUSES:
        return normalized_terminal, None

    legacy_terminal = terminal_status_from_legacy(legacy_status)
    if legacy_terminal is not None:
        return legacy_terminal, None

    normalized_legacy = normalize_legacy_status(legacy_status)
    diagnostic = (
        None
        if normalized_legacy in _KNOWN_LEGACY_STATUSES
        else STATUS_DIAGNOSTIC_UNKNOWN_LEGACY
    )
    reference_date = today or date.today()
    if start_date is not None and start_date <= reference_date:
        return "active", diagnostic
    return "planned", diagnostic


def validate_load_intervals(
    *,
    start_date: date | None,
    end_date: date | None,
    planned_hours: Decimal | None,
    intervals: tuple[WorkPackageLoadIntervalValue, ...],
) -> tuple[WorkPackageLoadIntervalValue, ...]:
    if planned_hours is None:
        if intervals:
            raise ApplicationValidationError(
                "La charge totale du WorkPackage est requise lorsqu'une répartition explicite existe.",
                code="work_package_load_planned_hours_required",
            )
        return ()

    total = decimal_hours(planned_hours, field="planned_hours")
    if intervals and (start_date is None or end_date is None):
        raise ApplicationValidationError(
            "Les dates du WorkPackage sont requises lorsqu'une répartition explicite existe.",
            code="work_package_load_dates_required",
        )
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ApplicationValidationError(
            "La date de fin du WorkPackage ne peut pas précéder sa date de début.",
            code="work_package_date_window_invalid",
        )

    seen_ids: set[str] = set()
    normalized: list[WorkPackageLoadIntervalValue] = []
    for interval in intervals:
        identifier = str(interval.id or "").strip() or None
        if identifier is not None:
            if identifier in seen_ids:
                raise ApplicationValidationError(
                    "Un intervalle de charge ne peut apparaître qu'une fois.",
                    code="work_package_load_interval_duplicate_id",
                    context={"id": identifier},
                )
            seen_ids.add(identifier)
        if interval.end_date < interval.start_date:
            raise ApplicationValidationError(
                "La fin d'un intervalle de charge ne peut pas précéder son début.",
                code="work_package_load_interval_window_invalid",
                context={
                    "id": identifier,
                    "start_date": interval.start_date.isoformat(),
                    "end_date": interval.end_date.isoformat(),
                },
            )
        if start_date is not None and interval.start_date < start_date:
            raise ApplicationValidationError(
                "Un intervalle explicite ne peut pas commencer avant le WorkPackage.",
                code="work_package_load_interval_outside_window",
                context={"id": identifier, "start_date": interval.start_date.isoformat()},
            )
        if end_date is not None and interval.end_date > end_date:
            raise ApplicationValidationError(
                "Un intervalle explicite ne peut pas finir après le WorkPackage.",
                code="work_package_load_interval_outside_window",
                context={"id": identifier, "end_date": interval.end_date.isoformat()},
            )
        origin = str(interval.origin or "").strip().upper()
        if origin not in LOAD_INTERVAL_ORIGINS:
            raise ApplicationValidationError(
                "La provenance de l'intervalle de charge est invalide.",
                code="work_package_load_interval_origin_invalid",
                context={"id": identifier, "origin": interval.origin},
            )
        normalized.append(
            WorkPackageLoadIntervalValue(
                id=identifier,
                start_date=interval.start_date,
                end_date=interval.end_date,
                hours=decimal_hours(interval.hours),
                origin=origin,
            )
        )

    normalized.sort(
        key=lambda row: (
            row.start_date,
            row.end_date,
            row.id or "",
            row.origin,
        )
    )
    explicit_total = sum((row.hours for row in normalized), Decimal("0.00"))
    if explicit_total > total:
        raise ApplicationValidationError(
            "La somme des intervalles explicites ne peut pas dépasser la charge totale du WorkPackage.",
            code="work_package_load_explicit_exceeds_planned",
            context={
                "planned_hours": str(total),
                "explicit_hours": str(explicit_total),
            },
        )
    return tuple(normalized)


def _daily_distribution(total: Decimal, start_date: date, end_date: date) -> dict[date, Decimal]:
    if end_date < start_date:
        return {}
    day_count = (end_date - start_date).days + 1
    cents = int((decimal_hours(total) / HOUR_QUANTUM).to_integral_value(rounding=ROUND_DOWN))
    base_cents, remainder = divmod(cents, day_count)
    return {
        start_date + timedelta(days=index): Decimal(
            base_cents + (1 if index < remainder else 0)
        )
        * HOUR_QUANTUM
        for index in range(day_count)
    }


def projected_daily_loads(
    state: WorkPackageLoadState,
) -> tuple[WorkPackageProjectedDay, ...]:
    """Project explicit intervals and automatic balance before any date cutoff."""
    normalized = validate_load_intervals(
        start_date=state.start_date,
        end_date=state.end_date,
        planned_hours=state.planned_hours,
        intervals=state.intervals,
    )
    if state.planned_hours is None:
        return ()
    total = decimal_hours(state.planned_hours, field="planned_hours")
    if total == Decimal("0.00") and state.start_date is None and state.end_date is None:
        return ()
    if state.start_date is None or state.end_date is None:
        raise ApplicationValidationError(
            "Les dates du WorkPackage sont requises pour projeter son solde automatique.",
            code="work_package_load_dates_required",
        )

    explicit_total = sum((row.hours for row in normalized), Decimal("0.00"))
    automatic_total = total - explicit_total

    explicit_by_day: dict[date, Decimal] = {}
    for interval in normalized:
        for day, hours in _daily_distribution(
            interval.hours,
            interval.start_date,
            interval.end_date,
        ).items():
            explicit_by_day[day] = explicit_by_day.get(day, Decimal("0.00")) + hours

    automatic_by_day = _daily_distribution(
        automatic_total,
        state.start_date,
        state.end_date,
    )
    days = sorted(set(explicit_by_day) | set(automatic_by_day))
    return tuple(
        WorkPackageProjectedDay(
            day=day,
            explicit_hours=explicit_by_day.get(day, Decimal("0.00")),
            automatic_hours=automatic_by_day.get(day, Decimal("0.00")),
        )
        for day in days
    )


def projected_weekly_loads(
    state: WorkPackageLoadState,
) -> tuple[WorkPackageProjectedWeek, ...]:
    daily = projected_daily_loads(state)
    week_starts = {monday_of(row.day) for row in daily}
    return tuple(
        WorkPackageProjectedWeek(
            week_start=week_start,
            explicit_hours=sum(
                (
                    row.explicit_hours
                    for row in daily
                    if monday_of(row.day) == week_start
                ),
                Decimal("0.00"),
            ),
            automatic_hours=sum(
                (
                    row.automatic_hours
                    for row in daily
                    if monday_of(row.day) == week_start
                ),
                Decimal("0.00"),
            ),
        )
        for week_start in sorted(week_starts)
    )


def load_diagnostic(state: WorkPackageLoadState) -> str | None:
    if state.planned_hours is None:
        return LOAD_DIAGNOSTIC_TOTAL_UNKNOWN
    try:
        total = decimal_hours(state.planned_hours, field="planned_hours")
    except ApplicationValidationError:
        return LOAD_DIAGNOSTIC_INCONSISTENT
    if (
        (state.start_date is None or state.end_date is None)
        and (total > 0 or state.intervals)
    ):
        return LOAD_DIAGNOSTIC_DATES_MISSING
    try:
        projected_weekly_loads(state)
    except ApplicationValidationError:
        return LOAD_DIAGNOSTIC_INCONSISTENT
    return None


def legacy_weekly_as_intervals(
    *,
    work_package_start: date | None,
    work_package_end: date | None,
    weekly_loads: tuple[tuple[date, Decimal], ...],
    origin: str | None,
) -> tuple[WorkPackageLoadIntervalValue, ...]:
    if not weekly_loads:
        return ()
    if work_package_start is None or work_package_end is None:
        raise ApplicationValidationError(
            "Les dates du WorkPackage sont requises pour convertir la répartition historique.",
            code="work_package_load_dates_required",
        )
    source_origin = str(origin or "").strip().upper()
    mapped_origin = (
        LOAD_INTERVAL_ORIGIN_LEGACY_AUTO
        if source_origin == "AUTO"
        else LOAD_INTERVAL_ORIGIN_LEGACY_MANUAL
    )
    result: list[WorkPackageLoadIntervalValue] = []
    for week_start, hours in weekly_loads:
        start = max(work_package_start, week_start)
        end = min(work_package_end, week_start + timedelta(days=6))
        if end < start:
            raise ApplicationValidationError(
                "Une répartition hebdomadaire historique est hors de la période du WorkPackage.",
                code="work_package_load_interval_outside_window",
                context={"week_start": week_start.isoformat()},
            )
        result.append(
            WorkPackageLoadIntervalValue(
                id=f"legacy:{week_start.isoformat()}",
                start_date=start,
                end_date=end,
                hours=decimal_hours(hours),
                origin=mapped_origin,
            )
        )
    return tuple(result)
