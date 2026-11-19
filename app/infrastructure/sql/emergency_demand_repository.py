from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ...application.demand_completion import DemandCompletionFacts
from ...application.read_models import DemandLineReadModel, DemandReadModel
from .demand_repository import SqlDemandRepository, _optional_text
from .models import (
    Project,
    Resource,
    WorkforceRequest,
    WorkforceRequestHistory,
    WorkPackage,
)


class SqlEmergencyDemandRepository(SqlDemandRepository):
    def _read_model(
        self,
        request: WorkforceRequest,
        project: Project,
        work_package: WorkPackage | None,
        proposed_resource: Resource | None,
        competency_ids: tuple[str, ...] = (),
        lines: tuple[DemandLineReadModel, ...] = (),
        completion_facts: DemandCompletionFacts | None = None,
        planning_progress: str | None = None,
    ) -> DemandReadModel:
        base = super()._read_model(
            request,
            project,
            work_package,
            proposed_resource,
            competency_ids,
            lines,
            completion_facts,
            planning_progress,
        )
        return replace(
            base,
            emergency_override_active=bool(request.emergency_override_active),
            emergency_override_reason=_optional_text(request.emergency_override_reason),
            emergency_override_by=_optional_text(request.emergency_override_by_name),
            emergency_override_at=request.emergency_override_at,
        )

    def activate_emergency_override(
        self,
        number: str,
        *,
        reason: str,
        actor_name: str,
        occurred_at: datetime,
        previous_status: str,
    ) -> None:
        request = self._request(number)
        if request.emergency_override_active:
            raise ValueError("Une dérogation urgente est déjà active pour cette demande.")
        request.emergency_override_active = True
        request.emergency_override_reason = str(reason or "").strip() or None
        request.emergency_override_by_name = str(actor_name or "").strip() or None
        request.emergency_override_at = occurred_at
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Dérogation d'approbation urgente",
                previous_status=str(previous_status or "").strip() or None,
                status=request.status,
                comment=request.emergency_override_reason,
                details=(
                    "Planification matérialisée en urgence sans modifier le statut "
                    "d'approbation régulier. Régularisation formelle requise."
                ),
                actor_user_id=self._actor_user_id,
                actor_name=request.emergency_override_by_name,
                occurred_at=occurred_at,
            )
        )
        self._session.flush()

    def clear_emergency_override(self, number: str) -> None:
        request = self._request(number)
        if not request.emergency_override_active:
            return
        request.emergency_override_active = False
        self._session.flush()
