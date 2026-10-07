from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from collections.abc import Sequence

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.query_models import MediumTermCapacityBucketReadModel
from ...domain.availability_rules import availability_hours_for_day
from ...domain.workload import LOAD_FIRM, PENDING_LOAD_ADDITIVE
from .availability_class_scope import availability_class_codes_by_rule
from .models import ResourceAvailabilityRule


UNCLASSIFIED = "Non classé"
UNASSIGNED = "Non assigné"


def _availability_record(
    rule: ResourceAvailabilityRule,
    resource_class_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "Type": rule.availability_type,
        "Actif": bool(rule.active),
        "Technicien": rule.resource_id or "",
        "ClassesRessources": resource_class_codes,
        "DateDebut": rule.start_date,
        "DateFin": rule.end_date,
        "JoursSemaine": rule.weekdays,
        "HeureDebut": rule.start_time,
        "HeureFin": rule.end_time,
    }


def _capacity_slice(
    queries: Any,
    availability_records: tuple[dict[str, Any], ...],
    *,
    start: date,
    end: date,
) -> tuple[tuple[Any, ...], float, defaultdict[str, float], defaultdict[str, set[str]]]:
    schedulable = tuple(queries.list_schedulable_resources(start=start, end=end))
    capacity_by_class: defaultdict[str, float] = defaultdict(float)
    resources_by_class: defaultdict[str, set[str]] = defaultdict(set)
    total_capacity = 0.0
    day = start
    while day <= end:
        for resource in schedulable:
            hours = availability_hours_for_day(
                availability_records,
                resource.id,
                day,
                resource_class=resource.resource_class,
            )
            total_capacity += hours
            resource_class = resource.resource_class or UNCLASSIFIED
            capacity_by_class[resource_class] += hours
            resources_by_class[resource_class].add(resource.id)
        day += timedelta(days=1)
    return schedulable, total_capacity, capacity_by_class, resources_by_class


def _hours_decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal("0.01"))


def build_workforce_weekly_capacity_details(
    queries: Any,
    session: Session,
    *,
    start: date,
    end: date,
) -> dict[
    date,
    tuple[Decimal, dict[str, Decimal], dict[str, Decimal]],
]:
    """Return one batched gross-capacity projection for class and skill analytics."""

    if end < start:
        start, end = end, start
    first = start - timedelta(days=start.weekday())
    last = end - timedelta(days=end.weekday())
    rules = session.scalars(
        select(ResourceAvailabilityRule).where(ResourceAvailabilityRule.active == true())
    ).all()
    class_codes = availability_class_codes_by_rule(
        session,
        tuple(rule.id for rule in rules),
    )
    availability_records = tuple(
        _availability_record(rule, class_codes.get(rule.id, ()))
        for rule in rules
    )

    result: dict[
        date,
        tuple[Decimal, dict[str, Decimal], dict[str, Decimal]],
    ] = {}
    cursor = first
    while cursor <= last:
        schedulable = tuple(
            queries.list_schedulable_resources(
                start=cursor,
                end=cursor + timedelta(days=6),
            )
        )
        total_capacity = 0.0
        capacity_by_class: defaultdict[str, float] = defaultdict(float)
        capacity_by_resource: defaultdict[str, float] = defaultdict(float)
        day = cursor
        while day <= cursor + timedelta(days=6):
            for resource in schedulable:
                hours = availability_hours_for_day(
                    availability_records,
                    resource.id,
                    day,
                    resource_class=resource.resource_class,
                )
                total_capacity += hours
                capacity_by_class[resource.resource_class or UNCLASSIFIED] += hours
                capacity_by_resource[resource.id] += hours
            day += timedelta(days=1)
        result[cursor] = (
            _hours_decimal(total_capacity),
            {
                resource_class: _hours_decimal(hours)
                for resource_class, hours in capacity_by_class.items()
            },
            {
                resource.id: _hours_decimal(capacity_by_resource.get(resource.id, 0.0))
                for resource in schedulable
            },
        )
        cursor += timedelta(days=7)
    return result


