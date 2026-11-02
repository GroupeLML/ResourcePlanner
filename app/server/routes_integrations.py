from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
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
    ProjectSyncService,
    ProjectTaskCatalogSourcePort,
    TaskCatalogProjectSyncMetadata,
    TaskCatalogProjectSyncResult,
    TaskCatalogSyncService,
)
from ..application.errors import (
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from ..infrastructure.sql import (
    Project,
    SqlEmployeeSyncRepository,
    SqlErpUserDirectoryRepository,
    SqlProjectSyncRepository,
    SqlSessionFactory,
    SqlTaskCatalogRepository,
    SqlTaskCatalogWorkforcePolicy,
)
from .erp_sync_orchestration import ErpSyncOrchestrator
from .performance import performance_phase, record_external_call, record_external_items


SessionProvider = Callable[[], Iterator[Session]]


def _actor_name(request: Request) -> str:
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
        with performance_phase("compute"):
            return erp_sync_orchestrator.run_targeted_project_tasks_compat(
                project_id=project_id,
                actor_name=_actor_name(request),
                project_task_source=_InstrumentedProjectTaskSource(
                    project_task_source
                ),
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
