from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ..application.security import AuthPrincipal, UserIdentityRecord
from ..application.user_admin import UserAdminService, UserRoleDefinition


UserAdminProvider = Callable[..., Any]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserCreateRequest(StrictRequest):
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    email: str | None = None
    phone: str | None = None
    roles: list[str] = Field(min_length=1)
    active: bool = True


class UserUpdateRequest(StrictRequest):
    display_name: str = Field(min_length=1)
    email: str | None = None
    phone: str | None = None
    roles: list[str] = Field(min_length=1)
    active: bool = True


def _user_payload(record: UserIdentityRecord) -> dict[str, object]:
    return {
        "user_id": record.user_id,
        "app_user_id": record.user_id,
        "issuer": record.issuer,
        "subject": record.subject,
        "display_name": record.display_name,
        "email": record.email,
        "employee_external_id": record.employee_external_id,
        "erp_user_id": record.erp_user_id,
        "business_contact_id": record.business_contact_id,
        "phone": record.phone,
        "roles": list(record.roles),
        "active": record.active,
        "oidc_state": record.oidc_state,
    }


def _role_payload(role: UserRoleDefinition) -> dict[str, object]:
    return role.to_dict()


def build_user_admin_router(user_admin_dependency: UserAdminProvider) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/users", tags=["user-admin"])

    @router.get("")
    def list_users(
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> list[dict[str, object]]:
        return [_user_payload(record) for record in service.list_users()]

    @router.get("/roles")
    def list_roles(
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> list[dict[str, object]]:
        return [_role_payload(role) for role in service.role_catalog()]

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_user(
        body: UserCreateRequest,
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> dict[str, object]:
        return _user_payload(
            service.create_user(
                issuer=body.issuer,
                subject=body.subject,
                display_name=body.display_name,
                email=body.email,
                phone=body.phone,
                roles=tuple(body.roles),
                active=body.active,
            )
        )

    @router.patch("/{user_id}")
    def update_user(
        user_id: str,
        body: UserUpdateRequest,
        request: Request,
        service: UserAdminService = Depends(user_admin_dependency),
    ) -> dict[str, object]:
        principal: AuthPrincipal = request.state.auth_principal
        return _user_payload(
            service.update_user(
                user_id,
                display_name=body.display_name,
                email=body.email,
                phone=body.phone,
                roles=tuple(body.roles),
                active=body.active,
                actor_user_id=principal.local_user_id,
            )
        )

    return router
