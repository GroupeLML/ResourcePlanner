from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

from fastapi import APIRouter, Depends
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
from .acumatica_project_task_sync import AcumaticaProjectTaskSyncRunner
from .performance import performance_phase, record_external_call, record_external_items


SessionProvider = Callable[[], Iterator[Session]]
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
    project_task_sync_runner: AcumaticaProjectTaskSyncRunner | None = None,
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
    def sync_projects(
        session: Session = Depends(session_dependency),
    ) -> ProjectSyncResult:
        if project_source is None:
            raise ApplicationUnavailableError(
                "L'intégration Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_not_configured",
            )
        with performance_phase("compute"):
            return ProjectSyncService(
                _InstrumentedProjectSource(project_source),
                SqlProjectSyncRepository(session),
            ).synchronize()

    @router.post("/projects/tasks/sync", status_code=202)
    def sync_active_project_tasks() -> dict[str, Any]:
        if project_task_sync_runner is None:
            raise ApplicationUnavailableError(
                "La synchronisation globale des tâches projet Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_project_task_not_configured",
            )
        return project_task_sync_runner.launch()

    @router.get("/projects/tasks/sync/current")
    def current_project_task_sync() -> dict[str, Any] | None:
        if project_task_sync_runner is None:
            raise ApplicationUnavailableError(
                "Le suivi de synchronisation des tâches projet n'est pas configuré sur ce serveur.",
                code="acumatica_project_task_sync_not_configured",
            )
        return project_task_sync_runner.current()

    @router.get("/projects/tasks/sync/{run_id}")
    def project_task_sync_status(run_id: str) -> dict[str, Any]:
        if project_task_sync_runner is None:
            raise ApplicationUnavailableError(
                "Le suivi de synchronisation des tâches projet n'est pas configuré sur ce serveur.",
                code="acumatica_project_task_sync_not_configured",
            )
        return project_task_sync_runner.get(run_id)

    @router.post("/projects/{project_id}/tasks/sync")
    def sync_project_tasks(
        project_id: str,
        session: Session = Depends(session_dependency),
    ) -> TaskCatalogProjectSyncResult:
        project = _get_project(session, project_id)
        project_external_id = str(project.erp_external_id or "").strip()
        if not project_external_id:
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

        repository = SqlTaskCatalogRepository(session)
        with performance_phase("compute"):
            return TaskCatalogSyncService(
                _InstrumentedProjectTaskSource(project_task_source),
                repository,
                sync_metadata_repository=repository,
                workforce_policy=SqlTaskCatalogWorkforcePolicy(session),
            ).synchronize_project(
                project_external_id=project_external_id,
                project_number=project.number,
            )

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
