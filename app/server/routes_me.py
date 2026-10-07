from __future__ import annotations

from datetime import date
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request

from ..application.planning_visibility import (
    PlanningViewPolicyReadModel,
    PlanningVisibilityService,
)
from ..application.query_ports import PlannerQueryPort
from ..application.security import AuthPrincipal
from ..application.technician_schedule import TechnicianScheduleReadModel, TechnicianScheduleService
from ..application.user_view_context import (
    UserViewContextReadModel,
    UserViewContextRepositoryPort,
    UserViewContextService,
)


QueryProvider = Callable[..., Any]


def build_me_router(
    query_dependency: QueryProvider,
    user_view_context_dependency: QueryProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["me"])

    def no_context_repository() -> None:
        return None

    context_dependency = user_view_context_dependency or no_context_repository

    @router.get("/planning-policy")
    def planning_policy(request: Request) -> PlanningViewPolicyReadModel:
        principal: AuthPrincipal = request.state.auth_principal
        return PlanningVisibilityService.view_policy(principal)

    @router.get("/schedule")
    def schedule(
        request: Request,
        start: date = Query(),
        end: date = Query(),
        queries: PlannerQueryPort = Depends(query_dependency),
        context_repository: Any = Depends(context_dependency),
    ) -> TechnicianScheduleReadModel:
        principal: AuthPrincipal = request.state.auth_principal
        return TechnicianScheduleService(
            queries,
            resource_resolver=context_repository,
        ).read(
            principal=principal,
            start=start,
            end=end,
        )

    if user_view_context_dependency is not None:

        @router.get("/context")
        def context(
            request: Request,
            repository: Any = Depends(context_dependency),
        ) -> UserViewContextReadModel:
            principal: AuthPrincipal = request.state.auth_principal
            return UserViewContextService(repository).read(principal)

    return router
