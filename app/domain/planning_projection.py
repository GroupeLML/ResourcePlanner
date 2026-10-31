from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .availability_rules import (
    availability_hours_for_day,
    has_standard_schedule,
    outside_schedule_eligible_for_day,
)
from .load_profiles import normalize_load_profile
from .plan_comparison import AllocationProjection
from .planning_engine import (
    MISSING_ALLOCATION_TYPE,
    CapacityKey,
    LockedAllocationInput,
    SegmentInput,
)
from .planning_snapshot import PlanningSnapshot
from .value_coercion import date_from_value


PRIORITY_ORDER = {"Urgent": 0, "Élevée": 1, "Normale": 2, "Basse": 3}
PLAN_TYPES = {"Flexible", "Fixe"}
TRUE_VALUES = {"oui", "yes", "true", "1", "x", "verrouille", "verrouillée"}
SEGMENT_OVERTIME_FIELD = "HorsHoraireAutorise"
SEGMENT_ACTIVE_DAYS_FIELD = "JoursActifsCibles"
SEGMENT_LOAD_PROFILE_FIELD = "ProfilCharge"


@dataclass(frozen=True, slots=True)
class PlanningCalculationSnapshot:
    """Typed, storage-neutral inputs for one pure planning calculation.

    The source ``PlanningSnapshot`` may still keep compatibility mappings for the V1
    persistence adapter, but calculation code no longer needs to interpret those
    physical records once this projection has been built.
    """

    segments: tuple[SegmentInput, ...]
    locked_allocations: tuple[LockedAllocationInput, ...]
    capacity_by_resource_day: Mapping[CapacityKey, float]
    outside_schedule_eligible_by_resource_day: Mapping[CapacityKey, bool]
    persisted_allocations: tuple[AllocationProjection, ...]
    preserved_segment_ids: frozenset[str]
    unsupported_segment_ids: tuple[str, ...]


def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _optional_positive_int(value: Any) -> int | None:
    numeric = _number(value)
    if numeric <= 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_VALUES


def _demand_lookup(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("NoDemande") or ""): row
        for row in rows
        if row.get("NoDemande")
    }


def _priority_rank(row: dict[str, Any], demands: dict[str, dict[str, Any]]) -> int:
    priority = str(row.get("Priorite") or "").strip()
    if not priority:
        demand = demands.get(str(row.get("NoDemande") or ""), {})
        priority = str(demand.get("Priorite") or "Normale")
    return PRIORITY_ORDER.get(priority, 2)


def _plan_type(row: dict[str, Any]) -> str:
    value = str(row.get("TypePlanification") or "Flexible").strip()
    return value if value in PLAN_TYPES else "Flexible"


def _schedulable_resource_ids(
    technician_rows: Sequence[dict[str, Any]],
    availability_rows: Sequence[dict[str, Any]],
) -> set[str]:
    configured = {str(row.get("name") or "").strip() for row in technician_rows}
    return {
        resource_id
        for resource_id in configured
        if resource_id and has_standard_schedule(availability_rows, resource_id)
    }


def _segment_inputs(
    segment_rows: Sequence[dict[str, Any]],
    demand_rows: Sequence[dict[str, Any]],
    schedulable: set[str],
) -> tuple[tuple[SegmentInput, ...], tuple[str, ...]]:
    demands = _demand_lookup(demand_rows)
    inputs: list[SegmentInput] = []
    unsupported: list[str] = []

    for row in segment_rows:
        status = str(row.get("Statut") or "")
        resource_id = str(row.get("Technicien") or "").strip()
        hours = _number(row.get("HeuresPrevues"))
        if status in {"Annulé", "Terminé"} or resource_id not in schedulable or hours <= 0:
            continue

        segment_id = str(row.get("IDSegment") or "").strip()
        start = date_from_value(row.get("DateDebut"))
        end = date_from_value(row.get("DateFin")) or start
        if not segment_id or not start or not end:
            if segment_id:
                unsupported.append(segment_id)
            continue
        try:
            load_profile = normalize_load_profile(row.get(SEGMENT_LOAD_PROFILE_FIELD))
        except ValueError:
            unsupported.append(segment_id)
            continue

        inputs.append(
            SegmentInput(
                segment_id=segment_id,
                resource_id=resource_id,
                start=start,
                end=end,
                hours=hours,
                plan_type=_plan_type(row),
                priority_rank=_priority_rank(row, demands),
                created_order=str(row.get("DateCreation") or ""),
                overtime_allowed=_truthy(row.get(SEGMENT_OVERTIME_FIELD)),
                desired_active_days=_optional_positive_int(row.get(SEGMENT_ACTIVE_DAYS_FIELD)),
                load_profile=load_profile,
                excluded_dates=frozenset(
                    day
                    for value in row.get("DatesExclues") or ()
                    for day in (date_from_value(value),)
                    if day is not None
                ),
                placement_not_before_date=date_from_value(
                    row.get("PlacementNotBeforeDate")
                ),
            )
        )

    return tuple(inputs), tuple(sorted(set(unsupported)))


