from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN

from .errors import ApplicationValidationError
from .results import ApplicationResult


WEEKLY_LOAD_ORIGIN_AUTO = "AUTO"
WEEKLY_LOAD_ORIGIN_MANUAL = "MANUAL"
WEEKLY_LOAD_ORIGINS = frozenset({WEEKLY_LOAD_ORIGIN_AUTO, WEEKLY_LOAD_ORIGIN_MANUAL})
HOUR_QUANTUM = Decimal("0.01")

WEEKLY_LOAD_DIAGNOSTIC_MISSING = "WEEKLY_LOAD_MISSING"
WEEKLY_LOAD_DIAGNOSTIC_DATES_MISSING = "WEEKLY_LOAD_DATES_MISSING"
WEEKLY_LOAD_DIAGNOSTIC_TOTAL_UNKNOWN = "WEEKLY_LOAD_TOTAL_UNKNOWN"
WEEKLY_LOAD_DIAGNOSTIC_INCONSISTENT = "WEEKLY_LOAD_INCONSISTENT"


def _decimal_hours(value: object) -> Decimal:
    try:
        hours = Decimal(str(value))
    except Exception as exc:
        raise ApplicationValidationError(
            "Les heures hebdomadaires doivent être numériques.",
            code="work_package_weekly_load_hours_invalid",
        ) from exc
    if not hours.is_finite() or hours < 0:
        raise ApplicationValidationError(
            "Les heures hebdomadaires ne peuvent pas être négatives.",
            code="work_package_weekly_load_hours_invalid",
            context={"hours": str(value)},
        )
    quantized = hours.quantize(HOUR_QUANTUM)
    if hours != quantized:
        raise ApplicationValidationError(
            "Les heures hebdomadaires doivent respecter une précision de 0,01 h.",
            code="work_package_weekly_load_precision_invalid",
            context={"hours": str(value)},
        )
    return quantized


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_starts(start_date: date, end_date: date) -> tuple[date, ...]:
    first = monday_of(start_date)
    last = monday_of(end_date)
    result: list[date] = []
    cursor = first
    while cursor <= last:
        result.append(cursor)
        cursor += timedelta(days=7)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class WeeklyLoadValue:
    week_start: date
    hours: Decimal


