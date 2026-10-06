"""Canonical computed association between autonomous asset allocations and human Shifts.

ADR-026 keeps AssetAllocation as the only physical reservation authority.  This
module therefore answers only whether an existing allocation is relevant to a
Shift; it never persists a Shift-to-asset link.
"""

from __future__ import annotations

from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import AssetAllocation, AssetRequirement
from .models import ResourceRequirement, Shift


ASSOCIATION_OWNED_SHIFT = "OWNED_SHIFT"
ASSOCIATION_RELATED_REQUEST = "RELATED_REQUEST"
ASSOCIATION_INHERITED_RESOURCE_PERIOD = "INHERITED_RESOURCE_PERIOD"
ASSOCIATION_INHERITED_PROJECT_DIRECT = "INHERITED_PROJECT_DIRECT"
ASSOCIATION_INHERITED_SEGMENT = "INHERITED_SEGMENT"
ASSOCIATION_SEGMENT_CONTEXT = "SEGMENT_CONTEXT"


def asset_shift_association(
    *,
    requirement: AssetRequirement,
    allocation: AssetAllocation,
    shift: Shift,
    human_requirement: ResourceRequirement,
) -> str | None:
    """Return the computed ADR-026 association kind for one Shift/allocation pair."""

    if requirement.status == "Annulé":
        return None
    if not allocation.start_date <= shift.work_date <= allocation.end_date:
        return None

    origin = requirement.origin
    if origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
        return (
            ASSOCIATION_OWNED_SHIFT
            if requirement.shift_id == shift.id
            else None
        )

    if origin == AssetRequirementOrigin.REQUEST.value:
        if (
            requirement.workforce_request_id
            and human_requirement.workforce_request_id
            and requirement.workforce_request_id
            == human_requirement.workforce_request_id
            and requirement.project_id == human_requirement.project_id
            and requirement.start_date <= shift.work_date <= requirement.end_date
        ):
            return ASSOCIATION_RELATED_REQUEST
        return None

    if origin == AssetRequirementOrigin.RESOURCE_PERIOD.value:
        if (
            requirement.context_resource_id == shift.resource_id
            and allocation.operator_resource_id == shift.resource_id
        ):
            return ASSOCIATION_INHERITED_RESOURCE_PERIOD
        return None

    if origin == AssetRequirementOrigin.PROJECT_DIRECT.value:
        if (
            requirement.project_id == human_requirement.project_id
            and allocation.operator_resource_id == shift.resource_id
        ):
            return ASSOCIATION_INHERITED_PROJECT_DIRECT
        return None

    if origin == AssetRequirementOrigin.SEGMENT.value:
        if requirement.resource_requirement_id != human_requirement.id:
            return None
        if allocation.operator_resource_id is None:
            return ASSOCIATION_SEGMENT_CONTEXT
        if allocation.operator_resource_id == shift.resource_id:
            return ASSOCIATION_INHERITED_SEGMENT
        return None

    return None
