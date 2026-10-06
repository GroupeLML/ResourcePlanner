from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..application.security import AuthPrincipal
from ..application.verification_documents import (
    render_phase_report_html,
    render_test_plan_html,
    render_traceability_csv,
)
from ..application.verification_execution_service import VerificationExecutionService
from ..domain.verification import VerificationExecutionResult, VerificationPhase
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

    def document_headers(filename: str, *, inline: bool) -> dict[str, str]:
        disposition = "inline" if inline else "attachment"
        return {
            "Cache-Control": "no-store",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        }

    def filename_token(value: str) -> str:
        normalized = "".join(
            char if char.isascii() and (char.isalnum() or char in "-_.") else "-"
            for char in str(value).strip()
        ).strip("-")
        return normalized or "work-package"

    @router.get(
        "/work-packages/{work_package_id}/documents/test-plan",
        response_class=HTMLResponse,
    )
    def get_test_plan_document(
        work_package_id: str,
        request: Request,
        phase: VerificationPhase | None = None,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        snapshot = service(session).document_snapshot(
            work_package_id,
            principal=principal(request),
        )
        suffix = f"-{phase.value.lower()}" if phase is not None else ""
        filename = f"verification-test-plan-{filename_token(work_package_id)}{suffix}.html"
        return HTMLResponse(
            render_test_plan_html(
                snapshot,
                phase=phase.value if phase is not None else None,
            ),
            headers=document_headers(filename, inline=True),
        )

    @router.get(
        "/work-packages/{work_package_id}/documents/reports/{phase}",
        response_class=HTMLResponse,
    )
    def get_phase_report_document(
        work_package_id: str,
        phase: VerificationPhase,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        snapshot = service(session).document_snapshot(
            work_package_id,
            principal=principal(request),
        )
        filename = (
            f"verification-report-{phase.value.lower()}-"
            f"{filename_token(work_package_id)}.html"
        )
        return HTMLResponse(
            render_phase_report_html(snapshot, phase.value),
            headers=document_headers(filename, inline=True),
        )

    @router.get("/work-packages/{work_package_id}/documents/traceability.csv")
    def get_traceability_document(
        work_package_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> Response:
        snapshot = service(session).document_snapshot(
            work_package_id,
            principal=principal(request),
        )
        filename = f"verification-traceability-{filename_token(work_package_id)}.csv"
        return Response(
            render_traceability_csv(snapshot),
            media_type="text/csv",
            headers=document_headers(filename, inline=False),
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
