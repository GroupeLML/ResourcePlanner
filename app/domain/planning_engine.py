from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

from .allocation_rules import remaining_segment_hours, spread_hours
from .load_profiles import (
    LOAD_PROFILE_BACK_LOADED,
    LOAD_PROFILE_BELL,
    LOAD_PROFILE_FRONT_LOADED,
    LOAD_PROFILE_UNIFORM,
    normalize_load_profile,
    spread_profile_hours,
)


CapacityKey = tuple[str, date]
MISSING_ALLOCATION_TYPE = "Hors horaire requis"


@dataclass(frozen=True)
class SegmentInput:
    """Generic schedulable work segment independent from Excel and UI concepts."""

    segment_id: str
    resource_id: str
    start: date
    end: date
    hours: float
    plan_type: str = "Flexible"
    priority_rank: int = 2
    created_order: str = ""
    overtime_allowed: bool = False
    desired_active_days: int | None = None
    load_profile: str = LOAD_PROFILE_UNIFORM
    excluded_dates: frozenset[date] = frozenset()
    placement_not_before_date: date | None = None


@dataclass(frozen=True)
class LockedAllocationInput:
    segment_id: str
    resource_id: str
    day: date
    hours: float
    outside_schedule: bool = False


@dataclass(frozen=True)
class PreservedAllocationInput:
    segment_id: str
    resource_id: str
    day: date
    hours: float
    locked: bool
    outside_schedule: bool = False
    counts_as_allocated: bool = True


@dataclass(frozen=True)
class PlannedAllocation:
    segment_id: str
    resource_id: str
    day: date
    hours: float
    allocation_type: str
    locked: bool = False
    outside_schedule: bool = False
    counts_as_allocated: bool = True
    preserved: bool = False


@dataclass(frozen=True)
class ActiveDayDiagnostic:
    segment_id: str
    desired_active_days: int
    planned_active_days: int
    code: str
    message: str


@dataclass(frozen=True)
class PlanResult:
    allocations: tuple[PlannedAllocation, ...]
    segment_count: int
    locked_allocation_count: int
    requested_hours: float
    allocated_hours: float
    unallocated_hours: float
    overtime_hours: float = 0.0
    missing_allocation_count: int = 0
    active_day_diagnostics: tuple[ActiveDayDiagnostic, ...] = ()



def _segment_sort_key(segment: SegmentInput) -> tuple[object, ...]:
    return (
        segment.priority_rank,
        segment.start,
        segment.created_order,
        segment.segment_id,
    )



def _capacity_window(
    segment: SegmentInput,
    capacity_by_resource_day: Mapping[CapacityKey, float],
) -> list[tuple[date, float]]:
    placement_start = max(
        segment.start,
        segment.placement_not_before_date or segment.start,
    )
    if segment.end < placement_start:
        return []
    result: list[tuple[date, float]] = []
    cursor = placement_start
    while cursor <= segment.end:
        if cursor not in segment.excluded_dates:
            result.append(
                (
                    cursor,
                    max(float(capacity_by_resource_day.get((segment.resource_id, cursor), 0.0)), 0.0),
                )
            )
        cursor += timedelta(days=1)
    return result



def _profile_day_sort_key(
    segment: SegmentInput,
    day: date,
    room: float,
) -> tuple[object, ...]:
    profile = normalize_load_profile(segment.load_profile)
    if profile == LOAD_PROFILE_FRONT_LOADED:
        return (0, day, -room)
    if profile == LOAD_PROFILE_BACK_LOADED:
        return (0, -day.toordinal(), -room)
    if profile == LOAD_PROFILE_BELL:
        midpoint = (segment.start.toordinal() + segment.end.toordinal()) / 2.0
        return (0, abs(day.toordinal() - midpoint), day, -room)
    return (0, -room, day)



