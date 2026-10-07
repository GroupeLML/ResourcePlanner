from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
import unicodedata

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session, aliased

from ...application.query_models import (
    AssetPlanningWindowReadModel,
    AssetRequirementReadModel,
    DemandCancellationMaterializationReadModel,
    DemandMaterializedRequirementReadModel,
    DemandMaterializedResourceReadModel,
    MediumTermUnlinkedSegmentReadModel,
    PendingDemandLoadReadModel,
    PlanningActionReadModel,
    PlanningCapacityGridReadModel,
    PlanningDayCapacityReadModel,
    PlanningResourceCapacityReadModel,
    PlanningSegmentCapacityDiagnosticReadModel,
    PlanningSnapshotReadModel,
    ProjectReadModel,
    ResourceReadModel,
    ResourceRecommendationReadModel,
    ShiftAssetActionReadModel,
    ShiftAssetActionsReadModel,
    ShiftAssetReservationReadModel,
    ShiftReadModel,
)
from ...application.query_ports import PlannerQueryPort
from ...application.read_models import DemandPeriodReadModel, DemandReadModel, SegmentReadModel
from ...domain.availability_rules import availability_hours_for_day, availability_state_for_day
from ...domain.confirmation import effective_confirmation
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from ...domain.resource_recommendations import (
    COMPETENCY_MISSING,
    COMPETENCY_NOT_REQUIRED,
    COMPETENCY_SATISFIED,
    COMPETENCY_UNRESOLVED,
    RecommendationCandidate,
    rank_recommendation_candidates,
)
from ...domain.reservable_assets import AssetRequirementOrigin
from ...domain.demand_periods import (
    DemandPeriodDefinition,
    projected_hours_in_window,
    projected_hours_without_double_counting,
    projected_period_hours_in_window_without_double_counting,
)
from ...domain.workload import (
    PENDING_LOAD_ADDITIVE,
    WorkloadTotals,
    pending_load_mode,
    workload_kind,
)
from .availability_class_scope import availability_class_codes_by_rule
from .asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
)
from .asset_qualification import (
    QUALIFICATION_MISSING_OPERATOR,
    QUALIFICATION_NO_OVERLAP,
    QUALIFICATION_SATISFIED,
    QUALIFICATION_SKILL_MISMATCH,
)
from .asset_query import SqlAssetPlanningQuery
from .asset_shift_projection import (
    ASSOCIATION_OWNED_SHIFT,
    asset_shift_association,
)
from .demand_period_repository import SqlDemandPeriodRepository
from .demand_repository import SqlDemandRepository
from .models import (
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
)
from .segment_repository import SqlSegmentRepository
from .planning_version import SqlPlanningMutationVersionRepository