def _active_segment_ids(
    segment_rows: Sequence[dict[str, Any]],
) -> frozenset[str]:
    return frozenset(
        segment_id
        for row in segment_rows
        if str(row.get("Statut") or "") not in {"Annulé", "Terminé"}
        for segment_id in (str(row.get("IDSegment") or "").strip(),)
        if segment_id
    )


def _locked_inputs(
    allocation_rows: Sequence[dict[str, Any]],
    *,
    preserved_segment_ids: frozenset[str],
) -> tuple[LockedAllocationInput, ...]:
    result: list[LockedAllocationInput] = []
    for row in allocation_rows:
        if not _truthy(row.get("Verrouillee")):
            continue
        day = date_from_value(row.get("Date"))
        resource_id = str(row.get("Technicien") or "").strip()
        segment_id = str(row.get("IDSegment") or "").strip()
        hours = _number(row.get("Heures"))
        if (
            not day
            or not resource_id
            or not segment_id
            or segment_id not in preserved_segment_ids
            or hours <= 0
        ):
            continue
        result.append(
            LockedAllocationInput(
                segment_id=segment_id,
                resource_id=resource_id,
                day=day,
                hours=hours,
                outside_schedule=_truthy(row.get("HorsHoraire")),
            )
        )
    return tuple(result)


def _capacity_snapshot(
    availability_rows: Sequence[dict[str, Any]],
    segments: Sequence[SegmentInput],
) -> Mapping[CapacityKey, float]:
    result: dict[CapacityKey, float] = {}
    for segment in segments:
        cursor = segment.start
        while cursor <= segment.end:
            key = (segment.resource_id, cursor)
            if key not in result:
                result[key] = availability_hours_for_day(
                    availability_rows,
                    segment.resource_id,
                    cursor,
                )
            cursor += timedelta(days=1)
    return MappingProxyType(result)


def _outside_schedule_eligibility_snapshot(
    availability_rows: Sequence[dict[str, Any]],
    segments: Sequence[SegmentInput],
) -> Mapping[CapacityKey, bool]:
    result: dict[CapacityKey, bool] = {}
    for segment in segments:
        cursor = segment.start
        while cursor <= segment.end:
            key = (segment.resource_id, cursor)
            if key not in result:
                result[key] = outside_schedule_eligible_for_day(
                    availability_rows,
                    segment.resource_id,
                    cursor,
                )
            cursor += timedelta(days=1)
    return MappingProxyType(result)


def _persisted_projection(
    allocation_rows: Sequence[dict[str, Any]],
    included_segment_ids: set[str] | frozenset[str],
) -> tuple[AllocationProjection, ...]:
    result: list[AllocationProjection] = []
    for row in allocation_rows:
        segment_id = str(row.get("IDSegment") or "").strip()
        if segment_id not in included_segment_ids:
            continue
        if str(row.get("TypeAllocation") or "").strip() == MISSING_ALLOCATION_TYPE:
            continue
        day = date_from_value(row.get("Date"))
        resource_id = str(row.get("Technicien") or "").strip()
        hours = _number(row.get("Heures"))
        if not day or not resource_id or hours <= 0:
            continue
        locked = _truthy(row.get("Verrouillee"))
        result.append(
            AllocationProjection(
                segment_id=segment_id,
                resource_id=resource_id,
                day=day,
                hours=hours,
                allocation_type="Locked" if locked else str(row.get("TypeAllocation") or ""),
                locked=locked,
                outside_schedule=_truthy(row.get("HorsHoraire")),
            )
        )
    return tuple(result)


def project_planning_snapshot(snapshot: PlanningSnapshot) -> PlanningCalculationSnapshot:
    """Normalize a source snapshot once into the exact typed inputs the engine uses."""

    schedulable = _schedulable_resource_ids(snapshot.technicians, snapshot.availability)
    segments, unsupported = _segment_inputs(snapshot.segments, snapshot.demands, schedulable)
    preserved_segment_ids = _active_segment_ids(snapshot.segments)
    locked = _locked_inputs(
        snapshot.allocations,
        preserved_segment_ids=preserved_segment_ids,
    )

    return PlanningCalculationSnapshot(
        segments=segments,
        locked_allocations=locked,
        capacity_by_resource_day=_capacity_snapshot(snapshot.availability, segments),
        outside_schedule_eligible_by_resource_day=_outside_schedule_eligibility_snapshot(
            snapshot.availability,
            segments,
        ),
        persisted_allocations=_persisted_projection(
            snapshot.allocations,
            preserved_segment_ids,
        ),
        preserved_segment_ids=preserved_segment_ids,
        unsupported_segment_ids=unsupported,
    )
