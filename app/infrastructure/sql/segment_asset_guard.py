from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.errors import ApplicationConflictError
from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import AssetAllocation, AssetRequirement
from .models import ResourceRequirement


SEGMENT_ASSET_RESERVATION_CONFLICT = "segment_asset_reservation_conflict"


@dataclass(frozen=True, slots=True)
class SegmentAssetDependency:
    asset_requirement_id: str
    allocation_id: str
    resource_requirement_id: str
    project_id: str
    start_date: date
    end_date: date


def segment_asset_dependencies(
    session: Session,
    requirement_ids: Sequence[str],
) -> tuple[SegmentAssetDependency, ...]:
    identifiers = tuple(dict.fromkeys(str(value) for value in requirement_ids if str(value)))
    if not identifiers:
        return ()
    rows = session.execute(
        select(AssetRequirement, AssetAllocation)
        .join(
            AssetAllocation,
            AssetAllocation.asset_requirement_id == AssetRequirement.id,
        )
        .where(
            AssetRequirement.origin == AssetRequirementOrigin.SEGMENT.value,
            AssetRequirement.resource_requirement_id.in_(identifiers),
            AssetRequirement.status != "Annulé",
        )
        .order_by(
            AssetRequirement.resource_requirement_id,
            AssetRequirement.id,
            AssetAllocation.id,
        )
    ).all()
    return tuple(
        SegmentAssetDependency(
            asset_requirement_id=requirement.id,
            allocation_id=allocation.id,
            resource_requirement_id=str(requirement.resource_requirement_id),
            project_id=requirement.project_id,
            start_date=allocation.start_date,
            end_date=allocation.end_date,
        )
        for requirement, allocation in rows
        if requirement.resource_requirement_id
    )


def assert_segment_asset_mutation_compatible(
    session: Session,
    requirement: ResourceRequirement,
    *,
    target_project_id: str | None = None,
    target_start_date: date | None = None,
    target_end_date: date | None = None,
    removing: bool = False,
) -> None:
    dependencies = segment_asset_dependencies(session, (requirement.id,))
    if not dependencies:
        return

    project_id = target_project_id or requirement.project_id
    start_date = target_start_date or requirement.start_date
    end_date = target_end_date or requirement.end_date

    incompatible = removing
    if not incompatible:
        incompatible = any(
            dependency.project_id != project_id
            or dependency.start_date < start_date
            or dependency.end_date > end_date
            for dependency in dependencies
        )
    if not incompatible:
        return

    raise ApplicationConflictError(
        "Le segment possède une réservation d'actif incompatible avec cette modification. "
        "Libère ou adapte explicitement la réservation avant de continuer.",
        code=SEGMENT_ASSET_RESERVATION_CONFLICT,
        context={
            "resource_requirement_id": requirement.id,
            "asset_requirement_ids": [
                row.asset_requirement_id for row in dependencies
            ],
            "allocation_ids": [row.allocation_id for row in dependencies],
        },
    )
