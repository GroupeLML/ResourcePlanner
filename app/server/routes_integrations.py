from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..application import (
    EmployeeSourcePort,
    EmployeeSyncResult,
    EmployeeSyncService,
    ErpUserSourcePort,
    ErpUserSyncResult,
    ErpUserSyncService,
    ProjectSourcePort,
    ProjectSyncResult,
    ProjectTaskCatalogSourcePort,
    TaskCatalogProjectSyncMetadata,
    TaskCatalogProjectSyncResult,
)
from ..application.erp_sync_operations import (
    ErpSyncOperationsService,
    ErpSyncOperationsSnapshot,
)
from ..application.errors import (
    ApplicationAuthorizationError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from .erp_sync_evidence import ErpSyncEvidenceService
from ..application.security import PERMISSION_ADMIN_SETTINGS, PERMISSION_SYNC_PROJECTS
from ..infrastructure.sql import (
    Project,
    SqlEmployeeSyncRepository,
    SqlErpUserDirectoryRepository,
    SqlSessionFactory,
    SqlTaskCatalogRepository,
)
from ..infrastructure.sql.erp_sync_operations_repository import (
    SqlErpSyncOperationsRepository,
)
from .erp_sync_orchestration import ErpSyncOrchestrator
from .performance import performance_phase, record_external_call, record_external_items


SessionProvider = Callable[[], Iterator[Session]]


def _require_sensitive_erp_authority(request: Request) -> None:
    principal = getattr(request.state, "auth_principal", None)
    if principal is None or not all(
        principal.has_permission(permission)
        for permission in (PERMISSION_SYNC_PROJECTS, PERMISSION_ADMIN_SETTINGS)
    ):
        raise ApplicationAuthorizationError(
            "Cette opération ERP sensible exige sync_projects et admin_settings.",
            code="erp_sync_sensitive_authority_required",
            context={
                "required_permissions": [
                    PERMISSION_SYNC_PROJECTS,
                    PERMISSION_ADMIN_SETTINGS,
                ]
            },
        )


class ErpBaselineApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class ErpPlausibilityExceptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_id: str = Field(min_length=1, max_length=36)
    threshold_scope: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=1000)


class ErpPlausibilityReviewDeclineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class ErpCompensationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class ErpCompensationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


def _actor_name(request: Request) -> str:
    forbidden_actor_inputs = {
        key
        for key in ("actor", "actor_name", "system_actor")
        if key in request.query_params
    }
    if forbidden_actor_inputs:
        raise ApplicationValidationError(
            "L'acteur ERP est dérivé de l'identité authentifiée et ne peut pas être fourni par le client.",
            code="erp_sync_client_actor_forbidden",
            context={"fields": sorted(forbidden_actor_inputs)},
        )
    principal = getattr(request.state, "auth_principal", None)
    if principal is None:
        return "api"
    return str(getattr(principal, "display_name", None) or "api").strip() or "api"


class _InstrumentedEmployeeSource:
    def __init__(self, source: EmployeeSourcePort) -> None:
        self._source = source

    def list_employees(self):
        record_external_call()
        with performance_phase("external"):
            rows = tuple(self._source.list_employees())
        record_external_items(len(rows))
        return rows


class _InstrumentedErpUserSource:
    def __init__(self, source: ErpUserSourcePort) -> None:
        self._source = source

    def list_users(self):
        record_external_call()
        with performance_phase("external"):
            rows = tuple(self._source.list_users())
        record_external_items(len(rows))
        return rows


class _InstrumentedProjectSource:
    def __init__(self, source: ProjectSourcePort) -> None:
        self._source = source

    def list_projects(self):
        record_external_call()
        with performance_phase("external"):
            rows = tuple(self._source.list_projects())
        record_external_items(len(rows))
        return rows


