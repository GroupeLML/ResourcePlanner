from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request, status

from ..application import (
    CompetencyCatalogService,
    CompetencyCreateCommand,
    CompetencyReadModel,
    CompetencyUpdateCommand,
)
from .schemas import (
    CompetencyCreateRequest,
    CompetencyResourceClassUpdateRequest,
    CompetencyUpdateRequest,
)


CompetencyProvider = Callable[..., Any]


def _actor_user_id(request: Request) -> str | None:
    principal = request.state.auth_principal
    return str(principal.local_user_id or "").strip() or None


def build_competency_router(competency_dependency: CompetencyProvider) -> APIRouter:
    router = APIRouter(prefix="/api/v1/competencies", tags=["competencies"])

    @router.get("")
    def list_competencies(
        q: str | None = Query(default=None),
        active_only: bool = True,
        limit: int = Query(default=200, ge=1, le=500),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> list[CompetencyReadModel]:
        return list(
            competencies.list(
                query=q,
                active_only=active_only,
                limit=limit,
            )
        )

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_competency(
        body: CompetencyCreateRequest,
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, object]:
        return competencies.create(
            CompetencyCreateCommand(**body.model_dump())
        ).to_dict()

    @router.patch("/{competency_id}")
    def update_competency(
        competency_id: str,
        body: CompetencyUpdateRequest,
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, object]:
        return competencies.update(
            CompetencyUpdateCommand(
                competency_id=competency_id,
                **body.model_dump(exclude_unset=True),
            )
        ).to_dict()

    @router.patch("/{competency_id}/resource-class")
    def set_competency_resource_class(
        competency_id: str,
        body: CompetencyResourceClassUpdateRequest,
        request: Request,
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, object]:
        return competencies.set_resource_class(
            competency_id,
            body.resource_class_code,
            actor_user_id=_actor_user_id(request),
            expected_version=body.expected_version,
        ).to_dict()

    @router.post("/{competency_id}/deactivate")
    def deactivate_competency(
        competency_id: str,
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, object]:
        return competencies.deactivate(competency_id).to_dict()

    return router