def _preferred_active_day_window(
    segment: SegmentInput,
    residual: Sequence[tuple[date, float]],
    *,
    remaining_hours: float,
    locked_days: set[date],
) -> list[tuple[date, float]]:
    """Prefer a profile-aware set of active days for flexible work.

    Locked/manual days already count toward the requested active-day target. For
    ``UNIFORM`` the historical capacity-first selection is preserved. Other profiles
    choose their preferred chronological region first, then the set is expanded only
    when those target days cannot hold the remaining work. Capacity therefore still
    wins over the target without silently dropping hours.
    """

    target = segment.desired_active_days
    if segment.plan_type == "Fixe" or target is None or target <= 0 or not residual:
        return list(residual)

    by_day = {day: max(float(room), 0.0) for day, room in residual if room > 0}
    selected_days = {day for day in locked_days if day in by_day}
    additional_target = max(int(target) - len(locked_days), 0)
    candidates = sorted(
        ((day, room) for day, room in by_day.items() if day not in selected_days),
        key=lambda item: _profile_day_sort_key(segment, item[0], item[1]),
    )

    for day, _room in candidates[:additional_target]:
        selected_days.add(day)

    selected_capacity = sum(by_day.get(day, 0.0) for day in selected_days)
    if selected_capacity + 0.001 < float(remaining_hours):
        for day, room in candidates[additional_target:]:
            if day in selected_days:
                continue
            selected_days.add(day)
            selected_capacity += room
            if selected_capacity + 0.001 >= float(remaining_hours):
                break

    return [(day, by_day[day]) for day in sorted(selected_days) if by_day.get(day, 0.0) > 0]



def _outside_schedule_slots(
    segment: SegmentInput,
    hours: float,
    capacity_by_resource_day: Mapping[CapacityKey, float],
    eligible_by_resource_day: Mapping[CapacityKey, bool] | None,
    *,
    daily_limit: float,
) -> list[tuple[date, float]]:
    """Mirror the refined V1.5 fallback slots without embedding Excel concepts.

    Days with no standard capacity are preferred before standard-capacity days. Within
    each capacity class, the selected load profile determines chronological preference.
    The adapter can mark individual days as ineligible (for example vacation); when no
    eligibility map is supplied every day in the segment window is considered eligible.
    """
    if hours <= 0 or segment.end < segment.start:
        return []

    candidates: list[tuple[int, date, float]] = []
    cursor = segment.start
    while cursor <= segment.end:
        if cursor in segment.excluded_dates:
            cursor += timedelta(days=1)
            continue
        key = (segment.resource_id, cursor)
        eligible = (
            bool(eligible_by_resource_day.get(key, False))
            if eligible_by_resource_day is not None
            else True
        )
        if eligible:
            standard_capacity = max(float(capacity_by_resource_day.get(key, 0.0)), 0.0)
            priority = 0 if standard_capacity <= 0 else 1
            candidates.append((priority, cursor, standard_capacity))
        cursor += timedelta(days=1)

    candidates.sort(
        key=lambda item: (
            item[0],
            *_profile_day_sort_key(segment, item[1], item[2]),
        )
    )
    remaining = float(hours)
    result: list[tuple[date, float]] = []
    for _priority, day, _capacity in candidates:
        if remaining <= 0.001:
            break
        amount = min(float(daily_limit), remaining)
        if amount > 0:
            result.append((day, round(amount, 4)))
            remaining -= amount
    return result



def _active_day_diagnostics(
    segments: Sequence[SegmentInput],
    allocations: Sequence[PlannedAllocation],
) -> tuple[ActiveDayDiagnostic, ...]:
    diagnostics: list[ActiveDayDiagnostic] = []
    for segment in segments:
        target = segment.desired_active_days
        if segment.plan_type == "Fixe" or target is None or target <= 0:
            continue
        rows = [
            row
            for row in allocations
            if row.segment_id == segment.segment_id
            and row.counts_as_allocated
            and float(row.hours) > 0
        ]
        planned_days = len({row.day for row in rows})
        if planned_days == target:
            continue
        locked_days = len({row.day for row in rows if row.locked})
        if locked_days > target:
            code = "LOCKED_DAYS_EXCEED_TARGET"
            message = (
                f"{locked_days} jour(s) verrouillé(s) dépassent la cible de {target} jour(s); "
                "les décisions manuelles sont conservées."
            )
        elif planned_days > target:
            code = "CAPACITY_REQUIRES_MORE_DAYS"
            message = (
                f"La capacité disponible exige {planned_days} jour(s) actifs au lieu de la cible de {target}."
            )
        else:
            code = "ACTIVE_DAY_TARGET_UNDERFILLED"
            message = (
                f"Le plan utilise {planned_days} jour(s) actifs sur une cible de {target}; "
                "la capacité ou les heures verrouillées ne permettent pas de matérialiser davantage de jours utiles."
            )
        diagnostics.append(
            ActiveDayDiagnostic(
                segment_id=segment.segment_id,
                desired_active_days=int(target),
                planned_active_days=planned_days,
                code=code,
                message=message,
            )
        )
    return tuple(diagnostics)



