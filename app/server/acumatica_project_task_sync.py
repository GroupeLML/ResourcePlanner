from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
from threading import Lock
from time import perf_counter
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from ..application import (
    ProjectTaskCatalogSourcePort,
    TaskCatalogSyncService,
)
from ..application.errors import (
    ApplicationError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from ..infrastructure.sql import (
    AcumaticaProjectTaskSyncProjectResult,
    AcumaticaProjectTaskSyncRun,
    GLOBAL_SYNC_RUN_KEY,
    Project,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    SqlSessionFactory,
    SqlTaskCatalogRepository,
    SqlTaskCatalogWorkforcePolicy,
    SYNC_RUN_COMPLETED,
    SYNC_RUN_COMPLETED_WITH_ERRORS,
    SYNC_RUN_FAILED,
    SYNC_RUN_INTERRUPTED,
    SYNC_RUN_PENDING,
    SYNC_RUN_RUNNING,
    transactional_session,
)
from ..infrastructure.sql.base import new_id, utc_now


logger = logging.getLogger(__name__)


class _PreloadedProjectTaskSource:
    def __init__(self, snapshot: Any) -> None:
        self._snapshot = snapshot

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ):
        return self._snapshot


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_ms(started_at: datetime | None, finished_at: datetime) -> int | None:
    if started_at is None:
        return None
    elapsed = _normalized_utc(finished_at) - _normalized_utc(started_at)
    return max(0, int(elapsed.total_seconds() * 1000))


