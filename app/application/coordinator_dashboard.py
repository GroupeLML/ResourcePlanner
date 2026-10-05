from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, Sequence

from .approval_progress import ApprovalCycleProgressReadModel
from .demand_cancellation import demand_cancellation_policy
from .operational_contacts import OperationalContactService
from .query_models import DemandCancellationMaterializationReadModel, PlanningActionReadModel
from .query_ports import PlannerQueryPort
from .read_models import DemandReadModel, SegmentReadModel
from .security import AuthPrincipal
from .user_view_context import DemandScopeResolution, SCOPE_MINE
from ..domain.operational_contacts import ContactResolution


ATTENTION_HORIZON_DAYS = 7

ACTION_CATEGORY_ASSIGNMENT = "ASSIGNMENT"
ACTION_CATEGORY_CANCELLATION = "CANCELLATION"
ACTION_CATEGORY_APPROVAL = "APPROVAL"
ACTION_CATEGORY_COVERAGE = "COVERAGE"

ACTION_WORKFORCE_ASSIGNMENT = "WORKFORCE_ASSIGNMENT"
ACTION_ASSET_ASSIGNMENT = "ASSET_ASSIGNMENT"
ACTION_CANCELLATION = "CANCELLATION"
ACTION_APPROVAL = "APPROVAL"
ACTION_PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
ACTION_CONFLICT = "CONFLICT"

ATTENTION_URGENT = "URGENT"
ATTENTION_OVERDUE = "OVERDUE"
ATTENTION_SOON = "SOON"
ATTENTION_NORMAL = "NORMAL"

TARGET_DEMANDS = "DEMANDS"
TARGET_PLANNING = "PLANNING"


class DemandScopeResolverPort(Protocol):
    def resolve_demand_scope(
        self,
        principal: AuthPrincipal,
        requested_scope: str | None = None,
    ) -> DemandScopeResolution: ...


class ApprovalProgressReaderPort(Protocol):
    def get(
        self,
        demand_number: str,
        *,
        current_user_id: str | None,
        permissions: Sequence[str],
    ) -> ApprovalCycleProgressReadModel | None: ...


@dataclass(frozen=True, slots=True)
class CoordinatorDashboardKpiReadModel:
    personal_demands: int
    total_actions: int
    assignments: int
    cancellations: int
    approvals: int
    coverage_issues: int
    partial_coverages: int
    conflicts: int
    attention_items: int


@dataclass(frozen=True, slots=True)
class CoordinatorDashboardDemandReadModel:
    demand_number: str
    project_number: str | None
    project_name: str | None
    effective_status: str
    priority: str | None
    desired_start: date | None
    desired_end: date | None
    cancellation_pending: bool
    attention: str
    days_until_start: int | None


@dataclass(frozen=True, slots=True)
class CoordinatorDashboardActionReadModel:
    action_id: str
    category: str
    kind: str
    label: str
    detail: str | None
    demand_number: str
    source_id: str
    project_number: str | None
    project_name: str | None
    status: str | None
    priority: str | None
    start_date: date | None
    end_date: date | None
    planned_hours: float | None
    covered_hours: float | None
    remaining_hours: float | None
    attention: str
    days_until_start: int | None
    target: str
    resource_kind: str | None = None
    related_ids: tuple[str, ...] = ()
    operational_responsible_contact_id: str | None = None
    operational_responsible_display_name: str | None = None
    operational_responsible_status: str | None = None
    operational_responsible_source_type: str | None = None
    operational_responsible_source_label: str | None = None


@dataclass(frozen=True, slots=True)
class CoordinatorDashboardReadModel:
    as_of: date
    attention_horizon_days: int
    kpis: CoordinatorDashboardKpiReadModel
    personal_demands: tuple[CoordinatorDashboardDemandReadModel, ...]
    actions: tuple[CoordinatorDashboardActionReadModel, ...]


def _text(value: object) -> str:
    return str(value or "").strip()


def _demand_start(demand: DemandReadModel) -> date | None:
    candidates = [demand.desired_start]
    candidates.extend(
        line.desired_start
        for line in demand.lines
        if line.active and line.desired_start is not None
    )
    values = [value for value in candidates if value is not None]
    return min(values) if values else None