class _InstrumentedProjectTaskSource:
    def __init__(self, source: ProjectTaskCatalogSourcePort) -> None:
        self._source = source

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ):
        record_external_call()
        with performance_phase("external"):
            snapshot = self._source.fetch_project_snapshot(
                project_external_id=project_external_id,
                project_number=project_number,
            )
        record_external_items(snapshot.source_rows)
        return snapshot


def _get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ApplicationNotFoundError(
            "Le projet demandé n'existe pas.",
            code="project_not_found",
            context={"project_id": project_id},
        )
    return project


def build_integration_router(
    session_dependency: SessionProvider,
    *,
    session_factory: SqlSessionFactory,
    project_source: ProjectSourcePort | None = None,
    employee_source: EmployeeSourcePort | None = None,
    user_source: ErpUserSourcePort | None = None,
    project_task_source: ProjectTaskCatalogSourcePort | None = None,
    erp_sync_orchestrator: ErpSyncOrchestrator | None = None,
    erp_sync_evidence_service: ErpSyncEvidenceService | None = None,
    acumatica_info: dict[str, Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/integrations/acumatica", tags=["integrations"])
    safe_info = dict(acumatica_info or {})

    @router.get("")
    def acumatica_status() -> dict[str, Any]:
        return {
            "configured": any(
                source is not None
                for source in (
                    project_source,
                    employee_source,
                    user_source,
                    project_task_source,
                )
            ),
            "project_tasks_configured": project_task_source is not None,
            **safe_info,
        }

    @router.post("/employees/sync")
    def sync_employees(
        session: Session = Depends(session_dependency),
    ) -> EmployeeSyncResult:
        if employee_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des employés Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_employee_not_configured",
            )
        with performance_phase("compute"):
            return EmployeeSyncService(
                _InstrumentedEmployeeSource(employee_source),
                SqlEmployeeSyncRepository(session),
            ).synchronize()

    @router.post("/users/sync")
    def sync_users(
        session: Session = Depends(session_dependency),
    ) -> ErpUserSyncResult:
        if user_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des utilisateurs Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_user_not_configured",
            )
        with performance_phase("compute"):
            return ErpUserSyncService(
                _InstrumentedErpUserSource(user_source),
                SqlErpUserDirectoryRepository(session),
            ).synchronize()

    @router.post("/projects/sync")
    def sync_projects(request: Request) -> ProjectSyncResult:
        if project_source is None:
            raise ApplicationUnavailableError(
                "L'intégration Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_not_configured",
            )
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        with performance_phase("compute"):
            return erp_sync_orchestrator.run_projects_only_compat(
                actor_name=_actor_name(request),
                project_source=_InstrumentedProjectSource(project_source),
            )

    @router.post("/projects/tasks/sync", status_code=202)
    def sync_active_project_tasks(request: Request) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "La synchronisation globale des tâches projet Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_project_task_not_configured",
            )
        return erp_sync_orchestrator.launch(actor_name=_actor_name(request))

    @router.get("/projects/tasks/sync/current")
    def current_project_task_sync() -> dict[str, Any] | None:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "Le suivi de synchronisation des tâches projet n'est pas configuré sur ce serveur.",
                code="acumatica_project_task_sync_not_configured",
            )
        return erp_sync_orchestrator.current()

    @router.get("/projects/tasks/sync/{run_id}")
    def project_task_sync_status(run_id: str) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "Le suivi de synchronisation des tâches projet n'est pas configuré sur ce serveur.",
                code="acumatica_project_task_sync_not_configured",
            )
        return erp_sync_orchestrator.get(run_id)

    @router.post("/projects/{project_id}/tasks/sync")
    def sync_project_tasks(
        project_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> TaskCatalogProjectSyncResult:
        # Preserve historical lookup/validation ordering before entering the
        # shared claimed/fenced orchestration scope.
        project = _get_project(session, project_id)
        if not str(project.erp_external_id or "").strip():
            raise ApplicationValidationError(
                "Ce projet local ne possède pas d'identifiant ERP pour synchroniser ses tâches.",
                code="task_catalog_project_external_id_required",
                context={"project_id": project.id, "project_number": project.number},
            )
        if project_task_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_project_task_not_configured",
            )
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        # The lookup/validation contract above is read-only. End that transaction
        # before the orchestrator acquires the durable control row and writes.
        session.rollback()
        with performance_phase("compute"):
            return erp_sync_orchestrator.run_targeted_project_tasks_compat(
                project_id=project_id,
                actor_name=_actor_name(request),
                project_task_source=_InstrumentedProjectTaskSource(
                    project_task_source
                ),
            )

    @router.get("/erp-sync/operations")
    def erp_sync_operations() -> ErpSyncOperationsSnapshot:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'observabilité ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_operations_not_configured",
            )
        evidence_budget_bytes = (
            erp_sync_evidence_service.settings.evidence_budget_bytes
            if (
                erp_sync_evidence_service is not None
                and erp_sync_evidence_service.storage_available
            )
            else None
        )
        try:
            return ErpSyncOperationsService(
                SqlErpSyncOperationsRepository(
                    session_factory,
                    timezone_name=erp_sync_orchestrator.settings.timezone_name,
                ),
                settings=erp_sync_orchestrator.settings,
                automatic_safety=erp_sync_orchestrator.automatic_safety,
                freshness_sla_hours=erp_sync_orchestrator.settings.freshness_sla_hours,
            ).snapshot(evidence_budget_bytes=evidence_budget_bytes)
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ApplicationUnavailableError(
                "L'état opérationnel ERP durable est indisponible.",
                code="erp_sync_operations_database_unavailable",
            ) from exc

    @router.get("/erp-sync/baselines")
    def erp_sync_baselines() -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        if erp_sync_evidence_service is None:
            return {
                "available": False,
                "sources": {"PROJECTS": None, "TASKS": None},
            }
        profile = erp_sync_orchestrator.settings.config_fingerprint
        return {
            "available": True,
            "sources": {
                source_type: erp_sync_evidence_service.get_baseline_summary(
                    source_type=source_type,
                    profile_fingerprint=profile,
                )
                for source_type in ("PROJECTS", "TASKS")
            },
        }

    @router.get("/erp-sync/runs/{attempt_id}/plausibility-review")
    def erp_sync_plausibility_review(attempt_id: str) -> dict[str, Any]:
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        return erp_sync_evidence_service.plausibility_review_summary(
            attempt_id=attempt_id,
        )

    @router.post("/erp-sync/projects/run-now", status_code=202)
    def run_projects_now(
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.run_now(
            intent="PROJECTS_ONLY",
            actor_name=_actor_name(request),
            idempotency_key=idempotency_key,
        )

    @router.post("/erp-sync/run-now", status_code=202)
    def run_erp_sync_now(
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.run_now(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name=_actor_name(request),
            idempotency_key=idempotency_key,
        )

    @router.post("/erp-sync/occurrences/{occurrence_id}/retry", status_code=202)
    def retry_erp_sync(
        occurrence_id: str,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.retry(
            occurrence_id=occurrence_id,
            actor_name=_actor_name(request),
            idempotency_key=idempotency_key,
        )

    @router.get("/erp-sync/runs/current")
    def current_erp_sync() -> dict[str, Any] | None:
        if erp_sync_orchestrator is None:
            return None
        return erp_sync_orchestrator.current()

    @router.get("/erp-sync/runs/{attempt_id}/evidence")
    def erp_sync_evidence(attempt_id: str) -> dict[str, Any]:
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        return {
            "attempt_id": attempt_id,
            "sources": list(
                erp_sync_evidence_service.list_attempt_sources(
                    attempt_id=attempt_id
                )
            ),
        }

    @router.post("/erp-sync/evidence/{manifest_id}/approve-baseline")
    def approve_erp_baseline(
        manifest_id: str,
        payload: ErpBaselineApprovalRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        baseline_id = erp_sync_evidence_service.approve_baseline(
            manifest_id=manifest_id,
            actor_name=_actor_name(request),
            reason=payload.reason,
        )
        return {"baseline_id": baseline_id, "manifest_id": manifest_id}

    @router.post("/erp-sync/runs/{attempt_id}/plausibility-exceptions")
    def approve_erp_plausibility_exception(
        attempt_id: str,
        payload: ErpPlausibilityExceptionRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        manifests = {
            row["manifest_id"]: row
            for row in erp_sync_evidence_service.list_attempt_sources(
                attempt_id=attempt_id
            )
        }
        manifest = manifests.get(payload.manifest_id)
        if manifest is None or not manifest.get("source_fingerprint"):
            raise ApplicationNotFoundError(
                "La preuve source demandée n'existe pas pour cette tentative.",
                code="erp_sync_evidence_not_found",
            )
        exception_id = erp_sync_evidence_service.approve_plausibility_exception(
            attempt_id=attempt_id,
            source_type=str(manifest["source_type"]),
            source_fingerprint=str(manifest["source_fingerprint"]),
            threshold_scope=payload.threshold_scope,
            actor_name=_actor_name(request),
            reason=payload.reason,
        )
        return {"exception_id": exception_id, "attempt_id": attempt_id}

    @router.post("/erp-sync/runs/{attempt_id}/plausibility-decline")
    def decline_erp_plausibility_review(
        attempt_id: str,
        payload: ErpPlausibilityReviewDeclineRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.decline_plausibility_review(
            attempt_id=attempt_id,
            actor_name=_actor_name(request),
            reason=payload.reason,
        )

    @router.post("/erp-sync/runs/{attempt_id}/plausibility-continue", status_code=202)
    def continue_erp_plausibility_review(
        attempt_id: str,
        request: Request,
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.continue_plausibility_review(
            attempt_id=attempt_id,
            actor_name=_actor_name(request),
        )

    @router.post("/erp-sync/runs/{attempt_id}/compensation/preview")
    def preview_erp_compensation(
        attempt_id: str,
        payload: ErpCompensationPreviewRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        preview = erp_sync_evidence_service.compensation_preview(
            attempt_id=attempt_id
        )
        return {**preview, "reason": payload.reason}

    @router.post("/erp-sync/runs/{attempt_id}/compensation/confirm")
    def confirm_erp_compensation(
        attempt_id: str,
        payload: ErpCompensationConfirmRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _require_sensitive_erp_authority(request)
        if erp_sync_evidence_service is None:
            raise ApplicationUnavailableError(
                "Le stockage de preuve ERP n'est pas configuré.",
                code="erp_sync_evidence_capability_unavailable",
            )
        return erp_sync_evidence_service.confirm_compensation(
            attempt_id=attempt_id,
            preview_hash=payload.preview_hash,
            command_identity=(
                f"erp-sync-compensation:{attempt_id}:{idempotency_key}"
            ),
            actor_name=_actor_name(request),
            reason=payload.reason,
        )

    @router.get("/erp-sync/runs/{attempt_id}")
    def erp_sync_status(attempt_id: str) -> dict[str, Any]:
        if erp_sync_orchestrator is None:
            raise ApplicationUnavailableError(
                "L'orchestration ERP n'est pas configurée sur ce serveur.",
                code="erp_sync_orchestrator_not_configured",
            )
        return erp_sync_orchestrator.get(attempt_id)

    @router.get("/projects/{project_id}/tasks/sync-metadata")
    def project_task_sync_metadata(
        project_id: str,
        session: Session = Depends(session_dependency),
    ) -> TaskCatalogProjectSyncMetadata | None:
        project = _get_project(session, project_id)
        return SqlTaskCatalogRepository(session).get_project_sync_metadata(
            project.number
        )

    return router
