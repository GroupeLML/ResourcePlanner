from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..application.errors import ApplicationNotFoundError
from ..application.project_manager_admin import (
    ProjectManagerAdminService,
    ProjectManagersReadModel,
)
from ..application.project_managers import EffectiveProjectManager
from ..application.security import AuthPrincipal
from ..application.user_view_context import (
    SCOPE_GLOBAL,
    UserViewContextRepositoryPort,
    UserViewContextService,
)


ProjectManagerAdminProvider = Callable[..., Any]
UserViewContextProvider = Callable[..., Any]
ViewScope = Literal["mine", "global"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCoManagerMutationRequest(StrictRequest):
    expected_version: int = Field(ge=1)


def _manager_payload(row: EffectiveProjectManager) -> dict[str, object]:
    return {
        "sources": list(row.sources),
        "employee_external_id": row.employee_external_id,
        "erp_display_name": row.erp_display_name,
        "app_user_id": row.app_user_id,
        "business_contact_id": row.business_contact_id,
        "display_name": row.display_name,
        "user_active": row.user_active,
        "contact_active": row.contact_active,
        "resolution_status": row.resolution_status,
        "diagnostics": list(row.diagnostics),
    }


def _read_payload(row: ProjectManagersReadModel) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "project_number": row.project_number,
        "co_managers_version": row.co_managers_version,
        "primary": _manager_payload(row.primary) if row.primary is not None else None,
        "co_managers": [_manager_payload(manager) for manager in row.co_managers],
        "diagnostics": list(row.diagnostics),
    }


def _mutation_payload(row: Any) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "business_contact_id": row.business_contact_id,
        "version": row.version,
        "action": row.action,
    }


def _actor_user_id(request: Request) -> str | None:
    principal: AuthPrincipal = request.state.auth_principal
    return str(principal.local_user_id or "").strip() or None


def build_project_manager_admin_router(
    dependency: ProjectManagerAdminProvider,
    user_view_context_dependency: UserViewContextProvider,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["project-managers"])

    @router.get("/projects/{project_number}/managers")
    def project_managers(
        project_number: str,
        request: Request,
        scope: ViewScope = Query(default=SCOPE_GLOBAL),
        service: ProjectManagerAdminService = Depends(dependency),
        context_repository: UserViewContextRepositoryPort = Depends(
            user_view_context_dependency
        ),
    ) -> dict[str, object]:
        row = service.get_managers(project_number)
        principal: AuthPrincipal = request.state.auth_principal
        visible_project_ids = UserViewContextService(
            context_repository
        ).resolve_project_scope(principal, scope).project_ids
        if visible_project_ids is not None and row.project_id not in visible_project_ids:
            raise ApplicationNotFoundError(
                f"Projet {project_number} introuvable dans le périmètre demandé.",
                code="project_not_found",
                context={"project_number": project_number, "scope": scope},
            )
        return _read_payload(row)

    @router.put(
        "/projects/{project_number}/co-managers/{business_contact_id}"
    )
    def add_project_co_manager(
        project_number: str,
        business_contact_id: str,
        body: ProjectCoManagerMutationRequest,
        request: Request,
        idempotency_key: str = Header(
            min_length=1,
            alias="Idempotency-Key",
        ),
        service: ProjectManagerAdminService = Depends(dependency),
    ) -> dict[str, object]:
        return _mutation_payload(
            service.add_co_manager(
                project_number,
                business_contact_id,
                actor_user_id=_actor_user_id(request),
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @router.delete(
        "/projects/{project_number}/co-managers/{business_contact_id}"
    )
    def remove_project_co_manager(
        project_number: str,
        business_contact_id: str,
        body: ProjectCoManagerMutationRequest,
        request: Request,
        idempotency_key: str = Header(
            min_length=1,
            alias="Idempotency-Key",
        ),
        service: ProjectManagerAdminService = Depends(dependency),
    ) -> dict[str, object]:
        return _mutation_payload(
            service.remove_co_manager(
                project_number,
                business_contact_id,
                actor_user_id=_actor_user_id(request),
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    return router
