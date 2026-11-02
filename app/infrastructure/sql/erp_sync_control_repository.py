from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...application.erp_sync_scheduler import (
    ERP_SYNC_DAILY_POLICY,
    ERP_SYNC_PROTOCOL_VERSION,
    ErpSyncAutomaticSafetyCapability,
    ErpSyncSchedulerSettings,
    scheduler_decision,
)
from ...application.errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
)
from .acumatica_project_task_sync_models import (
    SYNC_RUN_ACTIVE_STATUSES,
    SYNC_RUN_INTERRUPTED,
    AcumaticaProjectTaskSyncRun,
)
from .base import new_id, utc_now
from .erp_sync_control_models import (
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_INTERRUPTED,
    ERP_ATTEMPT_PENDING,
    ERP_ATTEMPT_RUNNING,
    ERP_ATTEMPT_SUCCEEDED,
    ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
    ERP_ATTEMPT_TERMINAL,
    ERP_OCCURRENCE_DAILY,
    ERP_OCCURRENCE_DISABLED,
    ERP_OCCURRENCE_FAILED,
    ERP_OCCURRENCE_INTERRUPTED,
    ERP_OCCURRENCE_MANUAL,
    ERP_OCCURRENCE_MISSED,
    ERP_OCCURRENCE_PENDING,
    ERP_OCCURRENCE_SUCCEEDED,
    ERP_OCCURRENCE_SUCCEEDED_WITH_ERRORS,
    ERP_SYNC_CONTROL_ROW_ID,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncOccurrence,
)
from .session import SqlSessionFactory, transactional_session


class ErpSyncFenceLost(ApplicationConflictError):
    default_code = "erp_sync_fence_lost"


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sql_now(session: Session) -> datetime:
    if session.get_bind().dialect.name == "mssql":
        value = session.scalar(select(func.sysutcdatetime()))
        if value is None:
            raise RuntimeError("SYSUTCDATETIME() n'a retourné aucune valeur.")
        return _utc_aware(value)
    return _utc_aware(utc_now())


def _lock_control(session: Session) -> ErpSyncControl:
    statement = (
        select(ErpSyncControl)
        .where(ErpSyncControl.id == ERP_SYNC_CONTROL_ROW_ID)
        .with_hint(
            ErpSyncControl,
            "WITH (UPDLOCK, HOLDLOCK)",
            dialect_name="mssql",
        )
    )
    row = session.scalar(statement)
    if row is None:
        raise RuntimeError("Le contrôle ERP singleton attendu est absent.")
    return row


def _command_identity(
    *,
    actor_name: str,
    intent: str,
    idempotency_key: str,
    prefix: str = "MANUAL",
) -> str:
    payload = {
        "actor": str(actor_name or "").strip(),
        "intent": str(intent or "").strip(),
        "idempotency_key": str(idempotency_key or "").strip(),
    }
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{prefix}:{digest}"


def retry_command_identity(
    *,
    actor_name: str,
    occurrence_id: str,
    idempotency_key: str,
) -> str:
    return _command_identity(
        actor_name=actor_name,
        intent=f"RETRY:{occurrence_id}",
        idempotency_key=idempotency_key,
        prefix="RETRY",
    )


def _assert_control_protocol(
    control: ErpSyncControl,
) -> None:
    if int(control.protocol_version) != ERP_SYNC_PROTOCOL_VERSION:
        raise ApplicationConflictError(
            "Le protocole du contrôle ERP est incompatible avec ce processus.",
            code="erp_sync_protocol_incompatible",
            context={
                "expected_protocol_version": ERP_SYNC_PROTOCOL_VERSION,
                "stored_protocol_version": int(control.protocol_version),
            },
        )


def _ensure_config_compatible_locked(
    control: ErpSyncControl,
    settings: ErpSyncSchedulerSettings,
    *,
    allow_boundary_adoption: bool = False,
    business_date=None,
) -> None:
    _assert_control_protocol(control)
    expected = settings.config_fingerprint
    stored = str(control.config_fingerprint or "").strip()
    if not stored:
        control.config_fingerprint = expected
        return
    if stored == expected:
        return

    watermark = control.last_evaluated_business_date
    if (
        allow_boundary_adoption
        and not control.owner_attempt_id
        and business_date is not None
        and (watermark is None or business_date > watermark)
    ):
        control.config_fingerprint = expected
        return

    raise ApplicationConflictError(
        "La configuration ERP de ce processus est incompatible avec l'autorité SQL.",
        code="erp_sync_configuration_incompatible",
    )