@dataclass(frozen=True, slots=True)
class WorkPackageWeeklyLoadState:
    reference: str
    version: int
    start_date: date | None
    end_date: date | None
    planned_hours: Decimal | None
    origin: str | None
    loads: tuple[WeeklyLoadValue, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkPackageWeeklyLoadReplaceCommand:
    reference: str
    expected_version: int
    origin: str
    loads: tuple[WeeklyLoadValue, ...]

    def __post_init__(self) -> None:
        if not str(self.reference or "").strip():
            raise ApplicationValidationError(
                "La référence du WorkPackage est requise.",
                code="work_package_reference_required",
            )
        if self.expected_version < 1:
            raise ApplicationValidationError(
                "La version attendue du WorkPackage doit être au moins 1.",
                code="work_package_version_invalid",
                context={"expected_version": self.expected_version},
            )
        if self.origin not in WEEKLY_LOAD_ORIGINS:
            raise ApplicationValidationError(
                "L'origine de répartition doit être AUTO ou MANUAL.",
                code="work_package_weekly_load_origin_invalid",
                context={"origin": self.origin},
            )


@dataclass(frozen=True, slots=True)
class WorkPackageWeeklyLoadProposalResult(ApplicationResult):
    reference: str
    version: int
    planned_hours: Decimal
    origin: str
    loads: tuple[WeeklyLoadValue, ...]


def validate_weekly_loads(
    *,
    start_date: date | None,
    end_date: date | None,
    planned_hours: Decimal | None,
    loads: tuple[WeeklyLoadValue, ...],
) -> tuple[WeeklyLoadValue, ...]:
    if start_date is None or end_date is None:
        raise ApplicationValidationError(
            "Les dates du WorkPackage sont requises pour valider une répartition hebdomadaire.",
            code="work_package_weekly_load_dates_required",
        )
    if end_date < start_date:
        raise ApplicationValidationError(
            "La date de fin du WorkPackage ne peut pas précéder sa date de début.",
            code="work_package_date_window_invalid",
        )
    if planned_hours is None:
        raise ApplicationValidationError(
            "La charge totale du WorkPackage est requise pour valider une répartition hebdomadaire.",
            code="work_package_weekly_load_planned_hours_required",
        )
    total_expected = _decimal_hours(planned_hours)
    if not loads:
        raise ApplicationValidationError(
            "Une répartition validée doit contenir au moins une semaine.",
            code="work_package_weekly_load_empty",
        )

    first_week = monday_of(start_date)
    last_week = monday_of(end_date)
    seen: set[date] = set()
    normalized: list[WeeklyLoadValue] = []
    for load in loads:
        if load.week_start.weekday() != 0:
            raise ApplicationValidationError(
                "week_start doit représenter le lundi de la semaine.",
                code="work_package_weekly_load_week_start_invalid",
                context={"week_start": load.week_start.isoformat()},
            )
        if load.week_start in seen:
            raise ApplicationValidationError(
                "Une semaine ne peut apparaître qu'une fois dans la répartition.",
                code="work_package_weekly_load_duplicate_week",
                context={"week_start": load.week_start.isoformat()},
            )
        if not (first_week <= load.week_start <= last_week):
            raise ApplicationValidationError(
                "La semaine de répartition doit appartenir à la fenêtre du WorkPackage.",
                code="work_package_weekly_load_outside_window",
                context={
                    "week_start": load.week_start.isoformat(),
                    "first_week": first_week.isoformat(),
                    "last_week": last_week.isoformat(),
                },
            )
        seen.add(load.week_start)
        normalized.append(
            WeeklyLoadValue(
                week_start=load.week_start,
                hours=_decimal_hours(load.hours),
            )
        )

    normalized.sort(key=lambda item: item.week_start)
    actual = sum((item.hours for item in normalized), Decimal("0.00"))
    if actual != total_expected:
        raise ApplicationValidationError(
            "La somme des heures hebdomadaires doit égaler la charge totale du WorkPackage.",
            code="work_package_weekly_load_total_mismatch",
            context={
                "planned_hours": str(total_expected),
                "weekly_load_hours": str(actual),
            },
        )
    return tuple(normalized)


def propose_weekly_loads(state: WorkPackageWeeklyLoadState) -> tuple[WeeklyLoadValue, ...]:
    if state.start_date is None or state.end_date is None:
        raise ApplicationValidationError(
            "Les dates du WorkPackage sont requises pour générer une proposition.",
            code="work_package_weekly_load_dates_required",
        )
    if state.planned_hours is None:
        raise ApplicationValidationError(
            "La charge totale du WorkPackage est requise pour générer une proposition.",
            code="work_package_weekly_load_planned_hours_required",
        )
    total = _decimal_hours(state.planned_hours)
    weeks = week_starts(state.start_date, state.end_date)
    cents = int((total / HOUR_QUANTUM).to_integral_value(rounding=ROUND_DOWN))
    base_cents, remainder = divmod(cents, len(weeks))
    proposal = tuple(
        WeeklyLoadValue(
            week_start=week,
            hours=Decimal(base_cents + (1 if index < remainder else 0)) * HOUR_QUANTUM,
        )
        for index, week in enumerate(weeks)
    )
    return validate_weekly_loads(
        start_date=state.start_date,
        end_date=state.end_date,
        planned_hours=total,
        loads=proposal,
    )


def weekly_load_diagnostic(state: WorkPackageWeeklyLoadState) -> str | None:
    if state.start_date is None or state.end_date is None:
        return WEEKLY_LOAD_DIAGNOSTIC_DATES_MISSING
    if state.planned_hours is None:
        return WEEKLY_LOAD_DIAGNOSTIC_TOTAL_UNKNOWN
    if state.origin is None and not state.loads:
        return WEEKLY_LOAD_DIAGNOSTIC_MISSING
    if state.origin not in WEEKLY_LOAD_ORIGINS:
        return WEEKLY_LOAD_DIAGNOSTIC_INCONSISTENT
    try:
        validate_weekly_loads(
            start_date=state.start_date,
            end_date=state.end_date,
            planned_hours=state.planned_hours,
            loads=state.loads,
        )
    except ApplicationValidationError:
        return WEEKLY_LOAD_DIAGNOSTIC_INCONSISTENT
    return None
