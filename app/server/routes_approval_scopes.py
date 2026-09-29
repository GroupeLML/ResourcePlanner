from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from ..application.approval_scopes import (
    ApprovalScopeRecord,
    ApprovalScopeService,
)


ApprovalScopeProvider = Callable[..., Any]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalScopeCreateRequest(StrictRequest):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    active: bool = True


class ApprovalScopeUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None


class ApprovalScopeAssociationRequest(StrictRequest):
    expected_version: int = Field(ge=1)


def _scope_payload(row: ApprovalScopeRecord) -> dict[str, object]:
    return {
        "id": row.id,
        "code": row.code,
        "label": row.label,
        "active": row.active,
        "version": row.version,
        "approver_user_ids": list(row.approver_user_ids),
        "resource_class_codes": list(row.resource_class_codes),
        "task_catalog_item_ids": list(row.task_catalog_item_ids),
    }


def build_approval_scope_router(
    dependency: ApprovalScopeProvider,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/approval-scopes",
        tags=["approval-scopes"],
    )

    @router.get("")
    def list_scopes(
        service: ApprovalScopeService = Depends(dependency),
    ) -> list[dict[str, object]]:
        return [_scope_payload(row) for row in service.list_scopes()]

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_scope(
        body: ApprovalScopeCreateRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.create_scope(
                code=body.code,
                label=body.label,
                active=body.active,
            )
        )

    @router.patch("/{scope_id}")
    def update_scope(
        scope_id: str,
        body: ApprovalScopeUpdateRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.update_scope(
                scope_id,
                expected_version=body.expected_version,
                label=body.label,
                active=body.active,
            )
        )

    @router.put("/{scope_id}/approvers/{user_id}")
    def assign_approver(
        scope_id: str,
        user_id: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_approver(
                scope_id,
                user_id,
                assigned=True,
                expected_version=body.expected_version,
            )
        )

    @router.delete("/{scope_id}/approvers/{user_id}")
    def remove_approver(
        scope_id: str,
        user_id: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_approver(
                scope_id,
                user_id,
                assigned=False,
                expected_version=body.expected_version,
            )
        )

    @router.put("/{scope_id}/resource-classes/{class_code}")
    def assign_resource_class(
        scope_id: str,
        class_code: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_resource_class(
                scope_id,
                class_code,
                assigned=True,
                expected_version=body.expected_version,
            )
        )

    @router.delete("/{scope_id}/resource-classes/{class_code}")
    def remove_resource_class(
        scope_id: str,
        class_code: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_resource_class(
                scope_id,
                class_code,
                assigned=False,
                expected_version=body.expected_version,
            )
        )

    @router.put("/{scope_id}/tasks/{task_id}")
    def assign_task(
        scope_id: str,
        task_id: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_task(
                scope_id,
                task_id,
                assigned=True,
                expected_version=body.expected_version,
            )
        )

    @router.delete("/{scope_id}/tasks/{task_id}")
    def remove_task(
        scope_id: str,
        task_id: str,
        body: ApprovalScopeAssociationRequest,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        return _scope_payload(
            service.set_task(
                scope_id,
                task_id,
                assigned=False,
                expected_version=body.expected_version,
            )
        )

    @router.get("/request-lines/{line_id}/resolution")
    def resolve_request_line(
        line_id: str,
        service: ApprovalScopeService = Depends(dependency),
    ) -> dict[str, object]:
        row = service.resolve_request_line(line_id)
        return {
            "request_line_id": row.request_line_id,
            "task_catalog_item_id": row.task_catalog_item_id,
            "suggested_scope_code": row.suggested_scope_code,
            "approval_scope_id": row.resolution.approval_scope_id,
            "eligible_approvers": [
                {
                    "app_user_id": item.user_id,
                    "sources": list(item.sources),
                }
                for item in row.resolution.eligible_approvers
            ],
            "diagnostics": list(row.resolution.diagnostics),
            "blocked": row.resolution.blocked,
        }

    return router
