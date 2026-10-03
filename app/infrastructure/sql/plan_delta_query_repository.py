from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import json
from decimal import Decimal

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.plan_delta import (
    DemandApprovalStateReadModel,
    DemandAssetPlanDeltaItemReadModel,
    DemandPlanDeltaDiagnosticReadModel,
    DemandPlanDeltaItemReadModel,
    DemandPlanDeltaReadModel,
)
from ...domain.reservable_assets import AssetRequirementOrigin
from ...domain.active_days import split_total_workforce_hours
from ...domain.availability_rules import availability_hours_for_day
from ...domain.confirmation import CONFIRMATION_CONFIRMED, normalize_confirmation
from ...domain.plan_comparison import (
    AllocationDifference,
    AllocationProjection,
    compare_allocation_plans,
)
from ...domain.planning_engine import build_allocation_plan
from ...domain.planning_projection import project_planning_snapshot
from ...domain.planning_snapshot import PlanningSnapshot
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .approval_revision_repository import SqlRequestApprovalRevisionRepository
from .asset_models import Asset, AssetAllocation, AssetRequirement, AssetType
from .approval_envelope_policy_repository import (
    SqlDemandApprovalEnvelopePolicyRepository,
)
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
)
from .models import (
    ORIGIN_REQUEST,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    WorkforceRequest,
    WorkPackage,
)
from .operational_choice_models import RequestOperationalState
from .planning_repository import SqlPlanningReadRepository
from .request_plan_preparation import SqlRequestPlanPreparer
from .web_query_repository import SqlPlannerQueryRepositoryWeb