def build_workforce_weekly_capacity_by_class(
    queries: Any,
    session: Session,
    *,
    start: date,
    end: date,
) -> dict[date, tuple[Decimal, dict[str, Decimal]]]:
    """Return gross workforce availability by Monday week, globally and by class."""

    return {
        week_start: (total, by_class)
        for week_start, (total, by_class, _by_resource) in (
            build_workforce_weekly_capacity_details(
                queries,
                session,
                start=start,
                end=end,
            ).items()
        )
    }


def build_workforce_weekly_capacity(
    queries: Any,
    session: Session,
    *,
    start: date,
    end: date,
) -> dict[date, Decimal]:
    """Return global workforce availability by Monday week, before Shift deductions."""

    return {
        week_start: capacity
        for week_start, (capacity, _by_class) in build_workforce_weekly_capacity_by_class(
            queries,
            session,
            start=start,
            end=end,
        ).items()
    }


def medium_term_capacity_state(
    capacity: Decimal | float,
    exposure: Decimal | float,
) -> str:
    """Apply the shared medium-term available/warning/overloaded policy."""

    capacity_value = Decimal(str(capacity))
    exposure_value = Decimal(str(exposure))
    if capacity_value <= 0:
        return "overloaded" if exposure_value > 0 else "unavailable"
    ratio = exposure_value / capacity_value
    if ratio > Decimal("1"):
        return "overloaded"
    if ratio >= Decimal("0.85"):
        return "warning"
    return "available"


def _bucket(
    *,
    week_start: date,
    week_end: date,
    resource_class: str | None,
    resource_count: int,
    capacity_hours: float,
    firm_hours: float,
    current_potential_hours: float,
    submitted_hours: float,
    replacement_proposal_hours: float,
    replacement_delta_hours: float,
) -> MediumTermCapacityBucketReadModel:
    capacity = round(capacity_hours, 2)
    firm = round(firm_hours, 2)
    current_potential = round(current_potential_hours, 2)
    submitted = round(submitted_hours, 2)
    replacement = round(replacement_proposal_hours, 2)
    replacement_delta = round(replacement_delta_hours, 2)
    exposure = round(firm + current_potential + submitted, 2)
    return MediumTermCapacityBucketReadModel(
        week_start=week_start,
        week_end=week_end,
        resource_class=resource_class,
        resource_count=resource_count,
        capacity_hours=capacity,
        firm_hours=firm,
        current_potential_hours=current_potential,
        submitted_hours=submitted,
        replacement_proposal_hours=replacement,
        replacement_delta_hours=replacement_delta,
        exposure_hours=exposure,
        firm_residual_hours=round(capacity - firm, 2),
        residual_hours=round(capacity - exposure, 2),
        utilization_pct=(round(exposure / capacity * 100, 1) if capacity > 0 else None),
        state=medium_term_capacity_state(capacity, exposure),
    )


