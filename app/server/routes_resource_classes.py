from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..application.resource_classes import (
    BudgetHoursProjection,
    ProjectTaskClassOverrideRecord,
    ProjectTaskClassRule,
    ResourceClassConfigRecord,
    ResourceClassConfigurationService,
    TaskClassResolution,
    TaskClassStandardRecord,
)
from ..infrastructure.sql.resource_class_repository import (
    SqlResourceClassRepository,
)


SessionProvider = Callable[..., Any]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceClassCreateRequest(StrictRequest):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    average_hourly_cost_cad: Decimal | None = None
    active: bool = True


class ResourceClassUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=255)
    average_hourly_cost_cad: Decimal | None = None
    active: bool | None = None


class TaskClassStandardCreateRequest(StrictRequest):
    task_code: str = Field(min_length=1, max_length=64)
    resource_class_code: str = Field(min_length=1, max_length=64)
    active: bool = True


class TaskClassStandardUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    resource_class_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    active: bool | None = None


class ProjectTaskClassOverrideRequest(StrictRequest):
    expected_version: int | None = Field(default=None, ge=1)
    resource_class_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    excluded: bool = False


class ProjectTaskClassOverrideDeleteRequest(StrictRequest):
    expected_version: int = Field(ge=1)


class BudgetProjectionRequest(StrictRequest):
    budget_amount_cad: Decimal


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _class_payload(row: ResourceClassConfigRecord) -> dict[str, object]:
    return {
        "code": row.code,
        "label": row.label,
        "average_hourly_cost_cad": _decimal_text(
            row.average_hourly_cost_cad
        ),
        "active": row.active,
        "version": row.version,
    }


def _operational_class_payload(
    row: ResourceClassConfigRecord,
) -> dict[str, object]:
    return {
        "code": row.code,
        "label": row.label,
        "active": row.active,
    }


def _standard_payload(row: TaskClassStandardRecord) -> dict[str, object]:
    return {
        "task_code": row.task_code,
        "resource_class_code": row.resource_class_code,
        "active": row.active,
        "version": row.version,
    }


def _override_payload(
    row: ProjectTaskClassOverrideRecord,
) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "task_code": row.task_code,
        "resource_class_code": row.resource_class_code,
        "excluded": row.excluded,
        "version": row.version,
    }


def _resolution_payload(row: TaskClassResolution) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "task_code": row.task_code,
        "status": row.status,
        "resource_class_code": row.resource_class_code,
        "configured_resource_class_code": (
            row.configured_resource_class_code
        ),
        "source": row.source,
        "diagnostics": list(row.diagnostics),
    }


def _rule_payload(row: ProjectTaskClassRule) -> dict[str, object]:
    return {
        "task_code": row.task_code,
        "standard": (
            _standard_payload(row.standard)
            if row.standard is not None
            else None
        ),
        "override": (
            _override_payload(row.override)
            if row.override is not None
            else None
        ),
        "resolution": _resolution_payload(row.resolution),
    }


def _projection_payload(
    row: BudgetHoursProjection,
) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "task_code": row.task_code,
        "budget_amount_cad": _decimal_text(row.budget_amount_cad),
        "resource_class_code": row.resource_class_code,
        "average_hourly_cost_cad": _decimal_text(
            row.average_hourly_cost_cad
        ),
        "budget_hours": _decimal_text(row.budget_hours),
        "resolution_status": row.resolution_status,
        "diagnostics": list(row.diagnostics),
    }


def build_operational_resource_class_router(
    session_dependency: SessionProvider,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/resource-classes",
        tags=["resource-classes"],
    )

    @router.get("")
    def list_resource_classes(
        session: Session = Depends(session_dependency),
    ) -> list[dict[str, object]]:
        service = ResourceClassConfigurationService(
            SqlResourceClassRepository(session)
        )
        return [
            _operational_class_payload(row)
            for row in service.list_resource_classes()
        ]

    return router


def build_resource_class_router(
    session_dependency: SessionProvider,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/resource-classes",
        tags=["resource-classes"],
    )

    def service(session: Session) -> ResourceClassConfigurationService:
        return ResourceClassConfigurationService(
            SqlResourceClassRepository(session)
        )

    @router.get("")
    def list_resource_classes(
        session: Session = Depends(session_dependency),
    ) -> list[dict[str, object]]:
        return [
            _class_payload(row)
            for row in service(session).list_resource_classes()
        ]

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_resource_class(
        body: ResourceClassCreateRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _class_payload(
            service(session).create_resource_class(
                code=body.code,
                label=body.label,
                average_hourly_cost_cad=body.average_hourly_cost_cad,
                active=body.active,
            )
        )

    @router.patch("/{class_code}")
    def update_resource_class(
        class_code: str,
        body: ResourceClassUpdateRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _class_payload(
            service(session).update_resource_class(
                class_code,
                expected_version=body.expected_version,
                label=body.label,
                average_hourly_cost_cad=body.average_hourly_cost_cad,
                average_hourly_cost_supplied=(
                    "average_hourly_cost_cad"
                    in body.model_fields_set
                ),
                active=body.active,
            )
        )

    @router.get("/task-standards")
    def list_task_standards(
        session: Session = Depends(session_dependency),
    ) -> list[dict[str, object]]:
        return [
            _standard_payload(row)
            for row in service(session).list_task_standards()
        ]

    @router.post(
        "/task-standards",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_standard(
        body: TaskClassStandardCreateRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _standard_payload(
            service(session).create_task_standard(
                task_code=body.task_code,
                resource_class_code=body.resource_class_code,
                active=body.active,
            )
        )

    @router.patch("/task-standards/{task_code}")
    def update_task_standard(
        task_code: str,
        body: TaskClassStandardUpdateRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _standard_payload(
            service(session).update_task_standard(
                task_code,
                expected_version=body.expected_version,
                resource_class_code=body.resource_class_code,
                active=body.active,
            )
        )

    @router.get("/projects/{project_id}/task-rules")
    def list_project_task_rules(
        project_id: str,
        session: Session = Depends(session_dependency),
    ) -> list[dict[str, object]]:
        return [
            _rule_payload(row)
            for row in service(session).list_project_task_rules(
                project_id
            )
        ]

    @router.put(
        "/projects/{project_id}/task-overrides/{task_code}"
    )
    def set_project_task_override(
        project_id: str,
        task_code: str,
        body: ProjectTaskClassOverrideRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _override_payload(
            service(session).set_project_override(
                project_id,
                task_code,
                resource_class_code=body.resource_class_code,
                excluded=body.excluded,
                expected_version=body.expected_version,
            )
        )

    @router.delete(
        "/projects/{project_id}/task-overrides/{task_code}"
    )
    def delete_project_task_override(
        project_id: str,
        task_code: str,
        body: ProjectTaskClassOverrideDeleteRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _resolution_payload(
            service(session).remove_project_override(
                project_id,
                task_code,
                expected_version=body.expected_version,
            )
        )

    @router.get(
        "/projects/{project_id}/tasks/{task_code}/resolution"
    )
    def resolve_project_task_class(
        project_id: str,
        task_code: str,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _resolution_payload(
            service(session).resolve(project_id, task_code)
        )

    @router.post(
        "/projects/{project_id}/tasks/{task_code}/budget-projection"
    )
    def project_budget_hours(
        project_id: str,
        task_code: str,
        body: BudgetProjectionRequest,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return _projection_payload(
            service(session).project_budget_hours(
                project_id,
                task_code,
                body.budget_amount_cad,
            )
        )

    return router
