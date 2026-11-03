from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from functools import partial
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
from ..application.erp_sync_evidence import (
    ErpSyncEvidenceService,
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
    TaskCatalogEntry,
    TaskCatalogProjectSyncState,
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
        evidence_service: ErpSyncEvidenceService | None = None,
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
        self._evidence = evidence_service
        self._control = SqlErpSyncControlRepository(session_factory)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="erp-sync-orchestration",
        )
        self._launch_lock = Lock()
        self._closed = False

    def _assert_recovery_clear(self) -> None:
        if self._evidence is not None:
            self._evidence.assert_no_required_recovery()

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
        # Drain local work before FastAPI disposes the engine. This does not
        # revoke or shorten the durable SQL lease and never touches a peer's owner.
        self._executor.shutdown(wait=True, cancel_futures=True)

    def reconcile_startup(self) -> int:
        self._control.ensure_control_row()
        # Protocol validation happens inside reconciliation before any owner,
        # attempt, or child state can be mutated.
        reconciled = self._control.reconcile_expired_owner()
        # Configuration adoption is independent from automatic scheduling. This
        # permits a valid later-boundary restart/manual run while enabled=false.
        self._control.reconcile_configuration(self._settings)
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
        self._assert_recovery_clear()
        if self._closed or not self._control.schema_available():
            return None

        try:
            occurrence = self._control.record_daily_decision(
                self._settings,
                self._automatic_safety,
            )
        except ApplicationConflictError as exc:
            if exc.code in {
                "erp_sync_configuration_incompatible",
                "erp_sync_protocol_incompatible",
            }:
                logger.warning(
                    "ERP scheduler compatibility block code=%s",
                    exc.code,
                )
                return {
                    "status": "BLOCKED",
                    "diagnostic": exc.code,
                }
            raise
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
            trigger = (
                "SCHEDULED"
                if now <= scheduled + timedelta(seconds=self._settings.poll_seconds)
                else "CATCHUP"
            )
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
            claim = self._control.claim_outcome(
                occurrence_id=occurrence.id,
                trigger=trigger,
                actor_name=self._system_actor,
                settings=self._settings,
                automatic=True,
                automatic_safety=self._automatic_safety,
            )
        except ApplicationConflictError as exc:
            if exc.code in {
                "erp_sync_configuration_incompatible",
                "erp_sync_occurrence_configuration_incompatible",
                "erp_sync_protocol_incompatible",
                "erp_sync_automatic_safety_unavailable",
            }:
                return {
                    "status": "BLOCKED",
                    "diagnostic": exc.code,
                }
            return None
        attempt = claim.attempt
        if attempt is None:
            return None
        payload = self._control.serialize_attempt(attempt.id)
        if not claim.admitted:
            payload["claim_outcome"] = "OBSERVED"
            return payload
        self._submit(
            attempt.id,
            int(attempt.generation),
            automatic=True,
        )
        payload["claim_outcome"] = "ADMITTED"
        return payload

    def run_now(
        self,
        *,
        intent: str,
        actor_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._assert_recovery_clear()
        self._assert_sources(intent)
        occurrence = self._control.accept_manual(
            intent=intent,
            actor_name=actor_name,
            idempotency_key=idempotency_key,
            settings=self._settings,
        )
        if occurrence.last_attempt_id:
            return self._compat_payload(occurrence.last_attempt_id)

        claim = self._control.claim_outcome(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name=actor_name,
            settings=self._settings,
        )
        attempt = claim.attempt
        if attempt is None:
            if occurrence.success_attempt_id:
                return self._compat_payload(occurrence.success_attempt_id)
            raise ApplicationConflictError(
                "La commande ERP n'a pas pu créer de tentative.",
                code="erp_sync_command_not_claimed",
            )
        accepted = self._compat_payload(attempt.id)
        if claim.admitted:
            self._submit(attempt.id, int(attempt.generation))
        return accepted

    def retry(
        self,
        *,
        occurrence_id: str,
        actor_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._assert_recovery_clear()
        occurrence = self._control.get_occurrence(occurrence_id)
        self._assert_sources(occurrence.intent)
        if occurrence.success_attempt_id:
            return self._compat_payload(occurrence.success_attempt_id)
        claim = self._control.claim_outcome(
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
        attempt = claim.attempt
        if attempt is None:
            raise ApplicationConflictError(
                "La reprise ERP n'a pas pu créer de tentative.",
                code="erp_sync_retry_not_claimed",
            )
        accepted = self._compat_payload(attempt.id)
        if claim.admitted:
            self._submit(attempt.id, int(attempt.generation))
        return accepted

    def run_projects_only_compat(
        self,
        *,
        actor_name: str,
        project_source: ProjectSourcePort | None = None,
    ):
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
        future = self._submit(
            attempt.id,
            int(attempt.generation),
            project_source=project_source,
            propagate_errors=True,
        )
        return future.result()

    def run_targeted_project_tasks_compat(
        self,
        *,
        project_id: str,
        actor_name: str,
        project_task_source: ProjectTaskCatalogSourcePort | None = None,
    ):
        source = project_task_source or self._project_task_source
        if source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée.",
                code="acumatica_project_task_not_configured",
            )
        with self._session_factory() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ApplicationNotFoundError(
                    "Le projet demandé n'existe pas.",
                    code="project_not_found",
                    context={"project_id": project_id},
                )
            project_external_id = str(project.erp_external_id or "").strip()
            project_number = str(project.number)
        if not project_external_id:
            raise ApplicationValidationError(
                "Ce projet local ne possède pas d'identifiant ERP pour synchroniser ses tâches.",
                code="task_catalog_project_external_id_required",
                context={
                    "project_id": project_id,
                    "project_number": project_number,
                },
            )

        intent = f"TARGETED_PROJECT_TASKS:{project_id}"
        self._assert_sources(intent)
        occurrence = self._control.accept_manual(
            intent=intent,
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
        future = self._submit(
            attempt.id,
            int(attempt.generation),
            project_task_source=source,
            propagate_errors=True,
        )
        return future.result()

    def launch_projects(self, *, actor_name: str = "api") -> dict[str, Any]:
        current = self.current()
        if current is not None and current.get("status") in {"PENDING", "RUNNING"}:
            if current.get("intent") == "PROJECTS_ONLY":
                return current
            raise ApplicationConflictError(
                "Une synchronisation ERP incompatible est déjà active.",
                code="erp_sync_run_active",
                context={"attempt_id": current.get("attempt_id")},
            )
        return self.run_now(
            intent="PROJECTS_ONLY",
            actor_name=actor_name,
            idempotency_key=new_id(),
        )

    def launch_targeted_project_tasks(
        self,
        *,
        project_id: str,
        actor_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._assert_recovery_clear()
        self._assert_sources(f"TARGETED_PROJECT_TASKS:{project_id}")
        occurrence = self._control.accept_manual(
            intent=f"TARGETED_PROJECT_TASKS:{project_id}",
            actor_name=actor_name,
            idempotency_key=idempotency_key,
            settings=self._settings,
        )
        if occurrence.last_attempt_id:
            return self._compat_payload(occurrence.last_attempt_id)
        claim = self._control.claim_outcome(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name=actor_name,
            settings=self._settings,
        )
        attempt = claim.attempt
        if attempt is None:
            raise ApplicationConflictError(
                "La synchronisation ciblée n'a pas pu créer de tentative.",
                code="erp_sync_command_not_claimed",
            )
        accepted = self._compat_payload(attempt.id)
        if claim.admitted:
            self._submit(attempt.id, int(attempt.generation))
        return accepted

    def launch(self, *, actor_name: str = "api") -> dict[str, Any]:
        """Compatibility for the existing global tasks button.

        Repeated clicks while the same full orchestration is active reuse the
        current durable attempt rather than creating a hidden queue.
        """

        current = self.current()
        if current is not None and current.get("status") in {"PENDING", "RUNNING"}:
            intent = current.get("intent")
            compatible_legacy_global = (
                current.get("attempt_id") is None
                and intent == "ACTIVE_PROJECT_TASKS_ONLY"
            )
            if intent == "PROJECTS_AND_ACTIVE_TASKS" or compatible_legacy_global:
                return current
            raise ApplicationConflictError(
                "Une synchronisation ERP incompatible est déjà active.",
                code="erp_sync_run_active",
                context={"attempt_id": current.get("attempt_id")},
            )
        return self.run_now(
            intent="PROJECTS_AND_ACTIVE_TASKS",
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

    def _submit(
        self,
        attempt_id: str,
        generation: int,
        *,
        automatic: bool = False,
        project_source: ProjectSourcePort | None = None,
        project_task_source: ProjectTaskCatalogSourcePort | None = None,
        propagate_errors: bool = False,
    ) -> Future[Any]:
        with self._launch_lock:
            if self._closed:
                raise ApplicationUnavailableError(
                    "Le worker ERP est arrêté.",
                    code="erp_sync_runner_unavailable",
                )
            try:
                work = partial(
                    self._execute_attempt,
                    attempt_id,
                    generation,
                    automatic,
                    project_source=project_source,
                    project_task_source=project_task_source,
                    propagate_errors=propagate_errors,
                )
                if propagate_errors:
                    # The synchronous legacy adapters retain their request
                    # instrumentation while the actual ERP work runs on the
                    # managed executor. Canonical async launches deliberately
                    # remain detached from the completed HTTP request context.
                    context = copy_context()
                    return self._executor.submit(context.run, work)
                return self._executor.submit(work)
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

    @staticmethod
    def _project_owned_state(row: Project | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "erp_external_id": row.erp_external_id,
            "number": row.number,
            "name": row.name,
            "client": row.client,
            "project_manager_external_id": row.project_manager_external_id,
            "project_manager_name": row.project_manager_name,
            "status": row.status,
        }

    @staticmethod
    def _task_owned_state(row: TaskCatalogEntry | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "project_number": row.project_number,
            "task_code": row.task_code,
            "label": row.label,
            "status": row.status,
            "active": bool(row.active),
            "billing_rule": row.billing_rule,
            "allocation_rule": row.allocation_rule,
            "completion_percent": row.completion_percent,
            "erp_created_at": row.erp_created_at,
            "branch": row.branch,
            "approver_name": row.approver_name,
            "cv_enabled": row.cv_enabled,
            "time_entry_enabled": row.time_entry_enabled,
            "expenses_enabled": row.expenses_enabled,
            "erp_task_id": row.erp_task_id,
            "account_group": row.account_group,
            "cost_code": row.cost_code,
            "inventory_id": row.inventory_id,
            "budget_amount_cad": row.budget_amount_cad,
            "budget_actual_cad": row.budget_actual_cad,
            "budget_diagnostic": row.budget_diagnostic,
            "workforce_eligible": row.workforce_eligible,
            "resource_class_code": row.resource_class_code,
            "average_hourly_cost_cad": row.average_hourly_cost_cad,
            "budget_hours": row.budget_hours,
            "workforce_diagnostics": row.workforce_diagnostics,
            "erp_budget_last_success_at": row.erp_budget_last_success_at,
        }

    @staticmethod
    def _task_sync_state(row: TaskCatalogProjectSyncState | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "project_number": row.project_number,
            "last_attempt_at": row.last_attempt_at,
            "last_success_at": row.last_success_at,
            "source_rows": row.source_rows,
            "task_count": row.task_count,
            "rejected_rows": row.rejected_rows,
            "duration_ms": row.duration_ms,
            "last_error_code": row.last_error_code,
        }

    @staticmethod
    def _project_source_active(status: object) -> bool:
        return str(status or "active").strip().casefold() not in {
            "annulé",
            "annule",
            "fermé",
            "ferme",
            "terminé",
            "termine",
            "closed",
            "cancelled",
        }

    def _validate_project_capture(self, projects: tuple[Any, ...]) -> None:
        external_ids: set[str] = set()
        numbers: set[str] = set()
        for row in projects:
            external_id = str(row.external_id or "").strip()
            number = str(row.number or "").strip()
            name = str(row.name or "").strip()
            status = str(row.status or "").strip()
            if not external_id or not number or not name or not status:
                raise ApplicationValidationError(
                    "La source projets ERP est structurellement incomplète.",
                    code="erp_sync_project_source_structural_invalid",
                )
            if external_id in external_ids or number in numbers:
                raise ApplicationValidationError(
                    "La source projets ERP contient une identité dupliquée.",
                    code="erp_sync_project_source_duplicate_identity",
                )
            external_ids.add(external_id)
            numbers.add(number)

        with self._session_factory() as session:
            for row in projects:
                external_id = str(row.external_id).strip()
                number = str(row.number).strip()
                by_external = session.scalar(
                    select(Project).where(Project.erp_external_id == external_id)
                )
                by_number = session.scalar(
                    select(Project).where(Project.number == number)
                )
                if (
                    by_external is not None
                    and by_number is not None
                    and by_external.id != by_number.id
                ):
                    raise ApplicationValidationError(
                        "La source projets ERP entre en conflit avec les identités locales.",
                        code="erp_sync_project_source_identity_conflict",
                    )
                if (
                    by_number is not None
                    and str(by_number.erp_external_id or "").strip()
                    and str(by_number.erp_external_id).strip() != external_id
                ):
                    raise ApplicationValidationError(
                        "La source projets ERP tente de relier un numéro à un autre identifiant.",
                        code="erp_sync_project_source_identity_conflict",
                    )

    @staticmethod
    def _validate_task_capture(
        portfolio: Any,
        *,
        expected_projects: tuple[str, ...],
    ) -> None:
        snapshots: dict[str, Any] = {}
        expected = set(expected_projects)
        seen_task_ids: dict[str, str] = {}
        for snapshot in tuple(portfolio.snapshots):
            number = str(snapshot.project_number or "").strip()
            if not number or number in snapshots or number not in expected:
                raise ApplicationValidationError(
                    "La source tâches ERP contient une partition projet invalide.",
                    code="erp_sync_task_source_structural_invalid",
                )
            snapshots[number] = snapshot
            if int(snapshot.source_rows) < len(tuple(snapshot.items)):
                raise ApplicationValidationError(
                    "Les compteurs de la source tâches ERP sont incohérents.",
                    code="erp_sync_task_source_counter_invalid",
                )
            for item in tuple(snapshot.items):
                if str(item.project_number or "").strip() != number:
                    raise ApplicationValidationError(
                        "Une tâche ERP appartient à un projet incohérent.",
                        code="erp_sync_task_source_project_mismatch",
                    )
                task_id = str(item.erp_task_id or "").strip()
                task_code = str(item.code or "").strip()
                if not task_id or not task_code:
                    raise ApplicationValidationError(
                        "Une tâche ERP ne possède pas son identité obligatoire.",
                        code="erp_sync_task_source_identity_missing",
                    )
                previous_project = seen_task_ids.get(task_id)
                if previous_project is not None and previous_project != number:
                    raise ApplicationValidationError(
                        "Une identité TaskID ERP appartient à plusieurs projets.",
                        code="erp_sync_task_source_identity_conflict",
                    )
                seen_task_ids[task_id] = number
        missing = expected.difference(snapshots)
        if missing:
            raise ApplicationValidationError(
                "La source tâches ERP ne contient pas toutes les partitions attendues.",
                code="erp_sync_task_source_partition_missing",
            )

    def _capture_full_sources(
        self,
        *,
        attempt_id: str,
        project_source: ProjectSourcePort | None = None,
        project_task_source: ProjectTaskCatalogSourcePort | None = None,
    ) -> tuple[tuple[Any, ...], Any]:
        if self._evidence is None:
            raise ApplicationUnavailableError(
                "Les garde-fous de preuve ERP 643B ne sont pas configurés.",
                code="erp_sync_evidence_capability_unavailable",
            )
        source = project_source or self._project_source
        task_source = project_task_source or self._project_task_source
        if source is None or task_source is None:
            raise ApplicationUnavailableError(
                "Les sources ERP requises ne sont pas configurées.",
                code="erp_sync_source_not_configured",
            )
        if projects_snapshot is None:
            try:
                projects = tuple(source.list_projects())
            except Exception as exc:
                if isinstance(exc, ApplicationError):
                    raise
                raise ApplicationUnavailableError(
                    "La lecture des projets Acumatica a échoué.",
                    code="acumatica_project_read_failed",
                ) from exc
        else:
            projects = projects_snapshot
        self._validate_project_capture(projects)

        # Freeze the post-project active target set before mutation. Absence from
        # RP_Projects does not deactivate an existing local project, while an
        # explicitly received ERP status does override the target status.
        with self._session_factory() as session:
            local_projects = tuple(session.scalars(select(Project)))
        target_by_number: dict[str, tuple[str, str]] = {
            str(row.number): (str(row.erp_external_id).strip(), str(row.number))
            for row in local_projects
            if self._project_source_active(row.status)
            and str(row.erp_external_id or "").strip()
        }
        for row in projects:
            number = str(row.number).strip()
            external_id = str(row.external_id).strip()
            if self._project_source_active(row.status):
                target_by_number[number] = (external_id, number)
            else:
                target_by_number.pop(number, None)
        project_targets = tuple(
            target_by_number[number] for number in sorted(target_by_number)
        )
        fetch_many = getattr(task_source, "fetch_project_snapshots", None)
        if not callable(fetch_many):
            raise ApplicationUnavailableError(
                "La lecture globale batch RP_ProjectTasks n'est pas disponible.",
                code="acumatica_project_task_batch_required",
            )
        try:
            portfolio = fetch_many(project_targets=project_targets)
        except Exception as exc:
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationUnavailableError(
                "La lecture globale des tâches projet Acumatica a échoué.",
                code="acumatica_project_task_read_failed",
            ) from exc
        expected_numbers = tuple(number for _external_id, number in project_targets)
        self._validate_task_capture(
            portfolio,
            expected_projects=expected_numbers,
        )

        profile = self._settings.config_fingerprint
        project_page_evidence = getattr(source, "last_read_evidence", lambda: None)()
        projects_evidence = self._evidence.capture_source(
            attempt_id=attempt_id,
            source_type="PROJECTS",
            profile_fingerprint=profile,
            payload={
                "records": projects,
                "page_evidence": project_page_evidence
                or {
                    "complete": True,
                    "pages": 1,
                    "rows": len(projects),
                    "adapter": "injected",
                },
            },
            row_count=len(projects),
        )
        task_counts = {
            str(snapshot.project_number): int(snapshot.source_rows)
            for snapshot in portfolio.snapshots
        }
        task_page_evidence = getattr(task_source, "last_read_evidence", lambda: None)()
        tasks_evidence = self._evidence.capture_source(
            attempt_id=attempt_id,
            source_type="TASKS",
            profile_fingerprint=profile,
            payload={
                "portfolio": portfolio,
                "page_evidence": task_page_evidence
                or {
                    "complete": True,
                    "pages": int(portfolio.source_pages),
                    "rows": int(portfolio.source_rows),
                    "adapter": "injected",
                },
            },
            row_count=int(portfolio.source_rows),
            project_counts=task_counts,
        )
        projects_plausibility = self._evidence.validate_plausibility(
            projects_evidence,
        )
        tasks_plausibility = self._evidence.validate_plausibility(
            tasks_evidence,
            active_task_targets=len(project_targets),
        )
        if not projects_plausibility.accepted or not tasks_plausibility.accepted:
            safe_codes = tuple(
                dict.fromkeys(
                    item.split(":", 1)[0]
                    for item in (
                        *projects_plausibility.diagnostics,
                        *tasks_plausibility.diagnostics,
                    )
                )
            )
            raise ApplicationValidationError(
                "La source ERP requiert une approbation de baseline ou dépasse les seuils de plausibilité.",
                code="erp_sync_source_plausibility_failed",
                context={"diagnostics": list(safe_codes)},
            )
        return projects, portfolio

    def _validate_full_publication(
        self,
        *,
        attempt_id: str,
        generation: int,
    ) -> None:
        attempt = self._control.get_attempt(attempt_id)
        run_id = str(attempt.task_sync_run_id or "").strip()
        if not run_id:
            if self._evidence is not None:
                self._evidence.mark_recovery_required(
                    attempt_id=attempt_id,
                    reason_code="erp_sync_global_task_run_missing",
                )
            raise ApplicationConflictError(
                "Le run tâches durable manque avant publication.",
                code="erp_sync_global_validation_failed",
            )
        try:
            with self._session_factory() as session:
                SqlErpSyncControlRepository.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                    settings=self._settings,
                )
                run = session.get(AcumaticaProjectTaskSyncRun, run_id)
                if run is None:
                    raise ApplicationConflictError(
                        "Le run tâches durable est introuvable.",
                        code="erp_sync_global_validation_failed",
                    )
                results = tuple(
                    session.scalars(
                        select(AcumaticaProjectTaskSyncProjectResult).where(
                            AcumaticaProjectTaskSyncProjectResult.run_id == run_id
                        )
                    )
                )
                if len(results) != int(run.projects_total):
                    raise ApplicationConflictError(
                        "Le nombre de reçus projet ne correspond pas aux cibles attendues.",
                        code="erp_sync_global_validation_failed",
                    )
                if int(run.projects_processed) != len(results):
                    raise ApplicationConflictError(
                        "Le compteur de projets traités ne correspond pas aux reçus.",
                        code="erp_sync_global_validation_failed",
                    )
                status_total = (
                    int(run.projects_synchronized)
                    + int(run.projects_ignored)
                    + int(run.projects_rejected)
                )
                if status_total != len(results):
                    raise ApplicationConflictError(
                        "Les compteurs de statuts projet ne correspondent pas aux reçus.",
                        code="erp_sync_global_validation_failed",
                    )
                synchronized = tuple(row for row in results if row.status == "synchronized")
                if int(run.projects_synchronized) != len(synchronized):
                    raise ApplicationConflictError(
                        "Le compteur des projets synchronisés est incohérent.",
                        code="erp_sync_global_validation_failed",
                    )
                counter_pairs = (
                    ("source_rows_received", "source_rows"),
                    ("source_rows_rejected", "source_rows_rejected"),
                    ("tasks_received", "tasks_received"),
                    ("tasks_created", "created"),
                    ("tasks_updated", "updated"),
                    ("tasks_unchanged", "unchanged"),
                    ("tasks_deactivated", "deactivated"),
                    ("tasks_rejected", "rejected"),
                )
                for run_field, result_field in counter_pairs:
                    expected = sum(int(getattr(row, result_field) or 0) for row in synchronized)
                    if int(getattr(run, run_field) or 0) != expected:
                        raise ApplicationConflictError(
                            "Un compteur global ne correspond pas aux reçus projet.",
                            code="erp_sync_global_validation_failed",
                            context={"counter": run_field},
                        )
        except (ApplicationConflictError, ErpSyncFenceLost):
            if self._evidence is not None:
                self._evidence.mark_recovery_required(
                    attempt_id=attempt_id,
                    reason_code="erp_sync_global_validation_failed",
                )
            raise

    def _execute_attempt(
        self,
        attempt_id: str,
        generation: int,
        automatic: bool = False,
        *,
        project_source: ProjectSourcePort | None = None,
        project_task_source: ProjectTaskCatalogSourcePort | None = None,
        propagate_errors: bool = False,
    ) -> Any:
        stop = Event()
        heartbeat: Thread | None = None
        started = False
        try:
            if not self._control.start_attempt(
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
            ):
                if propagate_errors:
                    raise ApplicationConflictError(
                        "La tentative ERP acceptée n'a pas pu démarrer.",
                        code="erp_sync_command_not_started",
                    )
                return None
            started = True
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
            attempt = self._control.get_attempt(attempt_id)
            self._assert_recovery_clear()
            if automatic and not self._automatic_safety.automatic_execution_available():
                self._control.finish(
                    attempt_id=attempt_id,
                    generation=generation,
                    status=ERP_ATTEMPT_FAILED,
                    phase="FAILED",
                    error_code="erp_sync_automatic_safety_unavailable",
                    diagnostic=self._automatic_safety.diagnostic(),
                    settings=self._settings,
                )
                return
            if attempt.intent.startswith("TARGETED_PROJECT_TASKS:"):
                project_id = attempt.intent.split(":", 1)[1]
                if project_task_source is not None:
                    result = self._execute_targeted_compat_business(
                        attempt_id,
                        generation,
                        project_id,
                        project_task_source,
                    )
                    self._control.finish(
                        attempt_id=attempt_id,
                        generation=generation,
                        status=ERP_ATTEMPT_SUCCEEDED,
                        phase="PUBLICATION_COMMITTED",
                        settings=self._settings,
                    )
                    return result
                task_status = self._execute_targeted_task_project(
                    attempt_id,
                    generation,
                    project_id,
                )
                self._control.finish(
                    attempt_id=attempt_id,
                    generation=generation,
                    status=(
                        ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS
                        if task_status == SYNC_RUN_COMPLETED_WITH_ERRORS
                        else ERP_ATTEMPT_SUCCEEDED
                    ),
                    phase="PUBLICATION_COMMITTED",
                    diagnostic=(
                        "La synchronisation ciblée des tâches projet a échoué."
                        if task_status == SYNC_RUN_COMPLETED_WITH_ERRORS
                        else None
                    ),
                    settings=self._settings,
                )
                return None

            captured_projects: tuple[Any, ...] | None = None
            captured_portfolio: Any | None = None
            if (
                attempt.intent == "PROJECTS_AND_ACTIVE_TASKS"
                and self._evidence is not None
            ):
                captured_projects, captured_portfolio = self._capture_full_sources(
                    attempt_id=attempt_id,
                    project_source=project_source,
                    project_task_source=project_task_source,
                )

            if attempt.intent != "ACTIVE_PROJECT_TASKS_ONLY":
                project_result = self._execute_projects(
                    attempt_id,
                    generation,
                    project_source=project_source,
                    projects_snapshot=captured_projects,
                )
                if attempt.intent == "PROJECTS_ONLY":
                    self._control.finish(
                        attempt_id=attempt_id,
                        generation=generation,
                        status=ERP_ATTEMPT_SUCCEEDED,
                        phase="PUBLICATION_COMMITTED",
                        settings=self._settings,
                    )
                    return project_result

            task_status = self._execute_tasks(
                attempt_id,
                generation,
                portfolio_snapshot=captured_portfolio,
            )
            if attempt.intent == "PROJECTS_AND_ACTIVE_TASKS":
                self._validate_full_publication(
                    attempt_id=attempt_id,
                    generation=generation,
                )
            parent_status = (
                ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS
                if task_status == SYNC_RUN_COMPLETED_WITH_ERRORS
                else ERP_ATTEMPT_SUCCEEDED
            )
            published = self._control.finish(
                attempt_id=attempt_id,
                generation=generation,
                status=parent_status,
                phase="PUBLICATION_COMMITTED",
                diagnostic=(
                    "Une ou plusieurs synchronisations de tâches projet ont échoué."
                    if parent_status == ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS
                    else None
                ),
                settings=self._settings,
            )
            if (
                published
                and parent_status == ERP_ATTEMPT_SUCCEEDED
                and attempt.intent == "PROJECTS_AND_ACTIVE_TASKS"
                and self._evidence is not None
            ):
                # Baseline advancement is derived from a fully published success.
                # A fenced/failed terminal write can never advance authority.
                self._evidence.advance_approved_baselines(attempt_id=attempt_id)
        except ErpSyncFenceLost:
            # A newer generation owns the control row. The stale worker is not
            # permitted to publish a late failure or terminal status.
            logger.warning(
                "ERP sync worker fenced attempt_id=%s generation=%s",
                attempt_id,
                generation,
            )
            if propagate_errors:
                raise
        except ApplicationError as exc:
            if started:
                self._control.finish(
                    attempt_id=attempt_id,
                    generation=generation,
                    status=ERP_ATTEMPT_FAILED,
                    phase="FAILED",
                    error_code=exc.code or "erp_sync_failed",
                    diagnostic="La synchronisation ERP n'a pas pu être exécutée.",
                    settings=self._settings,
                )
            if propagate_errors:
                raise
        except Exception as exc:
            logger.error(
                "ERP sync orchestration failed attempt_id=%s generation=%s error_type=%s",
                attempt_id,
                generation,
                type(exc).__name__,
            )
            if started:
                self._control.finish(
                    attempt_id=attempt_id,
                    generation=generation,
                    status=ERP_ATTEMPT_FAILED,
                    phase="FAILED",
                    error_code="erp_sync_failed",
                    diagnostic="La synchronisation ERP n'a pas pu être exécutée.",
                    settings=self._settings,
                )
            if propagate_errors:
                raise
        finally:
            stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)
            if self._evidence is not None:
                try:
                    terminal = self._control.get_attempt(attempt_id)
                    if terminal.status in ERP_ATTEMPT_TERMINAL:
                        self._evidence.finalize_attempt(
                            attempt_id=attempt_id,
                            successful=terminal.status == ERP_ATTEMPT_SUCCEEDED,
                        )
                except Exception:
                    logger.exception(
                        "ERP evidence finalization failed attempt_id=%s",
                        attempt_id,
                    )

    def _execute_targeted_compat_business(
        self,
        attempt_id: str,
        generation: int,
        project_id: str,
        source: ProjectTaskCatalogSourcePort,
    ):
        with self._session_factory() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ApplicationNotFoundError(
                    "Le projet demandé n'existe pas.",
                    code="project_not_found",
                    context={"project_id": project_id},
                )
            project_external_id = str(project.erp_external_id or "").strip()
            project_number = str(project.number)
        if not project_external_id:
            raise ApplicationValidationError(
                "Ce projet local ne possède pas d'identifiant ERP pour synchroniser ses tâches.",
                code="task_catalog_project_external_id_required",
                context={"project_id": project_id, "project_number": project_number},
            )

        snapshot = source.fetch_project_snapshot(
            project_external_id=project_external_id,
            project_number=project_number,
        )
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
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
                attempt_id=attempt_id,
                generation=generation,
                phase="TASKS_FINISHED",
                settings=self._settings,
            )
            return result

    def _execute_projects(
        self,
        attempt_id: str,
        generation: int,
        *,
        project_source: ProjectSourcePort | None = None,
        projects_snapshot: tuple[Any, ...] | None = None,
    ):
        source = project_source or self._project_source
        if source is None:
            raise ApplicationUnavailableError(
                "La synchronisation projets Acumatica n'est pas configurée.",
                code="acumatica_not_configured",
            )
        try:
            projects = tuple(source.list_projects())
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
                settings=self._settings,
            )
            project_evidence: dict[str, str] = {}
            if self._evidence is not None:
                for incoming in projects:
                    external_id = str(incoming.external_id or "").strip()
                    number = str(incoming.number or "").strip()
                    existing = (
                        session.scalar(
                            select(Project).where(Project.erp_external_id == external_id)
                        )
                        if external_id
                        else None
                    ) or session.scalar(select(Project).where(Project.number == number))
                    key = external_id or number
                    project_evidence[key] = self._evidence.record_before_in_session(
                        session,
                        attempt_id=attempt_id,
                        generation=generation,
                        unit_type="PROJECT_SNAPSHOT",
                        entity_type="PROJECT",
                        entity_key=key,
                        before=self._project_owned_state(existing),
                    )
            result = ProjectSyncService(
                _PreloadedProjectSource(projects),
                SqlProjectSyncRepository(session),
            ).synchronize()
            if self._evidence is not None:
                for incoming in projects:
                    external_id = str(incoming.external_id or "").strip()
                    number = str(incoming.number or "").strip()
                    row = (
                        session.scalar(
                            select(Project).where(Project.erp_external_id == external_id)
                        )
                        if external_id
                        else None
                    ) or session.scalar(select(Project).where(Project.number == number))
                    if row is None:
                        raise ApplicationConflictError(
                            "Le projet ERP reçu n'existe pas après synchronisation.",
                            code="erp_sync_project_post_validation_failed",
                        )
                    key = external_id or number
                    self._evidence.record_after_in_session(
                        session,
                        evidence_id=project_evidence[key],
                        attempt_id=attempt_id,
                        generation=generation,
                        after=self._project_owned_state(row) or {},
                    )
            SqlErpSyncControlRepository.update_phase_in_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                phase="PROJECTS_VALIDATED",
                project_result=result,
                settings=self._settings,
            )
            return result

    def _create_task_run(
        self,
        attempt_id: str,
        generation: int,
        *,
        active_key: str | None = GLOBAL_SYNC_RUN_KEY,
    ) -> str:
        run_id = new_id()
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
            )
            run = AcumaticaProjectTaskSyncRun(
                id=run_id,
                active_key=active_key,
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
                settings=self._settings,
            )
        return run_id

    def _execute_targeted_task_project(
        self,
        attempt_id: str,
        generation: int,
        project_id: str,
    ) -> str:
        if self._project_task_source is None:
            raise ApplicationUnavailableError(
                "La synchronisation des tâches projet Acumatica n'est pas configurée.",
                code="acumatica_project_task_not_configured",
            )

        with self._session_factory() as discovery:
            projects = tuple(
                SqlPlannerQueryRepositoryWithLoadProfiles(
                    discovery
                ).list_projects(active_only=False)
            )
        project = next(
            (row for row in projects if row.id == project_id),
            None,
        )
        if project is None:
            raise ApplicationNotFoundError(
                "Le projet demandé n'existe pas.",
                code="project_not_found",
                context={"project_id": project_id},
            )
        if not project.active:
            raise ApplicationValidationError(
                "Le projet demandé est inactif.",
                code="project_inactive",
                context={"project_id": project_id},
            )
        project_external_id = str(project.erp_external_id or "").strip()
        project_number = str(project.number)
        if not project_external_id:
            raise ApplicationValidationError(
                "Ce projet local ne possède pas d'identifiant ERP.",
                code="task_catalog_project_external_id_required",
                context={"project_id": project_id},
            )

        try:
            snapshot = self._project_task_source.fetch_project_snapshot(
                project_external_id=project_external_id,
                project_number=project_number,
            )
        except Exception as exc:
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationUnavailableError(
                "La lecture des tâches projet Acumatica a échoué.",
                code="acumatica_project_task_read_failed",
            ) from exc

        run_id = self._create_task_run(
            attempt_id,
            generation,
            active_key=None,
        )
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
            )
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None:
                raise RuntimeError("Run tâches enfant introuvable.")
            run.projects_total = 1
            run.source_requests = 1
            run.source_rows_scanned = int(snapshot.source_rows)

        self._process_task_project(
            run_id,
            attempt_id,
            generation,
            project,
            {project_number: snapshot},
        )

        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=self._settings,
            )
            run = session.get(AcumaticaProjectTaskSyncRun, run_id)
            if run is None:
                raise RuntimeError("Run tâches enfant introuvable.")
            run.status = (
                SYNC_RUN_COMPLETED_WITH_ERRORS
                if run.projects_rejected
                else SYNC_RUN_COMPLETED
            )
            run.finished_at = utc_now()
            if run.started_at is not None:
                run.duration_ms = max(
                    0,
                    int(
                        (
                            _utc(run.finished_at) - _utc(run.started_at)
                        ).total_seconds()
                        * 1000
                    ),
                )
            SqlErpSyncControlRepository.update_phase_in_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                phase="TASKS_FINISHED",
                settings=self._settings,
            )
            return run.status

    def _execute_tasks(
        self,
        attempt_id: str,
        generation: int,
        *,
        portfolio_snapshot: Any | None = None,
    ) -> str:
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
                settings=self._settings,
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
                portfolio = portfolio_snapshot
                if portfolio is None:
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
                    settings=self._settings,
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
                    settings=self._settings,
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
                if run.started_at is not None:
                    run.duration_ms = max(
                        0,
                        int(
                            (
                                _utc(run.finished_at) - _utc(run.started_at)
                            ).total_seconds()
                            * 1000
                        ),
                    )
                SqlErpSyncControlRepository.update_phase_in_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                    phase="TASKS_FINISHED",
                    settings=self._settings,
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
                    settings=self._settings,
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
                if run.started_at is not None:
                    run.duration_ms = max(
                        0,
                        int(
                            (
                                _utc(run.finished_at) - _utc(run.started_at)
                            ).total_seconds()
                            * 1000
                        ),
                    )
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
                    "status": "ignored",
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
                    settings=self._settings,
                )
                task_evidence: dict[str, str] = {}
                sync_state_evidence: str | None = None
                if self._evidence is not None:
                    for item in tuple(snapshot.items):
                        task_id = str(item.erp_task_id or "").strip()
                        existing = (
                            session.scalar(
                                select(TaskCatalogEntry).where(
                                    TaskCatalogEntry.erp_task_id == task_id
                                )
                            )
                            if task_id
                            else None
                        )
                        if existing is None:
                            existing = session.scalar(
                                select(TaskCatalogEntry).where(
                                    TaskCatalogEntry.project_number == project.number,
                                    TaskCatalogEntry.task_code == str(item.code).strip(),
                                )
                            )
                        key = task_id or f"{project.number}:{str(item.code).strip()}"
                        task_evidence[key] = self._evidence.record_before_in_session(
                            session,
                            attempt_id=attempt_id,
                            generation=generation,
                            unit_type=f"TASK_PROJECT:{project.id}",
                            entity_type="TASK",
                            entity_key=key,
                            before=self._task_owned_state(existing),
                        )
                    sync_state_evidence = self._evidence.record_before_in_session(
                        session,
                        attempt_id=attempt_id,
                        generation=generation,
                        unit_type=f"TASK_PROJECT:{project.id}",
                        entity_type="TASK_SYNC_STATE",
                        entity_key=project.number,
                        before=self._task_sync_state(
                            session.get(TaskCatalogProjectSyncState, project.number)
                        ),
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
                if self._evidence is not None:
                    for item in tuple(snapshot.items):
                        task_id = str(item.erp_task_id or "").strip()
                        row = (
                            session.scalar(
                                select(TaskCatalogEntry).where(
                                    TaskCatalogEntry.erp_task_id == task_id
                                )
                            )
                            if task_id
                            else None
                        )
                        if row is None:
                            row = session.scalar(
                                select(TaskCatalogEntry).where(
                                    TaskCatalogEntry.project_number == project.number,
                                    TaskCatalogEntry.task_code == str(item.code).strip(),
                                )
                            )
                        key = task_id or f"{project.number}:{str(item.code).strip()}"
                        if key in task_evidence and row is not None:
                            self._evidence.record_after_in_session(
                                session,
                                evidence_id=task_evidence[key],
                                attempt_id=attempt_id,
                                generation=generation,
                                after=self._task_owned_state(row) or {},
                            )
                    if sync_state_evidence is not None:
                        state_row = session.get(
                            TaskCatalogProjectSyncState,
                            project.number,
                        )
                        if state_row is None:
                            raise ApplicationConflictError(
                                "La provenance tâches n'a pas été publiée dans la transaction.",
                                code="erp_sync_task_provenance_missing",
                            )
                        self._evidence.record_after_in_session(
                            session,
                            evidence_id=sync_state_evidence,
                            attempt_id=attempt_id,
                            generation=generation,
                            after=self._task_sync_state(state_row) or {},
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
                settings=self._settings,
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