def build_medium_term_capacity_buckets(
    queries: Any,
    session: Session,
    *,
    start: date,
    end: date,
    preloaded_shifts: Sequence[Any] | None = None,
    preloaded_pending_loads: Sequence[Any] | None = None,
) -> tuple[MediumTermCapacityBucketReadModel, ...]:
    """Build weekly capacity without leaking rules into React.

    Current approved/tentative shifts and new submitted requests are additive exposure.
    A submitted modification with an existing approved plan is a replacement scenario:
    it is visible, including its delta, but never added over the current plan.
    """

    if end < start:
        start, end = end, start

    rules = session.scalars(
        select(ResourceAvailabilityRule).where(ResourceAvailabilityRule.active == true())
    ).all()
    class_codes = availability_class_codes_by_rule(
        session,
        tuple(rule.id for rule in rules),
    )
    availability_records = tuple(
        _availability_record(rule, class_codes.get(rule.id, ()))
        for rule in rules
    )
    all_resources = queries.list_resources(active_only=True)
    resource_by_id = {resource.id: resource for resource in all_resources}
    resource_by_name = {resource.name: resource for resource in all_resources}
    shifts = (
        tuple(preloaded_shifts)
        if preloaded_shifts is not None
        else queries.list_shifts(start=start, end=end)
    )

    result: list[MediumTermCapacityBucketReadModel] = []
    cursor = start
    while cursor <= end:
        week_end = min(cursor + timedelta(days=6), end)
        (
            schedulable,
            total_capacity,
            capacity_by_class,
            resources_by_class,
        ) = _capacity_slice(
            queries,
            availability_records,
            start=cursor,
            end=week_end,
        )
        schedulable_ids = {resource.id for resource in schedulable}

        firm_by_class: defaultdict[str, float] = defaultdict(float)
        potential_by_class: defaultdict[str, float] = defaultdict(float)
        total_firm = 0.0
        total_current_potential = 0.0
        for shift in shifts:
            if not (cursor <= shift.work_date <= week_end):
                continue
            resource = resource_by_id.get(shift.resource_id)
            resource_class = (
                resource.resource_class if resource and resource.resource_class else UNCLASSIFIED
            )
            if shift.load_kind == LOAD_FIRM:
                total_firm += shift.hours
                firm_by_class[resource_class] += shift.hours
            else:
                total_current_potential += shift.hours
                potential_by_class[resource_class] += shift.hours

        submitted_by_class: defaultdict[str, float] = defaultdict(float)
        replacement_by_class: defaultdict[str, float] = defaultdict(float)
        replacement_delta_by_class: defaultdict[str, float] = defaultdict(float)
        total_submitted = 0.0
        total_replacement = 0.0
        total_replacement_delta = 0.0
        pending_rows = (
            tuple(preloaded_pending_loads)
            if preloaded_pending_loads is not None and cursor == start and week_end == end
            else queries.list_pending_loads(start=cursor, end=week_end)
        )
        for pending in pending_rows:
            proposed = resource_by_name.get(pending.proposed_resource or "")
            resource_class = (
                proposed.resource_class if proposed and proposed.resource_class else UNASSIGNED
            )
            if pending.mode == PENDING_LOAD_ADDITIVE:
                total_submitted += pending.window_hours
                submitted_by_class[resource_class] += pending.window_hours
            else:
                total_replacement += pending.window_hours
                replacement_by_class[resource_class] += pending.window_hours
                if pending.delta_hours is not None:
                    total_replacement_delta += pending.delta_hours
                    replacement_delta_by_class[resource_class] += pending.delta_hours

        result.append(
            _bucket(
                week_start=cursor,
                week_end=week_end,
                resource_class=None,
                resource_count=len(schedulable_ids),
                capacity_hours=total_capacity,
                firm_hours=total_firm,
                current_potential_hours=total_current_potential,
                submitted_hours=total_submitted,
                replacement_proposal_hours=total_replacement,
                replacement_delta_hours=total_replacement_delta,
            )
        )

        classes = sorted(
            set(capacity_by_class)
            | set(firm_by_class)
            | set(potential_by_class)
            | set(submitted_by_class)
            | set(replacement_by_class),
            key=lambda value: (value == UNASSIGNED, value.casefold()),
        )
        for resource_class in classes:
            result.append(
                _bucket(
                    week_start=cursor,
                    week_end=week_end,
                    resource_class=resource_class,
                    resource_count=len(resources_by_class.get(resource_class, set())),
                    capacity_hours=capacity_by_class[resource_class],
                    firm_hours=firm_by_class[resource_class],
                    current_potential_hours=potential_by_class[resource_class],
                    submitted_hours=submitted_by_class[resource_class],
                    replacement_proposal_hours=replacement_by_class[resource_class],
                    replacement_delta_hours=replacement_delta_by_class[resource_class],
                )
            )

        cursor = week_end + timedelta(days=1)

    return tuple(result)