def _demand_end(demand: DemandReadModel) -> date | None:
    candidates = [demand.desired_end or demand.desired_start]
    candidates.extend(
        (line.desired_end or line.desired_start)
        for line in demand.lines
        if line.active and (line.desired_end or line.desired_start) is not None
    )
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def _attention(
    *,
    priority: str | None,
    start_date: date | None,
    emergency_override_active: bool,
    today: date,
    horizon_days: int,
) -> tuple[str, int | None]:
    days_until_start = (start_date - today).days if start_date is not None else None
    urgent_priority = _text(priority).casefold() in {"urgent", "urgente"}
    if emergency_override_active or urgent_priority:
        return ATTENTION_URGENT, days_until_start
    if days_until_start is not None and days_until_start < 0:
        return ATTENTION_OVERDUE, days_until_start
    if days_until_start is not None and days_until_start <= horizon_days:
        return ATTENTION_SOON, days_until_start
    return ATTENTION_NORMAL, days_until_start


def _action_sort_key(row: CoordinatorDashboardActionReadModel) -> tuple[object, ...]:
    attention_rank = {
        ATTENTION_URGENT: 0,
        ATTENTION_OVERDUE: 1,
        ATTENTION_SOON: 2,
        ATTENTION_NORMAL: 3,
    }
    category_rank = {
        ACTION_CATEGORY_CANCELLATION: 0,
        ACTION_CATEGORY_APPROVAL: 1,
        ACTION_CATEGORY_ASSIGNMENT: 2,
        ACTION_CATEGORY_COVERAGE: 3,
    }
    return (
        attention_rank.get(row.attention, 9),
        row.start_date or date.max,
        category_rank.get(row.category, 9),
        row.demand_number,
        row.action_id,
    )


def _personal_demand_sort_key(
    row: CoordinatorDashboardDemandReadModel,
) -> tuple[object, ...]:
    attention_rank = {
        ATTENTION_URGENT: 0,
        ATTENTION_OVERDUE: 1,
        ATTENTION_SOON: 2,
        ATTENTION_NORMAL: 3,
    }
    return (
        attention_rank.get(row.attention, 9),
        row.desired_start or date.max,
        row.demand_number,
    )


def _planning_window(
    demands: Sequence[DemandReadModel],
    segments: Sequence[SegmentReadModel],
    *,
    today: date,
    horizon_days: int,
) -> tuple[date, date]:
    dates: list[date] = [today, today + timedelta(days=horizon_days)]
    for demand in demands:
        start = _demand_start(demand)
        end = _demand_end(demand)
        if start is not None:
            dates.append(start)
        if end is not None:
            dates.append(end)
    for segment in segments:
        if segment.start_date is not None:
            dates.append(segment.start_date)
        if segment.end_date is not None:
            dates.append(segment.end_date)
    return min(dates), max(dates)


