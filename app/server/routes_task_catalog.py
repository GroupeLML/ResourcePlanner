from __future__ import annotations

from collections.abc import Iterator
from typing import Callable

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..application.security import AuthPrincipal
from ..application.task_catalog import TaskCatalogItem
from ..infrastructure.sql.task_catalog_repository import SqlTaskCatalogRepository


SessionProvider = Callable[[], Iterator[Session]]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreferredResourceMutationRequest(StrictRequest):
    resource_id: str | None = None
    expected_version: int = Field(ge=1)


def _actor_user_id(request: Request) -> str | None:
    principal: AuthPrincipal = request.state.auth_principal
    return str(principal.local_user_id or "").strip() or None


def build_task_catalog_router(session_dependency: SessionProvider) -> APIRouter:
    router = APIRouter(prefix="/api/v1/task-catalog", tags=["task-catalog"])

    @router.get("")
    def search_task_catalog(
        project_number: str | None = Query(default=None),
        q: str | None = Query(default=None),
        active_only: bool = True,
        limit: int = Query(default=200, ge=1, le=500),
        session: Session = Depends(session_dependency),
    ) -> list[TaskCatalogItem]:
        return list(
            SqlTaskCatalogRepository(session).search(
                project_number=project_number,
                query=q,
                active_only=active_only,
                limit=limit,
            )
        )

    @router.patch("/{task_catalog_item_id}/preferred-resource")
    def set_preferred_resource(
        task_catalog_item_id: str,
        body: PreferredResourceMutationRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        result = SqlTaskCatalogRepository(session).set_preferred_resource(
            task_catalog_item_id,
            body.resource_id,
            actor_user_id=_actor_user_id(request),
            expected_version=body.expected_version,
        )
        return {
            "task_catalog_item_id": result.task_catalog_item_id,
            "preferred_resource_id": result.preferred_resource_id,
            "version": result.version,
            "action": result.action,
        }

    return router
