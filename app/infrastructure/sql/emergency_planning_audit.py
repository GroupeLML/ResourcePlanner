from __future__ import annotations

from collections.abc import Mapping, Sequence
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.command_ports import ApprovedDemandSyncPort
from ...application.repository_ports import PlanningMutationVersionPort
from .models import WorkforceRequest
from .planning_audit import ENTITY_SEGMENT, SqlPlanningAuditJournal
from .planning_version import SqlPlanningMutationVersionRepository


def _text(value: object) -> str:
    return str(value or "").strip()


class EmergencyAwareApprovedDemandSyncAdapter(ApprovedDemandSyncPort):
    """Audit materialization without pretending an emergency override was approval."""

    def __init__(
        self,
        delegate: ApprovedDemandSyncPort,
        journal: SqlPlanningAuditJournal,
        session: Session,
        *,
        versioning: PlanningMutationVersionPort | None = None,
    ) -> None:
        self._delegate = delegate
        self._journal = journal
        self._session = session
        self._versioning = versioning or SqlPlanningMutationVersionRepository(session)

    def _is_emergency_materialization(self, demand_number: str) -> bool:
        wanted = _text(demand_number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.id == wanted)
                | (WorkforceRequest.legacy_demand_number == wanted)
            )
        )
        return bool(
            request is not None
            and request.emergency_override_active
            and request.status == "Soumise"
        )

    def cancel_materialized(self, demand_number: str) -> None:
        self._versioning.acquire()
        action = getattr(self._delegate, "cancel_materialized", None)
        if not callable(action):
            raise RuntimeError(
                "Le runtime ne supporte pas l'annulation des besoins matérialisés."
            )
        before = self._journal.request_requirements(demand_number)
        action(demand_number)
        after = self._journal.request_requirements(demand_number)
        for entity_id in sorted(set(before) | set(after)):
            previous = before.get(entity_id)
            current = after.get(entity_id)
            reference = (current or previous)[0]  # type: ignore[index]
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=reference,
                action="Annulation segment depuis demande annulée",
                before=previous[1] if previous is not None else None,
                after=current[1] if current is not None else None,
            )

    def sync_operational_choices(
        self,
        demand_number: str,
        *,
        acknowledged_overallocation_segment_ids: Sequence[str] = (),
        pre_mutation_effective_hours_by_segment: Mapping[str, float] | None = None,
    ) -> None:
        self._versioning.acquire()
        action = getattr(self._delegate, "sync_operational_choices", None)
        if not callable(action):
            raise RuntimeError(
                "Le runtime ne supporte pas la synchronisation des choix opérationnels."
            )
        before = self._journal.request_requirements(demand_number)
        sync_kwargs: dict[str, object] = {}
        if acknowledged_overallocation_segment_ids:
            sync_kwargs["acknowledged_overallocation_segment_ids"] = (
                acknowledged_overallocation_segment_ids
            )
        if pre_mutation_effective_hours_by_segment:
            sync_kwargs["pre_mutation_effective_hours_by_segment"] = dict(
                pre_mutation_effective_hours_by_segment
            )
        action(demand_number, **sync_kwargs)
        after = self._journal.request_requirements(demand_number)
        for entity_id in sorted(set(before) | set(after)):
            previous = before.get(entity_id)
            current = after.get(entity_id)
            reference = (current or previous)[0]  # type: ignore[index]
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=reference,
                action="Synchronisation segment depuis choix opérationnels",
                before=previous[1] if previous is not None else None,
                after=current[1] if current is not None else None,
            )

    def sync_approved(
        self,
        demand_number: str,
        *,
        approved_request_version: int | None = None,
    ) -> None:
        self._versioning.acquire()
        emergency = self._is_emergency_materialization(demand_number)
        before = self._journal.request_requirements(demand_number)
        self._delegate.sync_approved(
            demand_number,
            approved_request_version=approved_request_version,
        )
        after = self._journal.request_requirements(demand_number)

        for entity_id in sorted(set(before) | set(after)):
            previous = before.get(entity_id)
            current = after.get(entity_id)
            reference = (current or previous)[0]  # type: ignore[index]
            before_values = previous[1] if previous is not None else None
            after_values = current[1] if current is not None else None
            if emergency:
                action = (
                    "Création segment depuis dérogation urgente"
                    if previous is None
                    else "Synchronisation segment depuis dérogation urgente"
                )
            else:
                action = (
                    "Création segment depuis demande approuvée"
                    if previous is None
                    else "Synchronisation segment depuis demande approuvée"
                )
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=reference,
                action=action,
                before=before_values,
                after=after_values,
            )
