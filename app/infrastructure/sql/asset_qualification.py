"""Canonical qualification evaluation for reservable asset allocations.

The rule intentionally stays separate from the human planning engine: an asset keeps
its own reservation model while qualification is proven from canonical competencies
and actual human Shift assignments for the same approved request/project.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import AssetAllocation, AssetRequirement, AssetTypeCompetency
from .models import Competency, Resource, ResourceCompetency, ResourceRequirement, Shift


QUALIFICATION_POLICY_ANY_ASSIGNED_WORKFORCE = "ANY_ASSIGNED_WORKFORCE"

QUALIFICATION_SATISFIED = "SATISFIED"
QUALIFICATION_MISSING_OPERATOR = "MISSING_OPERATOR"
QUALIFICATION_SKILL_MISMATCH = "SKILL_MISMATCH"
QUALIFICATION_NO_OVERLAP = "NO_OVERLAP"


@dataclass(frozen=True, slots=True)
class AssetQualification:
    state: str
    required_competency_ids: tuple[str, ...]
    required_competency_names: tuple[str, ...]
    operator_resource_id: str | None = None
    operator_resource_name: str | None = None


def required_competencies(
    session: Session,
    asset_type_id: str,
) -> tuple[Competency, ...]:
    return tuple(
        session.scalars(
            select(Competency)
            .join(
                AssetTypeCompetency,
                AssetTypeCompetency.competency_id == Competency.id,
            )
            .where(AssetTypeCompetency.asset_type_id == asset_type_id)
            .order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
    )


def resource_competency_ids(session: Session, resource_id: str) -> frozenset[str]:
    return frozenset(
        session.scalars(
            select(ResourceCompetency.competency_id).where(
                ResourceCompetency.resource_id == resource_id
            )
        ).all()
    )


def has_compatible_assignment(
    session: Session,
    *,
    requirement: AssetRequirement,
    allocation: AssetAllocation,
    resource_id: str,
) -> bool:
    statement = (
        select(Shift.id)
        .join(
            ResourceRequirement,
            Shift.resource_requirement_id == ResourceRequirement.id,
        )
        .where(
            Shift.resource_id == resource_id,
            Shift.work_date >= allocation.start_date,
            Shift.work_date <= allocation.end_date,
            ResourceRequirement.project_id == requirement.project_id,
            ResourceRequirement.status != "Annulé",
        )
    )
    if requirement.origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
        if not requirement.shift_id:
            return False
        statement = statement.where(Shift.id == requirement.shift_id)
    elif requirement.origin == AssetRequirementOrigin.REQUEST.value:
        if not requirement.workforce_request_id or not requirement.project_id:
            return False
        statement = statement.where(
            ResourceRequirement.workforce_request_id
            == requirement.workforce_request_id
        )
    else:
        # 575A makes the new origins readable but does not implement their
        # qualification policy. Fail closed instead of treating them as REQUEST.
        return False
    return session.scalar(statement.limit(1)) is not None


def evaluate_asset_qualification(
    session: Session,
    *,
    requirement: AssetRequirement,
    allocation: AssetAllocation | None,
) -> AssetQualification:
    required = required_competencies(session, requirement.asset_type_id)
    required_ids = tuple(row.id for row in required)
    required_names = tuple(row.name for row in required)

    if allocation is None or not allocation.operator_resource_id:
        if not required_ids:
            return AssetQualification(
                state=QUALIFICATION_SATISFIED,
                required_competency_ids=required_ids,
                required_competency_names=required_names,
            )
        return AssetQualification(
            state=QUALIFICATION_MISSING_OPERATOR,
            required_competency_ids=required_ids,
            required_competency_names=required_names,
        )

    operator = session.get(Resource, allocation.operator_resource_id)
    if operator is None or not operator.active:
        return AssetQualification(
            state=QUALIFICATION_SKILL_MISMATCH,
            required_competency_ids=required_ids,
            required_competency_names=required_names,
            operator_resource_id=allocation.operator_resource_id,
            operator_resource_name=operator.name if operator is not None else None,
        )

    held = resource_competency_ids(session, operator.id)
    if not set(required_ids).issubset(held):
        return AssetQualification(
            state=QUALIFICATION_SKILL_MISMATCH,
            required_competency_ids=required_ids,
            required_competency_names=required_names,
            operator_resource_id=operator.id,
            operator_resource_name=operator.name,
        )

    if not has_compatible_assignment(
        session,
        requirement=requirement,
        allocation=allocation,
        resource_id=operator.id,
    ):
        return AssetQualification(
            state=QUALIFICATION_NO_OVERLAP,
            required_competency_ids=required_ids,
            required_competency_names=required_names,
            operator_resource_id=operator.id,
            operator_resource_name=operator.name,
        )

    return AssetQualification(
        state=QUALIFICATION_SATISFIED,
        required_competency_ids=required_ids,
        required_competency_names=required_names,
        operator_resource_id=operator.id,
        operator_resource_name=operator.name,
    )


def eligible_operator_resources(
    session: Session,
    *,
    requirement: AssetRequirement,
    allocation: AssetAllocation,
) -> tuple[Resource, ...]:
    required_ids = {
        row.id for row in required_competencies(session, requirement.asset_type_id)
    }
    resources = session.scalars(
        select(Resource)
        .where(Resource.active == true())
        .order_by(Resource.sort_order, Resource.name, Resource.id)
    ).all()
    result: list[Resource] = []
    for resource in resources:
        held = resource_competency_ids(session, resource.id)
        if not required_ids.issubset(held):
            continue
        if not has_compatible_assignment(
            session,
            requirement=requirement,
            allocation=allocation,
            resource_id=resource.id,
        ):
            continue
        result.append(resource)
    return tuple(result)