class AcumaticaProjectTaskSyncRunner:
    """Specific background runner for the global RP_ProjectTasks refresh.

    SQL is the authority for lifecycle, concurrency, progress and results. The
    executor is only a local execution mechanism for the canonical single-backend
    Uvicorn topology; it is deliberately not a generic job framework.
    """

    def __init__(
        self,
        session_factory: SqlSessionFactory,
        source: ProjectTaskCatalogSourcePort | None,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._launch_lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="acumatica-project-task-sync",
        )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def reconcile_interrupted_runs(self) -> int:
        # Server-isolation/health smoke can intentionally boot FastAPI against an
        # unmigrated empty database. Production starts only after the migrate
        # container, so absence here means there is no persisted run to reconcile.
        with self._session_factory() as schema_session:
            bind = schema_session.get_bind()
            if not inspect(bind).has_table("acumatica_project_task_sync_runs"):
                return 0

        now = utc_now()
        reconciled = 0
        with transactional_session(self._session_factory) as session:
            runs = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncRun).where(
                        AcumaticaProjectTaskSyncRun.active_key == GLOBAL_SYNC_RUN_KEY
                    )
                )
            )
            for run in runs:
                run.status = SYNC_RUN_INTERRUPTED
                run.active_key = None
                run.finished_at = now
                run.duration_ms = _elapsed_ms(run.started_at, now)
                run.error_code = "backend_restarted"
                run.diagnostic = (
                    "Le backend a redémarré avant la fin de la synchronisation."
                )
                reconciled += 1
        if reconciled:
            logger.warning(
                "Acumatica project-task sync startup reconciliation interrupted_runs=%s",
                reconciled,
            )
        return reconciled

    def launch(self) -> dict[str, Any]:
        if self._source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée sur ce serveur.",
                code="acumatica_project_task_not_configured",
            )

        with self._launch_lock:
            active = self._get_active()
            if active is not None:
                return active

            run_id = new_id()
            pending_payload: dict[str, Any]
            try:
                with transactional_session(self._session_factory) as session:
                    run = AcumaticaProjectTaskSyncRun(
                        id=run_id,
                        active_key=GLOBAL_SYNC_RUN_KEY,
                        status=SYNC_RUN_PENDING,
                    )
                    session.add(run)
                    session.flush()
                    pending_payload = self._serialize_run(session, run)
            except IntegrityError:
                # The filtered unique SQL index is the cross-request authority.
                # A concurrent creator may have won after our first read.
                active = self._get_active()
                if active is not None:
                    return active
                raise

            try:
                self._executor.submit(self._execute, run_id)
            except RuntimeError:
                self._fail_run(
                    run_id,
                    error_code="acumatica_project_task_sync_runner_unavailable",
                    diagnostic="Le traitement de synchronisation n'a pas pu être démarré.",
                )
                raise ApplicationUnavailableError(
                    "Le traitement de synchronisation n'est pas disponible.",
                    code="acumatica_project_task_sync_runner_unavailable",
                )
            return pending_payload

    def get(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None:
                raise ApplicationNotFoundError(
                    "Le run de synchronisation demandé n'existe pas.",
                    code="acumatica_project_task_sync_run_not_found",
                    context={"run_id": run_id},
                )
            return self._serialize_run(session, run)

    def current(self) -> dict[str, Any] | None:
        active = self._get_active()
        if active is not None:
            return active
        with self._session_factory() as session:
            run = session.scalar(
                select(AcumaticaProjectTaskSyncRun)
                .order_by(
                    AcumaticaProjectTaskSyncRun.created_at.desc(),
                    AcumaticaProjectTaskSyncRun.id.desc(),
                )
                .limit(1)
            )
            return None if run is None else self._serialize_run(session, run)

    def _get_active(self) -> dict[str, Any] | None:
        with self._session_factory() as session:
            run = session.scalar(
                select(AcumaticaProjectTaskSyncRun)
                .where(AcumaticaProjectTaskSyncRun.active_key == GLOBAL_SYNC_RUN_KEY)
                .order_by(AcumaticaProjectTaskSyncRun.created_at.desc())
                .limit(1)
            )
            return None if run is None else self._serialize_run(session, run)

    @staticmethod
    def _serialize_project_result(
        row: AcumaticaProjectTaskSyncProjectResult,
    ) -> dict[str, Any]:
        return {
            "project_id": row.project_id,
            "project_number": row.project_number,
            "status": row.status,
            "error_code": row.error_code,
            "reason_code": row.reason_code,
            "source_rows": row.source_rows,
            "source_rows_rejected": row.source_rows_rejected,
            "tasks_received": row.tasks_received,
            "created": row.created,
            "updated": row.updated,
            "unchanged": row.unchanged,
            "deactivated": row.deactivated,
            "rejected": row.rejected,
            "duration_ms": row.duration_ms,
        }

    def _serialize_run(
        self,
        session,
        run: AcumaticaProjectTaskSyncRun,
    ) -> dict[str, Any]:
        results = tuple(
            session.scalars(
                select(AcumaticaProjectTaskSyncProjectResult)
                .where(AcumaticaProjectTaskSyncProjectResult.run_id == run.id)
                .order_by(
                    AcumaticaProjectTaskSyncProjectResult.created_at,
                    AcumaticaProjectTaskSyncProjectResult.project_number,
                )
            )
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "projects_total": run.projects_total,
            "projects_inspected": run.projects_total,
            "projects_processed": run.projects_processed,
            "projects_synchronized": run.projects_synchronized,
            "projects_ignored": run.projects_ignored,
            "projects_rejected": run.projects_rejected,
            "source_requests": run.source_requests,
            "source_rows_scanned": run.source_rows_scanned,
            "source_read_duration_ms": run.source_read_duration_ms,
            "source_rows_received": run.source_rows_received,
            "source_rows_rejected": run.source_rows_rejected,
            "tasks_received": run.tasks_received,
            "tasks_created": run.tasks_created,
            "tasks_updated": run.tasks_updated,
            "tasks_unchanged": run.tasks_unchanged,
            "tasks_deactivated": run.tasks_deactivated,
            "tasks_rejected": run.tasks_rejected,
            "duration_ms": run.duration_ms,
            "error_code": run.error_code,
            "diagnostic": run.diagnostic,
            "project_results": [
                self._serialize_project_result(row)
                for row in results
            ],
        }

    def _execute(self, run_id: str) -> None:
        started_at = utc_now()
        try:
            with transactional_session(self._session_factory) as session:
                run = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if run is None or run.status != SYNC_RUN_PENDING:
                    return
                run.status = SYNC_RUN_RUNNING
                run.started_at = started_at

            with transactional_session(self._session_factory) as discovery_session:
                projects = tuple(
                    SqlPlannerQueryRepositoryWithLoadProfiles(
                        discovery_session
                    ).list_projects(active_only=False)
                )

            self._set_project_total(run_id, len(projects))
            project_targets = tuple(
                (str(project.erp_external_id).strip(), project.number)
                for project in projects
                if project.active and str(project.erp_external_id or "").strip()
            )
            batch_snapshots: dict[str, Any] = {}

            if project_targets:
                fetch_project_snapshots = getattr(
                    self._source,
                    "fetch_project_snapshots",
                    None,
                )
                if not callable(fetch_project_snapshots):
                    raise ApplicationUnavailableError(
                        "La lecture globale batch RP_ProjectTasks n'est pas disponible.",
                        code="acumatica_project_task_batch_required",
                    )

                source_started = perf_counter()
                portfolio_snapshot = fetch_project_snapshots(
                    project_targets=project_targets
                )
                source_read_duration_ms = max(
                    0,
                    int((perf_counter() - source_started) * 1000),
                )
                self._set_source_metrics(
                    run_id,
                    source_requests=int(portfolio_snapshot.source_pages),
                    source_rows_scanned=int(portfolio_snapshot.source_rows),
                    source_read_duration_ms=source_read_duration_ms,
                )
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
                self._process_project(run_id, project, batch_snapshots)

            self._complete_run(run_id)
        except ApplicationError as exc:
            self._fail_run(
                run_id,
                error_code=exc.code or "acumatica_project_task_read_failed",
                diagnostic="La synchronisation globale n'a pas pu être exécutée.",
            )
        except Exception as exc:
            logger.error(
                "Acumatica project-task sync failed run_id=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
            self._fail_run(
                run_id,
                error_code="acumatica_project_task_read_failed",
                diagnostic="La synchronisation globale n'a pas pu être exécutée.",
            )

    def _set_project_total(self, run_id: str, total: int) -> None:
        with transactional_session(self._session_factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is not None and run.status == SYNC_RUN_RUNNING:
                run.projects_total = max(int(total), 0)

    def _set_source_metrics(
        self,
        run_id: str,
        *,
        source_requests: int,
        source_rows_scanned: int,
        source_read_duration_ms: int,
    ) -> None:
        with transactional_session(self._session_factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is not None and run.status == SYNC_RUN_RUNNING:
                run.source_requests = max(source_requests, 0)
                run.source_rows_scanned = max(source_rows_scanned, 0)
                run.source_read_duration_ms = max(source_read_duration_ms, 0)

    def _process_project(
        self,
        run_id: str,
        project: Any,
        batch_snapshots: dict[str, Any],
    ) -> None:
        base = {
            "project_id": project.id,
            "project_number": project.number,
        }
        if not project.active:
            self._record_project(
                run_id,
                {
                    **base,
                    "status": "ignored",
                    "error_code": None,
                    "reason_code": "project_inactive",
                },
            )
            return

        project_external_id = str(project.erp_external_id or "").strip()
        if not project_external_id:
            self._record_project(
                run_id,
                {
                    **base,
                    "status": "rejected",
                    "error_code": "task_catalog_project_external_id_required",
                    "reason_code": "project_erp_identity_missing",
                },
            )
            return

        try:
            snapshot = batch_snapshots.get(project.number)
            if snapshot is None:
                raise ApplicationValidationError(
                    "La lecture globale ne contient pas le snapshot attendu.",
                    code="task_catalog_project_snapshot_missing",
                    context={"project_number": project.number},
                )
            with transactional_session(self._session_factory) as project_session:
                repository = SqlTaskCatalogRepository(project_session)
                result = TaskCatalogSyncService(
                    _PreloadedProjectTaskSource(snapshot),
                    repository,
                    sync_metadata_repository=repository,
                    workforce_policy=SqlTaskCatalogWorkforcePolicy(project_session),
                ).synchronize_project(
                    project_external_id=project_external_id,
                    project_number=project.number,
                )
        except ApplicationError as exc:
            self._record_project(
                run_id,
                {
                    **base,
                    "status": "rejected",
                    "error_code": exc.code,
                    "reason_code": "project_sync_failed",
                },
            )
            return
        except Exception as exc:
            logger.error(
                "Acumatica project-task project sync failed run_id=%s project_id=%s error_type=%s",
                run_id,
                project.id,
                type(exc).__name__,
            )
            self._record_project(
                run_id,
                {
                    **base,
                    "status": "rejected",
                    "error_code": "task_catalog_project_sync_failed",
                    "reason_code": "project_sync_failed",
                },
            )
            return

        self._record_project(
            run_id,
            {
                **base,
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
            },
        )

    def _record_project(self, run_id: str, outcome: dict[str, Any]) -> None:
        with transactional_session(self._session_factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None or run.status != SYNC_RUN_RUNNING:
                return

            row = AcumaticaProjectTaskSyncProjectResult(
                run_id=run_id,
                project_id=str(outcome["project_id"]),
                project_number=str(outcome["project_number"]),
                status=str(outcome["status"]),
                error_code=outcome.get("error_code"),
                reason_code=outcome.get("reason_code"),
                source_rows=int(outcome.get("source_rows") or 0),
                source_rows_rejected=int(outcome.get("source_rows_rejected") or 0),
                tasks_received=int(outcome.get("tasks_received") or 0),
                created=int(outcome.get("created") or 0),
                updated=int(outcome.get("updated") or 0),
                unchanged=int(outcome.get("unchanged") or 0),
                deactivated=int(outcome.get("deactivated") or 0),
                rejected=int(outcome.get("rejected") or 0),
                duration_ms=outcome.get("duration_ms"),
            )
            session.add(row)

            run.projects_processed += 1
            if row.status == "synchronized":
                run.projects_synchronized += 1
                run.source_rows_received += row.source_rows
                run.source_rows_rejected += row.source_rows_rejected
                run.tasks_received += row.tasks_received
                run.tasks_created += row.created
                run.tasks_updated += row.updated
                run.tasks_unchanged += row.unchanged
                run.tasks_deactivated += row.deactivated
                run.tasks_rejected += row.rejected
            elif row.status == "ignored":
                run.projects_ignored += 1
            else:
                run.projects_rejected += 1

    def _complete_run(self, run_id: str) -> None:
        now = utc_now()
        with transactional_session(self._session_factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None or run.status != SYNC_RUN_RUNNING:
                return
            run.status = (
                SYNC_RUN_COMPLETED_WITH_ERRORS
                if run.projects_rejected
                else SYNC_RUN_COMPLETED
            )
            run.active_key = None
            run.finished_at = now
            run.duration_ms = _elapsed_ms(run.started_at, now)
            run.error_code = None
            run.diagnostic = None
            logger.info(
                "Acumatica project-task sync completed run_id=%s status=%s processed=%s total=%s synchronized=%s rejected=%s source_requests=%s source_rows=%s duration_ms=%s",
                run.id,
                run.status,
                run.projects_processed,
                run.projects_total,
                run.projects_synchronized,
                run.projects_rejected,
                run.source_requests,
                run.source_rows_scanned,
                run.duration_ms,
            )

    def _fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        diagnostic: str,
    ) -> None:
        now = utc_now()
        with transactional_session(self._session_factory) as session:
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None or run.status in {
                SYNC_RUN_COMPLETED,
                SYNC_RUN_COMPLETED_WITH_ERRORS,
                SYNC_RUN_FAILED,
                SYNC_RUN_INTERRUPTED,
            }:
                return
            run.status = SYNC_RUN_FAILED
            run.active_key = None
            run.finished_at = now
            run.duration_ms = _elapsed_ms(run.started_at, now)
            run.error_code = str(error_code or "acumatica_project_task_read_failed")
            run.diagnostic = diagnostic
