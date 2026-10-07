from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Query, Request

from ..application import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationOperationError,
    ApplicationValidationError,
    DemandApprovalStateReadModel,
    DemandCancellationMaterializationReadModel,
    DemandDetailReadModel,
    DemandDetailService,
    DemandHistoryReadModel,
    DemandPeriodReadModel,
    DemandPlanDeltaReadModel,
    DemandReadModel,
    DemandRequesterReadModel,
    DemandRequesterService,
    MediumTermUnlinkedSegmentReadModel,
    OperationalContactService,
    PlannerQueryPort,
    PlanningActionReadModel,
    PlanningCapacityGridReadModel,
    PlanningSnapshotReadModel,
    ProjectReadModel,
    ResourceAvailabilityRuleReadModel,
    ResourceReadModel,
    ResourceRecommendationReadModel,
    SegmentReadModel,
    ShiftReadModel,
    WorkPackageReadModel,
)
from ..application.medium_term_budget import MediumTermBudgetReadModel
from ..application.approval_progress import ApprovalProgressService
from ..application.coordinator_dashboard import (
    CoordinatorDashboardReadModel,
    CoordinatorDashboardService,
)
from ..application.demand_cancellation import demand_cancellation_policy
from ..application.demand_workflow_policy import (
    ACTION_APPROVE,
    ACTION_CANCEL,
    ACTION_REQUEST_CANCELLATION,
    ACTION_SUBMIT,
    demand_workflow_state,
)
from ..application.query_models import PlanningHistoryReadModel
from ..application.planning_visibility import (
    PlanningVisibilityRepositoryPort,
    PlanningVisibilityResolution,
    PlanningVisibilityService,
)
from ..application.security import AuthPrincipal, PERMISSION_MANAGE_PLANNING
from ..application.user_view_context import (
    SCOPE_GLOBAL,
    SCOPE_MINE,
    UserViewContextRepositoryPort,
    UserViewContextService,
)


QueryProvider = Callable[..., Any]
ViewScope = Literal["mine", "global"]


def _project_ids_for_scope(
    request: Request,
    scope: ViewScope,
    repository: UserViewContextRepositoryPort | None,
) -> tuple[str, ...] | None:
    if repository is None:
        return None
    principal: AuthPrincipal = request.state.auth_principal
    return UserViewContextService(repository).resolve_project_scope(
        principal,
        scope,
    ).project_ids


