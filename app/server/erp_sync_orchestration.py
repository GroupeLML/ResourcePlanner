from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
from threading import Event, Lock, Thread
from time import perf_counter
from typing import Any

from sqlalchemy import select

from ..application import (
    ProjectSourcePort,
    ProjectSyncService,
    ProjectTaskCatalogSourcePort,
    TaskCatalogSyncService,
)
from ..application.erp_sync_scheduler import (
    ErpSyncAutomaticSafetyCapability,
    ErpSyncSchedulerSettings,
    UnavailableErpSyncAutomaticSafetyCapability,
    scheduler_decision,
)
from ..application.errors import (
    ApplicationConflictError,
    ApplicationError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from ..infrastructure.sql import (
    AcumaticaProjectTaskSyncProjectResult,
    AcumaticaProjectTaskSyncRun,
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_RUNNING,
    ERP_ATTEMPT_SUCCEEDED,
    ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
    ERP_OCCURRENCE_DISABLED,
    ERP_OCCURRENCE_FAILED,
    ERP_OCCURRENCE_INTERRUPTED,
    ERP_OCCURRENCE_MISSED,
    ERP_OCCURRENCE_PENDING,
    ERP_OCCURRENCE_SUCCEEDED,
    ERP_OCCURRENCE_SUCCEEDED_WITH_ERRORS,
    GLOBAL_SYNC_RUN_KEY,
    Project,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    SqlProjectSyncRepository,
    SqlSessionFactory,
    SqlTaskCatalogRepository,
    SqlTaskCatalogWorkforcePolicy,
    SYNC_RUN_COMPLETED,
    SYNC_RUN_COMPLETED_WITH_ERRORS,
    SYNC_RUN_FAILED,
    SYNC_RUN_PENDING,
    SYNC_RUN_RUNNING,
    transactional_session,
)
from ..infrastructure.sql.base import new_id, utc_now
from ..infrastructure.sql.erp_sync_control_repository import (
    ErpSyncFenceLost,
    SqlErpSyncControlRepository,
    retry_command_identity,
)


logger = logging.getLogger(__name__)


class _PreloadedProjectSource:
    def __init__(self, projects: tuple[Any, ...]) -> None:
        self._projects = projects

    def list_projects(self):
        return self._projects


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


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ErpSyncOrchestrator:
    """ADR-033 specific projects -> active-project-tasks orchestration.

    The executor is only a local worker. SQL control/occurrence/attempt rows own
    concurrency and fencing, so multiple backend processes may safely evaluate.
    """

    def __init__(
        self,
        session_factory: SqlSessionFactory,
        *,
        project_source: ProjectSourcePort | None,
        project_task_source: ProjectTaskCatalogSourcePort | None,
        settings: ErpSyncSchedulerSettings,
        automatic_safety: ErpSyncAutomaticSafetyCapability | None = None,
        system_actor: str = "erp-scheduler",
    ) -> None:
        self._session_factory = session_factory
        self._project_source = project_source
        self._project_task_source = project_task_source
        self._settings = settings
        self._automatic_safety = (
            automatic_safety
            if automatic_safety is not None
            else UnavailableErpSyncAutomaticSafetyCapability()
        )
        self._system_actor = system_actor
        self._control = SqlErpSyncControlRepository(session_factory)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="erp-sync-orchestration",
        )
        self._launch_lock = Lock()
        self._closed = False

    @property
    def settings(self) -> ErpSyncSchedulerSettings:
        return self._settings

    @property
    def automatic_activation_diagnostic(self) -> str | None:
        if not self._settings.enabled:
            return "erp_sync_scheduler_disabled"
        if not self._automatic_safety.automatic_execution_available():
            return self._automatic_safety.diagnostic()
        return None

    def shutdown(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def reconcile_startup(self) -> int:
        self._control.ensure_control_row()
        reconciled = self._control.reconcile_expired_owner()
        reconciled += self._reconcile_expired_legacy_task_runs()
        return reconciled

    def _reconcile_expired_legacy_task_runs(self) -> int:
        now = self._control.current_sql_time()
        cutoff = now - timedelta(seconds=self._settings.max_attempt_seconds)
        with transactional_session(self._session_factory) as session:
            rows = tuple(
                session.scalars(
                    select(AcumaticaProjectTaskSyncRun).where(
                        AcumaticaProjectTaskSyncRun.parent_attempt_id.is_(None),
                        AcumaticaProjectTaskSyncRun.active_key == GLOBAL_SYNC_RUN_KEY,
                        AcumaticaProjectTaskSyncRun.status.in_(
                            {SYNC_RUN_PENDING, SYNC_RUN_RUNNING}
                        ),
                    )
                )
            )
            reconciled = 0
            for row in rows:
                reference = _utc(row.started_at) or _utc(row.created_at)
                if reference is None or reference > cutoff:
                    continue
                row.status = "INTERRUPTED"
                row.active_key = None
                row.finished_at = now
                row.error_code = "legacy_owner_deadline_expired"
                row.diagnostic = (
                    "Le run antérieur à 643A a dépassé la durée maximale sans lease durable."
                )
                reconciled += 1
            return reconciled

    def scheduler_tick(self) -> dict[str, Any] | None:
        if self._closed or not self._control.schema_available():
            return None

        occurrence = self._control.record_daily_decision(
            self._settings,
            self._automatic_safety,
        )
        if occurrence is None:
            return None
        if occurrence.status in {
            ERP_OCCURRENCE_DISABLED,
            ERP_OCCURRENCE_MISSED,
            ERP_OCCURRENCE_SUCCEEDED,
            ERP_OCCURRENCE_SUCCEEDED_WITH_ERRORS,
        }:
            return {
                "occurrence_id": occurrence.id,
                "status": occurrence.status,
                "diagnostic": occurrence.diagnostic,
            }

        now = self._control.current_sql_time()
        trigger = "CATCHUP"
        automatic_retry = occurrence.status in {
            ERP_OCCURRENCE_FAILED,
            ERP_OCCURRENCE_INTERRUPTED,
        }
        if not automatic_retry and occurrence.scheduled_at is not None:
            scheduled = _utc(occurrence.scheduled_at)
            trigger = "SCHEDULED" if scheduled == now else "CATCHUP"
        if automatic_retry:
            last_attempt = (
                self._control.get_attempt(occurrence.last_attempt_id)
                if occurrence.last_attempt_id
                else None
            )
            if last_attempt is not None:
                retryable = (
                    last_attempt.status == ERP_ATTEMPT_INTERRUPTED
                    or last_attempt.error_code
                    in {
                        "acumatica_project_read_failed",
                        "acumatica_project_task_read_failed",
                        "erp_sync_source_unavailable",
                    }
                )
                finished = _utc(last_attempt.finished_at)
                if not retryable:
                    return self._control.serialize_attempt(last_attempt.id)
                if finished is not None and now < finished + timedelta(
                    minutes=self._settings.retry_minutes
                ):
                    return self._control.serialize_attempt(last_attempt.id)
            trigger = "RETRY"

        try:
            attempt = self._control.claim(
                occurrence_id=occurrence.id,
                trigger=trigger,
                actor_name=self._system_actor,
                settings=self._settings,
                automatic=True,
            )
        except ApplicationConflictError:
            return None
        if attempt is None:
            return None
        self._submit(attempt.id, int(attempt.generation))
        return self._control.serialize_attempt(attempt.id)

    def run_now(
        self,
        *,
        intent: str,
        actor_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._assert_sources(intent)
        occurrence = self._control.accept_manual(
            intent=intent,
            actor_name=actor_name,
            idempotency_key=idempotency_key,
            settings=self._settings,
        )
        if occurrence.last_attempt_id:
            return self._compat_payload(occurrence.last_attempt_id)

        attempt = self._control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name=actor_name,
            settings=self._settings,
        )
        if attempt is None:
            if occurrence.success_attempt_id:
                return self._compat_payload(occurrence.success_attempt_id)
            raise ApplicationConflictError(
                "La commande ERP n'a pas pu créer de tentative.",
                code="erp_sync_command_not_claimed",
            )
        self._submit(attempt.id, int(attempt.generation))
        return self._compat_payload(attempt.id)

    def retry(
        self,
        *,
        occurrence_id: str,
        actor_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        occurrence = self._control.get_occurrence(occurrence_id)
        self._assert_sources(occurrence.intent)
        if occurrence.success_attempt_id:
            return self._compat_payload(occurrence.success_attempt_id)
        attempt = self._control.claim(
            occurrence_id=occurrence.id,
            trigger="RETRY",
            actor_name=actor_name,
            settings=self._settings,
            command_identity=retry_command_identity(
                actor_name=actor_name,
                occurrence_id=occurrence.id,
                idempotency_key=idempotency_key,
            ),
        )
        if attempt is None:
            raise ApplicationConflictError(
                "La reprise ERP n'a pas pu créer de tentative.",
                code="erp_sync_retry_not_claimed",
            )
        self._submit(attempt.id, int(attempt.generation))
        return self._compat_payload(attempt.id)

    def launch(self, *, actor_name: str = "api") -> dict[str, Any]:
        """Compatibility for the existing global tasks button.

        Repeated clicks while the same full orchestration is active reuse the
        current durable attempt rather than creating a hidden queue.
        """

        current = self.current()
        if current is not None and current.get("status") in {"PENDING", "RUNNING"}:
            if current.get("intent") == "ACTIVE_PROJECT_TASKS_ONLY":
                return current
            raise ApplicationConflictError(
                "Une synchronisation ERP incompatible est déjà active.",
                code="erp_sync_run_active",
                context={"attempt_id": current.get("attempt_id")},
            )
        return self.run_now(
            intent="ACTIVE_PROJECT_TASKS_ONLY",
            actor_name=actor_name,
            idempotency_key=new_id(),
        )

    def get(self, run_id: str) -> dict[str, Any]:
        try:
            return self._compat_payload(run_id)
        except ApplicationNotFoundError:
            with self._session_factory() as session:
                legacy = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if legacy is None or legacy.parent_attempt_id is not None:
                    raise
                return self._legacy_task_payload(session, legacy)

    def current(self) -> dict[str, Any] | None:
        attempt = self._control.current_attempt()
        if attempt is not None:
            return self._compat_payload(attempt.id)
        with self._session_factory() as session:
            legacy = session.scalar(
                select(AcumaticaProjectTaskSyncRun)
                .where(
                    AcumaticaProjectTaskSyncRun.parent_attempt_id.is_(None),
                    AcumaticaProjectTaskSyncRun.active_key == GLOBAL_SYNC_RUN_KEY,
                    AcumaticaProjectTaskSyncRun.status.in_(
                        {SYNC_RUN_PENDING, SYNC_RUN_RUNNING}
                    ),
                )
                .order_by(AcumaticaProjectTaskSyncRun.created_at.desc())
                .limit(1)
            )
            if legacy is None:
                return None
            return self._legacy_task_payload(session, legacy)

    def run_projects_only_compat(self, *, actor_name: str) -> dict[str, int]:
        """Compatibility adapter for legacy POST /projects/sync.

        The mutation is still executed under the shared SQL control/fence. The
        new asynchronous RUN_NOW endpoint is the preferred UI path.
        """

        self._assert_sources("PROJECTS_ONLY")
        occurrence = self._control.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name=actor_name,
            idempotency_key=new_id(),
            settings=self._settings,
        )
        attempt = self._control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name=actor_name,
            settings=self._settings,
        )
        if attempt is None:
            raise ApplicationConflictError(
                "La synchronisation projets n'a pas pu acquérir le contrôle ERP.",
                code="erp_sync_command_not_claimed",
            )
        result = self._execute_projects(
            attempt.id,
            int(attempt.generation),
        )
        self._control.finish(
            attempt_id=attempt.id,
            generation=int(attempt.generation),
            status=ERP_ATTEMPT_SUCCEEDED,
            phase="PUBLICATION_COMMITTED",
        )
        return {
            "received": int(result.received),
            "created": int(result.created),
            "updated": int(result.updated),
            "unchanged": int(result.unchanged),
        }

    def run_targeted_project_tasks_compat(
        self,
        *,
        project_id: str,
        actor_name: str,
    ):
        self._assert_sources("TARGETED_PROJECT_TASKS")
        occurrence = self._control.accept_manual(
            intent=f"TARGETED_PROJECT_TASKS:{project_id}",
            actor_name=actor_name,
            idempotency_key=new_id(),
            settings=self._settings,
        )
        attempt = self._control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name=actor_name,
            settings=self._settings,
        )
        if attempt is None:
            raise ApplicationConflictError(
                "La synchronisation ciblée n'a pas pu acquérir le contrôle ERP.",
                code="erp_sync_command_not_claimed",
            )
        try:
            with self._session_factory() as discovery:
                project = discovery.get(Project, project_id)
                if project is None:
                    raise ApplicationNotFoundError(
                        "Le projet demandé n'existe pas.",
                        code="project_not_found",
                        context={"project_id": project_id},
                    )
                project_external_id = str(project.erp_external_id or "").strip()
                project_number = project.number
            if not project_external_id:
                raise ApplicationValidationError(
                    "Ce projet local ne possède pas d'identifiant ERP.",
                    code="task_catalog_project_external_id_required",
                )
            snapshot = self._project_task_source.fetch_project_snapshot(
                project_external_id=project_external_id,
                project_number=project_number,
            )
            with transactional_session(self._session_factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt.id,
                    generation=int(attempt.generation),
                )
                repository = SqlTaskCatalogRepository(session)
                result = TaskCatalogSyncService(
                    _PreloadedProjectTaskSource(snapshot),
                    repository,
                    sync_metadata_repository=repository,
                    workforce_policy=SqlTaskCatalogWorkforcePolicy(session),
                ).synchronize_project(
                    project_external_id=project_external_id,
                    project_number=project_number,
                )
                SqlErpSyncControlRepository.update_phase_in_session(
                    session,
                    attempt_id=attempt.id,
                    generation=int(attempt.generation),
                    phase="TASKS_FINISHED",
                )
            self._control.finish(
                attempt_id=attempt.id,
                generation=int(attempt.generation),
                status=ERP_ATTEMPT_SUCCEEDED,
                phase="PUBLICATION_COMMITTED",
            )
            return result
        except Exception:
            self._control.finish(
                attempt_id=attempt.id,
                generation=int(attempt.generation),
                status=ERP_ATTEMPT_FAILED,
                phase="FAILED",
                error_code="targeted_project_task_sync_failed",
                diagnostic="La synchronisation ciblée des tâches a échoué.",
            )
            raise

    def _active_legacy_task_run(self) -> AcumaticaProjectTaskSyncRun | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(AcumaticaProjectTaskSyncRun)
                .where(
                    AcumaticaProjectTaskSyncRun.parent_attempt_id.is_(None),
                    AcumaticaProjectTaskSyncRun.active_key == GLOBAL_SYNC_RUN_KEY,
                    AcumaticaProjectTaskSyncRun.status.in_(
                        {SYNC_RUN_PENDING, SYNC_RUN_RUNNING}
                    ),
                )
                .order_by(AcumaticaProjectTaskSyncRun.created_at.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row

    def _assert_sources(self, intent: str) -> None:
        if self._project_source is None and intent in {"PROJECTS_ONLY", "PROJECTS_AND_ACTIVE_TASKS"}:
            raise ApplicationUnavailableError(
                "La synchronisation projets Acumatica n'est pas configurée.",
                code="acumatica_not_configured",
            )
        task_intent = (
            intent in {"PROJECTS_AND_ACTIVE_TASKS", "ACTIVE_PROJECT_TASKS_ONLY"}
            or intent.startswith("TARGETED_PROJECT_TASKS")
        )
        if task_intent and self._project_task_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée.",
                code="acumatica_project_task_not_configured",
            )
        legacy = self._active_legacy_task_run()
        if legacy is not None:
            raise ApplicationConflictError(
                "Un run ERP antérieur à 643A est encore actif; "
                "la nouvelle orchestration attend sa fin ou son échéance maximale.",
                code="erp_sync_legacy_task_run_active",
                context={"run_id": legacy.id},
            )

    def _submit(self, attempt_id: str, generation: int) -> None:
        with self._launch_lock:
            if self._closed:
                raise ApplicationUnavailableError(
                    "Le worker ERP est arrêté.",
                    code="erp_sync_runner_unavailable",
                )
            try:
                self._executor.submit(self._execute_attempt, attempt_id, generation)
            except RuntimeError as exc:
                raise ApplicationUnavailableError(
                    "Le worker ERP n'est pas disponible.",
                    code="erp_sync_runner_unavailable",
                ) from exc

    def _heartbeat(
        self,
        stop: Event,
        *,
        attempt_id: str,
        generation: int,
    ) -> None:
        while not stop.wait(self._settings.heartbeat_seconds):
            if not self._control.renew(
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
            ):
                return

    def _execute_attempt(self, attempt_id: str, generation: int) -> None:
        stop = Event()
        heartbeat = Thread(
            target=self._heartbeat,
            kwargs={
                "stop": stop,
                "attempt_id": attempt_id,
                "generation": generation,
            },
            name=f"erp-sync-heartbeat-{attempt_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if not self._control.start_attempt(
                attempt_id=attempt_id,
                generation=generation,
            ):
                return
            attempt = self._control.get_attempt(attempt_id)
            if attempt.intent != "ACTIVE_PROJECT_TASKS_ONLY":
                self._execute_projects(attempt_id, generation)
                if attempt.intent == "PROJECTS_ONLY":
                    self._control.finish(
                        attempt_id=attempt_id,
                        generation=generation,
                        status=ERP_ATTEMPT_SUCCEEDED,
                        phase="PUBLICATION_COMMITTED",
                    )
                    return

            task_status = self._execute_tasks(attempt_id, generation)
            parent_status = (
                ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS
                if task_status == SYNC_RUN_COMPLETED_WITH_ERRORS
                else ERP_ATTEMPT_SUCCEEDED
            )
            self._control.finish(
                attempt_id=attempt_id,
                generation=generation,
                status=parent_status,
                phase="PUBLICATION_COMMITTED",
                diagnostic=(
                    "Une ou plusieurs synchronisations de tâches projet ont échoué."
                    if parent_status == ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS
                    else None
                ),
            )
        except ErpSyncFenceLost:
            # A newer generation owns the control row. The stale worker is not
            # permitted to publish a late failure or terminal status.
            logger.warning(
                "ERP sync worker fenced attempt_id=%s generation=%s",
                attempt_id,
                generation,
            )
        except ApplicationError as exc:
            self._control.finish(
                attempt_id=attempt_id,
                generation=generation,
                status=ERP_ATTEMPT_FAILED,
                phase="FAILED",
                error_code=exc.code or "erp_sync_failed",
                diagnostic="La synchronisation ERP n'a pas pu être exécutée.",
            )
        except Exception as exc:
            logger.error(
                "ERP sync orchestration failed attempt_id=%s generation=%s error_type=%s",
                attempt_id,
                generation,
                type(exc).__name__,
            )
            self._control.finish(
                attempt_id=attempt_id,
                generation=generation,
                status=ERP_ATTEMPT_FAILED,
                phase="FAILED",
                error_code="erp_sync_failed",
                diagnostic="La synchronisation ERP n'a pas pu être exécutée.",
            )
        finally:
            stop.set()
            heartbeat.join(timeout=1.0)

    def _execute_projects(self, attempt_id: str, generation: int):
        if self._project_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation projets Acumatica n'est pas configurée.",
                code="acumatica_not_configured",
            )
        try:
            projects = tuple(self._project_source.list_projects())
        except Exception as exc:
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationUnavailableError(
                "La lecture des projets Acumatica a échoué.",
                code="acumatica_project_read_failed",
            ) from exc

        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
            )
            result = ProjectSyncService(
                _PreloadedProjectSource(projects),
                SqlProjectSyncRepository(session),
            ).synchronize()
            SqlErpSyncControlRepository.update_phase_in_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                phase="PROJECTS_VALIDATED",
                project_result=result,
            )
            return result

    def _create_task_run(self, attempt_id: str, generation: int) -> str:
        run_id = new_id()
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
            )
            run = AcumaticaProjectTaskSyncRun(
                id=run_id,
                active_key=GLOBAL_SYNC_RUN_KEY,
                status=SYNC_RUN_RUNNING,
                parent_attempt_id=attempt_id,
                erp_generation=generation,
                started_at=utc_now(),
            )
            session.add(run)
            SqlErpSyncControlRepository.update_phase_in_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                phase="TASKS_RUNNING",
                task_sync_run_id=run_id,
            )
        return run_id

    def _execute_tasks(self, attempt_id: str, generation: int) -> str:
        if self._project_task_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée.",
                code="acumatica_project_task_not_configured",
            )

        with self._session_factory() as discovery_session:
            projects = tuple(
                SqlPlannerQueryRepositoryWithLoadProfiles(
                    discovery_session
                ).list_projects(active_only=False)
            )

        run_id = self._create_task_run(attempt_id, generation)
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
            )
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None:
                raise RuntimeError("Run tâches enfant introuvable.")
            run.projects_total = len(projects)

        try:
            project_targets = tuple(
                (str(project.erp_external_id).strip(), project.number)
                for project in projects
                if project.active and str(project.erp_external_id or "").strip()
            )
            snapshots: dict[str, Any] = {}
            source_requests = 0
            source_rows = 0
            source_duration = 0
            if project_targets:
                fetch_many = getattr(
                    self._project_task_source,
                    "fetch_project_snapshots",
                    None,
                )
                if not callable(fetch_many):
                    raise ApplicationUnavailableError(
                        "La lecture globale batch RP_ProjectTasks n'est pas disponible.",
                        code="acumatica_project_task_batch_required",
                    )
                started = perf_counter()
                try:
                    portfolio = fetch_many(project_targets=project_targets)
                except Exception as exc:
                    if isinstance(exc, ApplicationError):
                        raise
                    raise ApplicationUnavailableError(
                        "La lecture globale des tâches projet Acumatica a échoué.",
                        code="acumatica_project_task_read_failed",
                    ) from exc
                source_duration = max(
                    0,
                    int((perf_counter() - started) * 1000),
                )
                source_requests = int(portfolio.source_pages)
                source_rows = int(portfolio.source_rows)
                for snapshot in portfolio.snapshots:
                    number = str(snapshot.project_number or "").strip()
                    if number in snapshots:
                        raise ApplicationValidationError(
                            "La lecture globale contient plusieurs snapshots pour le même projet.",
                            code="task_catalog_batch_duplicate_project",
                        )
                    snapshots[number] = snapshot

            with transactional_session(self._session_factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                )
                run = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if run is None:
                    raise RuntimeError("Run tâches enfant introuvable.")
                run.source_requests = source_requests
                run.source_rows_scanned = source_rows
                run.source_read_duration_ms = source_duration

            for project in projects:
                self._process_task_project(
                    run_id,
                    attempt_id,
                    generation,
                    project,
                    snapshots,
                )

            with transactional_session(self._session_factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                )
                run = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if run is None:
                    raise RuntimeError("Run tâches enfant introuvable.")
                run.status = (
                    SYNC_RUN_COMPLETED_WITH_ERRORS
                    if run.projects_rejected
                    else SYNC_RUN_COMPLETED
                )
                run.active_key = None
                run.finished_at = utc_now()
                SqlErpSyncControlRepository.update_phase_in_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                    phase="TASKS_FINISHED",
                )
                return run.status
        except Exception:
            self._fail_task_run(
                run_id,
                attempt_id=attempt_id,
                generation=generation,
            )
            raise

    def _fail_task_run(
        self,
        run_id: str,
        *,
        attempt_id: str,
        generation: int,
    ) -> None:
        try:
            with transactional_session(self._session_factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                )
                run = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if run is None or run.status not in {
                    SYNC_RUN_PENDING,
                    SYNC_RUN_RUNNING,
                }:
                    return
                run.status = SYNC_RUN_FAILED
                run.active_key = None
                run.finished_at = utc_now()
                run.error_code = "acumatica_project_task_read_failed"
                run.diagnostic = (
                    "La synchronisation globale des tâches n'a pas pu être exécutée."
                )
        except ErpSyncFenceLost:
            return

    def _process_task_project(
        self,
        run_id: str,
        attempt_id: str,
        generation: int,
        project: Any,
        snapshots: dict[str, Any],
    ) -> None:
        base = {
            "project_id": project.id,
            "project_number": project.number,
        }
        if not project.active:
            self._record_task_outcome(
                run_id,
                attempt_id,
                generation,
                {
                    **base,
                    "status": "ignored",
                    "reason_code": "project_inactive",
                },
            )
            return
        project_external_id = str(project.erp_external_id or "").strip()
        if not project_external_id:
            self._record_task_outcome(
                run_id,
                attempt_id,
                generation,
                {
                    **base,
                    "status": "rejected",
                    "error_code": "task_catalog_project_external_id_required",
                    "reason_code": "project_erp_identity_missing",
                },
            )
            return

        snapshot = snapshots.get(project.number)
        if snapshot is None:
            self._record_task_outcome(
                run_id,
                attempt_id,
                generation,
                {
                    **base,
                    "status": "rejected",
                    "error_code": "task_catalog_project_snapshot_missing",
                    "reason_code": "project_sync_failed",
                },
            )
            return

        try:
            with transactional_session(self._session_factory) as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                )
                repository = SqlTaskCatalogRepository(session)
                result = TaskCatalogSyncService(
                    _PreloadedProjectTaskSource(snapshot),
                    repository,
                    sync_metadata_repository=repository,
                    workforce_policy=SqlTaskCatalogWorkforcePolicy(session),
                ).synchronize_project(
                    project_external_id=project_external_id,
                    project_number=project.number,
                )
                self._record_task_outcome_in_session(
                    session,
                    run_id,
                    {
                        **base,
                        "status": "synchronized",
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
        except ErpSyncFenceLost:
            raise
        except ApplicationError as exc:
            self._record_task_outcome(
                run_id,
                attempt_id,
                generation,
                {
                    **base,
                    "status": "rejected",
                    "error_code": exc.code,
                    "reason_code": "project_sync_failed",
                },
            )
        except Exception as exc:
            logger.error(
                "ERP task project failed attempt_id=%s project_id=%s error_type=%s",
                attempt_id,
                project.id,
                type(exc).__name__,
            )
            self._record_task_outcome(
                run_id,
                attempt_id,
                generation,
                {
                    **base,
                    "status": "rejected",
                    "error_code": "task_catalog_project_sync_failed",
                    "reason_code": "project_sync_failed",
                },
            )

    def _record_task_outcome(
        self,
        run_id: str,
        attempt_id: str,
        generation: int,
        outcome: dict[str, Any],
    ) -> None:
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
            )
            self._record_task_outcome_in_session(session, run_id, outcome)

    @staticmethod
    def _record_task_outcome_in_session(
        session,
        run_id: str,
        outcome: dict[str, Any],
    ) -> None:
        run = session.get(AcumaticaProjectTaskSyncRun, run_id)
        if run is None or run.status != SYNC_RUN_RUNNING:
            raise RuntimeError("Run tâches enfant non actif.")
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

    def _legacy_task_payload(
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
            "attempt_id": None,
            "occurrence_id": None,
            "intent": "ACTIVE_PROJECT_TASKS_ONLY",
            "generation": None,
            "status": run.status,
            "phase": "LEGACY_TASKS",
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
                {
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
                for row in results
            ],
        }

    def _compat_payload(self, attempt_id: str) -> dict[str, Any]:
        payload = self._control.serialize_attempt(attempt_id)
        parent_status = str(payload["status"])
        status_map = {
            "SUCCEEDED": SYNC_RUN_COMPLETED,
            "SUCCEEDED_WITH_ERRORS": SYNC_RUN_COMPLETED_WITH_ERRORS,
        }
        payload["status"] = status_map.get(parent_status, parent_status)
        payload.setdefault("projects_total", int(payload.get("project_received") or 0))
        payload.setdefault("projects_inspected", int(payload.get("project_received") or 0))
        payload.setdefault("projects_processed", 0)
        payload.setdefault("projects_synchronized", 0)
        payload.setdefault("projects_ignored", 0)
        payload.setdefault("projects_rejected", 0)
        payload.setdefault("source_requests", None)
        payload.setdefault("source_rows_scanned", None)
        payload.setdefault("source_read_duration_ms", None)
        payload.setdefault("source_rows_received", 0)
        payload.setdefault("source_rows_rejected", 0)
        payload.setdefault("tasks_received", 0)
        payload.setdefault("tasks_created", 0)
        payload.setdefault("tasks_updated", 0)
        payload.setdefault("tasks_unchanged", 0)
        payload.setdefault("tasks_deactivated", 0)
        payload.setdefault("tasks_rejected", 0)
        payload.setdefault("duration_ms", None)
        payload["project_results"] = []

        child_id = payload.get("task_sync_run_id")
        if child_id:
            with self._session_factory() as session:
                run = session.get(AcumaticaProjectTaskSyncRun, str(child_id))
                if run is not None:
                    payload.update(
                        {
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
                        }
                    )
                    results = tuple(
                        session.scalars(
                            select(AcumaticaProjectTaskSyncProjectResult)
                            .where(
                                AcumaticaProjectTaskSyncProjectResult.run_id
                                == run.id
                            )
                            .order_by(
                                AcumaticaProjectTaskSyncProjectResult.created_at,
                                AcumaticaProjectTaskSyncProjectResult.project_number,
                            )
                        )
                    )
                    payload["project_results"] = [
                        {
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
                        for row in results
                    ]
        return payload
