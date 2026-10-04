from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..application.operational_contacts import OperationalContactService
from ..application.security import AuthPrincipal
from ..domain.operational_contacts import ContactResolution, resolve_operational_responsible
from ..infrastructure.sql.models import Project, ResourceRequirement, Shift
from ..infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from ..infrastructure.sql.operational_responsibility_mutation_repository import (
    SqlOperationalResponsibilityMutationRepository,
)


SessionProvider = Callable[..., Any]


class StrictResponsibilityWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str | None = None

    @model_validator(mode="after")
    def require_explicit_contact_id(self) -> "StrictResponsibilityWrite":
        if "contact_id" not in self.model_fields_set:
            raise ValueError(
                "contact_id doit être fourni explicitement; null retire l'override."
            )
        return self


class ProjectOperationalResponsibilityWrite(StrictResponsibilityWrite):
    expected_version: int = Field(ge=1)


class PlanningOperationalResponsibilityWrite(StrictResponsibilityWrite):
    expected_planning_version: int = Field(ge=1)


def _resolution_payload(value: ContactResolution) -> dict[str, object]:
    return {
        "status": value.status,
        "contact_id": value.contact_id,
        "display_name": value.display_name,
        "email": value.email,
        "phone": value.phone,
        "source_type": value.source_type,
        "source_entity_id": value.source_entity_id,
        "source_label": value.source_label,
        "diagnostics": list(value.diagnostics),
    }


def _materialized_payload(value: Any) -> dict[str, object]:
    return {
        "subject_type": value.subject_type,
        "subject_id": value.subject_id,
        "requirement_id": value.requirement_id,
        "shift_id": value.shift_id,
        "request_line_id": value.request_line_id,
        "demand_number": value.demand_number,
        "project_number": value.project_number,
        "approved_request_version": value.approved_request_version,
        "approved_contact_context_status": value.approved_contact_context_status,
        "task_id": value.task_id,
        "task_code": value.task_code,
        "task_label": value.task_label,
        "resource_id": value.resource_id,
        "resource_name": value.resource_name,
        "operational_responsible": _resolution_payload(
            value.operational_responsible
        ),
        "coordinator": _resolution_payload(value.coordinator),
        "diagnostics": list(value.diagnostics),
    }


def _mutation_payload(value: Any) -> dict[str, object]:
    return {
        "entity_type": value.entity_type,
        "entity_id": value.entity_id,
        "override_contact_id": value.override_contact_id,
        "project_override_version": value.project_override_version,
        "planning_version": value.planning_version,
        "auto_source_converted": value.auto_source_converted,
    }


def _actor_user_id(request: Request) -> str:
    principal: AuthPrincipal = request.state.auth_principal
    return str(principal.local_user_id or "").strip()


def _project(session: Session, project_number: str) -> Project:
    row = session.scalar(
        select(Project).where(Project.number == str(project_number or "").strip())
    )
    if row is None:
        from ..application.errors import ApplicationNotFoundError

        raise ApplicationNotFoundError(
            "Projet introuvable.",
            code="project_not_found",
            context={"project_number": str(project_number or "").strip()},
        )
    return row


def _requirement(session: Session, reference: str) -> ResourceRequirement:
    wanted = str(reference or "").strip()
    row = session.scalar(
        select(ResourceRequirement).where(
            or_(
                ResourceRequirement.id == wanted,
                ResourceRequirement.legacy_segment_id == wanted,
            )
        )
    )
    if row is None:
        from ..application.errors import ApplicationNotFoundError

        raise ApplicationNotFoundError(
            "Segment ou besoin introuvable.",
            code="resource_requirement_not_found",
            context={"segment_reference": wanted},
        )
    return row


def _shift(session: Session, reference: str) -> Shift:
    wanted = str(reference or "").strip()
    row = session.scalar(
        select(Shift).where(
            or_(Shift.id == wanted, Shift.legacy_allocation_id == wanted)
        )
    )
    if row is None:
        from ..application.errors import ApplicationNotFoundError

        raise ApplicationNotFoundError(
            "Quart introuvable.",
            code="shift_not_found",
            context={"allocation_reference": wanted},
        )
    return row


def build_operational_responsibility_router(
    session_dependency: SessionProvider,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["operational-responsibility"])

    @router.get("/projects/{project_number}/operational-responsibility")
    def project_operational_responsibility(
        project_number: str,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        project = _project(session, project_number)
        repository = SqlOperationalContactRepository(session)
        _task, project_override, project_manager = (
            repository.get_project_operational_candidates(project.id)
        )
        resolved = resolve_operational_responsible(
            project_override=project_override,
            project_manager=project_manager,
        )
        return {
            "project_id": project.id,
            "project_number": project.number,
            "override_contact_id": project.operational_responsible_override_contact_id,
            "override_version": int(
                project.operational_responsible_override_version or 0
            ),
            "operational_responsible": _resolution_payload(resolved),
        }

    @router.patch("/projects/{project_number}/operational-responsible")
    def set_project_operational_responsible(
        project_number: str,
        body: ProjectOperationalResponsibilityWrite,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        project = _project(session, project_number)
        result = SqlOperationalResponsibilityMutationRepository(
            session
        ).set_project_override(
            project.id,
            body.contact_id,
            actor_user_id=_actor_user_id(request),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return _mutation_payload(result)

    @router.get("/segments/{segment_reference}/operational-responsibility")
    def segment_operational_responsibility(
        segment_reference: str,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        requirement = _requirement(session, segment_reference)
        resolution = OperationalContactService(
            SqlOperationalContactRepository(session)
        ).resolve_resource_requirement(requirement.id)
        return _materialized_payload(resolution)

    @router.patch("/segments/{segment_reference}/operational-responsible")
    def set_segment_operational_responsible(
        segment_reference: str,
        body: PlanningOperationalResponsibilityWrite,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        requirement = _requirement(session, segment_reference)
        result = SqlOperationalResponsibilityMutationRepository(
            session
        ).set_requirement_override(
            requirement.id,
            body.contact_id,
            actor_user_id=_actor_user_id(request),
            expected_planning_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )
        return _mutation_payload(result)

    @router.get("/allocations/{allocation_reference}/operational-responsibility")
    def allocation_operational_responsibility(
        allocation_reference: str,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        shift = _shift(session, allocation_reference)
        resolution = OperationalContactService(
            SqlOperationalContactRepository(session)
        ).resolve_shift(shift.id)
        return _materialized_payload(resolution)

    @router.patch("/allocations/{allocation_reference}/operational-responsible")
    def set_allocation_operational_responsible(
        allocation_reference: str,
        body: PlanningOperationalResponsibilityWrite,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        shift = _shift(session, allocation_reference)
        result = SqlOperationalResponsibilityMutationRepository(
            session
        ).set_shift_override(
            shift.id,
            body.contact_id,
            actor_user_id=_actor_user_id(request),
            expected_planning_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )
        return _mutation_payload(result)

    return router