def _demand_scope_context(
    request: Request,
    scope: ViewScope,
    repository: UserViewContextRepositoryPort | None,
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    if repository is None:
        return None, None
    principal: AuthPrincipal = request.state.auth_principal
    resolution = UserViewContextService(repository).resolve_demand_scope(
        principal,
        scope,
    )
    return resolution.project_ids, resolution.demand_ids


def _planning_visibility(
    request: Request,
    scope: ViewScope | None,
    repository: PlanningVisibilityRepositoryPort | None,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[PlanningVisibilityService, PlanningVisibilityResolution]:
    if repository is None:
        raise ApplicationOperationError(
            "Le contexte d'autorisation du Planning est indisponible.",
            code="planning_visibility_context_unavailable",
        )
    principal: AuthPrincipal = request.state.auth_principal
    service = PlanningVisibilityService(repository)
    return service, service.resolve(
        principal,
        scope,
        start=start,
        end=end,
    )


def _planning_demand_or_404(
    request: Request,
    scope: ViewScope | None,
    repository: PlanningVisibilityRepositoryPort | None,
    queries: PlannerQueryPort,
    number: str,
) -> tuple[DemandReadModel, PlanningVisibilityResolution]:
    _service, visibility = _planning_visibility(
        request,
        scope,
        repository,
    )
    row = queries.get_demand(number)
    if row is None:
        raise ApplicationNotFoundError(
            f"Demande {number} introuvable",
            code="demand_not_found",
            context={"demand_number": number},
        )
    if visibility.project_ids is not None:
        visible = queries.list_demands(
            project_ids=visibility.project_ids,
            demand_ids=visibility.demand_ids,
        )
        if not any(candidate.number == row.number for candidate in visible):
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable",
                code="demand_not_found",
                context={"demand_number": number},
            )
    return row, visibility


def _planning_segment_or_404(
    request: Request,
    scope: ViewScope | None,
    repository: PlanningVisibilityRepositoryPort | None,
    queries: PlannerQueryPort,
    segment_id: str,
) -> tuple[SegmentReadModel, PlanningVisibilityResolution]:
    _service, visibility = _planning_visibility(
        request,
        scope,
        repository,
    )
    row = queries.get_segment(segment_id)
    if row is None:
        raise ApplicationNotFoundError(
            f"Segment {segment_id} introuvable",
            code="segment_not_found",
            context={"segment_id": segment_id},
        )
    if visibility.project_ids is not None:
        visible = queries.list_segments(
            include_cancelled=True,
            project_ids=visibility.project_ids,
            demand_ids=visibility.demand_ids,
            resource_ids=visibility.segment_resource_ids,
        )
        if not any(candidate.segment_id == row.segment_id for candidate in visible):
            raise ApplicationNotFoundError(
                f"Segment {segment_id} introuvable",
                code="segment_not_found",
                context={"segment_id": segment_id},
            )
    return row, visibility


def _planning_shift_or_404(
    request: Request,
    scope: ViewScope | None,
    repository: PlanningVisibilityRepositoryPort | None,
    queries: PlannerQueryPort,
    allocation_id: str,
) -> tuple[ShiftReadModel, PlanningVisibilityResolution]:
    service, visibility = _planning_visibility(
        request,
        scope,
        repository,
    )
    if visibility.project_ids is None:
        rows = tuple(
            queries.list_shifts(
                allocation_id=allocation_id,
            )
        )
    else:
        rows = tuple(
            queries.list_shifts(
                allocation_id=allocation_id,
                project_ids=visibility.project_ids,
                demand_ids=visibility.demand_ids,
                visible_resource_ids=visibility.shift_resource_ids,
                project_day_keys=visibility.project_day_keys,
            )
        )
    row = rows[0] if rows else None
    if row is None or not service.can_read_shift_details(row, visibility):
        raise ApplicationNotFoundError(
            f"Quart {allocation_id} introuvable",
            code="shift_not_found",
            context={"allocation_id": allocation_id},
        )
    return row, visibility


def _window(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise ApplicationValidationError(
            "La date de fin ne peut pas précéder la date de début.",
            code="query_date_window_invalid",
            context={"start": start.isoformat(), "end": end.isoformat()},
        )


def _with_cancellation_policy(
    demand: DemandReadModel,
    *,
    permissions: tuple[str, ...],
    materialization: DemandCancellationMaterializationReadModel | None = None,
) -> DemandReadModel:
    policy = demand_cancellation_policy(
        demand,
        permissions=permissions,
        materialization=materialization,
    )
    return replace(demand, cancellation_policy=policy.to_dict())


def build_read_router(
    query_dependency: QueryProvider,
    user_view_context_dependency: QueryProvider | None = None,
    demand_requester_dependency: QueryProvider | None = None,
    operational_contact_dependency: QueryProvider | None = None,
    approval_progress_dependency: QueryProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["reads"])

    def no_context_repository() -> None:
        return None

    context_dependency = user_view_context_dependency or no_context_repository
    approval_dependency = approval_progress_dependency or no_context_repository
    operational_dependency = operational_contact_dependency or no_context_repository

    @router.get("/projects")
    def list_projects(
        request: Request,
        active_only: bool = False,
        scope: ViewScope = Query(default=SCOPE_GLOBAL),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[ProjectReadModel]:
        project_ids = _project_ids_for_scope(request, scope, context_repository)
        if project_ids is None:
            return list(queries.list_projects(active_only=active_only))
        return list(
            queries.list_projects(
                active_only=active_only,
                project_ids=project_ids,
            )
        )

    @router.get("/work-packages")
    def list_work_packages(
        request: Request,
        project_number: str | None = Query(default=None),
        active_only: bool = True,
        scope: ViewScope = Query(default=SCOPE_GLOBAL),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[WorkPackageReadModel]:
        project_ids = _project_ids_for_scope(request, scope, context_repository)
        if project_ids is None:
            return list(
                queries.list_work_packages(
                    project_number=project_number,
                    active_only=active_only,
                )
            )
        return list(
            queries.list_work_packages(
                project_number=project_number,
                active_only=active_only,
                project_ids=project_ids,
            )
        )

    @router.get("/medium-term/budget")
    def medium_term_budget(
        request: Request,
        project_number: str | None = Query(default=None, min_length=1),
        task_catalog_item_id: str | None = Query(default=None, min_length=1),
        task_code: str | None = Query(default=None, min_length=1),
        resource_class_code: str | None = Query(default=None, min_length=1),
        include_inactive_projects: bool = Query(default=False),
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
        scope: ViewScope = Query(default=SCOPE_GLOBAL),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> MediumTermBudgetReadModel:
        if (start is None) != (end is None):
            raise ApplicationValidationError(
                "start et end doivent être fournis ensemble pour la vue Moyen terme.",
                code="medium_term_window_pair_required",
            )
        _window(start, end)
        project_ids = _project_ids_for_scope(request, scope, context_repository)
        projection = queries.medium_term_budget_projection(
            project_number=project_number,
            task_catalog_item_id=task_catalog_item_id,
            task_code=task_code,
            resource_class_code=resource_class_code,
            include_inactive_projects=include_inactive_projects,
            project_ids=project_ids,
            start=start,
            end=end,
        )
        if projection is None:
            raise ApplicationNotFoundError(
                f"Projet {project_number} introuvable dans le périmètre demandé.",
                code="medium_term_project_not_found",
                context={"project_number": project_number},
            )
        return projection

    @router.get("/resources")
    def list_resources(
        active_only: bool = True,
        queries: PlannerQueryPort = Depends(query_dependency),
    ) -> list[ResourceReadModel]:
        return list(queries.list_resources(active_only=active_only))

    @router.get("/availability-rules")
    def list_availability_rules(
        resource_id: str | None = Query(default=None),
        include_global: bool = True,
        active_only: bool = True,
        queries: PlannerQueryPort = Depends(query_dependency),
    ) -> list[ResourceAvailabilityRuleReadModel]:
        return list(
            queries.list_availability_rules(
                resource_id=resource_id,
                include_global=include_global,
                active_only=active_only,
            )
        )

    if demand_requester_dependency is not None:

        @router.get("/demand-requesters")
        def list_demand_requesters(
            service: DemandRequesterService = Depends(demand_requester_dependency),
        ) -> list[DemandRequesterReadModel]:
            return list(service.list_admissible())

    @router.get("/demands")
    def list_demands(
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
        approvals: ApprovalProgressService | None = Depends(approval_dependency),
    ) -> list[DemandReadModel]:
        _service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
        )
        project_ids = visibility.project_ids
        demand_ids = visibility.demand_ids
        combined_reader = getattr(
            queries,
            "list_demands_with_cancellation_materialization",
            None,
        )
        if callable(combined_reader):
            pairs = tuple(
                combined_reader()
                if project_ids is None and demand_ids is None
                else combined_reader(
                    project_ids=project_ids,
                    demand_ids=demand_ids,
                )
            )
        else:
            rows = (
                tuple(queries.list_demands())
                if project_ids is None and demand_ids is None
                else tuple(
                    queries.list_demands(
                        project_ids=project_ids,
                        demand_ids=demand_ids,
                    )
                )
            )
            pairs = tuple((row, None) for row in rows)
        principal: AuthPrincipal = request.state.auth_principal
        approvable_numbers = set(
            approvals.actor_approvable_demand_numbers(
                tuple(row.number for row, _ in pairs),
                current_user_id=principal.local_user_id,
                permissions=principal.permissions,
            )
            if approvals is not None
            else ()
        )
        quick_action_order = (
            ACTION_SUBMIT,
            ACTION_APPROVE,
            ACTION_CANCEL,
            ACTION_REQUEST_CANCELLATION,
        )
        projected: list[DemandReadModel] = []
        for row, materialization in pairs:
            workflow = demand_workflow_state(
                row,
                permissions=principal.permissions,
                materialization=materialization,
            )
            available = set(workflow.available_actions)
            if row.number not in approvable_numbers:
                available.discard(ACTION_APPROVE)
            projected.append(
                replace(
                    row,
                    cancellation_policy=(
                        workflow.cancellation.to_dict()
                        if workflow.cancellation is not None
                        else None
                    ),
                    available_quick_actions=tuple(
                        action for action in quick_action_order if action in available
                    ),
                )
            )
        return projected

    @router.get("/coordinator-dashboard")
    def coordinator_dashboard(
        request: Request,
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
        approvals: ApprovalProgressService | None = Depends(approval_dependency),
        operational_contacts: OperationalContactService | None = Depends(
            operational_dependency
        ),
    ) -> CoordinatorDashboardReadModel:
        if context_repository is None:
            raise ApplicationOperationError(
                "Le contexte utilisateur du dashboard coordonnateur est indisponible.",
                code="coordinator_dashboard_context_unavailable",
            )
        principal: AuthPrincipal = request.state.auth_principal
        return CoordinatorDashboardService(
            queries,
            UserViewContextService(context_repository),
            approvals,
            operational_contacts,
        ).read(principal)

    @router.get("/demands/{number}")
    def get_demand(
        number: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> DemandReadModel:
        scoped_row, _visibility = _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        combined_reader = getattr(
            queries,
            "get_demand_with_cancellation_materialization",
            None,
        )
        combined = combined_reader(number) if callable(combined_reader) else None
        if combined is not None:
            row, materialization = combined
        else:
            row = scoped_row
            materialization = None
        principal: AuthPrincipal = request.state.auth_principal
        return _with_cancellation_policy(
            row,
            permissions=principal.permissions,
            materialization=materialization,
        )

    if operational_contact_dependency is not None:

        @router.get("/demands/{number}/detail")
        def get_demand_detail(
            number: str,
            request: Request,
            scope: ViewScope | None = Query(default=None),
            queries: PlannerQueryPort = Depends(query_dependency),
            context_repository: Any = Depends(context_dependency),
            contacts: OperationalContactService = Depends(
                operational_contact_dependency
            ),
            approvals: ApprovalProgressService | None = Depends(
                approval_dependency
            ),
        ) -> DemandDetailReadModel:
            _planning_demand_or_404(
                request,
                scope,
                context_repository,
                queries,
                number,
            )
            principal: AuthPrincipal = request.state.auth_principal
            return DemandDetailService(queries, contacts, approvals).get(
                number,
                permissions=principal.permissions,
                current_user_id=principal.local_user_id,
            )

    @router.get("/demands/{number}/history")
    def list_demand_history(
        number: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[DemandHistoryReadModel]:
        _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        return list(queries.list_demand_history(number))

    @router.get("/demands/{number}/periods")
    def list_demand_periods(
        number: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[DemandPeriodReadModel]:
        demand, _visibility = _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        if demand.line_mode:
            raise ApplicationConflictError(
                "Les périodes d'une demande multi-lignes doivent être lues par ligne.",
                code="demand_line_period_scope_required",
                context={"demand_number": number},
            )
        return list(queries.list_demand_periods(number))

    @router.get("/demands/{number}/lines/{line_id}/periods")
    def list_demand_line_periods(
        number: str,
        line_id: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[DemandPeriodReadModel]:
        demand, _visibility = _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        if not demand.line_mode:
            raise ApplicationConflictError(
                "Les demandes historiques à une ligne utilisent l'endpoint de périodes de la demande.",
                code="demand_legacy_period_scope_invalid",
                context={"demand_number": number},
            )
        if not any(row.active and row.line_id == line_id for row in demand.lines):
            raise ApplicationNotFoundError(
                f"Ligne {line_id} introuvable pour la demande {number}",
                code="demand_line_not_found",
                context={"demand_number": number, "request_line_id": line_id},
            )
        return list(
            queries.list_demand_periods(
                number,
                request_line_id=line_id,
            )
        )

    @router.get("/demands/{number}/approval-state")
    def demand_approval_state(
        number: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> DemandApprovalStateReadModel:
        _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        row = queries.demand_approval_state(number)
        if row is None:
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable",
                code="demand_not_found",
                context={"demand_number": number},
            )
        return row

    @router.get("/demands/{number}/plan-delta")
    def demand_plan_delta(
        number: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> DemandPlanDeltaReadModel:
        _planning_demand_or_404(
            request,
            scope,
            context_repository,
            queries,
            number,
        )
        row = queries.demand_plan_delta(number)
        if row is None:
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable",
                code="demand_not_found",
                context={"demand_number": number},
            )
        return row

    @router.get("/segments")
    def list_segments(
        request: Request,
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
        include_cancelled: bool = False,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[SegmentReadModel]:
        _window(start, end)
        _service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
            start=start,
            end=end,
        )
        if visibility.project_ids is None:
            return list(
                queries.list_segments(
                    start=start,
                    end=end,
                    include_cancelled=include_cancelled,
                )
            )
        return list(
            queries.list_segments(
                start=start,
                end=end,
                include_cancelled=include_cancelled,
                project_ids=visibility.project_ids,
                demand_ids=visibility.demand_ids,
                resource_ids=visibility.segment_resource_ids,
            )
        )

    @router.get("/segments/{segment_id}")
    def get_segment(
        segment_id: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> SegmentReadModel:
        row, _visibility = _planning_segment_or_404(
            request,
            scope,
            context_repository,
            queries,
            segment_id,
        )
        return row

    @router.get("/segments/{segment_id}/history")
    def list_segment_history(
        segment_id: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[PlanningHistoryReadModel]:
        _planning_segment_or_404(
            request,
            scope,
            context_repository,
            queries,
            segment_id,
        )
        return list(queries.list_planning_history("SEGMENT", segment_id))

    @router.get("/shifts")
    def list_shifts(
        request: Request,
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
        resource_name: str | None = Query(default=None),
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[ShiftReadModel]:
        _window(start, end)
        service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
            start=start,
            end=end,
        )
        principal: AuthPrincipal = request.state.auth_principal
        can_manage_planning = principal.has_permission(
            PERMISSION_MANAGE_PLANNING
        )
        if visibility.project_ids is None:
            rows = queries.list_shifts(
                start=start,
                end=end,
                resource_name=resource_name,
                can_manage_planning=can_manage_planning,
            )
        else:
            rows = queries.list_shifts(
                start=start,
                end=end,
                resource_name=resource_name,
                project_ids=visibility.project_ids,
                demand_ids=visibility.demand_ids,
                visible_resource_ids=visibility.shift_resource_ids,
                project_day_keys=visibility.project_day_keys,
                can_manage_planning=can_manage_planning,
            )
        return [service.project_shift(row, visibility) for row in rows]

    @router.get("/shifts/{allocation_id}/history")
    def list_shift_history(
        allocation_id: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[PlanningHistoryReadModel]:
        _planning_shift_or_404(
            request,
            scope,
            context_repository,
            queries,
            allocation_id,
        )
        return list(queries.list_planning_history("SHIFT", allocation_id))

    @router.get("/medium-term/unlinked-segments")
    def medium_term_unlinked_segments(
        request: Request,
        start: date = Query(),
        end: date = Query(),
        scope: ViewScope = Query(default=SCOPE_GLOBAL),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[MediumTermUnlinkedSegmentReadModel]:
        _window(start, end)
        project_ids = _project_ids_for_scope(request, scope, context_repository)
        if project_ids is None:
            return list(
                queries.list_medium_term_unlinked_segments(
                    start=start,
                    end=end,
                )
            )
        return list(
            queries.list_medium_term_unlinked_segments(
                start=start,
                end=end,
                project_ids=project_ids,
            )
        )

    @router.get("/planning/capacity-grid")
    def planning_capacity_grid(
        request: Request,
        start: date = Query(),
        end: date = Query(),
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> PlanningCapacityGridReadModel:
        _window(start, end)
        service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
            start=start,
            end=end,
        )
        if visibility.project_ids is None:
            return queries.planning_capacity_grid(start=start, end=end)
        grid = queries.planning_capacity_grid(
            start=start,
            end=end,
            project_ids=visibility.project_ids,
            demand_ids=visibility.demand_ids,
            include_resource_ids=visibility.include_resource_ids,
            shift_resource_ids=visibility.shift_resource_ids,
            segment_resource_ids=visibility.segment_resource_ids,
            project_day_keys=visibility.project_day_keys,
        )
        scoped_shifts = tuple(
            queries.list_shifts(
                start=start,
                end=end,
                project_ids=visibility.project_ids,
                demand_ids=visibility.demand_ids,
                visible_resource_ids=visibility.shift_resource_ids,
                project_day_keys=visibility.project_day_keys,
            )
        )
        return service.project_capacity(
            grid,
            scoped_shifts=scoped_shifts,
            resolution=visibility,
        )

    @router.get("/planning/actions")
    def planning_actions(
        request: Request,
        start: date = Query(),
        end: date = Query(),
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[PlanningActionReadModel]:
        _window(start, end)
        _service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
            start=start,
            end=end,
        )
        if visibility.project_ids is None:
            return list(queries.list_planning_actions(start=start, end=end))
        return list(
            queries.list_planning_actions(
                start=start,
                end=end,
                project_ids=visibility.project_ids,
                demand_ids=visibility.demand_ids,
                resource_ids=visibility.segment_resource_ids,
            )
        )

    @router.get("/segments/{segment_id}/resource-recommendations")
    def resource_recommendations(
        segment_id: str,
        request: Request,
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> list[ResourceRecommendationReadModel]:
        _planning_segment_or_404(
            request,
            scope,
            context_repository,
            queries,
            segment_id,
        )
        return list(queries.recommend_resources(segment_id))

    @router.get("/planning/snapshot")
    def planning_snapshot(
        request: Request,
        start: date = Query(),
        end: date = Query(),
        scope: ViewScope | None = Query(default=None),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> PlanningSnapshotReadModel:
        _window(start, end)
        service, visibility = _planning_visibility(
            request,
            scope,
            context_repository,
            start=start,
            end=end,
        )
        principal: AuthPrincipal = request.state.auth_principal
        can_manage_planning = principal.has_permission(
            PERMISSION_MANAGE_PLANNING
        )
        if visibility.project_ids is None:
            return queries.planning_snapshot(
                start=start,
                end=end,
                can_manage_planning=can_manage_planning,
            )
        snapshot = queries.planning_snapshot(
            start=start,
            end=end,
            project_ids=visibility.project_ids,
            demand_ids=visibility.demand_ids,
            include_resource_ids=visibility.include_resource_ids,
            shift_resource_ids=visibility.shift_resource_ids,
            segment_resource_ids=visibility.segment_resource_ids,
            project_day_keys=visibility.project_day_keys,
            can_manage_planning=can_manage_planning,
        )
        return service.project_snapshot(snapshot, visibility)

    return router
