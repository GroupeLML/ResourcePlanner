from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Callable

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..application.delivery_projection import DeliveryProjectionService
from ..application.delivery_service import DeliveryService
from ..application.security import AuthPrincipal
from ..application.verification_story_closure import (
    StoryRequirementDefinition,
    StoryVerificationClosureService,
    StoryVerificationDecisionInput,
)
from ..domain.delivery import DeliveryItemStatus, DeliveryItemType, DeliveryPlanStatus
from ..domain.verification import VerificationDecisionKind, VerificationPhase
from ..infrastructure.sql import (
    SqlDeliveryPlanningReadRepository,
    SqlDeliveryRepository,
    SqlVerificationRepository,
)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanCreateRequest(StrictBody):
    work_package_id: str = Field(min_length=1)
    lead_user_id: str | None = None


class VersionRequest(StrictBody):
    expected_delivery_version: int = Field(ge=1)


class LeadUpdateRequest(VersionRequest):
    lead_user_id: str | None = None


class ItemCreateRequest(VersionRequest):
    item_type: DeliveryItemType
    title: str = Field(min_length=1, max_length=255)
    parent_id: str | None = None
    description: str | None = None
    priority: str | None = None
    status: DeliveryItemStatus = DeliveryItemStatus.BACKLOG
    assignee_user_id: str | None = None
    current_estimate_hours: float | None = Field(default=None, gt=0)
    remaining_hours: float | None = Field(default=None, ge=0)
    due_date: date | None = None
    position: int | None = Field(default=None, ge=0)
    sprint: str | None = None


class ItemUpdateRequest(VersionRequest):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = None
    description: str | None = None
    priority: str | None = None
    status: DeliveryItemStatus | None = None
    assignee_user_id: str | None = None
    current_estimate_hours: float | None = Field(default=None, gt=0)
    remaining_hours: float | None = Field(default=None, ge=0)
    due_date: date | None = None
    position: int | None = Field(default=None, ge=0)
    sprint: str | None = None


class BlockageNoteRequest(VersionRequest):
    note: str = Field(min_length=1, max_length=4000)


class VerificationRequirementDefinitionRequest(StrictBody):
    phase: VerificationPhase
    objective: str = Field(min_length=1)
    method: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    criticality: str | None = None


class StoryVerificationDecisionRequest(StrictBody):
    kind: VerificationDecisionKind
    justification: str | None = None
    existing_requirement_ids: list[str] = Field(default_factory=list)
    new_requirements: list[VerificationRequirementDefinitionRequest] = Field(
        default_factory=list
    )


class StoryVerificationCommandRequest(VersionRequest):
    expected_verification_version: int | None = Field(default=None, ge=1)
    decision: StoryVerificationDecisionRequest


def _story_verification_decision_input(
    body: StoryVerificationDecisionRequest,
) -> StoryVerificationDecisionInput:
    return StoryVerificationDecisionInput(
        kind=body.kind,
        justification=body.justification,
        existing_requirement_ids=tuple(body.existing_requirement_ids),
        new_requirements=tuple(
            StoryRequirementDefinition(
                phase=requirement.phase,
                objective=requirement.objective,
                method=requirement.method,
                expected_result=requirement.expected_result,
                prerequisites=tuple(requirement.prerequisites),
                criticality=requirement.criticality,
            )
            for requirement in body.new_requirements
        ),
    )


def build_delivery_router(
    session_dependency: Callable[[], Iterator[Session]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/delivery", tags=["delivery"])

    def service(session: Session) -> DeliveryService:
        return DeliveryService(SqlDeliveryRepository(session))

    def projection_service(session: Session) -> DeliveryProjectionService:
        repository = SqlDeliveryRepository(session)
        planning = SqlDeliveryPlanningReadRepository(session)
        return DeliveryProjectionService(repository, planning, planning)

    def verification_closure_service(
        session: Session,
    ) -> StoryVerificationClosureService:
        return StoryVerificationClosureService(
            SqlDeliveryRepository(session),
            SqlVerificationRepository(session),
        )

    def principal(request: Request) -> AuthPrincipal:
        return request.state.auth_principal

    @router.get("/plans/{plan_id}")
    def get_plan(
        plan_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).board(plan_id, principal(request))

    @router.get("/work-packages/{work_package_id}/plan")
    def get_work_package_plan(
        work_package_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object] | None:
        return service(session).board_for_work_package(
            work_package_id, principal(request)
        )

    @router.get("/work-packages/{work_package_id}/summary")
    def get_work_package_summary(
        work_package_id: str,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return projection_service(session).work_package_rollup(work_package_id)

    @router.post("/plans", status_code=status.HTTP_201_CREATED)
    def create_plan(
        body: PlanCreateRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).create_plan(
            work_package_id=body.work_package_id,
            lead_user_id=body.lead_user_id,
            principal=principal(request),
        )

    @router.post("/plans/{plan_id}/activate")
    def activate_plan(
        plan_id: str,
        body: VersionRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).set_plan_status(
            plan_id,
            target_status=DeliveryPlanStatus.ACTIVE,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    @router.post("/plans/{plan_id}/archive")
    def archive_plan(
        plan_id: str,
        body: VersionRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).set_plan_status(
            plan_id,
            target_status=DeliveryPlanStatus.ARCHIVED,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    @router.patch("/plans/{plan_id}/lead")
    def set_lead(
        plan_id: str,
        body: LeadUpdateRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).set_lead(
            plan_id,
            lead_user_id=body.lead_user_id,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    @router.post("/plans/{plan_id}/items", status_code=status.HTTP_201_CREATED)
    def create_item(
        plan_id: str,
        body: ItemCreateRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        values = body.model_dump(exclude={"expected_delivery_version"})
        return service(session).create_item(
            plan_id,
            **values,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    @router.patch("/items/{item_id}")
    def update_item(
        item_id: str,
        body: ItemUpdateRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        changes = body.model_dump(
            exclude={"expected_delivery_version"},
            exclude_unset=True,
        )
        return service(session).update_item(
            item_id,
            changes=changes,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    @router.post("/items/{item_id}/complete-with-verification")
    def complete_story_with_verification(
        item_id: str,
        body: StoryVerificationCommandRequest,
        request: Request,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
        ),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        decision = _story_verification_decision_input(body.decision)
        return verification_closure_service(session).complete_story(
            item_id,
            expected_delivery_version=body.expected_delivery_version,
            expected_verification_version=body.expected_verification_version,
            decision=decision,
            idempotency_key=idempotency_key,
            principal=principal(request),
        )

    @router.post("/items/{item_id}/historical-verification-decision")
    def record_historical_story_verification_decision(
        item_id: str,
        body: StoryVerificationCommandRequest,
        request: Request,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
        ),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        decision = _story_verification_decision_input(body.decision)
        return verification_closure_service(session).record_historical_decision(
            item_id,
            expected_delivery_version=body.expected_delivery_version,
            expected_verification_version=body.expected_verification_version,
            decision=decision,
            idempotency_key=idempotency_key,
            principal=principal(request),
        )

    @router.post("/items/{item_id}/blockage-note")
    def document_blockage(
        item_id: str,
        body: BlockageNoteRequest,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).document_blockage(
            item_id,
            note=body.note,
            expected_delivery_version=body.expected_delivery_version,
            principal=principal(request),
        )

    return router