class CoordinatorDashboardService:
    """Compose the coordinator dashboard from existing authoritative read policies."""

    def __init__(
        self,
        queries: PlannerQueryPort,
        scope_resolver: DemandScopeResolverPort,
        approval_progress: ApprovalProgressReaderPort | None,
        operational_contacts: OperationalContactService | None = None,
    ) -> None:
        self._queries = queries
        self._scope_resolver = scope_resolver
        self._approval_progress = approval_progress
        self._operational_contacts = operational_contacts

    def read(
        self,
        principal: AuthPrincipal,
        *,
        today: date | None = None,
        attention_horizon_days: int = ATTENTION_HORIZON_DAYS,
    ) -> CoordinatorDashboardReadModel:
        as_of = today or date.today()
        horizon_days = max(int(attention_horizon_days), 0)
        scope = self._scope_resolver.resolve_demand_scope(principal, SCOPE_MINE)
        coordinated_ids = tuple(scope.demand_ids or ())

        personal_pairs = tuple(
            self._queries.list_demands_with_cancellation_materialization(
                demand_ids=coordinated_ids,
            )
        )
        active_personal_pairs = tuple(
            (demand, materialization)
            for demand, materialization in personal_pairs
            if not demand.terminal
        )
        personal_demands = tuple(demand for demand, _ in active_personal_pairs)
        personal_numbers = {demand.number for demand in personal_demands}

        personal_segments = tuple(
            segment
            for segment in self._queries.list_segments(include_cancelled=False)
            if segment.demand_number in personal_numbers
        )
        responsibility_by_segment: dict[str, ContactResolution] = {}
        if self._operational_contacts is not None:
            requirement_ids = tuple(
                dict.fromkeys(
                    segment.requirement_id
                    for segment in personal_segments
                    if segment.requirement_id
                )
            )
            if requirement_ids:
                resolved_by_requirement = {
                    resolution.requirement_id: resolution.operational_responsible
                    for resolution in self._operational_contacts.resolve_resource_requirements(
                        requirement_ids
                    )
                }
                responsibility_by_segment = {
                    segment.segment_id: resolved_by_requirement[segment.requirement_id]
                    for segment in personal_segments
                    if segment.requirement_id in resolved_by_requirement
                }
        window_start, window_end = _planning_window(
            personal_demands,
            personal_segments,
            today=as_of,
            horizon_days=horizon_days,
        )

        actions: list[CoordinatorDashboardActionReadModel] = []
        for planning_action in self._queries.list_planning_actions(
            start=window_start,
            end=window_end,
        ):
            if (
                planning_action.kind != "ASSIGNMENT"
                or planning_action.demand_number not in personal_numbers
            ):
                continue
            actions.append(
                self._assignment_action(
                    planning_action,
                    today=as_of,
                    horizon_days=horizon_days,
                    operational_responsible=responsibility_by_segment.get(
                        planning_action.segment_id or ""
                    ),
                )
            )

        for segment in personal_segments:
            actions.extend(
                self._coverage_actions(
                    segment,
                    today=as_of,
                    horizon_days=horizon_days,
                    operational_responsible=responsibility_by_segment.get(
                        segment.segment_id
                    ),
                )
            )

        for demand, materialization in active_personal_pairs:
            cancellation = demand_cancellation_policy(
                demand,
                permissions=principal.permissions,
                materialization=materialization,
            )
            if cancellation.resolve_cancellation and demand.cancellation_request_id:
                actions.append(
                    self._cancellation_action(
                        demand,
                        materialization,
                        today=as_of,
                        horizon_days=horizon_days,
                    )
                )
            actions.extend(
                self._asset_actions(
                    demand,
                    today=as_of,
                    horizon_days=horizon_days,
                )
            )

        actions.extend(
            self._approval_actions(
                principal,
                today=as_of,
                horizon_days=horizon_days,
            )
        )

        actions = sorted(
            {row.action_id: row for row in actions}.values(),
            key=_action_sort_key,
        )
        personal_projection = tuple(
            sorted(
                (
                    self._personal_demand(
                        demand,
                        today=as_of,
                        horizon_days=horizon_days,
                    )
                    for demand in personal_demands
                ),
                key=_personal_demand_sort_key,
            )
        )

        return CoordinatorDashboardReadModel(
            as_of=as_of,
            attention_horizon_days=horizon_days,
            kpis=CoordinatorDashboardKpiReadModel(
                personal_demands=len(personal_projection),
                total_actions=len(actions),
                assignments=sum(
                    row.category == ACTION_CATEGORY_ASSIGNMENT for row in actions
                ),
                cancellations=sum(
                    row.category == ACTION_CATEGORY_CANCELLATION for row in actions
                ),
                approvals=sum(
                    row.category == ACTION_CATEGORY_APPROVAL for row in actions
                ),
                coverage_issues=sum(
                    row.category == ACTION_CATEGORY_COVERAGE for row in actions
                ),
                partial_coverages=sum(
                    row.kind == ACTION_PARTIAL_COVERAGE for row in actions
                ),
                conflicts=sum(row.kind == ACTION_CONFLICT for row in actions),
                attention_items=sum(
                    row.attention != ATTENTION_NORMAL for row in actions
                ),
            ),
            personal_demands=personal_projection,
            actions=tuple(actions),
        )

    def _personal_demand(
        self,
        demand: DemandReadModel,
        *,
        today: date,
        horizon_days: int,
    ) -> CoordinatorDashboardDemandReadModel:
        start = _demand_start(demand)
        attention, days_until_start = _attention(
            priority=demand.priority,
            start_date=start,
            emergency_override_active=bool(demand.emergency_override_active),
            today=today,
            horizon_days=horizon_days,
        )
        return CoordinatorDashboardDemandReadModel(
            demand_number=demand.number,
            project_number=demand.project_number,
            project_name=demand.project_name,
            effective_status=demand.effective_status or demand.status,
            priority=demand.priority,
            desired_start=start,
            desired_end=_demand_end(demand),
            cancellation_pending=demand.cancellation_state == "PENDING",
            attention=attention,
            days_until_start=days_until_start,
        )

    def _assignment_action(
        self,
        row: PlanningActionReadModel,
        *,
        today: date,
        horizon_days: int,
        operational_responsible: ContactResolution | None = None,
    ) -> CoordinatorDashboardActionReadModel:
        attention, days_until_start = _attention(
            priority=row.priority,
            start_date=row.start_date,
            emergency_override_active=row.emergency_override_active,
            today=today,
            horizon_days=horizon_days,
        )
        detail_parts = [
            value
            for value in (row.task_code, row.task_label, row.required_competency)
            if _text(value)
        ]
        return CoordinatorDashboardActionReadModel(
            action_id=f"{ACTION_WORKFORCE_ASSIGNMENT}:{row.reference}",
            category=ACTION_CATEGORY_ASSIGNMENT,
            kind=ACTION_WORKFORCE_ASSIGNMENT,
            label="Attribuer une ressource",
            detail=" · ".join(detail_parts) or None,
            demand_number=row.demand_number or row.reference,
            source_id=row.reference,
            project_number=row.project_number,
            project_name=row.project_name,
            status=row.status,
            priority=row.priority,
            start_date=row.start_date,
            end_date=row.end_date,
            planned_hours=row.planned_hours,
            covered_hours=None,
            remaining_hours=row.planned_hours,
            attention=attention,
            days_until_start=days_until_start,
            target=TARGET_PLANNING,
            resource_kind="WORKFORCE",
            operational_responsible_contact_id=(
                operational_responsible.contact_id
                if operational_responsible is not None
                else None
            ),
            operational_responsible_display_name=(
                operational_responsible.display_name
                if operational_responsible is not None
                else None
            ),
            operational_responsible_status=(
                operational_responsible.status
                if operational_responsible is not None
                else None
            ),
            operational_responsible_source_type=(
                operational_responsible.source_type
                if operational_responsible is not None
                else None
            ),
            operational_responsible_source_label=(
                operational_responsible.source_label
                if operational_responsible is not None
                else None
            ),
        )

    def _coverage_actions(
        self,
        segment: SegmentReadModel,
        *,
        today: date,
        horizon_days: int,
        operational_responsible: ContactResolution | None = None,
    ) -> tuple[CoordinatorDashboardActionReadModel, ...]:
        if not segment.demand_number:
            return ()
        attention, days_until_start = _attention(
            priority=segment.priority,
            start_date=segment.start_date,
            emergency_override_active=False,
            today=today,
            horizon_days=horizon_days,
        )
        result: list[CoordinatorDashboardActionReadModel] = []
        if segment.covered_hours > 0.01 and segment.remaining_hours > 0.01:
            result.append(
                CoordinatorDashboardActionReadModel(
                    action_id=f"{ACTION_PARTIAL_COVERAGE}:{segment.segment_id}",
                    category=ACTION_CATEGORY_COVERAGE,
                    kind=ACTION_PARTIAL_COVERAGE,
                    label="Couverture partielle",
                    detail=(
                        "Le besoin possède déjà des heures couvertes, "
                        "mais un reliquat demeure à planifier."
                    ),
                    demand_number=segment.demand_number,
                    source_id=segment.segment_id,
                    project_number=segment.project_number,
                    project_name=segment.project_name,
                    status=segment.status,
                    priority=segment.priority,
                    start_date=segment.start_date,
                    end_date=segment.end_date,
                    planned_hours=segment.planned_hours,
                    covered_hours=segment.covered_hours,
                    remaining_hours=segment.remaining_hours,
                    attention=attention,
                    days_until_start=days_until_start,
                    target=TARGET_PLANNING,
                    resource_kind="WORKFORCE",
                    operational_responsible_contact_id=(
                        operational_responsible.contact_id
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_display_name=(
                        operational_responsible.display_name
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_status=(
                        operational_responsible.status
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_source_type=(
                        operational_responsible.source_type
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_source_label=(
                        operational_responsible.source_label
                        if operational_responsible is not None
                        else None
                    ),
                )
            )

        conflict_details: list[str] = []
        if segment.overallocated:
            conflict_details.append(
                f"Surallocation de {segment.overallocated_hours:.1f} h"
            )
        if segment.active_day_target_met is False:
            conflict_details.append(
                segment.active_day_diagnostic
                or "La cible de jours actifs n'est pas satisfaite"
            )
        if conflict_details:
            result.append(
                CoordinatorDashboardActionReadModel(
                    action_id=f"{ACTION_CONFLICT}:{segment.segment_id}",
                    category=ACTION_CATEGORY_COVERAGE,
                    kind=ACTION_CONFLICT,
                    label="Conflit de planification",
                    detail=" · ".join(conflict_details),
                    demand_number=segment.demand_number,
                    source_id=segment.segment_id,
                    project_number=segment.project_number,
                    project_name=segment.project_name,
                    status=segment.status,
                    priority=segment.priority,
                    start_date=segment.start_date,
                    end_date=segment.end_date,
                    planned_hours=segment.planned_hours,
                    covered_hours=segment.covered_hours,
                    remaining_hours=segment.remaining_hours,
                    attention=attention,
                    days_until_start=days_until_start,
                    target=TARGET_PLANNING,
                    resource_kind="WORKFORCE",
                    operational_responsible_contact_id=(
                        operational_responsible.contact_id
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_display_name=(
                        operational_responsible.display_name
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_status=(
                        operational_responsible.status
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_source_type=(
                        operational_responsible.source_type
                        if operational_responsible is not None
                        else None
                    ),
                    operational_responsible_source_label=(
                        operational_responsible.source_label
                        if operational_responsible is not None
                        else None
                    ),
                )
            )
        return tuple(result)

    def _cancellation_action(
        self,
        demand: DemandReadModel,
        materialization: DemandCancellationMaterializationReadModel,
        *,
        today: date,
        horizon_days: int,
    ) -> CoordinatorDashboardActionReadModel:
        start = _demand_start(demand)
        attention, days_until_start = _attention(
            priority=demand.priority,
            start_date=start,
            emergency_override_active=bool(demand.emergency_override_active),
            today=today,
            horizon_days=horizon_days,
        )
        decisions = materialization.human_shift_count + materialization.asset_allocation_count
        return CoordinatorDashboardActionReadModel(
            action_id=f"{ACTION_CANCELLATION}:{demand.cancellation_request_id}",
            category=ACTION_CATEGORY_CANCELLATION,
            kind=ACTION_CANCELLATION,
            label="Traiter la demande d'annulation",
            detail=(
                f"{decisions} décision(s) opérationnelle(s) active(s)"
                if decisions
                else demand.cancellation_reason
            ),
            demand_number=demand.number,
            source_id=str(demand.cancellation_request_id),
            project_number=demand.project_number,
            project_name=demand.project_name,
            status=demand.status,
            priority=demand.priority,
            start_date=start,
            end_date=_demand_end(demand),
            planned_hours=None,
            covered_hours=None,
            remaining_hours=None,
            attention=attention,
            days_until_start=days_until_start,
            target=TARGET_DEMANDS,
            related_ids=(str(demand.cancellation_request_id),),
        )

    def _asset_actions(
        self,
        demand: DemandReadModel,
        *,
        today: date,
        horizon_days: int,
    ) -> tuple[CoordinatorDashboardActionReadModel, ...]:
        result: list[CoordinatorDashboardActionReadModel] = []
        terminal_statuses = {"annulé", "annule", "terminé", "termine"}
        for requirement in self._queries.list_demand_asset_requirements(demand.number):
            if _text(requirement.status).casefold() in terminal_statuses:
                continue
            attention, days_until_start = _attention(
                priority=demand.priority,
                start_date=requirement.start_date,
                emergency_override_active=bool(demand.emergency_override_active),
                today=today,
                horizon_days=horizon_days,
            )
            if requirement.allocation_id is None:
                result.append(
                    CoordinatorDashboardActionReadModel(
                        action_id=(
                            f"{ACTION_ASSET_ASSIGNMENT}:{requirement.requirement_id}"
                        ),
                        category=ACTION_CATEGORY_ASSIGNMENT,
                        kind=ACTION_ASSET_ASSIGNMENT,
                        label="Attribuer un actif",
                        detail=requirement.asset_type_label,
                        demand_number=demand.number,
                        source_id=requirement.requirement_id,
                        project_number=requirement.project_number,
                        project_name=demand.project_name,
                        status=requirement.status,
                        priority=demand.priority,
                        start_date=requirement.start_date,
                        end_date=requirement.end_date,
                        planned_hours=requirement.usage_hours,
                        covered_hours=None,
                        remaining_hours=requirement.usage_hours,
                        attention=attention,
                        days_until_start=days_until_start,
                        target=TARGET_PLANNING,
                        resource_kind="ASSET",
                    )
                )
            elif requirement.qualification_state != "SATISFIED":
                result.append(
                    CoordinatorDashboardActionReadModel(
                        action_id=(
                            f"{ACTION_CONFLICT}:ASSET:{requirement.requirement_id}"
                        ),
                        category=ACTION_CATEGORY_COVERAGE,
                        kind=ACTION_CONFLICT,
                        label="Conflit de qualification d'actif",
                        detail=requirement.qualification_state,
                        demand_number=demand.number,
                        source_id=requirement.requirement_id,
                        project_number=requirement.project_number,
                        project_name=demand.project_name,
                        status=requirement.status,
                        priority=demand.priority,
                        start_date=requirement.start_date,
                        end_date=requirement.end_date,
                        planned_hours=requirement.usage_hours,
                        covered_hours=None,
                        remaining_hours=None,
                        attention=attention,
                        days_until_start=days_until_start,
                        target=TARGET_PLANNING,
                        resource_kind="ASSET",
                    )
                )
        return tuple(result)

    def _approval_actions(
        self,
        principal: AuthPrincipal,
        *,
        today: date,
        horizon_days: int,
    ) -> tuple[CoordinatorDashboardActionReadModel, ...]:
        if self._approval_progress is None:
            return ()
        result: list[CoordinatorDashboardActionReadModel] = []
        pairs = self._queries.list_demands_with_cancellation_materialization()
        for demand, _materialization in pairs:
            if demand.terminal or demand.status != "Soumise":
                continue
            progress = self._approval_progress.get(
                demand.number,
                current_user_id=principal.local_user_id,
                permissions=principal.permissions,
            )
            if progress is None or not progress.actor_approvable_requirement_ids:
                continue
            start = _demand_start(demand)
            attention, days_until_start = _attention(
                priority=demand.priority,
                start_date=start,
                emergency_override_active=bool(demand.emergency_override_active),
                today=today,
                horizon_days=horizon_days,
            )
            count = len(progress.actor_approvable_requirement_ids)
            result.append(
                CoordinatorDashboardActionReadModel(
                    action_id=f"{ACTION_APPROVAL}:{progress.approval_cycle_id}",
                    category=ACTION_CATEGORY_APPROVAL,
                    kind=ACTION_APPROVAL,
                    label="Approuver mes lignes",
                    detail=(
                        f"{count} ligne(s) admissible(s) · "
                        f"{progress.satisfied_requirements}/"
                        f"{progress.total_requirements} satisfaite(s)"
                    ),
                    demand_number=demand.number,
                    source_id=progress.approval_cycle_id,
                    project_number=demand.project_number,
                    project_name=demand.project_name,
                    status=demand.status,
                    priority=demand.priority,
                    start_date=start,
                    end_date=_demand_end(demand),
                    planned_hours=None,
                    covered_hours=None,
                    remaining_hours=None,
                    attention=attention,
                    days_until_start=days_until_start,
                    target=TARGET_DEMANDS,
                    related_ids=progress.actor_approvable_requirement_ids,
                )
            )
        return tuple(result)
