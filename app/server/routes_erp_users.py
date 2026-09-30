from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..application.erp_user_directory import ErpUserDirectoryRecord
from ..application.security import AuthPrincipal
from ..application.user_admin import UserAdminService


UserAdminProvider = Callable[..., Any]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErpUserAccessUpdate(StrictRequest):
    active: bool
    roles: list[str] = Field(default_factory=list)


def _payload(record: ErpUserDirectoryRecord) -> dict[str, object]:
    return {
        "user_id": record.user_id,
        "erp_user_id": record.user_id,
        "employee_external_id": record.employee_external_id,
        "display_name": record.display_name,
        "first_name": record.first_name,
        "last_name": record.last_name,
        "email": record.email,
        "erp_user_active": record.erp_user_active,
        "employee_status": record.employee_status,
        "source_admissible": record.source_admissible,
        "local_active": record.local_active,
        "active": record.local_active,
        "roles": list(record.roles),
        "resource_id": record.resource_id,
        "resource_name": record.resource_name,
        "resource_erp_active": record.resource_erp_active,
        "app_user_id": record.app_user_id,
        "issuer": record.issuer,
        "subject": record.subject,
        "oidc_state": record.oidc_state,
        "access_ready": record.access_ready,
    }


def build_erp_user_admin_router(user_admin_dependency: UserAdminProvider) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/erp-users", tags=["user-admin"])

    @router.get("")
    def list_erp_users(
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> list[dict[str, object]]:
        return [_payload(record) for record in service.list_erp_users()]

    @router.patch("/{user_id}")
    def update_erp_user_access(
        user_id: str,
        body: ErpUserAccessUpdate,
        request: Request,
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> dict[str, object]:
        principal: AuthPrincipal = request.state.auth_principal
        return _payload(
            service.update_erp_user_access(
                user_id,
                active=body.active,
                roles=tuple(body.roles),
                actor_user_id=principal.local_user_id,
            )
        )

    return router
