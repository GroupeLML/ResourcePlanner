from __future__ import annotations

from datetime import date
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ..application.communications import CommunicationBatchRecord
from ..application.project_communications import (
    ProjectCommunicationReviewInput,
    ProjectCommunicationService,
    ProjectCommunicationWorkflowPreview,
)
from ..application.security import AuthPrincipal
from ..domain.project_communication import ProjectCommunicationProjection


ProjectCommunicationDependency = Callable[..., Any]


class ProjectReviewBody(BaseModel):
    message_key: str
    include: bool = True
    subject: str | None = None
    body: str | None = None


class PrepareProjectBatchBody(BaseModel):
    week_start: date
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    reviews: list[ProjectReviewBody] = Field(default_factory=list)
    add_generator_cc: bool = False
    expected_generator_identity_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )


def _actor(request: Request) -> str:
    principal: AuthPrincipal | None = getattr(request.state, "auth_principal", None)
    return principal.display_name if principal is not None else "api"


def build_project_communication_router(
    dependency: ProjectCommunicationDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/communications", tags=["communications"])

    @router.get("/project-projection")
    def project_projection(
        week_start: date,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> ProjectCommunicationProjection:
        return service.project_projection(week_start=week_start)

    @router.get("/project-preview")
    def project_preview(
        week_start: date,
        request: Request,
        add_generator_cc: bool = False,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> ProjectCommunicationWorkflowPreview:
        return service.project_preview(
            week_start=week_start,
            add_generator_cc=add_generator_cc,
            generator_principal=getattr(request.state, "auth_principal", None),
        )

    @router.post("/project-batches", status_code=201)
    def prepare_project_batch(
        body: PrepareProjectBatchBody,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.prepare_project_batch(
            week_start=body.week_start,
            expected_fingerprint=body.expected_fingerprint,
            reviews=tuple(
                ProjectCommunicationReviewInput(
                    message_key=row.message_key,
                    include=row.include,
                    subject=row.subject,
                    body=row.body,
                )
                for row in body.reviews
            ),
            actor_name=_actor(request),
            add_generator_cc=body.add_generator_cc,
            expected_generator_identity_fingerprint=(
                body.expected_generator_identity_fingerprint
            ),
            generator_principal=getattr(request.state, "auth_principal", None),
        )

    @router.get("/project-batches")
    def list_project_batches(
        week_start: date | None = None,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> list[CommunicationBatchRecord]:
        return list(service.list_project_batches(week_start=week_start))

    @router.post("/project-batches/{batch_id}/approve")
    def approve_project_batch(
        batch_id: str,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.approve_project_batch(
            batch_id=batch_id,
            actor_name=_actor(request),
        )

    @router.post("/project-batches/{batch_id}/create-drafts")
    def create_project_drafts(
        batch_id: str,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.create_project_drafts(
            batch_id=batch_id,
            actor_name=_actor(request),
        )

    @router.get("/project-batches/{batch_id}/draft-download")
    def download_project_drafts(
        batch_id: str,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> Response:
        artifact = service.project_draft_download(batch_id=batch_id)
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/project-batches/{batch_id}/send-smtp")
    def send_project_smtp(
        batch_id: str,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.send_project_smtp(
            batch_id=batch_id,
            actor_name=_actor(request),
        )

    @router.post("/project-batches/{batch_id}/cancel")
    def cancel_project_batch(
        batch_id: str,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.cancel_project_batch(
            batch_id=batch_id,
            actor_name=_actor(request),
        )

    @router.post("/project-batches/{batch_id}/mark-communicated")
    def mark_project_communicated(
        batch_id: str,
        request: Request,
        service: ProjectCommunicationService = Depends(dependency),
    ) -> CommunicationBatchRecord:
        return service.mark_project_communicated(
            batch_id=batch_id,
            actor_name=_actor(request),
        )

    return router
