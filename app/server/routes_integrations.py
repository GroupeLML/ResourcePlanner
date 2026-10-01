from __future__ import annotations

from collections.abc import Iterator
import logging
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
    ApplicationError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from ..infrastructure.sql import (
    Project,
    SqlEmployeeSyncRepository,
    SqlErpUserDirectoryRepository,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    SqlProjectSyncRepository,
    SqlSessionFactory,
    SqlTaskCatalogRepository,
    SqlTaskCatalogWorkforcePolicy,
    transactional_session,
)
from .performance import performance_phase, record_external_call, record_external_items


SessionProvider = Callable[[], Iterator[Session]]
logger = logging.getLogger(__name__)


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

    @router.post("/projects/tasks/sync")
    def sync_active_project_tasks() -> dict[str, Any]:
        if project_task_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_project_task_not_configured",
            )

        # Resolve the portfolio through the canonical project read model so #525
        # inherits the existing #207 active/inactive status semantics instead of
        # introducing a second definition of an active project.
        with transactional_session(session_factory) as discovery_session:
            projects = tuple(
                SqlPlannerQueryRepositoryWithLoadProfiles(
                    discovery_session
                ).list_projects(active_only=False)
            )

        totals: dict[str, int] = {
            "projects_inspected": len(projects),
            "projects_synchronized": 0,
            "projects_ignored": 0,
            "projects_rejected": 0,
            "source_rows_received": 0,
            "source_rows_rejected": 0,
            "tasks_received": 0,
            "tasks_created": 0,
            "tasks_updated": 0,
            "tasks_unchanged": 0,
            "tasks_deactivated": 0,
            "tasks_rejected": 0,
        }
        project_results: list[dict[str, Any]] = []

        for project in projects:
            base_outcome = {
                "project_id": project.id,
                "project_number": project.number,
            }
            if not project.active:
                totals["projects_ignored"] += 1
                project_results.append(
                    {
                        **base_outcome,
                        "status": "ignored",
                        "error_code": None,
                        "reason_code": "project_inactive",
                    }
                )
                continue

            project_external_id = str(project.erp_external_id or "").strip()
            if not project_external_id:
                totals["projects_rejected"] += 1
                project_results.append(
                    {
                        **base_outcome,
                        "status": "rejected",
                        "error_code": "task_catalog_project_external_id_required",
                        "reason_code": "project_erp_identity_missing",
                    }
                )
                continue

            try:
                # One transaction per project is intentional. A project failure
                # rolls back only that project and does not turn the whole
                # portfolio refresh into a giant SQL transaction.
                with transactional_session(session_factory) as project_session:
                    repository = SqlTaskCatalogRepository(project_session)
                    result = TaskCatalogSyncService(
                        _InstrumentedProjectTaskSource(project_task_source),
                        repository,
                        sync_metadata_repository=repository,
                        workforce_policy=SqlTaskCatalogWorkforcePolicy(project_session),
                    ).synchronize_project(
                        project_external_id=project_external_id,
                        project_number=project.number,
                    )
            except ApplicationError as exc:
                totals["projects_rejected"] += 1
                project_results.append(
                    {
                        **base_outcome,
                        "status": "rejected",
                        "error_code": exc.code,
                        "reason_code": "project_sync_failed",
                    }
                )
                continue
            except Exception:
                # Keep unexpected infrastructure details out of the API/log payload.
                logger.error(
                    "Global Acumatica project-task sync failed project_id=%s project_number=%s",
                    project.id,
                    project.number,
                )
                totals["projects_rejected"] += 1
                project_results.append(
                    {
                        **base_outcome,
                        "status": "rejected",
                        "error_code": "task_catalog_project_sync_failed",
                        "reason_code": "project_sync_failed",
                    }
                )
                continue

            totals["projects_synchronized"] += 1
            totals["source_rows_received"] += result.source_rows
            totals["source_rows_rejected"] += result.rejected_rows
            totals["tasks_received"] += result.task_count
            totals["tasks_created"] += result.created
            totals["tasks_updated"] += result.updated
            totals["tasks_unchanged"] += result.unchanged
            totals["tasks_deactivated"] += result.deactivated
            # For new ERP rows the existing repository returns "ignored" when
            # the canonical #454 TaskCD policy resolves to no workforce class.
            totals["tasks_rejected"] += result.ignored
            project_results.append(
                {
                    **base_outcome,
                    "status": "synchronized",
                    "error_code": None,
                    "reason_code": None,
                    "source_rows": result.source_rows,
                    "source_rows_rejected": result.rejected_rows,
                    "tasks_received": result.task_count,
                    "created": result.created,
                    "updated": result.updated,
                    "unchanged": result.unchanged,
                    "deactivated": result.deactivated,
                    "rejected": result.ignored,
                    "duration_ms": result.duration_ms,
                }
            )

        return {
            **totals,
            "project_results": project_results,
        }

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