INACTIVE_PROJECT_STATUSES = {
    "annulé",
    "annule",
    "fermé",
    "ferme",
    "terminé",
    "termine",
    "closed",
    "cancelled",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value_text = _text(value)
    return value_text or None


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", _text(value)).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.casefold().split())


def _split_competencies(value: object) -> tuple[str, ...]:
    raw = _text(value)
    if not raw:
        return ()
    return tuple(
        part.strip()
        for part in raw.replace(",", ";").split(";")
        if part.strip()
    )


def _availability_record(
    rule: ResourceAvailabilityRule,
    resource_class_codes: tuple[str, ...] = (),
) -> dict[str, object]:
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


def _demand_overlaps(row: DemandReadModel, start: date, end: date) -> bool:
    if row.desired_end is not None and row.desired_end < start:
        return False
    if row.desired_start is not None and row.desired_start > end:
        return False
    return True


def _period_definition(row: DemandPeriodReadModel) -> DemandPeriodDefinition:
    return DemandPeriodDefinition(
        period_id=row.period_id,
        start_date=row.start_date,
        end_date=row.end_date,
        hours=row.hours,
        kind=row.kind,
        alternative_group=row.alternative_group,
        confirmation=row.confirmation,
        proposed_resource=row.proposed_resource,
        resource_count=row.resource_count,
        note=row.note,
    )


def _resource_read_model(resource: Resource, competency_ids: tuple[str, ...] = ()) -> ResourceReadModel:
    return ResourceReadModel(
        id=resource.id,
        name=resource.name,
        email=_optional_text(resource.email),
        resource_class=_optional_text(resource.resource_class),
        competencies=_optional_text(resource.competencies),
        competency_ids=competency_ids,
        note=_optional_text(resource.note),
        active=bool(resource.active),
        sort_order=int(resource.sort_order or 0),
        external_id=_optional_text(resource.external_id),
        erp_status=_optional_text(resource.erp_status),
        erp_active=bool(resource.erp_active),
        erp_department_description=_optional_text(resource.erp_department_description),
        erp_department_code=_optional_text(resource.erp_department_code),
        erp_employee_class=_optional_text(resource.erp_employee_class),
        erp_supervisor_external_id=_optional_text(resource.erp_supervisor_external_id),
        erp_phone=_optional_text(resource.erp_phone),
        erp_branch_code=_optional_text(resource.erp_branch_code),
        erp_contact_id=resource.erp_contact_id,
    )


class SqlPlannerQueryRepository(PlannerQueryPort):
    """Read-only SQL projection for the future web frontend."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._demands = SqlDemandRepository(session)
        self._periods = SqlDemandPeriodRepository(session)
        self._segments = SqlSegmentRepository(session)

    def _resource_competency_ids_by_resource(
        self,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        identifiers = tuple(str(value) for value in resource_ids if str(value))
        if not identifiers:
            return {}
        grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
        rows = self._session.execute(
            select(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            )
            .where(ResourceCompetency.resource_id.in_(identifiers))
            .order_by(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            )
        ).all()
        for resource_id, competency_id in rows:
            grouped.setdefault(resource_id, []).append(competency_id)
        return {
            resource_id: tuple(competency_ids)
            for resource_id, competency_ids in grouped.items()
        }

    def list_projects(
        self,
        *,
        active_only: bool = False,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[ProjectReadModel, ...]:
        statement = select(Project)
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            statement = statement.where(Project.id.in_(identifiers))
        rows = self._session.scalars(statement.order_by(Project.number)).all()
        result: list[ProjectReadModel] = []
        for project in rows:
            status = _text(project.status) or "active"
            active = status.casefold() not in INACTIVE_PROJECT_STATUSES
            if active_only and not active:
                continue
            result.append(
                ProjectReadModel(
                    id=project.id,
                    number=project.number,
                    name=project.name,
                    client=_optional_text(project.client),
                    project_manager=_optional_text(project.project_manager_name),
                    status=status,
                    active=active,
                    erp_external_id=_optional_text(project.erp_external_id),
                )
            )
        return tuple(result)

    def list_resources(self, *, active_only: bool = True) -> tuple[ResourceReadModel, ...]:
        statement = select(Resource)
        if active_only:
            statement = statement.where(
                Resource.active == true(),
                Resource.erp_active == true(),
            )
        rows = self._session.scalars(
            statement.order_by(Resource.sort_order, Resource.name)
        ).all()
        competency_ids = self._resource_competency_ids_by_resource(
            tuple(resource.id for resource in rows)
        )
        return tuple(
            _resource_read_model(resource, competency_ids.get(resource.id, ()))
            for resource in rows
        )

    def list_schedulable_resources(
        self,
        *,
        start: date,
        end: date,
    ) -> tuple[ResourceReadModel, ...]:
        """Return active resources whose standard-schedule envelope overlaps the window."""

        if end < start:
            start, end = end, start
        scheduled_ids = select(ResourceAvailabilityRule.resource_id).where(
            ResourceAvailabilityRule.availability_type == "Horaire standard",
            ResourceAvailabilityRule.active == true(),
            ResourceAvailabilityRule.resource_id.is_not(None),
            or_(
                ResourceAvailabilityRule.start_date.is_(None),
                ResourceAvailabilityRule.start_date <= end,
            ),
            or_(
                ResourceAvailabilityRule.end_date.is_(None),
                ResourceAvailabilityRule.end_date >= start,
            ),
        )
        rows = self._session.scalars(
            select(Resource)
            .where(
                Resource.active == true(),
                Resource.erp_active == true(),
                Resource.id.in_(scheduled_ids),
            )
            .order_by(Resource.sort_order, Resource.name)
        ).all()
        competency_ids = self._resource_competency_ids_by_resource(
            tuple(resource.id for resource in rows)
        )
        return tuple(
            _resource_read_model(resource, competency_ids.get(resource.id, ()))
            for resource in rows
        )

    def list_demands(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> tuple[DemandReadModel, ...]:
        return tuple(
            self._demands.list(
                project_ids=project_ids,
                demand_ids=demand_ids,
            )
        )

    def list_demands_with_cancellation_materialization(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> tuple[
        tuple[DemandReadModel, DemandCancellationMaterializationReadModel],
        ...,
    ]:
        return tuple(
            self._demands.list_with_cancellation_materialization(
                project_ids=project_ids,
                demand_ids=demand_ids,
            )
        )

    def get_demand(self, number: str) -> DemandReadModel | None:
        return self._demands.get(number)

    def get_demand_with_cancellation_materialization(
        self,
        number: str,
    ) -> tuple[DemandReadModel, DemandCancellationMaterializationReadModel] | None:
        return self._demands.get_with_cancellation_materialization(number)

    def demand_cancellation_materialization(
        self,
        number: str,
    ) -> DemandCancellationMaterializationReadModel:
        row = self.get_demand_with_cancellation_materialization(number)
        if row is not None:
            return row[1]
        return DemandCancellationMaterializationReadModel(
            demand_number=_text(number),
        )

    def list_demand_cancellation_materializations(
        self,
        numbers: Sequence[str],
    ) -> tuple[DemandCancellationMaterializationReadModel, ...]:
        wanted = tuple(dict.fromkeys(_text(value) for value in numbers if _text(value)))
        if not wanted:
            return ()
        pairs = self.list_demands_with_cancellation_materialization()
        by_number = {
            demand.number: materialization
            for demand, materialization in pairs
        }
        return tuple(
            by_number[number]
            for number in wanted
            if number in by_number
        )

    def list_demand_periods(
        self,
        number: str,
        *,
        request_line_id: str | None = None,
    ) -> tuple[DemandPeriodReadModel, ...]:
        return tuple(
            self._periods.list_for_demand(
                number,
                request_line_id=request_line_id,
            )
        )

    def list_demand_requirements(
        self,
        number: str,
    ) -> tuple[DemandMaterializedRequirementReadModel, ...]:
        """Project the active materialized plan for one demand without N+1 reads."""

        wanted = _text(number)
        if not wanted:
            return ()
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            return ()

        requirements = tuple(
            self._session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.origin == "REQUEST",
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(
                    ResourceRequirement.start_date,
                    ResourceRequirement.created_at,
                    ResourceRequirement.id,
                )
            ).all()
        )
        if not requirements:
            return ()

        requirement_ids = tuple(row.id for row in requirements)
        shifts = tuple(
            self._session.scalars(
                select(Shift)
                .where(
                    Shift.resource_requirement_id.in_(requirement_ids),
                    (Shift.allocation_type.is_(None))
                    | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                )
                .order_by(
                    Shift.resource_requirement_id,
                    Shift.work_date,
                    Shift.resource_id,
                    Shift.id,
                )
            ).all()
        )
        resource_ids = {
            row.resource_id for row in shifts if _text(row.resource_id)
        }
        resource_ids.update(
            row.assigned_resource_id
            for row in requirements
            if _text(row.assigned_resource_id)
        )
        resources = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(Resource).where(Resource.id.in_(tuple(resource_ids)))
                ).all()
            }
            if resource_ids
            else {}
        )

        shifts_by_requirement: dict[str, list[Shift]] = {}
        for shift in shifts:
            shifts_by_requirement.setdefault(
                shift.resource_requirement_id,
                [],
            ).append(shift)

        result: list[DemandMaterializedRequirementReadModel] = []
        for requirement in requirements:
            grouped: dict[str, list[float]] = {}
            for shift in shifts_by_requirement.get(requirement.id, ()):
                values = grouped.setdefault(shift.resource_id, [0.0, 0.0])
                values[0] += float(shift.hours)
                if shift.locked:
                    values[1] += float(shift.hours)

            mobilized = tuple(
                DemandMaterializedResourceReadModel(
                    resource_id=resource_id,
                    resource_name=(
                        resources[resource_id].name
                        if resource_id in resources
                        else resource_id
                    ),
                    allocated_hours=round(values[0], 2),
                    locked_hours=round(values[1], 2),
                )
                for resource_id, values in sorted(
                    grouped.items(),
                    key=lambda item: (
                        resources[item[0]].name
                        if item[0] in resources
                        else item[0],
                        item[0],
                    ),
                )
            )
            covered = round(sum(row.allocated_hours for row in mobilized), 2)
            locked = round(sum(row.locked_hours for row in mobilized), 2)
            planned = round(float(requirement.planned_hours), 2)
            target = resources.get(requirement.assigned_resource_id)
            result.append(
                DemandMaterializedRequirementReadModel(
                    requirement_id=requirement.id,
                    segment_id=_text(requirement.legacy_segment_id) or requirement.id,
                    source_request_line_id=_optional_text(
                        requirement.source_request_line_id
                    ),
                    status=_text(requirement.status),
                    start_date=requirement.start_date,
                    end_date=requirement.end_date,
                    planned_hours=planned,
                    covered_hours=covered,
                    locked_hours=locked,
                    remaining_hours=round(max(planned - covered, 0.0), 2),
                    excess_hours=round(max(covered - planned, 0.0), 2),
                    automatic_target_resource_id=_optional_text(
                        requirement.assigned_resource_id
                    ),
                    automatic_target_resource_name=(
                        _optional_text(target.name) if target is not None else None
                    ),
                    mobilized_resources=mobilized,
                    approval_revision_id=_optional_text(
                        requirement.approval_revision_id
                    ),
                    approved_entry_key=_optional_text(
                        requirement.approved_entry_key
                    ),
                    approval_reference_status=_optional_text(
                        requirement.approval_reference_status
                    ),
                )
            )
        return tuple(result)

    def list_demand_asset_requirements(
        self,
        number: str,
    ) -> tuple[AssetRequirementReadModel, ...]:
        return SqlAssetPlanningQuery(self._session).list_demand_requirements(number)

    def asset_planning_window(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        context_resource_ids: Sequence[str] = (),
    ) -> AssetPlanningWindowReadModel:
        return SqlAssetPlanningQuery(self._session).planning_window(
            start=start,
            end=end,
            project_ids=project_ids,
            context_resource_ids=context_resource_ids,
        )

    def list_pending_loads(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[PendingDemandLoadReadModel, ...]:
        """Project submitted requests without mutating or double-counting approved work."""

        statement = select(WorkforceRequest).where(WorkforceRequest.status == "Soumise")
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            statement = statement.where(WorkforceRequest.project_id.in_(identifiers))
        requests = self._session.scalars(
            statement.order_by(
                WorkforceRequest.desired_start,
                WorkforceRequest.legacy_demand_number,
                WorkforceRequest.id,
            )
        ).all()
        result: list[PendingDemandLoadReadModel] = []
        demands_by_number = {
            demand.number: demand
            for demand in self._demands.list(project_ids=project_ids)
        }

        for request in requests:
            number = _text(request.legacy_demand_number) or request.id
            demand = demands_by_number.get(number)
            if demand is None:
                continue

            periods = tuple(self._periods.list_for_demand(number))
            if demand.line_mode:
                active_line_ids = {
                    line.line_id for line in demand.lines if line.active
                }
                periods = tuple(
                    period
                    for period in periods
                    if period.request_line_id in active_line_ids
                )
                periods_by_line: dict[str, list[DemandPeriodReadModel]] = {}
                for period in periods:
                    if period.request_line_id:
                        periods_by_line.setdefault(period.request_line_id, []).append(period)

                windows: list[tuple[date, date]] = []
                projected_total = 0.0
                window_total = 0.0
                for line in demand.lines:
                    if not line.active:
                        continue
                    if line.kind == "ASSET":
                        continue  # A physical reservation is not human workload.
                    line_periods = periods_by_line.get(line.line_id, [])
                    if line_periods:
                        definitions = tuple(
                            _period_definition(row) for row in line_periods
                        )
                        selections = {
                            _text(row.alternative_group): row.period_id
                            for row in line_periods
                            if row.selected and _text(row.alternative_group)
                        }
                        line_start = min(row.start_date for row in line_periods)
                        line_end = max(row.end_date for row in line_periods)
                        projected_total += projected_hours_without_double_counting(
                            definitions,
                            selections,
                        )
                        window_total += projected_period_hours_in_window_without_double_counting(
                            definitions,
                            start,
                            end,
                            selections,
                        )
                    else:
                        line_start = line.desired_start
                        if line_start is None:
                            continue
                        line_end = line.desired_end or line_start
                        if line.estimated_hours is not None:
                            projected_total += float(line.estimated_hours)
                            window_total += projected_hours_in_window(
                                line.estimated_hours,
                                line_start,
                                line_end,
                                start,
                                end,
                            )
                    windows.append((line_start, line_end))

                if not windows:
                    continue
                proposal_start = min(row[0] for row in windows)
                proposal_end = max(row[1] for row in windows)
                if not any(
                    line_end >= start and line_start <= end
                    for line_start, line_end in windows
                ):
                    continue
                projected_hours = round(projected_total, 2)
                window_hours = round(window_total, 2)
            elif periods:
                definitions = tuple(_period_definition(row) for row in periods)
                selections = {
                    _text(row.alternative_group): row.period_id
                    for row in periods
                    if row.selected and _text(row.alternative_group)
                }
                proposal_start = min(row.start_date for row in periods)
                proposal_end = max(row.end_date for row in periods)
                if proposal_end < start or proposal_start > end:
                    continue
                projected_hours = projected_hours_without_double_counting(
                    definitions,
                    selections,
                )
                window_hours = projected_period_hours_in_window_without_double_counting(
                    definitions,
                    start,
                    end,
                    selections,
                )
            else:
                proposal_start = demand.desired_start
                if proposal_start is None:
                    continue
                proposal_end = demand.desired_end or proposal_start
                if proposal_end < start or proposal_start > end:
                    continue
                projected_hours = demand.estimated_hours
                window_hours = (
                    projected_hours_in_window(
                        projected_hours,
                        proposal_start,
                        proposal_end,
                        start,
                        end,
                    )
                    if projected_hours is not None
                    else 0.0
                )

            current = self._session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.status != "Annulé",
                )
            ).all()
            current_plan_hours = round(
                sum(
                    projected_hours_in_window(
                        float(requirement.planned_hours),
                        requirement.start_date,
                        requirement.end_date,
                        start,
                        end,
                    )
                    for requirement in current
                ),
                2,
            )
            mode = pending_load_mode(has_current_plan=bool(current))
            delta_hours = (
                round(window_hours - current_plan_hours, 2)
                if projected_hours is not None
                else None
            )
            result.append(
                PendingDemandLoadReadModel(
                    demand_number=number,
                    project_number=demand.project_number,
                    project_name=demand.project_name,
                    start_date=proposal_start,
                    end_date=proposal_end,
                    projected_hours=projected_hours,
                    window_hours=window_hours,
                    mode=mode,
                    current_plan_hours=current_plan_hours,
                    delta_hours=delta_hours,
                    resource_count=demand.resource_count,
                    required_competencies=demand.required_competencies,
                    proposed_resource=demand.proposed_resource,
                    work_package_ref=demand.work_package_ref,
                    confirmation=demand.confirmation,
                    periods=periods,
                )
            )

        return tuple(result)

    def list_medium_term_unlinked_segments(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[MediumTermUnlinkedSegmentReadModel, ...]:
        """Expose active segments with no valid medium-term WorkPackage classification."""

        if end < start:
            start, end = end, start

        statement = (
            select(ResourceRequirement, Project, WorkforceRequest, Resource)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(
                WorkforceRequest,
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
            )
            .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
            .where(
                ResourceRequirement.status.notin_(("Annulé", "Terminé")),
                ResourceRequirement.end_date >= start,
                ResourceRequirement.start_date <= end,
            )
        )
        work_package_statement = select(WorkPackage)
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            statement = statement.where(ResourceRequirement.project_id.in_(identifiers))
            work_package_statement = work_package_statement.where(
                WorkPackage.project_id.in_(identifiers)
            )
        rows = self._session.execute(
            statement.order_by(
                ResourceRequirement.start_date,
                Project.number,
                ResourceRequirement.legacy_segment_id,
                ResourceRequirement.id,
            )
        ).all()
        work_packages = self._session.scalars(work_package_statement).all()
        package_by_reference: dict[str, WorkPackage] = {}
        package_by_id: dict[str, WorkPackage] = {}
        for package in work_packages:
            package_by_id[package.id] = package
            package_by_reference[package.id] = package
            legacy = _text(package.legacy_effort_id)
            if legacy:
                package_by_reference[legacy] = package

        result: list[MediumTermUnlinkedSegmentReadModel] = []
        for requirement, project, request, resource in rows:
            segment_id = _text(requirement.legacy_segment_id) or requirement.id
            origin = _text(requirement.origin) or "REQUEST"
            current_ref = _optional_text(requirement.source_effort_id)

            if request is not None and bool(request.line_mode):
                linked_package = package_by_reference.get(current_ref or "")
                if linked_package is not None and linked_package.project_id == project.id:
                    continue
                classification = (
                    "BROKEN_REFERENCE" if current_ref else "REQUEST_UNLINKED"
                )
                anomaly = True
                link_target = "DEMAND"
            elif request is not None and request.work_package_id:
                package = package_by_id.get(request.work_package_id)
                if package is not None:
                    continue
                classification = "BROKEN_REFERENCE"
                anomaly = True
                link_target = "DEMAND"
            elif request is not None:
                classification = "REQUEST_UNLINKED"
                anomaly = True
                link_target = "DEMAND"
            else:
                linked_package = package_by_reference.get(current_ref or "")
                if linked_package is not None and linked_package.project_id == project.id:
                    continue
                if current_ref:
                    classification = "BROKEN_REFERENCE"
                    anomaly = True
                elif origin in {"QUICK_SHIFT", "AD_HOC"}:
                    classification = "AD_HOC_ALLOWED"
                    anomaly = False
                else:
                    classification = "ORPHAN_SEGMENT"
                    anomaly = True
                link_target = "SEGMENT"

            demand_number = (
                _text(request.legacy_demand_number) or request.id
                if request is not None
                else None
            )
            result.append(
                MediumTermUnlinkedSegmentReadModel(
                    segment_id=segment_id,
                    demand_number=demand_number,
                    project_number=project.number,
                    project_name=project.name,
                    task_code=_optional_text(request.erp_task_code) if request is not None else None,
                    task_label=_optional_text(request.erp_task_label) if request is not None else None,
                    start_date=requirement.start_date,
                    end_date=requirement.end_date,
                    planned_hours=float(requirement.planned_hours),
                    resource_name=_optional_text(resource.name) if resource is not None else None,
                    status=_text(requirement.status),
                    origin=origin,
                    classification=classification,
                    anomaly=anomaly,
                    link_target=link_target,
                    current_work_package_ref=current_ref,
                    reapproval_on_link=(
                        request is not None and request.status == "En planification"
                    ),
                    description=_optional_text(requirement.description),
                )
            )
        return tuple(result)

    def planning_capacity_grid(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        include_resource_ids: Sequence[str] = (),
    ) -> PlanningCapacityGridReadModel:
        """Return capacity for visible resources using their real organization-wide load.

        project_ids limits which project work selects resources and diagnostics.
        Capacity and occupied hours for those resources still include every project so
        a contextual view never invents free capacity by hiding outside commitments.
        """

        if end < start:
            start, end = end, start

        identifiers: tuple[str, ...] | None = None
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))

        rules = self._session.scalars(
            select(ResourceAvailabilityRule).where(ResourceAvailabilityRule.active == true())
        ).all()
        class_codes = availability_class_codes_by_rule(
            self._session,
            tuple(rule.id for rule in rules),
        )
        availability = tuple(
            _availability_record(rule, class_codes.get(rule.id, ()))
            for rule in rules
        )

        requirement_statement = (
            select(ResourceRequirement, Resource)
            .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
            .where(
                ResourceRequirement.status.notin_(("Annulé", "Terminé")),
                ResourceRequirement.end_date >= start,
                ResourceRequirement.start_date <= end,
            )
        )
        if identifiers is not None:
            if identifiers:
                requirement_statement = requirement_statement.where(
                    ResourceRequirement.project_id.in_(identifiers)
                )
            else:
                requirement_statement = requirement_statement.where(False)
        requirements = self._session.execute(
            requirement_statement.order_by(
                ResourceRequirement.start_date,
                ResourceRequirement.id,
            )
        ).all()

        if identifiers is None:
            resources = self.list_schedulable_resources(start=start, end=end)
            occupied_shifts = self.list_shifts(start=start, end=end)
        else:
            contextual_shifts = self.list_shifts(
                start=start,
                end=end,
                project_ids=identifiers,
            )
            pending = self.list_pending_loads(
                start=start,
                end=end,
                project_ids=identifiers,
            )
            all_resources = self.list_resources(active_only=False)
            resource_by_name = {resource.name: resource for resource in all_resources}
            visible_ids = {
                str(value)
                for value in include_resource_ids
                if str(value)
            }
            visible_ids.update(shift.resource_id for shift in contextual_shifts)
            visible_ids.update(
                resource.id
                for _, resource in requirements
                if resource is not None
            )
            visible_ids.update(
                resource_by_name[pending_load.proposed_resource].id
                for pending_load in pending
                if pending_load.proposed_resource in resource_by_name
            )
            resources = tuple(
                resource
                for resource in all_resources
                if resource.id in visible_ids
            )
            occupied_shifts = tuple(
                shift
                for shift in self.list_shifts(start=start, end=end)
                if shift.resource_id in visible_ids
            )

        shifts_by_resource_day: dict[tuple[str, date], list[ShiftReadModel]] = {}
        for shift in occupied_shifts:
            if shift.allocation_type == MISSING_ALLOCATION_TYPE:
                continue
            shifts_by_resource_day.setdefault((shift.resource_id, shift.work_date), []).append(shift)

        resource_rows: list[PlanningResourceCapacityReadModel] = []
        for resource in resources:
            day_rows: list[PlanningDayCapacityReadModel] = []
            cursor = start
            while cursor <= end:
                state = availability_state_for_day(
                    availability,
                    resource.id,
                    cursor,
                    resource_class=resource.resource_class,
                )
                own = shifts_by_resource_day.get((resource.id, cursor), [])
                confirmed = 0.0
                tentative = 0.0
                outside = 0.0
                for shift in own:
                    amount = float(shift.hours)
                    if shift.outside_standard_hours:
                        outside += amount
                    elif shift.load_kind == "FIRM":
                        confirmed += amount
                    else:
                        tentative += amount
                total = confirmed + tentative + outside
                prudent_free = max(float(state.hours) - confirmed - tentative, 0.0)
                overloaded = confirmed + tentative > float(state.hours) + 0.01
                day_rows.append(
                    PlanningDayCapacityReadModel(
                        day=cursor,
                        capacity_hours=round(float(state.hours), 2),
                        confirmed_hours=round(confirmed, 2),
                        tentative_hours=round(tentative, 2),
                        outside_standard_hours=round(outside, 2),
                        total_hours=round(total, 2),
                        prudent_free=round(prudent_free, 2),
                        available=bool(state.available),
                        overloaded=overloaded,
                        reason=state.reason,
                    )
                )
                cursor += timedelta(days=1)

            capacity = round(sum(row.capacity_hours for row in day_rows), 2)
            confirmed = round(sum(row.confirmed_hours for row in day_rows), 2)
            tentative = round(sum(row.tentative_hours for row in day_rows), 2)
            outside = round(sum(row.outside_standard_hours for row in day_rows), 2)
            resource_rows.append(
                PlanningResourceCapacityReadModel(
                    resource_id=resource.id,
                    resource_name=resource.name,
                    resource_class=resource.resource_class,
                    capacity_hours=capacity,
                    confirmed_hours=confirmed,
                    tentative_hours=tentative,
                    outside_standard_hours=outside,
                    prudent_free=round(max(capacity - confirmed - tentative, 0.0), 2),
                    overloaded=any(row.overloaded for row in day_rows),
                    days=tuple(day_rows),
                )
            )

        requirement_ids = tuple(requirement.id for requirement, _ in requirements)
        full_shift_totals: dict[str, tuple[float, float]] = {}
        if requirement_ids:
            raw_shift_rows = self._session.execute(
                select(
                    Shift.resource_requirement_id,
                    Shift.hours,
                    Shift.outside_standard_hours,
                ).where(
                    Shift.resource_requirement_id.in_(requirement_ids),
                    (Shift.allocation_type.is_(None))
                    | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                )
            ).all()
            accumulated: dict[str, list[float]] = {}
            for requirement_id, shift_hours, outside_flag in raw_shift_rows:
                values = accumulated.setdefault(requirement_id, [0.0, 0.0])
                values[0] += float(shift_hours)
                if outside_flag:
                    values[1] += float(shift_hours)
            full_shift_totals = {
                requirement_id: (round(values[0], 2), round(values[1], 2))
                for requirement_id, values in accumulated.items()
            }

        diagnostics: list[PlanningSegmentCapacityDiagnosticReadModel] = []
        for requirement, resource in requirements:
            segment_id = _text(requirement.legacy_segment_id) or requirement.id
            allocated, outside = full_shift_totals.get(requirement.id, (0.0, 0.0))
            planned = round(float(requirement.planned_hours), 2)
            unplaced = round(max(planned - allocated, 0.0), 2)
            diagnostics.append(
                PlanningSegmentCapacityDiagnosticReadModel(
                    segment_id=segment_id,
                    resource_id=resource.id if resource is not None else None,
                    resource_name=resource.name if resource is not None else None,
                    planned_hours=planned,
                    allocated_hours=allocated,
                    outside_standard_hours=outside,
                    unplaced_hours=unplaced,
                    requires_outside_standard_hours=unplaced > 0.01,
                    automatic_target_resource_id=(
                        resource.id if resource is not None else None
                    ),
                    automatic_target_resource_name=(
                        resource.name if resource is not None else None
                    ),
                )
            )

        return PlanningCapacityGridReadModel(
            start=start,
            end=end,
            resources=tuple(resource_rows),
            segment_diagnostics=tuple(diagnostics),
        )

    def list_planning_actions(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[PlanningActionReadModel, ...]:
        """Return the coordinator inbox that historically sat above NiceGUI planning."""

        if end < start:
            start, end = end, start
        demands = {
            row.number: row
            for row in self.list_demands(project_ids=project_ids)
        }
        result: list[PlanningActionReadModel] = []

        for pending in self.list_pending_loads(
            start=start,
            end=end,
            project_ids=project_ids,
        ):
            demand = demands.get(pending.demand_number)
            if demand is None:
                continue
            competency_id = (
                demand.required_competency_ids[0]
                if len(demand.required_competency_ids) == 1
                else None
            )
            result.append(
                PlanningActionReadModel(
                    kind="APPROVAL",
                    reference=demand.number,
                    demand_number=demand.number,
                    segment_id=None,
                    project_number=demand.project_number,
                    project_name=demand.project_name,
                    task_code=demand.task_code,
                    task_label=demand.task_label,
                    start_date=pending.start_date,
                    end_date=pending.end_date,
                    planned_hours=float(
                        pending.projected_hours
                        if pending.projected_hours is not None
                        else pending.window_hours
                    ),
                    required_competency=demand.required_competencies,
                    required_competency_id=competency_id,
                    priority=demand.priority,
                    status=demand.status,
                    confirmation=demand.confirmation,
                    project_manager=demand.project_manager,
                    requester=demand.requester,
                    emergency_override_active=bool(demand.emergency_override_active),
                )
            )

        for segment in self.list_segments(
            start=start,
            end=end,
            include_cancelled=False,
            project_ids=project_ids,
        ):
            if segment.resource_name:
                continue
            if segment.automatic_rebuild_hours <= 0.01:
                # The budget is already covered by locked real shifts; no automatic
                # target is required until some of those decisions are released.
                continue
            if _normalized_text(segment.status) in {"annule", "termine"}:
                continue
            demand = demands.get(segment.demand_number or "")
            if demand is not None and not (
                demand.status == "En planification" or demand.emergency_override_active
            ):
                # A submitted modification keeps the old plan visible, but should not
                # invite assigning that stale plan until it is approved again.
                continue
            if segment.start_date is None or segment.end_date is None:
                continue
            result.append(
                PlanningActionReadModel(
                    kind="ASSIGNMENT",
                    reference=segment.segment_id,
                    demand_number=segment.demand_number,
                    segment_id=segment.segment_id,
                    project_number=segment.project_number,
                    project_name=segment.project_name,
                    task_code=demand.task_code if demand is not None else None,
                    task_label=demand.task_label if demand is not None else None,
                    start_date=segment.start_date,
                    end_date=segment.end_date,
                    planned_hours=float(segment.automatic_rebuild_hours),
                    required_competency=segment.required_competency,
                    required_competency_id=segment.required_competency_id,
                    priority=segment.priority,
                    status=segment.status,
                    confirmation=segment.confirmation,
                    project_manager=segment.project_manager,
                    requester=segment.requester,
                    emergency_override_active=(
                        bool(demand.emergency_override_active)
                        if demand is not None
                        else False
                    ),
                )
            )

        kind_order = {"APPROVAL": 0, "ASSIGNMENT": 1}
        return tuple(
            sorted(
                result,
                key=lambda row: (
                    row.start_date,
                    kind_order.get(row.kind, 9),
                    _text(row.project_number),
                    row.reference,
                ),
            )
        )

    def recommend_resources(
        self,
        segment_id: str,
    ) -> tuple[ResourceRecommendationReadModel, ...]:
        """Rank schedulable resources using the deterministic ADR-025 contract."""

        segment = self.get_segment(segment_id)
        if segment is None or segment.start_date is None or segment.end_date is None:
            return ()
        requirement_id = _optional_text(segment.requirement_id)
        requirement = (
            self._session.get(ResourceRequirement, requirement_id)
            if requirement_id is not None
            else None
        )
        if requirement is None:
            return ()

        start = segment.start_date
        end = segment.end_date
        if end < start:
            start, end = end, start
        required_hours = max(float(segment.automatic_rebuild_hours), 0.0)
        if required_hours <= 0.01:
            return ()

        resources = self.list_schedulable_resources(start=start, end=end)
        if not resources:
            return ()

        # Canonical requirement competency snapshots win over legacy text.  A legacy
        # token that cannot be resolved stays explicitly unverifiable instead of
        # becoming an implicit match.
        required_competency_ids = tuple(
            self._session.scalars(
                select(ResourceRequirementCompetency.competency_id)
                .where(
                    ResourceRequirementCompetency.resource_requirement_id
                    == requirement.id
                )
                .order_by(ResourceRequirementCompetency.competency_id)
            ).all()
        )
        if not required_competency_ids and requirement.required_competency_id:
            required_competency_ids = (requirement.required_competency_id,)

        legacy_competency_names = _split_competencies(requirement.required_competency)
        unresolved_competency_names: tuple[str, ...] = ()
        required_names: tuple[str, ...] = ()
        if required_competency_ids:
            catalog_rows = self._session.scalars(
                select(Competency).where(Competency.id.in_(required_competency_ids))
            ).all()
            names_by_id = {row.id: row.name for row in catalog_rows}
            required_names = tuple(
                names_by_id[competency_id]
                for competency_id in required_competency_ids
                if competency_id in names_by_id
            )
        elif legacy_competency_names:
            catalog_rows = self._session.scalars(select(Competency)).all()
            by_name: dict[str, list[Competency]] = {}
            for row in catalog_rows:
                by_name.setdefault(_normalized_text(row.name), []).append(row)
            resolved_ids: list[str] = []
            resolved_names: list[str] = []
            unresolved: list[str] = []
            for name in legacy_competency_names:
                matches = by_name.get(_normalized_text(name), [])
                if len(matches) != 1:
                    unresolved.append(name)
                    continue
                resolved_ids.append(matches[0].id)
                resolved_names.append(matches[0].name)
            required_competency_ids = tuple(dict.fromkeys(resolved_ids))
            required_names = tuple(dict.fromkeys(resolved_names))
            unresolved_competency_names = tuple(unresolved)

        preferred_resource_id: str | None = None
        preferred_resource_name: str | None = None
        preferred_resource_status = "NONE"
        if (
            _normalized_text(requirement.origin) == "request"
            and requirement.approval_reference_status == "CAPTURED"
            and requirement.approved_task_catalog_item_id
        ):
            task_context = self._session.execute(
                select(
                    TaskCatalogEntry.preferred_resource_id,
                    Resource.name,
                    Resource.active,
                    Resource.erp_active,
                )
                .select_from(TaskCatalogEntry)
                .join(Project, Project.number == TaskCatalogEntry.project_number)
                .outerjoin(Resource, Resource.id == TaskCatalogEntry.preferred_resource_id)
                .where(
                    TaskCatalogEntry.id == requirement.approved_task_catalog_item_id,
                    Project.id == requirement.project_id,
                )
            ).one_or_none()
            if task_context is None:
                preferred_resource_status = "INVALID_TASK_CONTEXT"
            else:
                (
                    preferred_resource_id,
                    preferred_resource_name,
                    preferred_active,
                    preferred_erp_active,
                ) = task_context
                if preferred_resource_id is None:
                    preferred_resource_status = "NONE"
                elif preferred_resource_name is None:
                    preferred_resource_status = "NOT_FOUND"
                elif not bool(preferred_active):
                    preferred_resource_status = "INACTIVE_LOCAL"
                elif not bool(preferred_erp_active):
                    preferred_resource_status = "INACTIVE_ERP"
                elif preferred_resource_id not in {resource.id for resource in resources}:
                    preferred_resource_status = "NO_SCHEDULE_IN_WINDOW"
                else:
                    preferred_resource_status = "ELIGIBLE"
        elif requirement.approved_task_catalog_item_id:
            preferred_resource_status = "UNRESOLVED_CONTEXT"

        rules = self._session.scalars(
            select(ResourceAvailabilityRule).where(ResourceAvailabilityRule.active == true())
        ).all()
        class_codes = availability_class_codes_by_rule(
            self._session,
            tuple(rule.id for rule in rules),
        )
        availability = tuple(
            _availability_record(rule, class_codes.get(rule.id, ()))
            for rule in rules
        )

        shifts_by_resource_day: dict[tuple[str, date], list[ShiftReadModel]] = {}
        for shift in self.list_shifts(start=start, end=end):
            if shift.allocation_type == MISSING_ALLOCATION_TYPE:
                continue
            if shift.requirement_id == requirement.id and not shift.locked:
                # Replaceable automatic output of the evaluated requirement is not a
                # commitment. Locked decisions remain on their real resource.
                continue
            shifts_by_resource_day.setdefault(
                (shift.resource_id, shift.work_date), []
            ).append(shift)

        required_class = _optional_text(requirement.required_resource_class)
        prepared_rows: dict[str, dict[str, object]] = {}
        domain_candidates: list[RecommendationCandidate] = []

        for resource in resources:
            capacity = 0.0
            confirmed = 0.0
            tentative = 0.0
            outside = 0.0
            cursor = start
            while cursor <= end:
                capacity += availability_hours_for_day(
                    availability,
                    resource.id,
                    cursor,
                    resource_class=resource.resource_class,
                )
                for shift in shifts_by_resource_day.get((resource.id, cursor), ()):
                    shift_hours = float(shift.hours)
                    if shift.outside_standard_hours:
                        outside += shift_hours
                    elif shift.load_kind == "FIRM":
                        confirmed += shift_hours
                    else:
                        tentative += shift_hours
                cursor += timedelta(days=1)

            free_after_confirmed = max(capacity - confirmed, 0.0)
            prudent_free = max(capacity - confirmed - tentative, 0.0)
            overtime_needed = max(required_hours - prudent_free, 0.0)
            resource_competency_ids = set(resource.competency_ids)
            missing_competency_ids = tuple(
                competency_id
                for competency_id in required_competency_ids
                if competency_id not in resource_competency_ids
            )
            if unresolved_competency_names:
                competency_state = COMPETENCY_UNRESOLVED
                competency_match = False
            elif not required_competency_ids:
                competency_state = COMPETENCY_NOT_REQUIRED
                competency_match = True
            elif missing_competency_ids:
                competency_state = COMPETENCY_MISSING
                competency_match = False
            else:
                competency_state = COMPETENCY_SATISFIED
                competency_match = True

            class_match = (
                required_class is None
                or _text(resource.resource_class) == required_class
            )
            enough_prudent = prudent_free + 0.01 >= required_hours
            enough_after_confirmed = free_after_confirmed + 0.01 >= required_hours
            preferred = bool(
                preferred_resource_id and resource.id == preferred_resource_id
            )

            prepared_rows[resource.id] = {
                "resource": resource,
                "competency_match": competency_match,
                "competency_state": competency_state,
                "missing_competency_ids": missing_competency_ids,
                "class_match": class_match,
                "capacity": round(capacity, 2),
                "confirmed": round(confirmed, 2),
                "tentative": round(tentative, 2),
                "outside": round(outside, 2),
                "free_after_confirmed": round(free_after_confirmed, 2),
                "prudent_free": round(prudent_free, 2),
                "overtime_needed": round(overtime_needed, 2),
                "enough_after_confirmed": enough_after_confirmed,
                "enough_prudent": enough_prudent,
                "preferred": preferred,
            }
            domain_candidates.append(
                RecommendationCandidate(
                    resource_id=resource.id,
                    resource_name=resource.name,
                    preferred=preferred,
                    class_match=class_match,
                    competency_state=competency_state,
                    missing_competency_count=(
                        len(missing_competency_ids)
                        + len(unresolved_competency_names)
                    ),
                    prudent_free=prudent_free,
                    free_after_confirmed=free_after_confirmed,
                    tentative_hours=tentative,
                    required_hours=required_hours,
                )
            )

        ranked = rank_recommendation_candidates(tuple(domain_candidates))
        required_competency = (
            "; ".join(required_names)
            if required_names
            else _optional_text(requirement.required_competency)
        )
        result: list[ResourceRecommendationReadModel] = []
        for ranked_candidate in ranked:
            candidate = ranked_candidate.candidate
            row = prepared_rows[candidate.resource_id]
            resource = row["resource"]
            result.append(
                ResourceRecommendationReadModel(
                    resource_id=resource.id,
                    resource_name=resource.name,
                    resource_class=resource.resource_class,
                    required_competency=required_competency,
                    required_class=required_class,
                    competency_match=bool(row["competency_match"]),
                    class_match=bool(row["class_match"]),
                    capacity_hours=float(row["capacity"]),
                    confirmed_hours=float(row["confirmed"]),
                    tentative_hours=float(row["tentative"]),
                    outside_standard_hours=float(row["outside"]),
                    free_after_confirmed=float(row["free_after_confirmed"]),
                    prudent_free=float(row["prudent_free"]),
                    overtime_needed=float(row["overtime_needed"]),
                    enough_after_confirmed=bool(row["enough_after_confirmed"]),
                    enough_prudent=bool(row["enough_prudent"]),
                    # Compatibility only. ADR-025 ranking is category/tie-break based;
                    # this value is a monotonic projection of the authoritative rank.
                    score=round(1.0 / ranked_candidate.rank, 6),
                    rank=ranked_candidate.rank,
                    recommended=ranked_candidate.recommended,
                    preferred=bool(row["preferred"]),
                    recommendation_category=ranked_candidate.category,
                    competency_state=str(row["competency_state"]),
                    missing_competency_ids=tuple(row["missing_competency_ids"]),
                    capacity_state=ranked_candidate.capacity_state,
                    fallback_requires_confirmation=(
                        ranked_candidate.fallback_requires_confirmation
                    ),
                    preferred_resource_id=preferred_resource_id,
                    preferred_resource_name=preferred_resource_name,
                    preferred_resource_status=preferred_resource_status,
                )
            )
        return tuple(result)

    def list_segments(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        include_cancelled: bool = False,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[SegmentReadModel, ...]:
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            allowed_project_numbers = set(
                self._session.scalars(
                    select(Project.number).where(Project.id.in_(identifiers))
                ).all()
            )
        else:
            allowed_project_numbers = None

        rows = self._segments.list(include_cancelled=include_cancelled)
        result: list[SegmentReadModel] = []
        for row in rows:
            if (
                allowed_project_numbers is not None
                and row.project_number not in allowed_project_numbers
            ):
                continue
            if start is not None and row.end_date is not None and row.end_date < start:
                continue
            if end is not None and row.start_date is not None and row.start_date > end:
                continue
            result.append(row)
        return tuple(result)

    def get_segment(self, segment_id: str) -> SegmentReadModel | None:
        return self._segments.get(segment_id)

    def list_shifts(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        resource_name: str | None = None,
        resource_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        can_manage_planning: bool = False,
    ) -> tuple[ShiftReadModel, ...]:
        asset_context_marker = (
            select(AssetRequirement.id)
            .where(
                AssetRequirement.status != "Annulé",
                or_(
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SHIFT_AD_HOC.value
                    )
                    & (AssetRequirement.shift_id == Shift.id),
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.REQUEST.value
                    )
                    & (
                        AssetRequirement.workforce_request_id
                        == ResourceRequirement.workforce_request_id
                    ),
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.PROJECT_DIRECT.value
                    )
                    & (AssetRequirement.project_id == ResourceRequirement.project_id)
                    & (AssetRequirement.start_date <= Shift.work_date)
                    & (AssetRequirement.end_date >= Shift.work_date),
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.RESOURCE_PERIOD.value
                    )
                    & (AssetRequirement.context_resource_id == Shift.resource_id)
                    & (AssetRequirement.start_date <= Shift.work_date)
                    & (AssetRequirement.end_date >= Shift.work_date),
                    (
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SEGMENT.value
                    )
                    & (
                        AssetRequirement.resource_requirement_id
                        == ResourceRequirement.id
                    )
                    & (AssetRequirement.start_date <= Shift.work_date)
                    & (AssetRequirement.end_date >= Shift.work_date),
                ),
            )
            .limit(1)
            .correlate(Shift, ResourceRequirement)
            .scalar_subquery()
        )
        statement = (
            select(
                Shift,
                ResourceRequirement,
                Resource,
                Project,
                WorkforceRequest,
                asset_context_marker.label("asset_context_marker"),
            )
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .join(Resource, Shift.resource_id == Resource.id)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(
                WorkforceRequest,
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
            )
        )
        if start is not None:
            statement = statement.where(Shift.work_date >= start)
        if end is not None:
            statement = statement.where(Shift.work_date <= end)
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            statement = statement.where(ResourceRequirement.project_id.in_(identifiers))
        wanted_resource = _text(resource_name)
        if wanted_resource:
            statement = statement.where(Resource.name == wanted_resource)
        wanted_resource_id = _text(resource_id)
        if wanted_resource_id:
            statement = statement.where(Resource.id == wanted_resource_id)

        rows = self._session.execute(
            statement.order_by(
                Shift.work_date,
                Resource.sort_order,
                Resource.name,
                Shift.locked.desc(),
                Shift.id,
            )
        ).all()
        if not rows:
            return ()

        # 560C: load every asset relation needed by the visible Shift set in bounded
        # batches.  The grid must not perform one SQL query per Shift.
        visible_shift_ids = tuple(shift.id for shift, *_rest in rows)
        visible_request_ids = tuple(
            dict.fromkeys(
                requirement.workforce_request_id
                for _shift, requirement, _resource, _project, _request, _marker in rows
                if requirement.workforce_request_id
            )
        )
        visible_resource_ids = tuple(
            dict.fromkeys(
                shift.resource_id for shift, *_rest in rows
            )
        )
        visible_project_ids = tuple(
            dict.fromkeys(
                requirement.project_id
                for _shift, requirement, _resource, _project, _request, _marker in rows
            )
        )
        visible_requirement_ids = tuple(
            dict.fromkeys(
                requirement.id
                for _shift, requirement, _resource, _project, _request, _marker in rows
            )
        )
        visible_start = min(shift.work_date for shift, *_rest in rows)
        visible_end = max(shift.work_date for shift, *_rest in rows)
        asset_scope = [
            (
                (AssetRequirement.origin == AssetRequirementOrigin.SHIFT_AD_HOC.value)
                & (AssetRequirement.shift_id.in_(visible_shift_ids))
            )
        ]
        if visible_request_ids:
            asset_scope.append(
                (
                    (AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value)
                    & (
                        AssetRequirement.workforce_request_id.in_(
                            visible_request_ids
                        )
                    )
                )
            )
        if visible_project_ids:
            asset_scope.append(
                (
                    (AssetRequirement.origin == AssetRequirementOrigin.PROJECT_DIRECT.value)
                    & (AssetRequirement.project_id.in_(visible_project_ids))
                    & (AssetRequirement.start_date <= visible_end)
                    & (AssetRequirement.end_date >= visible_start)
                )
            )
        if visible_resource_ids:
            asset_scope.append(
                (
                    (AssetRequirement.origin == AssetRequirementOrigin.RESOURCE_PERIOD.value)
                    & (AssetRequirement.context_resource_id.in_(visible_resource_ids))
                    & (AssetRequirement.start_date <= visible_end)
                    & (AssetRequirement.end_date >= visible_start)
                )
            )
        if visible_requirement_ids:
            asset_scope.append(
                (
                    (AssetRequirement.origin == AssetRequirementOrigin.SEGMENT.value)
                    & (
                        AssetRequirement.resource_requirement_id.in_(
                            visible_requirement_ids
                        )
                    )
                    & (AssetRequirement.start_date <= visible_end)
                    & (AssetRequirement.end_date >= visible_start)
                )
            )
        has_asset_context = any(marker is not None for *_row, marker in rows)
        asset_requirements_by_id: dict[str, AssetRequirement] = {}
        allocation_by_requirement: dict[str, AssetAllocation] = {}
        assets_by_id: dict[str, Asset] = {}
        asset_types_by_id: dict[str, AssetType] = {}
        required_competencies_by_type: dict[str, set[str]] = {}
        held_required_competencies_by_allocation: dict[str, set[str]] = {}
        operator_resources_by_id: dict[str, Resource] = {}
        human_compatibility_by_allocation: dict[str, bool] = {}

        if has_asset_context:
            qualification_shift = aliased(Shift)
            qualification_requirement = aliased(ResourceRequirement)
            qualification_exists = (
                select(qualification_shift.id)
                .join(
                    qualification_requirement,
                    qualification_shift.resource_requirement_id
                    == qualification_requirement.id,
                )
                .where(
                    qualification_shift.resource_id
                    == AssetAllocation.operator_resource_id,
                    qualification_shift.work_date >= AssetAllocation.start_date,
                    qualification_shift.work_date <= AssetAllocation.end_date,
                    qualification_requirement.project_id
                    == AssetRequirement.project_id,
                    qualification_requirement.status != "Annulé",
                    or_(
                        (
                            AssetRequirement.origin
                            == AssetRequirementOrigin.SHIFT_AD_HOC.value
                        )
                        & (qualification_shift.id == AssetRequirement.shift_id),
                        (
                            AssetRequirement.origin
                            == AssetRequirementOrigin.REQUEST.value
                        )
                        & (
                            qualification_requirement.workforce_request_id
                            == AssetRequirement.workforce_request_id
                        ),
                    ),
                )
                .limit(1)
                .correlate(AssetRequirement, AssetAllocation)
                .exists()
            )
            operator_resource = aliased(Resource)
            asset_projection_rows = self._session.execute(
                select(
                    AssetRequirement,
                    AssetAllocation,
                    Asset,
                    AssetType,
                    operator_resource,
                    AssetTypeCompetency.competency_id.label(
                        "required_competency_id"
                    ),
                    ResourceCompetency.competency_id.label(
                        "held_required_competency_id"
                    ),
                    qualification_exists.label("human_compatible"),
                )
                .outerjoin(
                    AssetAllocation,
                    AssetAllocation.asset_requirement_id
                    == AssetRequirement.id,
                )
                .outerjoin(Asset, Asset.id == AssetAllocation.asset_id)
                .outerjoin(
                    AssetType,
                    AssetType.id == AssetRequirement.asset_type_id,
                )
                .outerjoin(
                    operator_resource,
                    operator_resource.id
                    == AssetAllocation.operator_resource_id,
                )
                .outerjoin(
                    AssetTypeCompetency,
                    AssetTypeCompetency.asset_type_id
                    == AssetRequirement.asset_type_id,
                )
                .outerjoin(
                    ResourceCompetency,
                    (
                        ResourceCompetency.resource_id
                        == AssetAllocation.operator_resource_id
                    )
                    & (
                        ResourceCompetency.competency_id
                        == AssetTypeCompetency.competency_id
                    ),
                )
                .where(
                    AssetRequirement.status != "Annulé",
                    or_(*asset_scope),
                )
                .order_by(
                    AssetRequirement.id,
                    AssetAllocation.id,
                    AssetTypeCompetency.competency_id,
                )
            ).all()

            for (
                asset_requirement,
                allocation,
                asset,
                asset_type,
                operator,
                required_competency_id,
                held_required_competency_id,
                human_compatible,
            ) in asset_projection_rows:
                asset_requirements_by_id[asset_requirement.id] = asset_requirement
                if allocation is not None:
                    allocation_by_requirement[asset_requirement.id] = allocation
                    human_compatibility_by_allocation[allocation.id] = bool(
                        human_compatible
                    )
                if asset is not None:
                    assets_by_id[asset.id] = asset
                if asset_type is not None:
                    asset_types_by_id[asset_type.id] = asset_type
                if operator is not None:
                    operator_resources_by_id[operator.id] = operator
                if required_competency_id is not None:
                    required_competencies_by_type.setdefault(
                        asset_requirement.asset_type_id,
                        set(),
                    ).add(required_competency_id)
                if (
                    allocation is not None
                    and held_required_competency_id is not None
                ):
                    held_required_competencies_by_allocation.setdefault(
                        allocation.id,
                        set(),
                    ).add(held_required_competency_id)

        asset_requirements = tuple(
            asset_requirements_by_id[key]
            for key in sorted(asset_requirements_by_id)
        )
        visible_human_requirements = {
            requirement.id: requirement
            for _shift, requirement, _resource, _project, _request, _marker in rows
        }

        def qualification_state(
            requirement: AssetRequirement,
            allocation: AssetAllocation,
        ) -> str:
            required = required_competencies_by_type.get(
                requirement.asset_type_id,
                set(),
            )
            operator_id = allocation.operator_resource_id
            if not operator_id:
                return (
                    QUALIFICATION_SATISFIED
                    if not required
                    else QUALIFICATION_MISSING_OPERATOR
                )
            operator = operator_resources_by_id.get(operator_id)
            if (
                operator is None
                or not operator.active
                or not required.issubset(
                    held_required_competencies_by_allocation.get(
                        allocation.id,
                        set(),
                    )
                )
            ):
                return QUALIFICATION_SKILL_MISMATCH

            if requirement.origin == AssetRequirementOrigin.PROJECT_DIRECT.value:
                compatible = bool(requirement.project_id)
            elif (
                requirement.origin
                == AssetRequirementOrigin.RESOURCE_PERIOD.value
            ):
                compatible = bool(
                    requirement.context_resource_id
                    and requirement.context_resource_id == operator_id
                )
            elif requirement.origin == AssetRequirementOrigin.SEGMENT.value:
                segment = visible_human_requirements.get(
                    requirement.resource_requirement_id or ""
                )
                compatible = bool(
                    segment is not None
                    and segment.status != "Annulé"
                    and segment.project_id == requirement.project_id
                )
            else:
                compatible = human_compatibility_by_allocation.get(
                    allocation.id,
                    False,
                )
            return (
                QUALIFICATION_SATISFIED
                if compatible
                else QUALIFICATION_NO_OVERLAP
            )

        ad_hoc_by_shift = {
            row.shift_id: row
            for row in asset_requirements
            if row.origin == AssetRequirementOrigin.SHIFT_AD_HOC.value
            and row.shift_id
        }

        def reservation_model(
            requirement: AssetRequirement,
            association_kind: str,
        ) -> ShiftAssetReservationReadModel | None:
            allocation = allocation_by_requirement.get(requirement.id)
            if allocation is None:
                return None
            asset = assets_by_id.get(allocation.asset_id)
            qualification = qualification_state(
                requirement,
                allocation,
            )
            return ShiftAssetReservationReadModel(
                requirement_id=requirement.id,
                allocation_id=allocation.id,
                origin=requirement.origin,
                association_kind=association_kind,
                asset_id=allocation.asset_id,
                asset_code=asset.code if asset is not None else allocation.asset_id,
                asset_label=asset.label if asset is not None else allocation.asset_id,
                asset_active=bool(asset is not None and asset.active),
                operator_resource_id=allocation.operator_resource_id,
                qualification_state=qualification,
                project_id=requirement.project_id,
                resource_requirement_id=requirement.resource_requirement_id,
                context_resource_id=requirement.context_resource_id,
                start_date=allocation.start_date,
                end_date=allocation.end_date,
            )

        result: list[ShiftReadModel] = []
        for shift, requirement, resource, project, request, _asset_marker in rows:
            confirmation = effective_confirmation(
                shift.confirmation,
                requirement.confirmation,
            )
            owned_requirement = ad_hoc_by_shift.get(shift.id)
            asset_assignment = (
                reservation_model(
                    owned_requirement,
                    ASSOCIATION_OWNED_SHIFT,
                )
                if owned_requirement is not None
                else None
            )
            related: list[ShiftAssetReservationReadModel] = []
            for asset_requirement in asset_requirements:
                if asset_requirement is owned_requirement:
                    continue
                allocation = allocation_by_requirement.get(asset_requirement.id)
                if allocation is None:
                    continue
                association_kind = asset_shift_association(
                    requirement=asset_requirement,
                    allocation=allocation,
                    shift=shift,
                    human_requirement=requirement,
                )
                if (
                    association_kind is None
                    or association_kind == ASSOCIATION_OWNED_SHIFT
                ):
                    continue
                model = reservation_model(
                    asset_requirement,
                    association_kind,
                )
                if model is not None:
                    related.append(model)

            diagnostics: list[str] = []
            if owned_requirement is not None and asset_assignment is None:
                diagnostics.append("asset_assignment_incomplete")
            if asset_assignment is not None:
                asset = assets_by_id.get(asset_assignment.asset_id)
                asset_type = asset_types_by_id.get(
                    owned_requirement.asset_type_id
                    if owned_requirement is not None
                    else ""
                )
                if asset is None or not asset.active or asset_type is None or not asset_type.active:
                    diagnostics.append("asset_inactive")
                if asset_assignment.qualification_state != QUALIFICATION_SATISFIED:
                    diagnostics.append("operator_not_qualified")

            if not can_manage_planning:
                denied = ShiftAssetActionReadModel(
                    allowed=False,
                    reason="permission_denied",
                )
                actions = ShiftAssetActionsReadModel(
                    assign=denied,
                    change=denied,
                    release=denied,
                )
            elif requirement.status in {"Annulé", "Terminé"}:
                denied = ShiftAssetActionReadModel(
                    allowed=False,
                    reason="shift_state_invalid",
                )
                actions = ShiftAssetActionsReadModel(
                    assign=denied,
                    change=denied,
                    release=denied,
                )
            elif owned_requirement is None:
                actions = ShiftAssetActionsReadModel(
                    assign=ShiftAssetActionReadModel(allowed=True),
                    change=ShiftAssetActionReadModel(
                        allowed=False,
                        reason="asset_assignment_missing",
                    ),
                    release=ShiftAssetActionReadModel(
                        allowed=False,
                        reason="asset_assignment_missing",
                    ),
                )
            elif asset_assignment is None:
                incomplete = ShiftAssetActionReadModel(
                    allowed=False,
                    reason="asset_assignment_incomplete",
                )
                actions = ShiftAssetActionsReadModel(
                    assign=incomplete,
                    change=incomplete,
                    release=incomplete,
                )
            else:
                actions = ShiftAssetActionsReadModel(
                    assign=ShiftAssetActionReadModel(
                        allowed=False,
                        reason="asset_assignment_attached",
                    ),
                    change=ShiftAssetActionReadModel(allowed=True),
                    release=ShiftAssetActionReadModel(allowed=True),
                )

            result.append(
                ShiftReadModel(
                    allocation_id=_text(shift.legacy_allocation_id) or shift.id,
                    segment_id=_text(requirement.legacy_segment_id) or requirement.id,
                    resource_id=resource.id,
                    requirement_id=requirement.id,
                    resource_name=resource.name,
                    work_date=shift.work_date,
                    hours=float(shift.hours),
                    allocation_type=_optional_text(shift.allocation_type),
                    source=_text(shift.source) or "AUTO",
                    locked=bool(shift.locked),
                    outside_standard_hours=bool(shift.outside_standard_hours),
                    confirmation=confirmation,
                    confirmation_override=_optional_text(shift.confirmation),
                    load_kind=workload_kind(confirmation),
                    note=_optional_text(shift.note),
                    demand_id=request.id if request is not None else None,
                    demand_number=(
                        _text(request.legacy_demand_number) or request.id
                        if request is not None
                        else None
                    ),
                    project_id=requirement.project_id,
                    project_number=_optional_text(project.number),
                    project_name=_optional_text(project.name),
                    project_manager=_optional_text(project.project_manager_name),
                    requester=(
                        _optional_text(request.requester_name)
                        if request is not None
                        else _optional_text(requirement.created_by_name)
                    ),
                    emergency_override_active=bool(
                        request is not None
                        and request.emergency_override_active
                    ),
                    asset_assignment=asset_assignment,
                    related_asset_reservations=tuple(related),
                    asset_actions=actions,
                    asset_diagnostics=tuple(dict.fromkeys(diagnostics)),
                )
            )
        return tuple(result)

    def planning_snapshot(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        include_resource_ids: Sequence[str] = (),
        can_manage_planning: bool = False,
    ) -> PlanningSnapshotReadModel:
        """Read one planning window while keeping capacity semantics separate from scope."""

        demands = tuple(
            row
            for row in self.list_demands(project_ids=project_ids)
            if _demand_overlaps(row, start, end)
        )
        shifts = self.list_shifts(
            start=start,
            end=end,
            project_ids=project_ids,
            can_manage_planning=can_manage_planning,
        )
        pending_loads = self.list_pending_loads(
            start=start,
            end=end,
            project_ids=project_ids,
        )
        segments = self.list_segments(
            start=start,
            end=end,
            include_cancelled=False,
            project_ids=project_ids,
        )

        if project_ids is None:
            resources = self.list_schedulable_resources(start=start, end=end)
        else:
            all_resources = self.list_resources(active_only=False)
            resource_by_name = {resource.name: resource for resource in all_resources}
            visible_ids = {
                str(value)
                for value in include_resource_ids
                if str(value)
            }
            visible_ids.update(shift.resource_id for shift in shifts)
            visible_ids.update(
                resource_by_name[segment.resource_name].id
                for segment in segments
                if segment.resource_name in resource_by_name
            )
            visible_ids.update(
                resource_by_name[pending.proposed_resource].id
                for pending in pending_loads
                if pending.proposed_resource in resource_by_name
            )
            resources = tuple(
                resource
                for resource in all_resources
                if resource.id in visible_ids
            )

        totals = WorkloadTotals()
        for shift in shifts:
            if shift.allocation_type == MISSING_ALLOCATION_TYPE:
                continue
            totals = totals.add(shift.hours, shift.confirmation)
        additive_pending = round(
            sum(
                row.window_hours
                for row in pending_loads
                if row.mode == PENDING_LOAD_ADDITIVE
            ),
            2,
        )
        replacement_proposals = round(
            sum(
                row.window_hours
                for row in pending_loads
                if row.mode != PENDING_LOAD_ADDITIVE
            ),
            2,
        )
        asset_window = self.asset_planning_window(
            start=start,
            end=end,
            project_ids=project_ids,
            context_resource_ids=include_resource_ids,
        )
        return PlanningSnapshotReadModel(
            start=start,
            end=end,
            resources=resources,
            demands=demands,
            segments=segments,
            shifts=shifts,
            planning_version=SqlPlanningMutationVersionRepository(
                self._session
            ).current_version(),
            pending_loads=pending_loads,
            asset_types=asset_window.asset_types,
            assets=asset_window.assets,
            asset_requirements=asset_window.requirements,
            asset_allocations=asset_window.allocations,
            asset_unavailability=asset_window.unavailability,
            asset_capacity=asset_window.capacity,
            asset_diagnostics=asset_window.diagnostics,
            firm_hours=totals.firm_hours,
            potential_hours=round(totals.potential_hours + additive_pending, 2),
            replacement_proposal_hours=replacement_proposals,
        )