INACTIVE_REQUIREMENT_STATUSES = {"Annulé", "Terminé"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _identifier(requirement: ResourceRequirement) -> str:
    return _text(requirement.legacy_segment_id) or requirement.id


def _allocation_projection(result: object) -> tuple[AllocationProjection, ...]:
    allocations = getattr(result, "allocations", ())
    return tuple(
        AllocationProjection(
            segment_id=row.segment_id,
            resource_id=row.resource_id,
            day=row.day,
            hours=float(row.hours),
            allocation_type=row.allocation_type,
            locked=bool(row.locked),
            outside_schedule=bool(row.outside_schedule),
        )
        for row in allocations
        if bool(getattr(row, "counts_as_allocated", True))
    )


def _item_from_difference(
    difference: AllocationDifference,
    *,
    change: str,
    resource_id_by_name: dict[str, str] | None = None,
) -> DemandPlanDeltaItemReadModel:
    current = difference.legacy_hours > 0
    proposed = difference.shadow_hours > 0
    return DemandPlanDeltaItemReadModel(
        change=change,
        segment_id=difference.segment_id,
        current_resource_name=difference.resource_id if current else None,
        proposed_resource_name=difference.resource_id if proposed else None,
        current_resource_id=(
            (resource_id_by_name or {}).get(difference.resource_id) if current else None
        ),
        proposed_resource_id=(
            (resource_id_by_name or {}).get(difference.resource_id) if proposed else None
        ),
        current_date=difference.day if current else None,
        proposed_date=difference.day if proposed else None,
        current_hours=float(difference.legacy_hours),
        proposed_hours=float(difference.shadow_hours),
        current_allocation_type=difference.allocation_type if current else None,
        proposed_allocation_type=difference.allocation_type if proposed else None,
        current_outside_standard_hours=(
            bool(difference.outside_schedule) if current else False
        ),
        proposed_outside_standard_hours=(
            bool(difference.outside_schedule) if proposed else False
        ),
        locked=bool(difference.locked),
    )


def _delta_items(
    differences: tuple[AllocationDifference, ...],
    *,
    resource_id_by_name: dict[str, str] | None = None,
) -> tuple[DemandPlanDeltaItemReadModel, ...]:
    """Turn semantic-key differences into coordinator-friendly add/move/change/cancel rows."""

    direct: list[DemandPlanDeltaItemReadModel] = []
    old_only: dict[str, list[AllocationDifference]] = defaultdict(list)
    new_only: dict[str, list[AllocationDifference]] = defaultdict(list)

    for difference in differences:
        if difference.legacy_hours > 0 and difference.shadow_hours > 0:
            direct.append(_item_from_difference(
                difference,
                change="MODIFY",
                resource_id_by_name=resource_id_by_name,
            ))
        elif difference.legacy_hours > 0:
            old_only[difference.segment_id].append(difference)
        elif difference.shadow_hours > 0:
            new_only[difference.segment_id].append(difference)

    for segment_id in sorted(set(old_only) | set(new_only)):
        old_rows = sorted(old_only.get(segment_id, []), key=lambda row: (row.day, row.resource_id))
        new_rows = sorted(new_only.get(segment_id, []), key=lambda row: (row.day, row.resource_id))
        used_new: set[int] = set()

        for old in old_rows:
            pair_index = next(
                (
                    index
                    for index, candidate in enumerate(new_rows)
                    if index not in used_new
                    and abs(float(candidate.shadow_hours) - float(old.legacy_hours)) <= 0.01
                ),
                None,
            )
            if pair_index is None:
                direct.append(_item_from_difference(
                    old,
                    change="CANCEL",
                    resource_id_by_name=resource_id_by_name,
                ))
                continue
            new = new_rows[pair_index]
            used_new.add(pair_index)
            direct.append(
                DemandPlanDeltaItemReadModel(
                    change="MOVE",
                    segment_id=segment_id,
                    current_resource_name=old.resource_id,
                    proposed_resource_name=new.resource_id,
                    current_resource_id=(resource_id_by_name or {}).get(old.resource_id),
                    proposed_resource_id=(resource_id_by_name or {}).get(new.resource_id),
                    current_date=old.day,
                    proposed_date=new.day,
                    current_hours=float(old.legacy_hours),
                    proposed_hours=float(new.shadow_hours),
                    current_allocation_type=old.allocation_type,
                    proposed_allocation_type=new.allocation_type,
                    current_outside_standard_hours=bool(old.outside_schedule),
                    proposed_outside_standard_hours=bool(new.outside_schedule),
                    locked=bool(old.locked or new.locked),
                )
            )

        for index, new in enumerate(new_rows):
            if index not in used_new:
                direct.append(_item_from_difference(
                    new,
                    change="ADD",
                    resource_id_by_name=resource_id_by_name,
                ))

    order = {"CANCEL": 0, "MOVE": 1, "MODIFY": 2, "ADD": 3}
    return tuple(
        sorted(
            direct,
            key=lambda row: (
                order.get(row.change, 9),
                row.current_date or row.proposed_date,
                row.segment_id,
            ),
        )
    )


class SqlPlannerQueryRepositoryWithPlanDelta(SqlPlannerQueryRepositoryWeb):
    """React query surface with a non-mutating preview of a pending reapproval."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._delta_session = session
        self._plan_preparer = SqlRequestPlanPreparer(session)

    def _request(self, number: str) -> WorkforceRequest | None:
        wanted = _text(number)
        if not wanted:
            return None
        return self._delta_session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )

    def _current_requirements(self, request_id: str) -> list[ResourceRequirement]:
        return list(
            self._delta_session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request_id,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.created_at, ResourceRequirement.id)
            ).all()
        )

    def _current_asset_requirements(self, request_id: str) -> list[AssetRequirement]:
        return list(
            self._delta_session.scalars(
                select(AssetRequirement)
                .where(
                    AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value,
                    AssetRequirement.workforce_request_id == request_id,
                    AssetRequirement.status != "Annulé",
                )
                .order_by(
                    AssetRequirement.approved_entry_key,
                    AssetRequirement.slot_index,
                    AssetRequirement.id,
                )
            ).all()
        )

    def _period_key_by_requirement(self, request_id: str) -> dict[str, str]:
        rows = self._delta_session.execute(
            select(WorkforceRequestPeriodRequirement, WorkforceRequestPeriod)
            .join(
                WorkforceRequestPeriod,
                WorkforceRequestPeriodRequirement.period_id == WorkforceRequestPeriod.id,
            )
            .where(WorkforceRequestPeriod.workforce_request_id == request_id)
        ).all()
        return {
            link.resource_requirement_id: period.period_key
            for link, period in rows
        }

    def _period_identity_by_requirement(
        self,
        request_id: str,
    ) -> dict[str, tuple[str, str]]:
        rows = self._delta_session.execute(
            select(WorkforceRequestPeriodRequirement, WorkforceRequestPeriod)
            .join(
                WorkforceRequestPeriod,
                WorkforceRequestPeriodRequirement.period_id == WorkforceRequestPeriod.id,
            )
            .where(WorkforceRequestPeriod.workforce_request_id == request_id)
        ).all()
        return {
            link.resource_requirement_id: (
                _text(period.request_line_id),
                period.period_key,
            )
            for link, period in rows
            if _text(period.request_line_id)
        }

    def _selected_period_ids_by_line(
        self,
        request_id: str,
    ) -> dict[tuple[str, str], str]:
        rows = self._delta_session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id == request_id
            )
        ).all()
        return {
            (row.request_line_id, row.alternative_group): row.period_id
            for row in rows
        }

    def _active_periods(self, request_id: str) -> list[WorkforceRequestPeriod]:
        return list(
            self._delta_session.scalars(
                select(WorkforceRequestPeriod)
                .where(
                    WorkforceRequestPeriod.workforce_request_id == request_id,
                    WorkforceRequestPeriod.active == true(),
                )
                .order_by(WorkforceRequestPeriod.sequence, WorkforceRequestPeriod.id)
            ).all()
        )

    def _effective_periods(
        self,
        request_id: str,
        *,
        active_periods: list[WorkforceRequestPeriod] | None = None,
    ) -> list[WorkforceRequestPeriod]:
        periods = active_periods if active_periods is not None else self._active_periods(request_id)
        selections = {
            row.alternative_group: row.period_id
            for row in self._delta_session.scalars(
                select(WorkforceRequestPeriodSelection).where(
                    WorkforceRequestPeriodSelection.workforce_request_id == request_id
                )
            ).all()
        }
        return [
            period
            for period in periods
            if period.kind == "CUMULATIVE"
            or selections.get(_text(period.alternative_group)) == period.id
        ]

    def _legacy_hours_per_resource(
        self,
        request: WorkforceRequest,
        desired: int,
        current: list[ResourceRequirement],
        proposed_resource: Resource | None,
        snapshot: PlanningSnapshot,
    ) -> float | None:
        if request.estimated_hours is not None and request.estimated_hours > 0:
            return float(
                (request.estimated_hours / Decimal(desired)).quantize(Decimal("0.01"))
            )
        if (
            request.estimated_days is not None
            and request.estimated_days > 0
            and proposed_resource is not None
            and request.desired_start is not None
        ):
            end = request.desired_end or request.desired_start
            capacities: list[float] = []
            cursor = request.desired_start
            while cursor <= end:
                capacity = availability_hours_for_day(
                    snapshot.availability,
                    proposed_resource.name,
                    cursor,
                )
                if capacity > 0:
                    capacities.append(capacity)
                cursor += timedelta(days=1)
            if capacities:
                return round(float(request.estimated_days) * (sum(capacities) / len(capacities)), 2)
        existing = [float(row.planned_hours) for row in current if row.planned_hours > 0]
        return round(sum(existing) / len(existing), 2) if existing else None

    def _segment_row(
        self,
        *,
        requirement: ResourceRequirement | None,
        request: WorkforceRequest,
        project: Project,
        resource: Resource | None,
        start_date: object,
        end_date: object,
        hours: float,
        confirmation: str,
        description: str | None,
        synthetic_id: str,
    ) -> dict[str, object]:
        assigned = resource is not None
        status = requirement.status if requirement is not None else ("Planifié" if assigned else "À assigner")
        if requirement is not None and assigned and status == "À assigner":
            status = "Planifié"
        if requirement is not None and not assigned and status not in INACTIVE_REQUIREMENT_STATUSES:
            status = "À assigner"
        return {
            "IDSegment": _identifier(requirement) if requirement is not None else synthetic_id,
            "NoDemande": _text(request.legacy_demand_number) or request.id,
            "NumeroProjet": project.number,
            "NomProjet": project.name,
            "Technicien": resource.name if resource is not None else None,
            "DateDebut": start_date,
            "DateFin": end_date,
            "HeuresPrevues": hours,
            "Statut": status,
            "Description": description,
            "SourceEffortID": requirement.source_effort_id if requirement is not None else None,
            "DateCreation": requirement.created_at if requirement is not None else f"preview:{synthetic_id}",
            "CompetenceRequise": (
                requirement.required_competency
                if requirement is not None
                else request.required_competencies
            ),
            "ClasseRessourceRequise": (
                requirement.required_resource_class
                if requirement is not None
                else None
            ),
            "TypePlanification": requirement.planning_type if requirement is not None else "Flexible",
            "Priorite": requirement.priority if requirement is not None else request.priority,
            "HorsHoraireAutorise": (
                requirement.outside_standard_hours_allowed if requirement is not None else False
            ),
            "OrigineSegment": "REQUEST",
            "Confirmation": (
                requirement.confirmation
                if requirement is not None and requirement.confirmation_overridden
                else confirmation
            ),
        }

    def _proposed_line_segments(
        self,
        request: WorkforceRequest,
        current: list[ResourceRequirement],
    ) -> list[dict[str, object]] | None:
        project = self._delta_session.get(Project, request.project_id)
        if project is None:
            return None

        lines = list(
            self._delta_session.scalars(
                select(RequestLine)
                .where(
                    RequestLine.workforce_request_id == request.id,
                    RequestLine.active == true(),
                )
                .order_by(RequestLine.position, RequestLine.id)
            ).all()
        )
        if not lines:
            return None

        resources = {
            row.id: row
            for row in self._delta_session.scalars(select(Resource)).all()
        }
        active_periods = self._active_periods(request.id)
        periods_by_line: dict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        for period in active_periods:
            if period.request_line_id:
                periods_by_line[period.request_line_id].append(period)
        selections = self._selected_period_ids_by_line(request.id)

        period_identity = self._period_identity_by_requirement(request.id)
        current_by_key: dict[tuple[str, ...], list[ResourceRequirement]] = defaultdict(list)
        for requirement in current:
            line_id = _text(requirement.source_request_line_id)
            if not line_id:
                continue
            identity = period_identity.get(requirement.id)
            key = (
                ("PERIOD", identity[0], identity[1])
                if identity is not None
                else ("LINE", line_id)
            )
            current_by_key[key].append(requirement)
        for rows in current_by_key.values():
            rows.sort(
                key=lambda row: (
                    0 if row.assigned_resource_id else 1,
                    row.created_at,
                    row.id,
                )
            )

        package_ids = {line.work_package_id for line in lines if line.work_package_id}
        packages = (
            self._delta_session.scalars(
                select(WorkPackage).where(WorkPackage.id.in_(package_ids))
            ).all()
            if package_ids
            else []
        )
        package_refs = {
            package.id: _text(package.legacy_effort_id) or package.id
            for package in packages
        }

        proposed: list[dict[str, object]] = []
        for line in lines:
            if _text(line.kind) != "WORKFORCE":
                return None
            source_effort_id = (
                package_refs.get(line.work_package_id)
                if line.work_package_id
                else None
            )
            required_competency = _text(line.required_competencies_snapshot) or None
            line_periods = periods_by_line.get(line.id, [])
            if line_periods:
                effective = [
                    period
                    for period in line_periods
                    if period.kind == "CUMULATIVE"
                    or (
                        period.alternative_group is not None
                        and selections.get(
                            (line.id, _text(period.alternative_group))
                        )
                        == period.id
                    )
                ]
                for period in effective:
                    key = ("PERIOD", line.id, period.period_key)
                    requirement = (
                        current_by_key.get(key, [None])[0]
                        if current_by_key.get(key)
                        else None
                    )
                    resource_id = (
                        requirement.assigned_resource_id
                        if requirement is not None and requirement.assigned_resource_id
                        else period.proposed_resource_id or line.proposed_resource_id
                    )
                    row = self._segment_row(
                        requirement=requirement,
                        request=request,
                        project=project,
                        resource=resources.get(resource_id),
                        start_date=period.start_date,
                        end_date=period.end_date,
                        hours=float(period.hours),
                        confirmation=normalize_confirmation(
                            period.confirmation,
                            default=CONFIRMATION_CONFIRMED,
                        ),
                        description=(
                            period.note
                            or line.description
                            or request.description
                            or "Période proposée"
                        ),
                        synthetic_id=f"PREVIEW-{line.id}-{period.period_key}",
                    )
                    proposed.append(
                        {
                            **row,
                            "SourceEffortID": source_effort_id,
                            "ClasseRessourceRequise": line.required_resource_class,
                            "CompetenceRequise": required_competency,
                            "JoursActifsCibles": period.desired_active_days,
                        }
                    )
                continue

            if line.desired_start is None or line.estimated_hours is None:
                return None
            if line.estimated_hours <= 0:
                return None
            key = ("LINE", line.id)
            requirement = (
                current_by_key.get(key, [None])[0]
                if current_by_key.get(key)
                else None
            )
            resource_id = (
                requirement.assigned_resource_id
                if requirement is not None and requirement.assigned_resource_id
                else line.proposed_resource_id
            )
            row = self._segment_row(
                requirement=requirement,
                request=request,
                project=project,
                resource=resources.get(resource_id),
                start_date=line.desired_start,
                end_date=line.desired_end or line.desired_start,
                hours=float(line.estimated_hours),
                confirmation=normalize_confirmation(
                    line.confirmation,
                    default=CONFIRMATION_CONFIRMED,
                ),
                description=(
                    line.description
                    or request.description
                    or "Besoin proposé"
                ),
                synthetic_id=f"PREVIEW-{line.id}",
            )
            proposed.append(
                {
                    **row,
                    "SourceEffortID": source_effort_id,
                    "ClasseRessourceRequise": line.required_resource_class,
                    "CompetenceRequise": required_competency,
                    "JoursActifsCibles": (
                        int(line.desired_active_days)
                        if line.desired_active_days is not None
                        else None
                    ),
                }
            )
        return proposed

    def _proposed_segments(
        self,
        request: WorkforceRequest,
        current: list[ResourceRequirement],
        snapshot: PlanningSnapshot,
    ) -> list[dict[str, object]] | None:
        del snapshot  # preparation is intentionally independent from engine capacity.
        project = self._delta_session.get(Project, request.project_id)
        if project is None:
            return None

        try:
            prepared = self._plan_preparer.prepare(
                request,
                current=current,
            )
        except ValueError:
            return None

        resources = {
            row.id: row
            for row in self._delta_session.scalars(select(Resource)).all()
        }
        matches, _obsolete = self._plan_preparer.match_current(
            request,
            current,
            prepared.specs,
        )

        proposed: list[dict[str, object]] = []
        for match in matches:
            spec = match.spec
            requirement = match.requirement
            resource_id = (
                requirement.assigned_resource_id
                if requirement is not None
                and requirement.assigned_resource_id
                else spec.proposed_resource_id
            )
            row = self._segment_row(
                requirement=requirement,
                request=request,
                project=project,
                resource=resources.get(resource_id),
                start_date=spec.start_date,
                end_date=spec.end_date,
                hours=float(spec.planned_hours),
                confirmation=spec.confirmation,
                description=spec.description,
                synthetic_id=(
                    "PREVIEW-" + "-".join(str(part) for part in spec.key)
                ),
            )
            proposed.append(
                {
                    **row,
                    "SourceEffortID": spec.source_effort_id,
                    "ClasseRessourceRequise": spec.required_resource_class,
                    "CompetenceRequise": spec.required_competency,
                    "JoursActifsCibles": spec.desired_active_days,
                }
            )
        return proposed

    def _asset_delta(
        self,
        request: WorkforceRequest,
    ) -> tuple[
        tuple[DemandAssetPlanDeltaItemReadModel, ...],
        tuple[DemandPlanDeltaDiagnosticReadModel, ...],
        bool,
    ]:
        """Preview exactly what asset materialization would retain, add or remove."""

        current = self._current_asset_requirements(request.id)
        allocations = {
            row.asset_requirement_id: row
            for row in self._delta_session.scalars(
                select(AssetAllocation).where(
                    AssetAllocation.asset_requirement_id.in_(
                        tuple(item.id for item in current)
                    )
                )
            ).all()
        } if current else {}

        try:
            envelope = SqlRequestApprovalRevisionRepository(
                self._delta_session
            ).candidate_envelope(request)
        except ValueError as exc:
            return (
                (),
                (
                    DemandPlanDeltaDiagnosticReadModel(
                        code="ASSET_CANDIDATE_INVALID",
                        message=str(exc),
                    ),
                ),
                True,
            )

        entries = tuple(
            entry
            for entry in envelope.entries
            if entry.line_kind == "ASSET"
            and (entry.group is None or entry.selected)
        )
        target_by_key = {
            (entry.identity.stable_key, slot): entry
            for entry in entries
            for slot in range(entry.slot_count)
        }
        current_by_key = {
            (row.approved_entry_key, int(row.slot_index or 0)): row
            for row in current
        }

        type_ids = {
            entry.asset_type_id
            for entry in entries
            if entry.asset_type_id
        }
        type_ids.update(row.asset_type_id for row in current)
        asset_types = {
            row.id: row
            for row in self._delta_session.scalars(
                select(AssetType).where(AssetType.id.in_(tuple(type_ids)))
            ).all()
        } if type_ids else {}

        asset_ids = {row.asset_id for row in allocations.values()}
        assets = {
            row.id: row
            for row in self._delta_session.scalars(
                select(Asset).where(Asset.id.in_(tuple(asset_ids)))
            ).all()
        } if asset_ids else {}

        diagnostics: list[DemandPlanDeltaDiagnosticReadModel] = []
        items: list[DemandAssetPlanDeltaItemReadModel] = []
        blocked = False

        for key, entry in sorted(
            target_by_key.items(),
            key=lambda item: (item[0][0], item[0][1]),
        ):
            entry_key, slot_index = key
            target_type = asset_types.get(entry.asset_type_id or "")
            if (
                target_type is None
                or not target_type.active
                or target_type.occupancy_policy != entry.occupancy_policy
            ):
                diagnostics.append(
                    DemandPlanDeltaDiagnosticReadModel(
                        code="ASSET_TYPE_INACTIVE_OR_INCOMPATIBLE",
                        message=(
                            "Le type d'actif candidat est absent, inactif ou "
                            "incompatible avec l'occupation approuvée."
                        ),
                        spec_key=("ASSET", entry_key, str(slot_index)),
                    )
                )
                blocked = True

            existing = current_by_key.get(key)
            if existing is None:
                items.append(
                    DemandAssetPlanDeltaItemReadModel(
                        change="ADD",
                        approved_entry_key=entry_key,
                        slot_index=slot_index,
                        proposed_asset_type_id=entry.asset_type_id,
                        proposed_start_date=entry.start_date,
                        proposed_end_date=entry.end_date,
                        proposed_usage_hours=(
                            float(entry.hours) if entry.hours is not None else None
                        ),
                    )
                )
                continue

            allocation = allocations.get(existing.id)
            asset = (
                assets.get(allocation.asset_id)
                if allocation is not None
                else None
            )
            allocation_compatible = bool(
                allocation is not None
                and asset is not None
                and asset.active
                and asset.asset_type_id == entry.asset_type_id
                and entry.start_date <= allocation.start_date
                and allocation.end_date <= entry.end_date
            )
            if (
                allocation is not None
                and allocation.locked
                and not allocation_compatible
            ):
                diagnostics.append(
                    DemandPlanDeltaDiagnosticReadModel(
                        code="LOCKED_ASSET_ALLOCATION_INCOMPATIBLE",
                        message=(
                            "La réapprobation rendrait une réservation d'actif "
                            "verrouillée incompatible."
                        ),
                        requirement_id=existing.id,
                        spec_key=("ASSET", entry_key, str(slot_index)),
                    )
                )
                blocked = True

            current_hours = (
                float(existing.usage_hours)
                if existing.usage_hours is not None
                else None
            )
            proposed_hours = (
                float(entry.hours) if entry.hours is not None else None
            )
            changed = (
                existing.asset_type_id != entry.asset_type_id
                or existing.start_date != entry.start_date
                or existing.end_date != entry.end_date
                or current_hours != proposed_hours
            )
            if changed:
                items.append(
                    DemandAssetPlanDeltaItemReadModel(
                        change="MODIFY",
                        approved_entry_key=entry_key,
                        slot_index=slot_index,
                        current_requirement_id=existing.id,
                        current_asset_type_id=existing.asset_type_id,
                        proposed_asset_type_id=entry.asset_type_id,
                        current_start_date=existing.start_date,
                        current_end_date=existing.end_date,
                        proposed_start_date=entry.start_date,
                        proposed_end_date=entry.end_date,
                        current_usage_hours=current_hours,
                        proposed_usage_hours=proposed_hours,
                        current_asset_id=(
                            allocation.asset_id if allocation is not None else None
                        ),
                        proposed_asset_id=(
                            allocation.asset_id
                            if allocation_compatible and allocation is not None
                            else None
                        ),
                        allocation_preserved=allocation_compatible,
                        locked=bool(allocation is not None and allocation.locked),
                    )
                )

        for key, existing in sorted(
            current_by_key.items(),
            key=lambda item: (item[0][0], item[0][1]),
        ):
            if key in target_by_key:
                continue
            allocation = allocations.get(existing.id)
            if allocation is not None and allocation.locked:
                diagnostics.append(
                    DemandPlanDeltaDiagnosticReadModel(
                        code="LOCKED_ASSET_REQUIREMENT_REMOVAL",
                        message=(
                            "Une réservation d'actif verrouillée empêche de retirer "
                            "le besoin lors de la réapprobation."
                        ),
                        requirement_id=existing.id,
                        spec_key=("ASSET", key[0], str(key[1])),
                    )
                )
                blocked = True
            items.append(
                DemandAssetPlanDeltaItemReadModel(
                    change="CANCEL",
                    approved_entry_key=key[0],
                    slot_index=key[1],
                    current_requirement_id=existing.id,
                    current_asset_type_id=existing.asset_type_id,
                    current_start_date=existing.start_date,
                    current_end_date=existing.end_date,
                    current_usage_hours=(
                        float(existing.usage_hours)
                        if existing.usage_hours is not None
                        else None
                    ),
                    current_asset_id=(
                        allocation.asset_id if allocation is not None else None
                    ),
                    locked=bool(allocation is not None and allocation.locked),
                )
            )

        order = {"CANCEL": 0, "MODIFY": 1, "ADD": 2}
        return (
            tuple(
                sorted(
                    items,
                    key=lambda item: (
                        order.get(item.change, 9),
                        item.approved_entry_key,
                        item.slot_index,
                    ),
                )
            ),
            tuple(diagnostics),
            blocked,
        )

    @staticmethod
    def _decoded_mapping(value: str) -> dict[str, str]:
        decoded = json.loads(value or "{}")
        if not isinstance(decoded, dict):
            raise ValueError("L'état opérationnel sérialisé est invalide.")
        return {
            _text(key): _text(item)
            for key, item in decoded.items()
            if _text(key) and _text(item)
        }

    def demand_approval_state(
        self,
        number: str,
    ) -> DemandApprovalStateReadModel | None:
        request = self._request(number)
        if request is None:
            return None

        demand_number = _text(request.legacy_demand_number) or request.id
        diagnostics: list[str] = []
        revisions = SqlRequestApprovalRevisionRepository(self._delta_session)

        candidate_fingerprint: str | None = None
        decision_name: str | None = None
        decision_reason: str | None = None
        decision_changes: tuple[dict[str, object], ...] = ()
        try:
            candidate = revisions.candidate_envelope(request)
            candidate_fingerprint = candidate.authorization_fingerprint
            decision = SqlDemandApprovalEnvelopePolicyRepository(
                self._delta_session
            ).evaluate_candidate(demand_number)
            decision_name = decision.decision
            decision_reason = decision.reason
            decision_changes = tuple(
                dict(row) for row in decision.to_dict()["changes"]
            )
        except ValueError as exc:
            decision_name = "INVALID"
            decision_reason = "INVALID"
            decision_changes = ({"code": "INVALID", "detail": str(exc)},)
            diagnostics.append(f"CANDIDATE_INVALID: {exc}")

        reference = self._delta_session.get(RequestApprovalReference, request.id)
        reference_status = reference.status if reference is not None else None
        revision = (
            self._delta_session.get(
                RequestApprovalRevision,
                reference.active_revision_id,
            )
            if reference is not None and reference.active_revision_id
            else None
        )
        if (
            reference is not None
            and reference.active_revision_id
            and revision is None
        ):
            diagnostics.append("ACTIVE_APPROVAL_REVISION_MISSING")

        selections: dict[str, str] = {}
        confirmations: dict[str, str] = {}
        budget_overrides: dict[str, float] = {}
        operational_version: int | None = None
        if revision is not None:
            state = self._delta_session.get(RequestOperationalState, request.id)
            if state is None:
                diagnostics.append("OPERATIONAL_STATE_MISSING")
            elif state.approval_revision_id != revision.id:
                diagnostics.append("OPERATIONAL_STATE_REVISION_MISMATCH")
            else:
                operational_version = max(int(state.version or 1), 1)
                try:
                    selections = self._decoded_mapping(state.selections_text)
                    confirmations = self._decoded_mapping(
                        state.confirmations_text
                    )
                    budget_overrides = {
                        key: float(value)
                        for key, value in self._decoded_mapping(
                            state.budget_overrides_text
                        ).items()
                    }
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    diagnostics.append(f"OPERATIONAL_STATE_INVALID: {exc}")

        current = [
            row
            for row in self._current_requirements(request.id)
            if row.origin == ORIGIN_REQUEST
        ]
        current_assets = self._current_asset_requirements(request.id)
        active_keys = tuple(
            sorted(
                {
                    *(
                        _text(row.approved_entry_key)
                        for row in current
                        if _text(row.approved_entry_key)
                    ),
                    *(
                        _text(row.approved_entry_key)
                        for row in current_assets
                        if _text(row.approved_entry_key)
                    ),
                }
            )
        )
        active_matches: bool | None = None
        if revision is not None:
            active_matches = bool(current or current_assets) and all(
                row.approval_revision_id == revision.id
                and row.approval_reference_status == APPROVAL_REFERENCE_CAPTURED
                and bool(_text(row.approved_entry_key))
                for row in current
            ) and all(
                row.approval_revision_id == revision.id
                and bool(_text(row.approved_entry_key))
                for row in current_assets
            )

        approved_fingerprint = (
            revision.authorization_fingerprint
            if revision is not None
            else None
        )
        return DemandApprovalStateReadModel(
            demand_number=demand_number,
            candidate_request_version=max(
                int(request.aggregate_version or 1),
                1,
            ),
            approval_reference_status=reference_status,
            active_revision_id=revision.id if revision is not None else None,
            previous_revision_id=(
                revision.previous_revision_id
                if revision is not None
                else None
            ),
            approved_request_version=(
                revision.request_version
                if revision is not None
                else None
            ),
            approved_at=revision.approved_at if revision is not None else None,
            approved_by_name=(
                revision.approved_by_name
                if revision is not None
                else None
            ),
            authorization_fingerprint=approved_fingerprint,
            candidate_authorization_fingerprint=candidate_fingerprint,
            candidate_matches_approved=(
                candidate_fingerprint == approved_fingerprint
                if candidate_fingerprint is not None
                and approved_fingerprint is not None
                else None
            ),
            payload_format_version=(
                revision.payload_format_version
                if revision is not None
                else None
            ),
            operational_version=operational_version,
            envelope_decision=decision_name,
            envelope_reason=decision_reason,
            envelope_changes=decision_changes,
            active_selections=selections,
            active_confirmations=confirmations,
            active_budget_overrides=budget_overrides,
            active_requirement_count=len(current),
            active_planned_hours=round(
                sum(float(row.planned_hours) for row in current),
                2,
            ),
            active_asset_requirement_count=len(current_assets),
            active_asset_usage_hours=round(
                sum(
                    float(row.usage_hours)
                    for row in current_assets
                    if row.usage_hours is not None
                ),
                2,
            ),
            active_asset_unbudgeted_requirement_count=sum(
                1 for row in current_assets if row.usage_hours is None
            ),
            active_approved_entry_keys=active_keys,
            active_matches_approved_revision=active_matches,
            diagnostics=tuple(diagnostics),
        )

    def demand_plan_delta(self, number: str) -> DemandPlanDeltaReadModel | None:
        request = self._request(number)
        if request is None:
            return None
        demand_number = _text(request.legacy_demand_number) or request.id
        approval_state = self.demand_approval_state(demand_number)

        def delta(**values: object) -> DemandPlanDeltaReadModel:
            values.setdefault("demand_number", demand_number)
            if approval_state is not None:
                values.setdefault(
                    "approval_reference_status",
                    approval_state.approval_reference_status,
                )
                values.setdefault(
                    "active_revision_id",
                    approval_state.active_revision_id,
                )
                values.setdefault(
                    "approved_request_version",
                    approval_state.approved_request_version,
                )
                values.setdefault(
                    "authorization_fingerprint",
                    approval_state.authorization_fingerprint,
                )
                values.setdefault(
                    "candidate_authorization_fingerprint",
                    approval_state.candidate_authorization_fingerprint,
                )
                values.setdefault(
                    "operational_version",
                    approval_state.operational_version,
                )
                values.setdefault(
                    "envelope_decision",
                    approval_state.envelope_decision,
                )
                values.setdefault(
                    "envelope_reason",
                    approval_state.envelope_reason,
                )
            return DemandPlanDeltaReadModel(**values)

        current_requirements = self._current_requirements(request.id)
        current_asset_requirements = self._current_asset_requirements(request.id)
        if request.status != "Soumise":
            return delta(
                demand_number=demand_number,
                available=False,
                reason="NOT_SUBMITTED",
                has_changes=False,
            )
        if not current_requirements and not current_asset_requirements:
            return delta(
                demand_number=demand_number,
                available=False,
                reason="NO_CURRENT_PLAN",
                has_changes=False,
            )

        asset_items, asset_diagnostics, asset_blocked = self._asset_delta(request)
        asset_counts = {
            name: sum(1 for item in asset_items if item.change == name)
            for name in ("ADD", "MODIFY", "CANCEL")
        }
        if asset_blocked:
            return delta(
                demand_number=demand_number,
                available=False,
                reason=(
                    asset_diagnostics[0].code
                    if asset_diagnostics
                    else "ASSET_PLAN_INCOMPATIBLE"
                ),
                has_changes=bool(asset_items),
                asset_add_count=asset_counts["ADD"],
                asset_modify_count=asset_counts["MODIFY"],
                asset_cancel_count=asset_counts["CANCEL"],
                asset_items=asset_items,
                diagnostics=asset_diagnostics,
            )

        try:
            prepared = self._plan_preparer.prepare(
                request,
                current=current_requirements,
            )
        except ValueError:
            prepared = None

        if prepared is not None and not current_requirements and not prepared.specs:
            return delta(
                demand_number=demand_number,
                available=True,
                reason=None,
                has_changes=bool(asset_items),
                asset_add_count=asset_counts["ADD"],
                asset_modify_count=asset_counts["MODIFY"],
                asset_cancel_count=asset_counts["CANCEL"],
                asset_items=asset_items,
                diagnostics=asset_diagnostics,
            )

        snapshot = SqlPlanningReadRepository(self._delta_session).capture()
        current_calculation = project_planning_snapshot(snapshot)
        if current_calculation.unsupported_segment_ids:
            return delta(
                demand_number=demand_number,
                available=False,
                reason="CURRENT_PLAN_UNSUPPORTED",
                has_changes=False,
            )

        if prepared is not None:
            locked_conflicts = self._plan_preparer.locked_conflicts(
                request,
                current_requirements,
                prepared.specs,
                target_project_id=request.project_id,
            )
            if locked_conflicts:
                return delta(
                    demand_number=demand_number,
                    available=False,
                    reason=locked_conflicts[0].code,
                    has_changes=False,
                    diagnostics=tuple(
                        DemandPlanDeltaDiagnosticReadModel(
                            code=conflict.code,
                            message=conflict.message,
                            requirement_id=conflict.requirement_id,
                            spec_key=conflict.spec_key,
                            shift_ids=conflict.shift_ids,
                        )
                        for conflict in locked_conflicts
                    ),
                )

        proposed_rows = self._proposed_segments(request, current_requirements, snapshot)
        if proposed_rows is None:
            return delta(
                demand_number=demand_number,
                available=False,
                reason="PROPOSAL_INCOMPLETE",
                has_changes=False,
            )

        remaining_segments = [
            row
            for row in snapshot.segments
            if _text(row.get("NoDemande")) != demand_number
        ]
        proposed_snapshot = PlanningSnapshot.capture(
            segments=[*remaining_segments, *proposed_rows],
            demands=snapshot.demands,
            allocations=snapshot.allocations,
            availability=snapshot.availability,
            technicians=snapshot.technicians,
        )
        proposed_calculation = project_planning_snapshot(proposed_snapshot)
        if proposed_calculation.unsupported_segment_ids:
            return delta(
                demand_number=demand_number,
                available=False,
                reason="PROPOSED_PLAN_UNSUPPORTED",
                has_changes=False,
            )

        proposed_result = build_allocation_plan(
            proposed_calculation.segments,
            proposed_calculation.locked_allocations,
            proposed_calculation.capacity_by_resource_day,
            preserve_locked_segment_ids=proposed_calculation.preserved_segment_ids,
            outside_schedule_eligible_by_resource_day=(
                proposed_calculation.outside_schedule_eligible_by_resource_day
            ),
        )
        comparison = compare_allocation_plans(
            current_calculation.persisted_allocations,
            _allocation_projection(proposed_result),
        )
        resource_id_by_name = {
            resource.name: resource.id
            for resource in self._delta_session.scalars(select(Resource)).all()
        }
        items = _delta_items(
            comparison.differences,
            resource_id_by_name=resource_id_by_name,
        )
        counts = {
            name: sum(1 for item in items if item.change == name)
            for name in ("ADD", "MODIFY", "MOVE", "CANCEL")
        }
        current_hours = round(sum(item.current_hours for item in items), 2)
        proposed_hours = round(sum(item.proposed_hours for item in items), 2)
        return delta(
            demand_number=demand_number,
            available=True,
            reason=None,
            has_changes=bool(items or asset_items),
            add_count=counts["ADD"],
            modify_count=counts["MODIFY"],
            move_count=counts["MOVE"],
            cancel_count=counts["CANCEL"],
            current_hours=current_hours,
            proposed_hours=proposed_hours,
            net_hours=round(proposed_hours - current_hours, 2),
            items=items,
            asset_add_count=asset_counts["ADD"],
            asset_modify_count=asset_counts["MODIFY"],
            asset_cancel_count=asset_counts["CANCEL"],
            asset_items=asset_items,
            diagnostics=asset_diagnostics,
        )
