from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..application.security import AuthPrincipal
from ..application.verification_execution_service import VerificationExecutionService
from ..domain.verification import VerificationExecutionResult
from ..domain.verification_execution import VerificationMeasure
from ..infrastructure.sql import SqlVerificationExecutionRepository


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerificationVersionBody(StrictBody):
    expected_verification_version: int = Field(ge=1)


class ExecutorAssignmentBody(VerificationVersionBody):
    executor_user_id: str = Field(min_length=1)


class ExecutorUnassignmentBody(ExecutorAssignmentBody):
    reason: str = Field(min_length=1)


class ExecutionBody(VerificationVersionBody):
    result: VerificationExecutionResult
    executed_at: datetime | None = None
    measurements: dict[str, VerificationMeasure] = Field(default_factory=dict)
    comments: str | None = None


class EvidenceLinkBody(VerificationVersionBody):
    url: str = Field(min_length=1)
    label: str | None = Field(default=None, max_length=255)
    provenance: str | None = Field(default=None, max_length=64)


class RetestBody(VerificationVersionBody):
    reason: str = Field(min_length=1)


def build_verification_router(
    session_dependency: Callable[[], Iterator[Session]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/verification", tags=["verification"])

    def service(session: Session) -> VerificationExecutionService:
        return VerificationExecutionService(SqlVerificationExecutionRepository(session))

    def principal(request: Request) -> AuthPrincipal:
        return request.state.auth_principal

    def idempotency_key(
        value: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ) -> str:
        return value

    @router.get("/work-packages/{work_package_id}/package")
    def get_package(
        work_package_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).package(
            work_package_id, principal=principal(request)
        )

    @router.post("/requirements/{requirement_id}/assignments")
    def assign_executor(
        requirement_id: str,
        body: ExecutorAssignmentBody,
        request: Request,
        key: str = Depends(idempotency_key),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).assign_executor(
            requirement_id,
            executor_user_id=body.executor_user_id,
            expected_verification_version=body.expected_verification_version,
            idempotency_key=key,
            principal=principal(request),
        )

    @router.post("/requirements/{requirement_id}/unassignments")
    def unassign_executor(
        requirement_id: str,
        body: ExecutorUnassignmentBody,
        request: Request,
        key: str = Depends(idempotency_key),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).unassign_executor(
            requirement_id,
            executor_user_id=body.executor_user_id,
            reason=body.reason,
            expected_verification_version=body.expected_verification_version,
            idempotency_key=key,
            principal=principal(request),
        )

    @router.post("/requirements/{requirement_id}/executions")
    def record_execution(
        requirement_id: str,
        body: ExecutionBody,
        request: Request,
        key: str = Depends(idempotency_key),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).record_execution(
            requirement_id,
            result=body.result.value,
            executed_at=body.executed_at,
            measurements=body.measurements,
            comments=body.comments,
            expected_verification_version=body.expected_verification_version,
            idempotency_key=key,
            principal=principal(request),
        )

    @router.post("/executions/{execution_id}/evidence-links")
    def add_evidence_link(
        execution_id: str,
        body: EvidenceLinkBody,
        request: Request,
        key: str = Depends(idempotency_key),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).add_evidence_link(
            execution_id,
            url=body.url,
            label=body.label,
            provenance=body.provenance,
            expected_verification_version=body.expected_verification_version,
            idempotency_key=key,
            principal=principal(request),
        )

    @router.post("/requirements/{requirement_id}/retests")
    def request_retest(
        requirement_id: str,
        body: RetestBody,
        request: Request,
        key: str = Depends(idempotency_key),
        session: Session = Depends(session_dependency),
    ) -> dict[str, object]:
        return service(session).request_retest(
            requirement_id,
            reason=body.reason,
            expected_verification_version=body.expected_verification_version,
            idempotency_key=key,
            principal=principal(request),
        )

    return router
