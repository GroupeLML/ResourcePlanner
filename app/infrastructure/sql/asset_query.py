"""Read-only planning projections for physical reservable assets."""

from __future__ import annotations

from datetime import date, timedelta
import json
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...application.query_models import (
    AssetAllocationReadModel,
    AssetDayCapacityReadModel,
    AssetPlanningDiagnosticReadModel,
    AssetPlanningWindowReadModel,
    AssetReadModel,
    AssetRequirementReadModel,
    AssetTypeReadModel,
    AssetUnavailabilityReadModel,
)
from ...domain.approval_envelope import approval_envelope_from_snapshot_payload
from ...domain.reservable_assets import AssetRequirementOrigin
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetUnavailability,
)
from .asset_qualification import (
    AssetQualification,
    QUALIFICATION_MISSING_OPERATOR,
    QUALIFICATION_NO_OVERLAP,
    QUALIFICATION_SATISFIED,
    QUALIFICATION_SKILL_MISMATCH,
    evaluate_asset_qualification,
    evaluate_asset_qualifications,
    required_competencies,
)
from .models import Project, ResourceRequirement, WorkforceRequest
from .operational_choice_models import RequestOperationalState


def _text(value: object) -> str:
    return str(value or "").strip()


def _overlaps(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    return start_a <= end_b and end_a >= start_b


class SqlAssetPlanningQuery:
    """Project asset occupancy without translating physical capacity into hours."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _request(self, number: str) -> WorkforceRequest | None:
        wanted = _text(number)
        if not wanted:
            return None
        return self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )

    def _qualification(
        self,
        *,
        requirement: AssetRequirement,
        allocation: AssetAllocation | None,
        cache: dict[tuple[str, str | None], AssetQualification],
    ) -> AssetQualification:
        key = (requirement.id, allocation.id if allocation is not None else None)
        if key not in cache:
            cache[key] = evaluate_asset_qualification(
                self._session,
                requirement=requirement,
                allocation=allocation,
            )
        return cache[key]

    def _requirement_models(
        self,
        requirements: Sequence[AssetRequirement],
        *,
        qualification_cache: dict[tuple[str, str | None], AssetQualification] | None = None,
    ) -> tuple[AssetRequirementReadModel, ...]:
        rows = tuple(requirements)
        if not rows:
            return ()

        project_ids = {row.project_id for row in rows if row.project_id}
        request_ids = {
            row.workforce_request_id for row in rows if row.workforce_request_id
        }
        resource_requirement_ids = {
            row.resource_requirement_id
            for row in rows
            if row.resource_requirement_id
        }
        type_ids = {row.asset_type_id for row in rows}
        requirement_ids = {row.id for row in rows}

        projects = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(Project).where(Project.id.in_(project_ids))
                ).all()
            }
            if project_ids
            else {}
        )
        requests = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(WorkforceRequest).where(WorkforceRequest.id.in_(request_ids))
                ).all()
            }
            if request_ids
            else {}
        )
        human_requirements = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(ResourceRequirement).where(
                        ResourceRequirement.id.in_(resource_requirement_ids)
                    )
                ).all()
            }
            if resource_requirement_ids
            else {}
        )
        asset_types = {
            row.id: row
            for row in self._session.scalars(
                select(AssetType).where(AssetType.id.in_(type_ids))
            ).all()
        }
        allocations = {
            row.asset_requirement_id: row
            for row in self._session.scalars(
                select(AssetAllocation).where(
                    AssetAllocation.asset_requirement_id.in_(requirement_ids)
                )
            ).all()
        }
        asset_ids = {row.asset_id for row in allocations.values()}
        assets = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(Asset).where(Asset.id.in_(asset_ids))
                ).all()
            }
            if asset_ids
            else {}
        )

        result: list[AssetRequirementReadModel] = []
        for row in rows:
            project = projects.get(row.project_id)
            request = requests.get(row.workforce_request_id)
            asset_type = asset_types.get(row.asset_type_id)
            human_requirement = human_requirements.get(row.resource_requirement_id)
            allocation = allocations.get(row.id)
            asset = assets.get(allocation.asset_id) if allocation is not None else None
            qualification = (
                self._qualification(
                    requirement=row,
                    allocation=allocation,
                    cache=qualification_cache,
                )
                if qualification_cache is not None
                else evaluate_asset_qualification(
                    self._session,
                    requirement=row,
                    allocation=allocation,
                )
            )
            result.append(
                AssetRequirementReadModel(
                    requirement_id=row.id,
                    origin=row.origin,
                    request_id=row.workforce_request_id,
                    demand_number=(
                        _text(request.legacy_demand_number) or request.id
                        if request is not None
                        else row.workforce_request_id
                    ),
                    project_id=row.project_id,
                    project_number=(
                        project.number if project is not None else row.project_id
                    ),
                    source_request_line_id=row.source_request_line_id,
                    source_period_id=row.source_period_id,
                    approval_revision_id=row.approval_revision_id,
                    approved_entry_key=row.approved_entry_key,
                    resource_requirement_id=row.resource_requirement_id,
                    segment_reference=(
                        (
                            _text(human_requirement.legacy_segment_id)
                            or human_requirement.id
                        )
                        if human_requirement is not None
                        else None
                    ),
                    shift_id=row.shift_id,
                    context_resource_id=row.context_resource_id,
                    slot_index=int(row.slot_index or 0),
                    asset_type_id=row.asset_type_id,
                    asset_type_code=(
                        asset_type.code if asset_type is not None else row.asset_type_id
                    ),
                    asset_type_label=(
                        asset_type.label if asset_type is not None else row.asset_type_id
                    ),
                    start_date=row.start_date,
                    end_date=row.end_date,
                    usage_hours=(
                        float(row.usage_hours)
                        if row.usage_hours is not None
                        else None
                    ),
                    status=row.status,
                    allocation_id=allocation.id if allocation is not None else None,
                    asset_id=allocation.asset_id if allocation is not None else None,
                    asset_code=asset.code if asset is not None else None,
                    asset_label=asset.label if asset is not None else None,
                    allocation_start_date=(
                        allocation.start_date if allocation is not None else None
                    ),
                    allocation_end_date=(
                        allocation.end_date if allocation is not None else None
                    ),
                    allocation_locked=bool(
                        allocation is not None and allocation.locked
                    ),
                    operator_resource_id=qualification.operator_resource_id,
                    operator_resource_name=qualification.operator_resource_name,
                    qualification_state=qualification.state,
                    required_competency_ids=qualification.required_competency_ids,
                    required_competency_names=qualification.required_competency_names,
                )
            )
        return tuple(result)

    def list_demand_requirements(
        self,
        number: str,
    ) -> tuple[AssetRequirementReadModel, ...]:
        request = self._request(number)
        if request is None:
            return ()
        rows = self._session.scalars(
            select(AssetRequirement)
            .where(
                AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value,
                AssetRequirement.workforce_request_id == request.id,
                AssetRequirement.status != "Annulé",
            )
            .order_by(
                AssetRequirement.start_date,
                AssetRequirement.approved_entry_key,
                AssetRequirement.slot_index,
                AssetRequirement.id,
            )
        ).all()
        return self._requirement_models(rows)

    def _approval_diagnostics(
        self,
        requirements: Sequence[AssetRequirement],
    ) -> list[AssetPlanningDiagnosticReadModel]:
        rows = tuple(
            row
            for row in requirements
            if row.origin == AssetRequirementOrigin.REQUEST.value
        )
        request_ids = {
            row.workforce_request_id for row in rows if row.workforce_request_id
        }
        references = {
            row.workforce_request_id: row
            for row in self._session.scalars(
                select(RequestApprovalReference).where(
                    RequestApprovalReference.workforce_request_id.in_(request_ids)
                )
            ).all()
        } if request_ids else {}
        revision_ids = {
            reference.active_revision_id
            for reference in references.values()
            if reference.active_revision_id
        }
        revisions = {
            row.id: row
            for row in self._session.scalars(
                select(RequestApprovalRevision).where(
                    RequestApprovalRevision.id.in_(revision_ids)
                )
            ).all()
        } if revision_ids else {}
        states = {
            row.workforce_request_id: row
            for row in self._session.scalars(
                select(RequestOperationalState).where(
                    RequestOperationalState.workforce_request_id.in_(request_ids)
                )
            ).all()
        } if request_ids else {}

        envelope_by_revision: dict[str, object] = {}
        selections_by_request: dict[str, dict[str, str]] = {}
        diagnostics: list[AssetPlanningDiagnosticReadModel] = []
        for row in rows:
            reference = references.get(row.workforce_request_id)
            if (
                reference is None
                or reference.status != APPROVAL_REFERENCE_CAPTURED
                or not reference.active_revision_id
                or row.approval_revision_id != reference.active_revision_id
            ):
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_APPROVAL_REFERENCE_UNKNOWN",
                        message="Le besoin d'actif n'est pas relié à la révision approuvée active.",
                        requirement_id=row.id,
                    )
                )
                continue

            revision = revisions.get(reference.active_revision_id)
            if revision is None:
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_APPROVAL_REFERENCE_UNKNOWN",
                        message="La révision approuvée active du besoin d'actif est introuvable.",
                        requirement_id=row.id,
                    )
                )
                continue

            if revision.id not in envelope_by_revision:
                try:
                    payload = json.loads(revision.payload_text)
                    envelope_by_revision[revision.id] = approval_envelope_from_snapshot_payload(
                        payload["authorization"]
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    envelope_by_revision[revision.id] = None
            envelope = envelope_by_revision[revision.id]
            if envelope is None:
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_APPROVAL_REFERENCE_UNKNOWN",
                        message="Le snapshot approuvé du besoin d'actif est invalide.",
                        requirement_id=row.id,
                    )
                )
                continue
            entry = next(
                (
                    item
                    for item in envelope.entries
                    if item.line_kind == "ASSET"
                    and item.identity.stable_key == row.approved_entry_key
                ),
                None,
            )
            if entry is None:
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_APPROVAL_REFERENCE_UNKNOWN",
                        message="Le besoin d'actif ne correspond à aucune entrée de l'enveloppe approuvée.",
                        requirement_id=row.id,
                    )
                )
                continue
            if entry.group is None:
                continue

            state = states.get(row.workforce_request_id)
            if (
                state is None
                or state.approval_revision_id != revision.id
            ):
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_ALTERNATIVE_NOT_SELECTED",
                        message="La sélection opérationnelle de l'alternative d'actif est absente.",
                        requirement_id=row.id,
                    )
                )
                continue
            if row.workforce_request_id not in selections_by_request:
                try:
                    decoded = json.loads(state.selections_text or "{}")
                    selections_by_request[row.workforce_request_id] = (
                        {
                            _text(key): _text(value)
                            for key, value in decoded.items()
                            if _text(key) and _text(value)
                        }
                        if isinstance(decoded, dict)
                        else {}
                    )
                except json.JSONDecodeError:
                    selections_by_request[row.workforce_request_id] = {}
            selected = selections_by_request[row.workforce_request_id].get(
                entry.group.stable_key
            )
            if selected != entry.identity.stable_key:
                diagnostics.append(
                    AssetPlanningDiagnosticReadModel(
                        code="ASSET_ALTERNATIVE_NOT_SELECTED",
                        message="Le besoin d'actif matérialisé n'est pas l'alternative opérationnelle sélectionnée.",
                        requirement_id=row.id,
                    )
                )
        return diagnostics

    def planning_window(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
        context_resource_ids: Sequence[str] = (),
    ) -> AssetPlanningWindowReadModel:
        type_rows = tuple(
            self._session.scalars(select(AssetType).order_by(AssetType.code)).all()
        )
        asset_rows = tuple(
            self._session.scalars(select(Asset).order_by(Asset.code)).all()
        )
        types_by_id = {row.id: row for row in type_rows}
        assets_by_id = {row.id: row for row in asset_rows}

        requirement_statement = select(AssetRequirement).where(
            AssetRequirement.status != "Annulé",
            AssetRequirement.start_date <= end,
            AssetRequirement.end_date >= start,
        )
        if project_ids is not None or demand_ids is not None:
            identifiers = tuple(
                _text(value) for value in (project_ids or ()) if _text(value)
            )
            demand_identifiers = tuple(
                _text(value) for value in (demand_ids or ()) if _text(value)
            )
            context_identifiers = tuple(
                _text(value) for value in context_resource_ids if _text(value)
            )
            scope_predicates = []
            if identifiers:
                scope_predicates.append(AssetRequirement.project_id.in_(identifiers))
            if demand_identifiers:
                scope_predicates.append(
                    AssetRequirement.workforce_request_id.in_(demand_identifiers)
                )
            if context_identifiers:
                scope_predicates.append(
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.RESOURCE_PERIOD.value
                    )
                    & AssetRequirement.project_id.is_(None)
                    & AssetRequirement.context_resource_id.in_(context_identifiers)
                )
            if not scope_predicates:
                visible_requirements: tuple[AssetRequirement, ...] = ()
            else:
                visible_requirements = tuple(
                    self._session.scalars(
                        requirement_statement.where(
                            or_(*scope_predicates)
                        ).order_by(
                            AssetRequirement.start_date,
                            AssetRequirement.approved_entry_key,
                            AssetRequirement.slot_index,
                            AssetRequirement.id,
                        )
                    ).all()
                )
        else:
            visible_requirements = tuple(
                self._session.scalars(
                    requirement_statement.order_by(
                        AssetRequirement.start_date,
                        AssetRequirement.approved_entry_key,
                        AssetRequirement.slot_index,
                        AssetRequirement.id,
                    )
                ).all()
            )

        visible_requirement_ids = {row.id for row in visible_requirements}
        visible_allocations = tuple(
            self._session.scalars(
                select(AssetAllocation)
                .where(
                    AssetAllocation.asset_requirement_id.in_(visible_requirement_ids),
                    AssetAllocation.start_date <= end,
                    AssetAllocation.end_date >= start,
                )
                .order_by(AssetAllocation.start_date, AssetAllocation.asset_id, AssetAllocation.id)
            ).all()
        ) if visible_requirement_ids else ()

        all_allocations = tuple(
            self._session.scalars(
                select(AssetAllocation)
                .where(
                    AssetAllocation.start_date <= end,
                    AssetAllocation.end_date >= start,
                )
                .order_by(AssetAllocation.asset_id, AssetAllocation.start_date, AssetAllocation.id)
            ).all()
        )
        unavailability_rows = tuple(
            self._session.scalars(
                select(AssetUnavailability)
                .where(
                    AssetUnavailability.start_date <= end,
                    AssetUnavailability.end_date >= start,
                )
                .order_by(AssetUnavailability.asset_id, AssetUnavailability.start_date, AssetUnavailability.id)
            ).all()
        )

        visible_requirements_by_id = {
            row.id: row for row in visible_requirements
        }
        visible_allocation_by_requirement = {
            row.asset_requirement_id: row for row in visible_allocations
        }
        qualification_cache: dict[
            tuple[str, str | None], AssetQualification
        ] = evaluate_asset_qualifications(
            self._session,
            pairs=tuple(
                (
                    requirement,
                    visible_allocation_by_requirement.get(requirement.id),
                )
                for requirement in visible_requirements
            ),
        )
        visible_requirement_models = self._requirement_models(
            visible_requirements,
            qualification_cache=qualification_cache,
        )
        visible_requirement_models_by_id = {
            row.requirement_id: row for row in visible_requirement_models
        }
        allocation_models_list: list[AssetAllocationReadModel] = []
        for row in visible_allocations:
            requirement = visible_requirements_by_id[row.asset_requirement_id]
            qualification = self._qualification(
                requirement=requirement,
                allocation=row,
                cache=qualification_cache,
            )
            allocation_models_list.append(
                AssetAllocationReadModel(
                    allocation_id=row.id,
                    requirement_id=row.asset_requirement_id,
                    asset_id=row.asset_id,
                    asset_code=(
                        assets_by_id[row.asset_id].code
                        if row.asset_id in assets_by_id
                        else row.asset_id
                    ),
                    asset_label=(
                        assets_by_id[row.asset_id].label
                        if row.asset_id in assets_by_id
                        else row.asset_id
                    ),
                    start_date=row.start_date,
                    end_date=row.end_date,
                    locked=bool(row.locked),
                    source=row.source,
                    operator_resource_id=qualification.operator_resource_id,
                    operator_resource_name=qualification.operator_resource_name,
                    qualification_state=qualification.state,
                    required_competency_ids=qualification.required_competency_ids,
                    required_competency_names=qualification.required_competency_names,
                    project_number=visible_requirement_models_by_id[
                        row.asset_requirement_id
                    ].project_number,
                )
            )
        allocation_models = tuple(allocation_models_list)
        unavailability_models = tuple(
            AssetUnavailabilityReadModel(
                id=row.id,
                asset_id=row.asset_id,
                start_date=row.start_date,
                end_date=row.end_date,
                reason=row.reason,
            )
            for row in unavailability_rows
        )

        diagnostics = self._approval_diagnostics(visible_requirements)
        allocation_by_requirement = {
            row.asset_requirement_id: row for row in all_allocations
        }
        unavailability_by_asset: dict[str, list[AssetUnavailability]] = {}
        for row in unavailability_rows:
            unavailability_by_asset.setdefault(row.asset_id, []).append(row)

        seen_diagnostics: set[tuple[str, str | None, str | None, str | None]] = set()

        def add_diagnostic(
            code: str,
            message: str,
            *,
            requirement_id: str | None = None,
            allocation_id: str | None = None,
            asset_id: str | None = None,
        ) -> None:
            key = (code, requirement_id, allocation_id, asset_id)
            if key in seen_diagnostics:
                return
            seen_diagnostics.add(key)
            diagnostics.append(
                AssetPlanningDiagnosticReadModel(
                    code=code,
                    message=message,
                    requirement_id=requirement_id,
                    allocation_id=allocation_id,
                    asset_id=asset_id,
                )
            )

        for requirement in visible_requirements:
            asset_type = types_by_id.get(requirement.asset_type_id)
            allocation = allocation_by_requirement.get(requirement.id)
            incompatible_locked = False
            if asset_type is None or not asset_type.active:
                add_diagnostic(
                    "ASSET_TYPE_INACTIVE",
                    "Le type d'actif requis est absent ou inactif.",
                    requirement_id=requirement.id,
                )
                incompatible_locked = allocation is not None and bool(allocation.locked)
            if allocation is None:
                add_diagnostic(
                    "ASSET_REQUIREMENT_UNASSIGNED",
                    "Le besoin d'actif n'a aucune réservation.",
                    requirement_id=requirement.id,
                )
                continue

            asset = assets_by_id.get(allocation.asset_id)
            if asset is None or not asset.active:
                add_diagnostic(
                    "ASSET_INACTIVE",
                    "L'actif réservé est absent ou inactif.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )
                incompatible_locked = incompatible_locked or bool(allocation.locked)
            elif asset.asset_type_id != requirement.asset_type_id:
                add_diagnostic(
                    "ASSET_INCOMPATIBLE",
                    "L'actif réservé ne correspond pas au type requis.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )
                incompatible_locked = incompatible_locked or bool(allocation.locked)

            if (
                allocation.start_date < requirement.start_date
                or allocation.end_date > requirement.end_date
            ):
                add_diagnostic(
                    "ASSET_OUTSIDE_REQUIREMENT_WINDOW",
                    "La réservation d'actif dépasse la fenêtre du besoin.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )
                incompatible_locked = incompatible_locked or bool(allocation.locked)

            if any(
                _overlaps(
                    allocation.start_date,
                    allocation.end_date,
                    unavailable.start_date,
                    unavailable.end_date,
                )
                for unavailable in unavailability_by_asset.get(allocation.asset_id, ())
            ):
                add_diagnostic(
                    "ASSET_UNAVAILABLE",
                    "La réservation chevauche une indisponibilité de l'actif.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )
                incompatible_locked = incompatible_locked or bool(allocation.locked)

            conflicts = [
                other
                for other in all_allocations
                if other.id != allocation.id
                and other.asset_id == allocation.asset_id
                and _overlaps(
                    allocation.start_date,
                    allocation.end_date,
                    other.start_date,
                    other.end_date,
                )
            ]
            if conflicts:
                add_diagnostic(
                    "ASSET_DOUBLE_BOOKING",
                    "L'actif est réservé par plusieurs besoins sur une même fenêtre.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )
                incompatible_locked = incompatible_locked or bool(allocation.locked)

            qualification = self._qualification(
                requirement=requirement,
                allocation=allocation,
                cache=qualification_cache,
            )
            qualification_messages = {
                QUALIFICATION_MISSING_OPERATOR: (
                    "La réservation exige une ressource opératrice qualifiée."
                ),
                QUALIFICATION_SKILL_MISMATCH: (
                    "La ressource opératrice liée est inactive ou ne possède pas les compétences requises."
                ),
                QUALIFICATION_NO_OVERLAP: (
                    "La ressource opératrice qualifiée n'a aucune affectation humaine compatible avec la réservation."
                ),
            }
            if qualification.state != QUALIFICATION_SATISFIED:
                add_diagnostic(
                    f"ASSET_QUALIFICATION_{qualification.state}",
                    qualification_messages.get(
                        qualification.state,
                        "La qualification de la réservation d'actif est invalide.",
                    ),
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )

            if incompatible_locked:
                add_diagnostic(
                    "ASSET_LOCKED_INCOMPATIBLE",
                    "Une réservation verrouillée est incompatible avec l'état courant du besoin ou de l'actif.",
                    requirement_id=requirement.id,
                    allocation_id=allocation.id,
                    asset_id=allocation.asset_id,
                )

        capacity: list[AssetDayCapacityReadModel] = []
        cursor = start
        while cursor <= end:
            for asset in asset_rows:
                asset_type = types_by_id.get(asset.asset_type_id)
                occupied = sum(
                    1
                    for allocation in all_allocations
                    if allocation.asset_id == asset.id
                    and allocation.start_date <= cursor <= allocation.end_date
                )
                visible_occupations = tuple(
                    allocation
                    for allocation in allocation_models
                    if allocation.asset_id == asset.id
                    and allocation.start_date <= cursor <= allocation.end_date
                )
                unavailable = any(
                    row.start_date <= cursor <= row.end_date
                    for row in unavailability_by_asset.get(asset.id, ())
                )
                usable = bool(
                    asset.active
                    and asset_type is not None
                    and asset_type.active
                    and not unavailable
                )
                remaining = 1 if usable and occupied == 0 else 0
                capacity.append(
                    AssetDayCapacityReadModel(
                        asset_id=asset.id,
                        day=cursor,
                        capacity_units=1,
                        occupied_units=occupied,
                        remaining_units=remaining,
                        unavailable=unavailable,
                        available=remaining > 0,
                        visible_occupations=visible_occupations,
                        has_hidden_occupancy=occupied > len(visible_occupations),
                    )
                )
            cursor += timedelta(days=1)

        return AssetPlanningWindowReadModel(
            asset_types=tuple(
                AssetTypeReadModel(
                    id=row.id,
                    code=row.code,
                    label=row.label,
                    category=row.category,
                    occupancy_policy=row.occupancy_policy,
                    active=bool(row.active),
                    qualification_policy=row.qualification_policy,
                    required_competency_ids=tuple(
                        competency.id
                        for competency in required_competencies(self._session, row.id)
                    ),
                    required_competency_names=tuple(
                        competency.name
                        for competency in required_competencies(self._session, row.id)
                    ),
                )
                for row in type_rows
            ),
            assets=tuple(
                AssetReadModel(
                    id=row.id,
                    code=row.code,
                    label=row.label,
                    asset_type_id=row.asset_type_id,
                    active=bool(row.active),
                )
                for row in asset_rows
            ),
            requirements=visible_requirement_models,
            allocations=allocation_models,
            unavailability=unavailability_models,
            capacity=tuple(capacity),
            diagnostics=tuple(diagnostics),
        )
