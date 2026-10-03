"""Canonical qualification evaluation for reservable asset allocations.

The rule intentionally stays separate from the human planning engine: an asset keeps
its own reservation model while qualification is proven from canonical competencies
and actual human Shift assignments for the same approved request/project.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    elif requirement.origin == AssetRequirementOrigin.PROJECT_DIRECT.value:
        # The direct-project command is itself the explicit designation of the
        # operator in the project context. No synthetic Shift is required.
        return bool(requirement.project_id)
    elif requirement.origin == AssetRequirementOrigin.RESOURCE_PERIOD.value:
        # The beneficiary is explicit and must remain the physical operator for
        # the complete direct period.
        return bool(
            requirement.context_resource_id
            and requirement.context_resource_id == resource_id
        )
    elif requirement.origin == AssetRequirementOrigin.SEGMENT.value:
        # SEGMENT uses an explicit operator designation in the human requirement
        # context. It deliberately does not infer ownership from the automatic
        # target and does not require a synthetic Shift.
        if not requirement.resource_requirement_id or not requirement.project_id:
            return False
        segment = session.get(
            ResourceRequirement,
            requirement.resource_requirement_id,
        )
        return bool(
            segment is not None
            and segment.status != "Annulé"
            and segment.project_id == requirement.project_id
        )
    else:
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


def evaluate_asset_qualifications(
    session: Session,
    *,
    pairs: Sequence[tuple[AssetRequirement, AssetAllocation | None]],
) -> dict[tuple[str, str | None], AssetQualification]:
    """Evaluate a planning window in bounded queries while preserving canonical rules."""

    rows = tuple(pairs)
    if not rows:
        return {}

    type_ids = {requirement.asset_type_id for requirement, _allocation in rows}
    required_by_type: dict[str, list[Competency]] = {}
    for asset_type_id, competency in session.execute(
        select(AssetTypeCompetency.asset_type_id, Competency)
        .join(Competency, AssetTypeCompetency.competency_id == Competency.id)
        .where(AssetTypeCompetency.asset_type_id.in_(type_ids))
        .order_by(
            AssetTypeCompetency.asset_type_id,
            Competency.sort_order,
            Competency.name,
            Competency.id,
        )
    ).all():
        required_by_type.setdefault(asset_type_id, []).append(competency)

    operator_ids = {
        allocation.operator_resource_id
        for _requirement, allocation in rows
        if allocation is not None and allocation.operator_resource_id
    }
    operators = (
        {
            resource.id: resource
            for resource in session.scalars(
                select(Resource).where(Resource.id.in_(operator_ids))
            ).all()
        }
        if operator_ids
        else {}
    )
    held_by_resource: dict[str, set[str]] = {}
    if operator_ids:
        for resource_id, competency_id in session.execute(
            select(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            ).where(ResourceCompetency.resource_id.in_(operator_ids))
        ).all():
            held_by_resource.setdefault(resource_id, set()).add(competency_id)

    segment_ids = {
        requirement.resource_requirement_id
        for requirement, _allocation in rows
        if requirement.origin == AssetRequirementOrigin.SEGMENT.value
        and requirement.resource_requirement_id
    }
    segments = (
        {
            segment.id: segment
            for segment in session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.id.in_(segment_ids)
                )
            ).all()
        }
        if segment_ids
        else {}
    )

    human_pairs = tuple(
        (requirement, allocation)
        for requirement, allocation in rows
        if allocation is not None
        and allocation.operator_resource_id
        and requirement.origin
        in {
            AssetRequirementOrigin.SHIFT_AD_HOC.value,
            AssetRequirementOrigin.REQUEST.value,
        }
    )
    human_rows: tuple[tuple[Shift, ResourceRequirement], ...] = ()
    if human_pairs:
        human_operator_ids = {
            allocation.operator_resource_id
            for _requirement, allocation in human_pairs
            if allocation.operator_resource_id
        }
        project_ids = {
            requirement.project_id
            for requirement, _allocation in human_pairs
            if requirement.project_id
        }
        min_start = min(allocation.start_date for _requirement, allocation in human_pairs)
        max_end = max(allocation.end_date for _requirement, allocation in human_pairs)
        statement = (
            select(Shift, ResourceRequirement)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .where(
                Shift.resource_id.in_(human_operator_ids),
                Shift.work_date >= min_start,
                Shift.work_date <= max_end,
                ResourceRequirement.status != "Annulé",
            )
        )
        if project_ids:
            statement = statement.where(ResourceRequirement.project_id.in_(project_ids))
        human_rows = tuple(session.execute(statement).all())

    result: dict[tuple[str, str | None], AssetQualification] = {}
    for requirement, allocation in rows:
        required = tuple(required_by_type.get(requirement.asset_type_id, ()))
        required_ids = tuple(row.id for row in required)
        required_names = tuple(row.name for row in required)
        key = (requirement.id, allocation.id if allocation is not None else None)

        if allocation is None or not allocation.operator_resource_id:
            result[key] = AssetQualification(
                state=(
                    QUALIFICATION_SATISFIED
                    if not required_ids
                    else QUALIFICATION_MISSING_OPERATOR
                ),
                required_competency_ids=required_ids,
                required_competency_names=required_names,
            )
            continue

        operator = operators.get(allocation.operator_resource_id)
        if operator is None or not operator.active:
            result[key] = AssetQualification(
                state=QUALIFICATION_SKILL_MISMATCH,
                required_competency_ids=required_ids,
                required_competency_names=required_names,
                operator_resource_id=allocation.operator_resource_id,
                operator_resource_name=operator.name if operator is not None else None,
            )
            continue

        if not set(required_ids).issubset(held_by_resource.get(operator.id, set())):
            result[key] = AssetQualification(
                state=QUALIFICATION_SKILL_MISMATCH,
                required_competency_ids=required_ids,
                required_competency_names=required_names,
                operator_resource_id=operator.id,
                operator_resource_name=operator.name,
            )
            continue

        compatible = False
        if requirement.origin == AssetRequirementOrigin.PROJECT_DIRECT.value:
            compatible = bool(requirement.project_id)
        elif requirement.origin == AssetRequirementOrigin.RESOURCE_PERIOD.value:
            compatible = bool(
                requirement.context_resource_id
                and requirement.context_resource_id == operator.id
            )
        elif requirement.origin == AssetRequirementOrigin.SEGMENT.value:
            segment = segments.get(requirement.resource_requirement_id)
            compatible = bool(
                requirement.resource_requirement_id
                and requirement.project_id
                and segment is not None
                and segment.status != "Annulé"
                and segment.project_id == requirement.project_id
            )
        elif requirement.origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
            compatible = bool(
                requirement.shift_id
                and any(
                    shift.id == requirement.shift_id
                    and shift.resource_id == operator.id
                    and allocation.start_date <= shift.work_date <= allocation.end_date
                    and human_requirement.project_id == requirement.project_id
                    for shift, human_requirement in human_rows
                )
            )
        elif requirement.origin == AssetRequirementOrigin.REQUEST.value:
            compatible = bool(
                requirement.workforce_request_id
                and requirement.project_id
                and any(
                    shift.resource_id == operator.id
                    and allocation.start_date <= shift.work_date <= allocation.end_date
                    and human_requirement.project_id == requirement.project_id
                    and human_requirement.workforce_request_id
                    == requirement.workforce_request_id
                    for shift, human_requirement in human_rows
                )
            )

        result[key] = AssetQualification(
            state=(
                QUALIFICATION_SATISFIED
                if compatible
                else QUALIFICATION_NO_OVERLAP
            ),
            required_competency_ids=required_ids,
            required_competency_names=required_names,
            operator_resource_id=operator.id,
            operator_resource_name=operator.name,
        )

    return result


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
