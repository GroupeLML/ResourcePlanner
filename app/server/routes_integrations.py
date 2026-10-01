from __future__ import annotations

from collections.abc import Iterator
import logging
from threading import Lock
from time import perf_counter
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
    ApplicationConflictError,
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


class _PreloadedProjectTaskSource:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ):
        return self._snapshot


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
    global_project_task_sync_lock = Lock()

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
        if not global_project_task_sync_lock.acquire(blocking=False):
            raise ApplicationConflictError(
                "Une synchronisation globale des tâches projet est déjà en cours.",
                code="acumatica_project_task_sync_in_progress",
            )

        sync_started_at = perf_counter()
        try:
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

            project_targets = tuple(
                (str(project.erp_external_id).strip(), project.number)
                for project in projects
                if project.active and str(project.erp_external_id or "").strip()
            )
            batch_snapshots: dict[str, Any] | None = None
            source_requests: int | None = None
            source_rows_scanned: int | None = None
            source_read_duration_ms: int | None = None

            fetch_project_snapshots = getattr(
                project_task_source,
                "fetch_project_snapshots",
                None,
            )
            if callable(fetch_project_snapshots) and project_targets:
                source_started_at = perf_counter()
                record_external_call()
                with performance_phase("external"):
                    portfolio_snapshot = fetch_project_snapshots(
                        project_targets=project_targets
                    )
                source_read_duration_ms = max(
                    0,
                    int((perf_counter() - source_started_at) * 1000),
                )
                source_requests = int(portfolio_snapshot.source_pages)
                source_rows_scanned = int(portfolio_snapshot.source_rows)
                record_external_items(source_rows_scanned)
                batch_snapshots = {}
                for snapshot in portfolio_snapshot.snapshots:
                    project_number = str(snapshot.project_number or "").strip()
                    if project_number in batch_snapshots:
                        raise ApplicationValidationError(
                            "La lecture globale contient plusieurs snapshots pour le même projet.",
                            code="task_catalog_batch_duplicate_project",
                            context={"project_number": project_number or None},
                        )
                    batch_snapshots[project_number] = snapshot

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
                        source_for_project: ProjectTaskCatalogSourcePort
                        if batch_snapshots is not None:
                            snapshot = batch_snapshots.get(project.number)
                            if snapshot is None:
                                raise ApplicationValidationError(
                                    "La lecture globale ne contient pas le snapshot attendu.",
                                    code="task_catalog_project_snapshot_missing",
                                    context={"project_number": project.number},
                                )
                            source_for_project = _PreloadedProjectTaskSource(snapshot)
                        else:
                            source_for_project = _InstrumentedProjectTaskSource(
                                project_task_source
                            )
                        result = TaskCatalogSyncService(
                            source_for_project,
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

            duration_ms = max(0, int((perf_counter() - sync_started_at) * 1000))
            logger.info(
                "Global Acumatica project-task sync completed inspected=%s synchronized=%s rejected=%s source_requests=%s source_rows=%s source_duration_ms=%s duration_ms=%s",
                totals["projects_inspected"],
                totals["projects_synchronized"],
                totals["projects_rejected"],
                source_requests if source_requests is not None else "-",
                source_rows_scanned if source_rows_scanned is not None else "-",
                source_read_duration_ms if source_read_duration_ms is not None else "-",
                duration_ms,
            )
            return {
                **totals,
                "source_requests": source_requests,
                "source_rows_scanned": source_rows_scanned,
                "source_read_duration_ms": source_read_duration_ms,
                "duration_ms": duration_ms,
                "project_results": project_results,
            }
        finally:
            global_project_task_sync_lock.release()

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
