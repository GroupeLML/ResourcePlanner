from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Iterable


WEEKDAY_LABELS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def is_active(value: Any) -> bool:
    return str(value or "Oui").strip().lower() not in {"non", "no", "false", "0", "inactif"}


def _date_from_value(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _record_applies(record: dict[str, Any], day: date) -> bool:
    start = _date_from_value(record.get("DateDebut"))
    end = _date_from_value(record.get("DateFin"))
    return not ((start and day < start) or (end and day > end))


def _record_overlaps(
    record: dict[str, Any],
    window_start: date,
    window_end: date,
) -> bool:
    start = _date_from_value(record.get("DateDebut"))
    end = _date_from_value(record.get("DateFin"))
    return not ((end and end < window_start) or (start and start > window_end))


def _weekday_matches(record: dict[str, Any], day: date) -> bool:
    raw = str(record.get("JoursSemaine") or "").strip()
    if not raw:
        return True
    tokens = {part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()}
    return WEEKDAY_LABELS[day.weekday()].lower() in tokens


def _record_resource_class_codes(record: dict[str, Any]) -> tuple[str, ...]:
    raw = record.get("ClassesRessources")
    if raw in (None, ""):
        return ()
    if isinstance(raw, str):
        values = raw.replace(";", ",").split(",")
    else:
        try:
            values = tuple(raw)
        except TypeError:
            return ()
    return tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _effective_resource_class(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    explicit: str | None,
) -> str | None:
    if explicit is not None:
        return str(explicit).strip() or None
    for row in records:
        if str(row.get("Technicien") or "").strip() != resource_id:
            continue
        if "ClasseRessource" not in row:
            continue
        return str(row.get("ClasseRessource") or "").strip() or None
    return None


def _fraction_to_hours(value: float) -> float:
    minutes = int(round((float(value) % 1.0) * 24 * 60)) % (24 * 60)
    return minutes / 60.0


def _time_hours(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.hour + value.minute / 60.0
    if isinstance(value, dt_time):
        return value.hour + value.minute / 60.0
    if isinstance(value, (int, float)):
        return _fraction_to_hours(float(value))
    text = str(value).strip()
    try:
        numeric = float(text.replace(",", "."))
    except ValueError:
        numeric = None
    if numeric is not None and 0 <= numeric < 1:
        return _fraction_to_hours(numeric)
    if ":" not in text:
        return None
    try:
        hour, minute = text.split(":", 1)
        return int(hour) + int(minute[:2]) / 60.0
    except (TypeError, ValueError):
        return None


def has_standard_schedule(records: Iterable[dict[str, Any]], resource_id: str) -> bool:
    resource_id = str(resource_id or "").strip()
    return any(
        str(row.get("Type") or "").strip() == "Horaire standard"
        and is_active(row.get("Actif"))
        and str(row.get("Technicien") or "").strip() == resource_id
        for row in records
    )


def has_standard_schedule_in_window(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    window_start: date,
    window_end: date,
) -> bool:
    """Return whether a resource has an active employment schedule in the window.

    This deliberately checks the standard-schedule date envelope, not vacation/holiday
    exceptions and not weekday capacity. A resource whose standard schedule ended before
    the displayed week is therefore hidden, while history and past shifts remain intact.
    """

    resource_id = str(resource_id or "").strip()
    if not resource_id:
        return False
    if window_end < window_start:
        window_start, window_end = window_end, window_start
    return any(
        str(row.get("Type") or "").strip() == "Horaire standard"
        and is_active(row.get("Actif"))
        and str(row.get("Technicien") or "").strip() == resource_id
        and _record_overlaps(row, window_start, window_end)
        for row in records
    )


@dataclass(frozen=True, slots=True)
class AvailabilityDayState:
    available: bool
    hours: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OutsideStandardHoursDecision:
    state: AvailabilityDayState
    allowed: bool
    override_eligible: bool
    override_required: bool
    override_applied: bool


_OUTSIDE_STANDARD_HOURS_OVERRIDABLE_REASONS = frozenset(
    {"Hors horaire standard", "Jour férié"}
)


def availability_state_for_day(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    day: date,
    *,
    resource_class: str | None = None,
) -> AvailabilityDayState:
    """Explain a resource's standard capacity for one day from the canonical rules."""

    rows = [row for row in records if is_active(row.get("Actif"))]
    resource_id = str(resource_id or "").strip()
    effective_resource_class = _effective_resource_class(rows, resource_id, resource_class)
    if not has_standard_schedule_in_window(rows, resource_id, day, day):
        return AvailabilityDayState(False, 0.0, "Aucun horaire standard")

    standards_in_window = [
        row
        for row in rows
        if str(row.get("Type") or "").strip() == "Horaire standard"
        and str(row.get("Technicien") or "").strip() == resource_id
        and _record_applies(row, day)
    ]
    valid_standards: list[tuple[dict[str, Any], float]] = []
    for row in standards_in_window:
        start = _time_hours(row.get("HeureDebut"))
        end = _time_hours(row.get("HeureFin"))
        if start is None or end is None:
            continue
        if end < start:
            end += 24.0
        hours = max(end - start, 0.0)
        if hours > 0:
            valid_standards.append((row, hours))
    if not valid_standards:
        return AvailabilityDayState(False, 0.0, "Horaire standard invalide")

    for row in rows:
        if str(row.get("Type") or "").strip() != "Vacances":
            continue
        if str(row.get("Technicien") or "").strip() == resource_id and _record_applies(row, day):
            return AvailabilityDayState(False, 0.0, "Vacances")

    for row in rows:
        if str(row.get("Type") or "").strip() != "Jour férié":
            continue
        target = str(row.get("Technicien") or "").strip()
        if target and target != resource_id:
            continue
        class_codes = _record_resource_class_codes(row)
        if not target and class_codes and effective_resource_class not in class_codes:
            continue
        if _record_applies(row, day):
            return AvailabilityDayState(False, 0.0, "Jour férié")

    matching_standards = [
        (row, hours)
        for row, hours in valid_standards
        if _weekday_matches(row, day)
    ]
    if not matching_standards:
        return AvailabilityDayState(False, 0.0, "Hors horaire standard")

    return AvailabilityDayState(True, matching_standards[0][1], None)


def outside_standard_hours_decision_for_day(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    day: date,
    *,
    outside_standard_hours: bool,
    resource_class: str | None = None,
) -> OutsideStandardHoursDecision:
    """Apply the canonical #536 override policy while preserving the root cause."""

    state = availability_state_for_day(
        records,
        resource_id,
        day,
        resource_class=resource_class,
    )
    if state.available:
        return OutsideStandardHoursDecision(
            state=state,
            allowed=True,
            override_eligible=False,
            override_required=False,
            override_applied=False,
        )

    override_eligible = state.reason in _OUTSIDE_STANDARD_HOURS_OVERRIDABLE_REASONS
    override_applied = override_eligible and bool(outside_standard_hours)
    return OutsideStandardHoursDecision(
        state=state,
        allowed=override_applied,
        override_eligible=override_eligible,
        override_required=override_eligible and not bool(outside_standard_hours),
        override_applied=override_applied,
    )


def availability_hours_for_day(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    day: date,
    *,
    resource_class: str | None = None,
) -> float:
    """Return historical schedulable hours using the canonical classified state."""
    return availability_state_for_day(
        records,
        resource_id,
        day,
        resource_class=resource_class,
    ).hours


def outside_schedule_eligible_for_day(
    records: Iterable[dict[str, Any]],
    resource_id: str,
    day: date,
) -> bool:
    """Return whether refined V1.5 may suggest outside-schedule work that day.

    The production fallback excludes vacation, but allows weekends, holidays and
    evenings on normal workdays. The resource must still be inside the date envelope
    of an explicit active standard schedule on the requested day.
    """
    rows = [row for row in records if is_active(row.get("Actif"))]
    resource_id = str(resource_id or "").strip()
    if not has_standard_schedule_in_window(rows, resource_id, day, day):
        return False
    for row in rows:
        if str(row.get("Type") or "").strip() != "Vacances":
            continue
        if str(row.get("Technicien") or "").strip() != resource_id:
            continue
        if _record_applies(row, day):
            return False
    return True