def _automatic_deadline(
    occurrence: ErpSyncOccurrence,
    settings: ErpSyncSchedulerSettings,
) -> datetime:
    if occurrence.scheduled_at is None:
        raise ApplicationConflictError(
            "L'occurrence automatique ne possède pas d'échéance figée.",
            code="erp_sync_automatic_schedule_missing",
        )
    scheduled = _utc_aware(occurrence.scheduled_at)
    zone = settings.timezone()
    next_midnight = datetime.combine(
        occurrence.business_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(timezone.utc)
    return min(
        scheduled + timedelta(hours=settings.catchup_hours),
        next_midnight,
    )


class SqlErpSyncControlRepository:
    """ADR-033 durable occurrence/claim/lease/generation authority."""

    def __init__(self, session_factory: SqlSessionFactory) -> None:
        self._session_factory = session_factory

    def schema_available(self) -> bool:
        with self._session_factory() as session:
            return inspect(session.get_bind()).has_table("erp_sync_control")

    def current_sql_time(self) -> datetime:
        with self._session_factory() as session:
            return _sql_now(session)

    def ensure_control_row(self) -> None:
        if not self.schema_available():
            return
        try:
            with transactional_session(self._session_factory) as session:
                if session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID) is None:
                    session.add(
                        ErpSyncControl(
                            id=ERP_SYNC_CONTROL_ROW_ID,
                            generation=0,
                            protocol_version=1,
                        )
                    )
                    session.flush()
        except IntegrityError:
            # Concurrent backend startup may race on the singleton primary key.
            # The migration/runtime invariant is satisfied once either insert wins.
            with self._session_factory() as session:
                if session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID) is None:
                    raise

    @staticmethod
    def guard_session(
        session: Session,
        *,
        attempt_id: str,
        generation: int,
        settings: ErpSyncSchedulerSettings | None = None,
    ) -> tuple[ErpSyncControl, datetime]:
        control = _lock_control(session)
        now = _sql_now(session)
        if int(control.protocol_version) != ERP_SYNC_PROTOCOL_VERSION:
            raise ErpSyncFenceLost(
                "Le protocole ERP du propriétaire n'est plus compatible.",
                code="erp_sync_protocol_incompatible",
                context={
                    "attempt_id": attempt_id,
                    "generation": int(generation),
                },
            )
        attempt = session.get(ErpSyncAttempt, attempt_id)
        occurrence = (
            session.get(ErpSyncOccurrence, attempt.occurrence_id)
            if attempt is not None
            else None
        )
        if (
            occurrence is None
            or str(control.config_fingerprint or "").strip()
            != str(occurrence.config_fingerprint or "").strip()
            or (
                settings is not None
                and occurrence.config_fingerprint != settings.config_fingerprint
            )
        ):
            raise ErpSyncFenceLost(
                "La configuration ERP de la tentative ne correspond plus à l'autorité SQL.",
                code="erp_sync_configuration_incompatible",
                context={"attempt_id": attempt_id, "generation": int(generation)},
            )
        lease = (
            _utc_aware(control.lease_expires_at)
            if control.lease_expires_at is not None
            else None
        )
        deadline = (
            _utc_aware(control.owner_deadline_at)
            if control.owner_deadline_at is not None
            else None
        )
        if (
            control.owner_attempt_id != attempt_id
            or int(control.generation) != int(generation)
            or lease is None
            or lease <= now
            or deadline is None
            or deadline <= now
        ):
            raise ErpSyncFenceLost(
                "La tentative ERP n'est plus propriétaire de la génération active.",
                code="erp_sync_fence_lost",
                context={
                    "attempt_id": attempt_id,
                    "generation": int(generation),
                },
            )
        return control, now

    def _interrupt_expired_locked(
        self,
        session: Session,
        control: ErpSyncControl,
        *,
        now: datetime,
    ) -> int:
        owner_id = str(control.owner_attempt_id or "").strip()
        if not owner_id:
            return 0
        lease = (
            _utc_aware(control.lease_expires_at)
            if control.lease_expires_at is not None
            else None
        )
        deadline = (
            _utc_aware(control.owner_deadline_at)
            if control.owner_deadline_at is not None
            else None
        )
        if (
            lease is not None
            and lease > now
            and deadline is not None
            and deadline > now
        ):
            return 0

        attempt = session.get(ErpSyncAttempt, owner_id)
        if attempt is not None and attempt.status not in ERP_ATTEMPT_TERMINAL:
            attempt.status = ERP_ATTEMPT_INTERRUPTED
            attempt.finished_at = now
            attempt.error_code = "lease_expired"
            attempt.diagnostic = "Le propriétaire ERP a perdu sa lease ou sa durée maximale."
            occurrence = session.get(ErpSyncOccurrence, attempt.occurrence_id)
            if occurrence is not None and occurrence.success_attempt_id is None:
                occurrence.status = ERP_OCCURRENCE_INTERRUPTED
                occurrence.diagnostic = attempt.diagnostic
            if attempt.task_sync_run_id:
                child = session.get(
                    AcumaticaProjectTaskSyncRun,
                    attempt.task_sync_run_id,
                )
                if child is not None and child.status in SYNC_RUN_ACTIVE_STATUSES:
                    child.status = SYNC_RUN_INTERRUPTED
                    child.active_key = None
                    child.finished_at = now
                    child.error_code = "lease_expired"
                    child.diagnostic = (
                        "Le propriétaire ERP parent a perdu sa lease ou sa durée maximale."
                    )

        control.owner_attempt_id = None
        control.lease_expires_at = None
        control.owner_deadline_at = None
        control.heartbeat_at = None
        return 1

    def reconcile_expired_owner(self) -> int:
        if not self.schema_available():
            return 0
        with transactional_session(self._session_factory) as session:
            control = _lock_control(session)
            return self._interrupt_expired_locked(
                session,
                control,
                now=_sql_now(session),
            )

    def record_daily_decision(
        self,
        settings: ErpSyncSchedulerSettings,
        capability: ErpSyncAutomaticSafetyCapability,
    ) -> ErpSyncOccurrence | None:
        if not self.schema_available():
            return None
        with transactional_session(self._session_factory) as session:
            control = _lock_control(session)
            now = _sql_now(session)
            self._interrupt_expired_locked(session, control, now=now)
            decision = scheduler_decision(now, settings)
            watermark = control.last_evaluated_business_date
            if watermark is not None and decision.business_date < watermark:
                return None

            _ensure_config_compatible_locked(
                control,
                settings,
                allow_boundary_adoption=True,
                business_date=decision.business_date,
            )
            if watermark is None or decision.business_date > watermark:
                control.last_evaluated_business_date = decision.business_date

            if decision.state == "NOT_DUE":
                return None
            if decision.state == "DISABLED":
                return None

            key = f"{ERP_SYNC_DAILY_POLICY}:{decision.business_date.isoformat()}"
            existing = session.scalar(
                select(ErpSyncOccurrence).where(
                    ErpSyncOccurrence.occurrence_key == key
                )
            )
            if existing is not None:
                return existing

            state = ERP_OCCURRENCE_PENDING
            diagnostic = None
            if not capability.automatic_execution_available():
                state = ERP_OCCURRENCE_DISABLED
                diagnostic = capability.diagnostic()
            elif decision.state == "MISSED":
                state = ERP_OCCURRENCE_MISSED
                diagnostic = "Fenêtre de rattrapage automatique dépassée."

            occurrence = ErpSyncOccurrence(
                id=new_id(),
                occurrence_key=key,
                occurrence_type=ERP_OCCURRENCE_DAILY,
                intent="PROJECTS_AND_ACTIVE_TASKS",
                business_date=decision.business_date,
                scheduled_at=decision.scheduled_at_utc,
                timezone_name=settings.timezone_name,
                scheduled_local_time=settings.local_time.strftime("%H:%M"),
                config_fingerprint=settings.config_fingerprint,
                status=state,
                diagnostic=diagnostic,
            )
            session.add(occurrence)
            session.flush()
            return occurrence

    def accept_manual(
        self,
        *,
        intent: str,
        actor_name: str,
        idempotency_key: str,
        settings: ErpSyncSchedulerSettings,
    ) -> ErpSyncOccurrence:
        identity = _command_identity(
            actor_name=actor_name,
            intent=intent,
            idempotency_key=idempotency_key,
        )
        with transactional_session(self._session_factory) as session:
            control = _lock_control(session)
            _ensure_config_compatible_locked(control, settings)
            existing = session.scalar(
                select(ErpSyncOccurrence).where(
                    ErpSyncOccurrence.command_identity == identity
                )
            )
            if existing is not None:
                return existing

            now = _sql_now(session)
            business_date = now.astimezone(settings.timezone()).date()
            occurrence = ErpSyncOccurrence(
                id=new_id(),
                occurrence_key=f"MANUAL:{new_id()}",
                occurrence_type=ERP_OCCURRENCE_MANUAL,
                intent=intent,
                business_date=business_date,
                scheduled_at=None,
                timezone_name=settings.timezone_name,
                scheduled_local_time=settings.local_time.strftime("%H:%M"),
                config_fingerprint=settings.config_fingerprint,
                command_identity=identity,
                status=ERP_OCCURRENCE_PENDING,
            )
            session.add(occurrence)
            session.flush()
            return occurrence

    def get_occurrence(self, occurrence_id: str) -> ErpSyncOccurrence:
        with self._session_factory() as session:
            row = session.get(ErpSyncOccurrence, occurrence_id)
            if row is None:
                raise ApplicationNotFoundError(
                    "Occurrence de synchronisation ERP introuvable.",
                    code="erp_sync_occurrence_not_found",
                    context={"occurrence_id": occurrence_id},
                )
            session.expunge(row)
            return row

    def get_attempt(self, attempt_id: str) -> ErpSyncAttempt:
        with self._session_factory() as session:
            row = session.get(ErpSyncAttempt, attempt_id)
            if row is None:
                raise ApplicationNotFoundError(
                    "Tentative de synchronisation ERP introuvable.",
                    code="erp_sync_attempt_not_found",
                    context={"attempt_id": attempt_id},
                )
            session.expunge(row)
            return row

    def current_attempt(self) -> ErpSyncAttempt | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ErpSyncAttempt)
                .order_by(ErpSyncAttempt.created_at.desc(), ErpSyncAttempt.id.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row

    def claim(
        self,
        *,
        occurrence_id: str,
        trigger: str,
        actor_name: str,
        settings: ErpSyncSchedulerSettings,
        automatic: bool = False,
        command_identity: str | None = None,
        automatic_safety: ErpSyncAutomaticSafetyCapability | None = None,
    ) -> ErpSyncAttempt | None:
        with transactional_session(self._session_factory) as session:
            control = _lock_control(session)
            now = _sql_now(session)

            occurrence = session.get(ErpSyncOccurrence, occurrence_id)
            if occurrence is None:
                raise ApplicationNotFoundError(
                    "Occurrence de synchronisation ERP introuvable.",
                    code="erp_sync_occurrence_not_found",
                    context={"occurrence_id": occurrence_id},
                )

            _ensure_config_compatible_locked(control, settings)
            if occurrence.config_fingerprint != settings.config_fingerprint:
                raise ApplicationConflictError(
                    "La configuration figée de l'occurrence ERP est incompatible.",
                    code="erp_sync_occurrence_configuration_incompatible",
                    context={"occurrence_id": occurrence.id},
                )

            if command_identity:
                existing_command = session.scalar(
                    select(ErpSyncAttempt).where(
                        ErpSyncAttempt.command_identity == command_identity
                    )
                )
                if existing_command is not None:
                    return existing_command

            self._interrupt_expired_locked(session, control, now=now)

            if automatic:
                if (
                    automatic_safety is None
                    or not automatic_safety.automatic_execution_available()
                ):
                    raise ApplicationConflictError(
                        "La capacité de sécurité automatique ERP n'est pas disponible.",
                        code="erp_sync_automatic_safety_unavailable",
                    )
                if occurrence.occurrence_type != ERP_OCCURRENCE_DAILY:
                    raise ApplicationConflictError(
                        "Seules les occurrences DAILY peuvent démarrer automatiquement.",
                        code="erp_sync_automatic_occurrence_invalid",
                    )
                current_business_date = now.astimezone(settings.timezone()).date()
                watermark = control.last_evaluated_business_date
                if (
                    current_business_date != occurrence.business_date
                    or (
                        watermark is not None
                        and occurrence.business_date < watermark
                    )
                    or now < _utc_aware(occurrence.scheduled_at)
                    or now >= _automatic_deadline(occurrence, settings)
                ):
                    occurrence.status = ERP_OCCURRENCE_MISSED
                    occurrence.diagnostic = (
                        "Fenêtre de départ automatique figée dépassée."
                    )
                    return None

            if occurrence.success_attempt_id:
                return None
            if control.owner_attempt_id:
                owner = session.get(ErpSyncAttempt, control.owner_attempt_id)
                if owner is not None and owner.occurrence_id == occurrence.id:
                    return owner
                raise ApplicationConflictError(
                    "Une synchronisation ERP incompatible est déjà active.",
                    code="erp_sync_run_active",
                    context={"attempt_id": control.owner_attempt_id},
                )

            attempts = tuple(
                session.scalars(
                    select(ErpSyncAttempt)
                    .where(ErpSyncAttempt.occurrence_id == occurrence.id)
                    .order_by(ErpSyncAttempt.attempt_number)
                )
            )
            if automatic and len(attempts) >= settings.max_automatic_attempts:
                return None

            generation = int(control.generation) + 1
            attempt = ErpSyncAttempt(
                id=new_id(),
                occurrence_id=occurrence.id,
                attempt_number=len(attempts) + 1,
                trigger_kind=trigger,
                actor_name=actor_name,
                intent=occurrence.intent,
                command_identity=command_identity,
                generation=generation,
                status=ERP_ATTEMPT_PENDING,
                phase="PREFLIGHT",
            )
            session.add(attempt)
            session.flush()

            control.generation = generation
            control.owner_attempt_id = attempt.id
            control.lease_expires_at = now + timedelta(seconds=settings.lease_seconds)
            control.owner_deadline_at = now + timedelta(seconds=settings.max_attempt_seconds)
            control.heartbeat_at = now
            occurrence.last_attempt_id = attempt.id
            occurrence.status = ERP_OCCURRENCE_PENDING
            return attempt

    def renew(
        self,
        *,
        attempt_id: str,
        generation: int,
        settings: ErpSyncSchedulerSettings,
    ) -> bool:
        with transactional_session(self._session_factory) as session:
            try:
                control, now = self.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                    settings=settings,
                )
            except ErpSyncFenceLost:
                return False
            deadline = _utc_aware(control.owner_deadline_at)
            control.lease_expires_at = min(
                now + timedelta(seconds=settings.lease_seconds),
                deadline,
            )
            control.heartbeat_at = now
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is not None:
                attempt.heartbeat_at = now
            return True

    def start_attempt(
        self,
        *,
        attempt_id: str,
        generation: int,
        settings: ErpSyncSchedulerSettings | None = None,
    ) -> bool:
        with transactional_session(self._session_factory) as session:
            _control, now = self.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=settings,
            )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationNotFoundError(
                    "Tentative de synchronisation ERP introuvable.",
                    code="erp_sync_attempt_not_found",
                )
            if attempt.status != ERP_ATTEMPT_PENDING:
                return False
            attempt.status = ERP_ATTEMPT_RUNNING
            attempt.started_at = now
            attempt.heartbeat_at = now
            return True

    @classmethod
    def update_phase_in_session(
        cls,
        session: Session,
        *,
        attempt_id: str,
        generation: int,
        phase: str,
        project_result: object | None = None,
        task_sync_run_id: str | None = None,
        settings: ErpSyncSchedulerSettings | None = None,
    ) -> None:
        _control, now = cls.guard_session(
            session,
            attempt_id=attempt_id,
            generation=generation,
            settings=settings,
        )
        attempt = session.get(ErpSyncAttempt, attempt_id)
        if attempt is None or attempt.status not in {
            ERP_ATTEMPT_PENDING,
            ERP_ATTEMPT_RUNNING,
        }:
            raise ErpSyncFenceLost(
                "La tentative ERP ne peut plus enregistrer de progression.",
                code="erp_sync_fence_lost",
            )
        attempt.phase = phase
        attempt.heartbeat_at = now
        if project_result is not None:
            attempt.project_received = int(getattr(project_result, "received", 0))
            attempt.projects_created = int(getattr(project_result, "created", 0))
            attempt.projects_updated = int(getattr(project_result, "updated", 0))
            attempt.projects_unchanged = int(getattr(project_result, "unchanged", 0))
        if task_sync_run_id is not None:
            attempt.task_sync_run_id = task_sync_run_id

    def finish(
        self,
        *,
        attempt_id: str,
        generation: int,
        status: str,
        phase: str,
        error_code: str | None = None,
        diagnostic: str | None = None,
        settings: ErpSyncSchedulerSettings | None = None,
    ) -> bool:
        with transactional_session(self._session_factory) as session:
            try:
                control, now = self.guard_session(
                    session,
                    attempt_id=attempt_id,
                    generation=generation,
                    settings=settings,
                )
            except ErpSyncFenceLost:
                return False
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None or attempt.status in ERP_ATTEMPT_TERMINAL:
                return False

            attempt.status = status
            attempt.phase = phase
            attempt.finished_at = now
            attempt.error_code = error_code
            attempt.diagnostic = diagnostic
            occurrence = session.get(ErpSyncOccurrence, attempt.occurrence_id)
            if occurrence is not None:
                if status == ERP_ATTEMPT_SUCCEEDED:
                    occurrence.status = ERP_OCCURRENCE_SUCCEEDED
                    occurrence.success_attempt_id = attempt.id
                    occurrence.diagnostic = None
                    if attempt.intent == "PROJECTS_AND_ACTIVE_TASKS":
                        control.last_success_attempt_id = attempt.id
                elif status == ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS:
                    occurrence.status = ERP_OCCURRENCE_SUCCEEDED_WITH_ERRORS
                    occurrence.success_attempt_id = None
                    occurrence.diagnostic = diagnostic
                elif status == ERP_ATTEMPT_INTERRUPTED:
                    occurrence.status = ERP_OCCURRENCE_INTERRUPTED
                    occurrence.diagnostic = diagnostic
                else:
                    occurrence.status = ERP_OCCURRENCE_FAILED
                    occurrence.diagnostic = diagnostic

            control.owner_attempt_id = None
            control.lease_expires_at = None
            control.owner_deadline_at = None
            control.heartbeat_at = None
            return True

    def serialize_attempt(self, attempt_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationNotFoundError(
                    "Tentative de synchronisation ERP introuvable.",
                    code="erp_sync_attempt_not_found",
                    context={"attempt_id": attempt_id},
                )
            occurrence = session.get(ErpSyncOccurrence, attempt.occurrence_id)
            return {
                "run_id": attempt.id,
                "attempt_id": attempt.id,
                "occurrence_id": attempt.occurrence_id,
                "occurrence_type": occurrence.occurrence_type if occurrence else None,
                "business_date": occurrence.business_date if occurrence else None,
                "trigger": attempt.trigger_kind,
                "intent": attempt.intent,
                "generation": int(attempt.generation),
                "status": attempt.status,
                "phase": attempt.phase,
                "created_at": attempt.created_at,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "heartbeat_at": attempt.heartbeat_at,
                "project_received": attempt.project_received,
                "projects_created": attempt.projects_created,
                "projects_updated": attempt.projects_updated,
                "projects_unchanged": attempt.projects_unchanged,
                "task_sync_run_id": attempt.task_sync_run_id,
                "error_code": attempt.error_code,
                "diagnostic": attempt.diagnostic,
            }
