from __future__ import annotations

from sqlalchemy import select
from zoneinfo import ZoneInfo

from ...application.erp_sync_operations import (
    ErpSyncAttemptOperations,
    ErpSyncEvidenceOperations,
    ErpSyncOperationsData,
    ErpSyncProjectOperations,
    ErpSyncRecoveryOperations,
)
from .acumatica_project_task_sync_models import (
    AcumaticaProjectTaskSyncProjectResult,
    AcumaticaProjectTaskSyncRun,
)
from .erp_sync_control_models import (
    ERP_SYNC_CONTROL_ROW_ID,
    ERP_OCCURRENCE_DAILY,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncOccurrence,
)
from .erp_sync_control_repository import SqlErpSyncControlRepository
from .erp_sync_evidence_models import (
    EVIDENCE_PROOF_CHECKPOINT,
    EVIDENCE_PROOF_SOURCE,
    EVIDENCE_STATE_EXPIRED,
    EVIDENCE_STATE_READY,
    EVIDENCE_STATE_SEALED,
    RECOVERY_NONE,
    ErpSyncEvidenceManifest,
    ErpSyncEvidenceReservation,
    ErpSyncProjectQuality,
    ErpSyncRecoveryState,
)


class SqlErpSyncOperationsRepository:
    def __init__(self, session_factory, *, timezone_name: str) -> None:
        self._session_factory = session_factory
        self._control = SqlErpSyncControlRepository(session_factory)
        self._timezone = ZoneInfo(timezone_name)

    @staticmethod
    def _attempt(
        row: ErpSyncAttempt,
        task_run: AcumaticaProjectTaskSyncRun | None = None,
    ) -> ErpSyncAttemptOperations:
        return ErpSyncAttemptOperations(
            id=row.id,
            occurrence_id=row.occurrence_id,
            status=row.status,
            phase=row.phase,
            trigger_kind=row.trigger_kind,
            generation=int(row.generation),
            started_at=row.started_at,
            finished_at=row.finished_at,
            heartbeat_at=row.heartbeat_at,
            duration_ms=task_run.duration_ms if task_run is not None else None,
            projects_received=int(row.project_received),
            projects_created=int(row.projects_created),
            projects_updated=int(row.projects_updated),
            projects_unchanged=int(row.projects_unchanged),
            task_projects_total=int(task_run.projects_total) if task_run is not None else None,
            task_projects_synchronized=(
                int(task_run.projects_synchronized) if task_run is not None else None
            ),
            task_projects_rejected=(
                int(task_run.projects_rejected) if task_run is not None else None
            ),
            task_rows_received=(
                int(task_run.source_rows_received) if task_run is not None else None
            ),
            tasks_received=int(task_run.tasks_received) if task_run is not None else None,
            tasks_created=int(task_run.tasks_created) if task_run is not None else None,
            tasks_updated=int(task_run.tasks_updated) if task_run is not None else None,
            tasks_unchanged=int(task_run.tasks_unchanged) if task_run is not None else None,
            tasks_deactivated=(
                int(task_run.tasks_deactivated) if task_run is not None else None
            ),
            tasks_rejected=int(task_run.tasks_rejected) if task_run is not None else None,
            error_code=row.error_code,
            diagnostic=row.diagnostic,
        )

    def read(self, *, evidence_budget_bytes: int | None) -> ErpSyncOperationsData:
        now = self._control.current_sql_time()
        with self._session_factory() as session:
            control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            if control is None:
                raise RuntimeError("ERP sync control row is unavailable")

            attempt_rows = tuple(
                session.scalars(
                    select(ErpSyncAttempt)
                    .order_by(ErpSyncAttempt.created_at.desc(), ErpSyncAttempt.id.desc())
                    .limit(50)
                )
            )
            if (
                control.owner_attempt_id
                and all(row.id != control.owner_attempt_id for row in attempt_rows)
            ):
                owner = session.get(ErpSyncAttempt, control.owner_attempt_id)
                if owner is not None:
                    attempt_rows = (owner, *attempt_rows)
            task_run_ids_for_attempts = tuple(
                dict.fromkeys(
                    row.task_sync_run_id
                    for row in attempt_rows
                    if row.task_sync_run_id
                )
            )
            task_runs = {
                row.id: row
                for row in (
                    session.scalars(
                        select(AcumaticaProjectTaskSyncRun).where(
                            AcumaticaProjectTaskSyncRun.id.in_(
                                task_run_ids_for_attempts
                            )
                        )
                    )
                    if task_run_ids_for_attempts
                    else ()
                )
            }
            attempts = tuple(
                self._attempt(
                    row,
                    task_runs.get(row.task_sync_run_id)
                    if row.task_sync_run_id
                    else None,
                )
                for row in attempt_rows
            )
            task_run_by_attempt = {
                row.id: row.task_sync_run_id
                for row in session.scalars(select(ErpSyncAttempt))
                if row.task_sync_run_id
            }
            task_run_ids = tuple(dict.fromkeys(task_run_by_attempt.values()))
            results_by_run_project = {
                (row.run_id, row.project_number): row
                for row in (
                    session.scalars(
                        select(AcumaticaProjectTaskSyncProjectResult).where(
                            AcumaticaProjectTaskSyncProjectResult.run_id.in_(
                                task_run_ids
                            )
                        )
                    )
                    if task_run_ids
                    else ()
                )
            }
            last_success = (
                session.get(ErpSyncAttempt, control.last_success_attempt_id)
                if control.last_success_attempt_id
                else None
            )

            business_date = now.astimezone(self._timezone).date()
            daily = session.scalar(
                select(ErpSyncOccurrence)
                .where(
                    ErpSyncOccurrence.occurrence_type == ERP_OCCURRENCE_DAILY,
                    ErpSyncOccurrence.business_date == business_date,
                )
                .order_by(ErpSyncOccurrence.created_at.desc())
                .limit(1)
            )

            generations = {
                row.id: int(row.generation)
                for row in session.scalars(select(ErpSyncAttempt))
            }
            projects = tuple(
                ErpSyncProjectOperations(
                    project_number=row.project_number,
                    result_status=(
                        results_by_run_project[
                            (
                                task_run_by_attempt.get(row.producer_attempt_id),
                                row.project_number,
                            )
                        ].status
                        if (
                            task_run_by_attempt.get(row.producer_attempt_id),
                            row.project_number,
                        )
                        in results_by_run_project
                        else None
                    ),
                    quality_state=row.quality_state,
                    source_observed_at=row.source_observed_at,
                    validated_at=row.validated_at,
                    reason_code=row.reason_code,
                    error_code=(
                        results_by_run_project[
                            (
                                task_run_by_attempt.get(row.producer_attempt_id),
                                row.project_number,
                            )
                        ].error_code
                        if (
                            task_run_by_attempt.get(row.producer_attempt_id),
                            row.project_number,
                        )
                        in results_by_run_project
                        else None
                    ),
                    producer_attempt_id=row.producer_attempt_id,
                    producer_generation=(
                        generations.get(row.producer_attempt_id)
                        if row.producer_attempt_id
                        else None
                    ),
                )
                for row in session.scalars(
                    select(ErpSyncProjectQuality).order_by(
                        ErpSyncProjectQuality.project_number
                    )
                )
            )

            recovery_row = session.scalar(
                select(ErpSyncRecoveryState)
                .order_by(
                    (ErpSyncRecoveryState.state == "REQUIRED").desc(),
                    ErpSyncRecoveryState.updated_at.desc(),
                )
                .limit(1)
            )
            recovery = ErpSyncRecoveryOperations(
                state=recovery_row.state if recovery_row is not None else RECOVERY_NONE,
                attempt_id=recovery_row.attempt_id if recovery_row is not None else None,
                reason_code=recovery_row.reason_code if recovery_row is not None else None,
                pinned=bool(recovery_row.pinned) if recovery_row is not None else False,
            )

            manifests = tuple(session.scalars(select(ErpSyncEvidenceManifest)))
            reservations = tuple(
                session.scalars(select(ErpSyncEvidenceReservation))
            )
            active_reservations = tuple(
                row for row in reservations if row.released_at is None
            )
            reserved_bytes = sum(int(row.reserved_bytes) for row in active_reservations)
            used_bytes = sum(int(row.used_bytes) for row in active_reservations)
            pinned = sum(1 for row in manifests if bool(row.pinned))
            expired = sum(
                1
                for row in manifests
                if row.state == EVIDENCE_STATE_EXPIRED
            )
            ready = sum(
                1
                for row in manifests
                if row.state in {EVIDENCE_STATE_READY, EVIDENCE_STATE_SEALED}
            )
            pressure = (
                reserved_bytes / evidence_budget_bytes
                if evidence_budget_bytes and evidence_budget_bytes > 0
                else None
            )
            if evidence_budget_bytes is None:
                evidence_status = "UNAVAILABLE"
            elif pressure is not None and pressure >= 0.80:
                evidence_status = "PRESSURE"
            elif pinned:
                evidence_status = "PINNED"
            elif manifests:
                evidence_status = "AVAILABLE"
            else:
                evidence_status = "EMPTY"

            evidence = ErpSyncEvidenceOperations(
                status=evidence_status,
                manifests_total=len(manifests),
                source_manifests=sum(
                    1 for row in manifests if row.proof_type == EVIDENCE_PROOF_SOURCE
                ),
                checkpoint_manifests=sum(
                    1
                    for row in manifests
                    if row.proof_type == EVIDENCE_PROOF_CHECKPOINT
                ),
                manifests_ready=ready,
                manifests_expired=expired,
                manifests_pinned=pinned,
                active_reservations=len(active_reservations),
                reserved_bytes=reserved_bytes,
                used_bytes=used_bytes,
                budget_bytes=evidence_budget_bytes,
                pressure_ratio=pressure,
            )

            return ErpSyncOperationsData(
                now_utc=now,
                control_generation=int(control.generation),
                owner_attempt_id=control.owner_attempt_id,
                lease_expires_at=control.lease_expires_at,
                owner_deadline_at=control.owner_deadline_at,
                control_heartbeat_at=control.heartbeat_at,
                control_updated_at=control.updated_at,
                last_evaluated_business_date=control.last_evaluated_business_date,
                control_protocol_version=int(control.protocol_version),
                control_config_fingerprint=control.config_fingerprint,
                current_daily_occurrence_status=daily.status if daily is not None else None,
                attempts=attempts,
                last_complete_success=(
                    self._attempt(
                        last_success,
                        session.get(
                            AcumaticaProjectTaskSyncRun,
                            last_success.task_sync_run_id,
                        )
                        if last_success is not None and last_success.task_sync_run_id
                        else None,
                    )
                    if last_success is not None
                    else None
                ),
                projects=projects,
                recovery=recovery,
                evidence=evidence,
            )