def build_allocation_plan(
    segments: Sequence[SegmentInput],
    locked_allocations: Sequence[LockedAllocationInput],
    capacity_by_resource_day: Mapping[CapacityKey, float],
    *,
    preserved_allocations: Sequence[PreservedAllocationInput] = (),
    preserve_locked_segment_ids: Sequence[str] | set[str] | frozenset[str] | None = None,
    outside_schedule_eligible_by_resource_day: Mapping[CapacityKey, bool] | None = None,
    outside_schedule_daily_limit: float = 8.0,
    missing_allocation_type: str = MISSING_ALLOCATION_TYPE,
) -> PlanResult:
    """Build the current refined allocation policy without Excel or NiceGUI.

    Locked/manual work is preserved first, then fixed work, then flexible work. For a
    flexible segment with ``desired_active_days``, the day count is a distribution
    target only. The load profile then shapes the residual automatic work inside the
    selected days. ``UNIFORM`` intentionally keeps the historical proportional spread.
    Capacity and explicit manual decisions always take precedence over profile shape.
    """
    active_segments = [segment for segment in segments if float(segment.hours) > 0]
    segment_map = {segment.segment_id: segment for segment in active_segments}
    preservable_segment_ids = (
        set(segment_map)
        if preserve_locked_segment_ids is None
        else {str(value) for value in preserve_locked_segment_ids if str(value)}
    )

    preserved: list[PlannedAllocation] = []
    consumed_by_segment: dict[str, float] = {}
    preserved_days_by_segment: dict[str, set[date]] = {}
    normal_used: dict[CapacityKey, float] = {}

    def register_preserved(
        *,
        segment_id: str,
        resource_id: str,
        day: date,
        hours: float,
        locked: bool,
        outside_schedule: bool,
        counts_as_allocated: bool = True,
    ) -> None:
        if segment_id not in preservable_segment_ids or hours <= 0:
            return
        preserved.append(
            PlannedAllocation(
                segment_id=segment_id,
                resource_id=resource_id,
                day=day,
                hours=round(hours, 4),
                allocation_type="Locked" if locked else "Preserved",
                locked=locked,
                outside_schedule=outside_schedule,
                counts_as_allocated=counts_as_allocated,
                preserved=True,
            )
        )
        consumed_by_segment[segment_id] = consumed_by_segment.get(segment_id, 0.0) + hours
        preserved_days_by_segment.setdefault(segment_id, set()).add(day)
        if counts_as_allocated:
            key = (resource_id, day)
            normal_used[key] = normal_used.get(key, 0.0) + hours

    for allocation in locked_allocations:
        register_preserved(
            segment_id=allocation.segment_id,
            resource_id=allocation.resource_id,
            day=allocation.day,
            hours=float(allocation.hours),
            locked=True,
            outside_schedule=allocation.outside_schedule,
        )
    for allocation in preserved_allocations:
        register_preserved(
            segment_id=allocation.segment_id,
            resource_id=allocation.resource_id,
            day=allocation.day,
            hours=float(allocation.hours),
            locked=bool(allocation.locked),
            outside_schedule=allocation.outside_schedule,
            counts_as_allocated=bool(allocation.counts_as_allocated),
        )

    fixed_segments = sorted(
        [segment for segment in active_segments if segment.plan_type == "Fixe"],
        key=_segment_sort_key,
    )
    flexible_segments = sorted(
        [segment for segment in active_segments if segment.plan_type != "Fixe"],
        key=_segment_sort_key,
    )

    allocations: list[PlannedAllocation] = list(preserved)
    missing_total = 0.0
    overtime_total = sum(
        item.hours
        for item in preserved
        if item.counts_as_allocated
        and item.outside_schedule
        and item.segment_id in segment_map
    )
    missing_count = sum(1 for item in preserved if not item.counts_as_allocated)

    def place_segment(segment: SegmentInput, allocation_type: str) -> None:
        nonlocal missing_total, overtime_total, missing_count
        remaining = remaining_segment_hours(
            segment.hours,
            consumed_by_segment.get(segment.segment_id, 0.0),
        )
        if remaining <= 0 or segment.end < segment.start:
            return

        profile = normalize_load_profile(segment.load_profile)
        window = _capacity_window(segment, capacity_by_resource_day)
        residual: list[tuple[date, float]] = []
        for day, raw_capacity in window:
            room = max(
                raw_capacity - normal_used.get((segment.resource_id, day), 0.0),
                0.0,
            )
            if room > 0:
                residual.append((day, room))

        preferred = _preferred_active_day_window(
            segment,
            residual,
            remaining_hours=remaining,
            locked_days=preserved_days_by_segment.get(segment.segment_id, set()),
        )
        spread = (
            spread_hours(remaining, preferred)
            if profile == LOAD_PROFILE_UNIFORM
            else spread_profile_hours(remaining, preferred, profile)
        )
        for day, hours in spread.items():
            key = (segment.resource_id, day)
            normal_used[key] = normal_used.get(key, 0.0) + hours
            allocations.append(
                PlannedAllocation(
                    segment_id=segment.segment_id,
                    resource_id=segment.resource_id,
                    day=day,
                    hours=round(hours, 4),
                    allocation_type=allocation_type,
                )
            )

        missing = max(remaining - sum(spread.values()), 0.0)
        if missing <= 0.001:
            return

        slots = _outside_schedule_slots(
            segment,
            missing,
            capacity_by_resource_day,
            outside_schedule_eligible_by_resource_day,
            daily_limit=outside_schedule_daily_limit,
        )
        placed_outside = 0.0
        for day, hours in slots:
            placed_outside += hours
            if segment.overtime_allowed:
                overtime_total += hours
                allocations.append(
                    PlannedAllocation(
                        segment_id=segment.segment_id,
                        resource_id=segment.resource_id,
                        day=day,
                        hours=round(hours, 4),
                        allocation_type=allocation_type,
                        outside_schedule=True,
                    )
                )
            else:
                missing_count += 1
                allocations.append(
                    PlannedAllocation(
                        segment_id=segment.segment_id,
                        resource_id=segment.resource_id,
                        day=day,
                        hours=round(hours, 4),
                        allocation_type=missing_allocation_type,
                        counts_as_allocated=False,
                    )
                )

        residual_missing = max(missing - placed_outside, 0.0)
        if segment.overtime_allowed:
            missing_total += residual_missing
        else:
            missing_total += missing

    for segment in fixed_segments:
        place_segment(segment, "Fixe")
    for segment in flexible_segments:
        place_segment(segment, "Flexible")

    allocations.sort(
        key=lambda item: (
            item.resource_id,
            item.day,
            0 if item.locked else 1,
            item.allocation_type,
            item.segment_id,
        )
    )
    requested = sum(float(segment.hours) for segment in active_segments)
    allocated = sum(
        float(item.hours)
        for item in allocations
        if item.counts_as_allocated and item.segment_id in segment_map
    )
    diagnostics = _active_day_diagnostics(active_segments, allocations)
    return PlanResult(
        allocations=tuple(allocations),
        segment_count=len(active_segments),
        locked_allocation_count=sum(1 for item in preserved if item.locked),
        requested_hours=round(requested, 4),
        allocated_hours=round(allocated, 4),
        unallocated_hours=round(max(requested - allocated, missing_total), 4),
        overtime_hours=round(overtime_total, 4),
        missing_allocation_count=missing_count,
        active_day_diagnostics=diagnostics,
    )
