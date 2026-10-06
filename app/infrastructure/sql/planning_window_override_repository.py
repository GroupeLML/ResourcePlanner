from __future__ import annotations

from datetime import date
import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain.planning_window import (
    PlanningWindow,
    planning_window_covers,
    resolve_effective_planning_window,
)
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalRevision,
)
from .base import utc_now
from .models import ORIGIN_REQUEST, ResourceRequirement
from .planning_window_override_models import (
    PLANNING_WINDOW_OVERRIDE_ABSORBED,
    PLANNING_WINDOW_OVERRIDE_ACTIVE,
    PLANNING_WINDOW_OVERRIDE_SUPERSEDED,
    PlanningWindowOverride,
)


def _text(value: object) -> str:
    return str(value or "").strip()


class SqlPlanningWindowOverrideRepository:
    """Resolve and retire persistent operational windows without altering approval."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def active_by_requirement_ids(
        self,
        requirement_ids: Iterable[str],
        *,
        approval_revision_id: str,
    ) -> dict[str, PlanningWindowOverride]:
        identifiers = tuple(
            dict.fromkeys(_text(value) for value in requirement_ids if _text(value))
        )
        if not identifiers:
            return {}
        rows = self._session.scalars(
            select(PlanningWindowOverride)
            .where(
                PlanningWindowOverride.resource_requirement_id.in_(identifiers),
                PlanningWindowOverride.approval_revision_id == approval_revision_id,
                PlanningWindowOverride.status == PLANNING_WINDOW_OVERRIDE_ACTIVE,
            )
            .order_by(
                PlanningWindowOverride.resource_requirement_id,
                PlanningWindowOverride.created_at,
                PlanningWindowOverride.id,
            )
        ).all()
        result: dict[str, PlanningWindowOverride] = {}
        for row in rows:
            if row.resource_requirement_id in result:
                raise ValueError(
                    "Plusieurs dérogations de fenêtre actives ciblent le même segment."
                )
            result[row.resource_requirement_id] = row
        return result

    @staticmethod
    def effective_window_for_requirement(
        requirement: ResourceRequirement,
        override: PlanningWindowOverride | None,
        *,
        approval_revision_id: str,
        approved_entry_key: str,
        approved_start_date: date,
        approved_end_date: date,
    ) -> PlanningWindow:
        if override is None:
            return resolve_effective_planning_window(
                approved_start=approved_start_date,
                approved_end=approved_end_date,
            )

        if (
            requirement.origin != ORIGIN_REQUEST
            or not requirement.workforce_request_id
            or requirement.approval_reference_status != APPROVAL_REFERENCE_CAPTURED
            or requirement.approval_revision_id != approval_revision_id
            or _text(requirement.approved_entry_key) != approved_entry_key
            or override.workforce_request_id != requirement.workforce_request_id
            or override.resource_requirement_id != requirement.id
            or override.approval_revision_id != approval_revision_id
            or override.approved_entry_key != approved_entry_key
        ):
            raise ValueError(
                "La dérogation de fenêtre ne référence pas le segment REQUEST approuvé actif."
            )
        if (
            override.approved_start_date != approved_start_date
            or override.approved_end_date != approved_end_date
        ):
            raise ValueError(
                "Les bornes approuvées de la dérogation ne correspondent plus à l'entrée approuvée."
            )

        return resolve_effective_planning_window(
            approved_start=approved_start_date,
            approved_end=approved_end_date,
            override_start=override.effective_start_date,
            override_end=override.effective_end_date,
        )

    def apply_widening(
        self,
        requirement: ResourceRequirement,
        *,
        approval_revision_id: str,
        approved_entry_key: str,
        approved_start_date: date,
        approved_end_date: date,
        effective_start_date: date,
        effective_end_date: date,
        actor_user_id: str,
        reason: str,
        correlation_id: str,
    ) -> PlanningWindowOverride:
        """Create or widen the active operational exception for one REQUEST segment."""

        if (
            requirement.origin != ORIGIN_REQUEST
            or not requirement.workforce_request_id
            or requirement.approval_reference_status != APPROVAL_REFERENCE_CAPTURED
            or requirement.approval_revision_id != approval_revision_id
            or _text(requirement.approved_entry_key) != _text(approved_entry_key)
        ):
            raise ValueError(
                "La dérogation de fenêtre exige un segment REQUEST relié "
                "à l'entrée approuvée active."
            )

        actor_id = _text(actor_user_id)
        reason_value = _text(reason)
        correlation = _text(correlation_id)
        if not actor_id or not reason_value or not correlation:
            raise ValueError(
                "Auteur, motif et corrélation sont requis pour une dérogation Planning."
            )

        requested = resolve_effective_planning_window(
            approved_start=approved_start_date,
            approved_end=approved_end_date,
            override_start=effective_start_date,
            override_end=effective_end_date,
        )
        active = self.active_by_requirement_ids(
            (requirement.id,),
            approval_revision_id=approval_revision_id,
        ).get(requirement.id)

        if active is not None:
            current = self.effective_window_for_requirement(
                requirement,
                active,
                approval_revision_id=approval_revision_id,
                approved_entry_key=approved_entry_key,
                approved_start_date=approved_start_date,
                approved_end_date=approved_end_date,
            )
            if (
                requested.start_date > current.start_date
                or requested.end_date < current.end_date
            ):
                raise ValueError(
                    "Une dérogation active ne peut pas être réduite par une nouvelle commande."
                )
            if requested == current:
                raise ValueError(
                    "La nouvelle dérogation doit élargir la fenêtre opérationnelle active."
                )
            active.status = PLANNING_WINDOW_OVERRIDE_SUPERSEDED
            active.resolution_reason = "WIDENED_BY_OVERRIDE_COMMAND"
            active.resolved_by_revision_id = None
            active.resolved_at = utc_now()
            self._session.flush()

        row = PlanningWindowOverride(
            workforce_request_id=requirement.workforce_request_id,
            resource_requirement_id=requirement.id,
            approval_revision_id=approval_revision_id,
            approved_entry_key=_text(approved_entry_key),
            approved_start_date=approved_start_date,
            approved_end_date=approved_end_date,
            effective_start_date=requested.start_date,
            effective_end_date=requested.end_date,
            actor_user_id=actor_id,
            reason=reason_value,
            correlation_id=correlation,
            status=PLANNING_WINDOW_OVERRIDE_ACTIVE,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def supersede_for_request(
        self,
        request_id: str,
        *,
        resolution_reason: str,
    ) -> int:
        rows = list(
            self._session.scalars(
                select(PlanningWindowOverride)
                .where(
                    PlanningWindowOverride.workforce_request_id == _text(request_id),
                    PlanningWindowOverride.status == PLANNING_WINDOW_OVERRIDE_ACTIVE,
                )
                .order_by(PlanningWindowOverride.created_at, PlanningWindowOverride.id)
            ).all()
        )
        if not rows:
            return 0
        resolved_at = utc_now()
        reason = _text(resolution_reason) or "REQUEST_CANCELLED"
        for row in rows:
            row.status = PLANNING_WINDOW_OVERRIDE_SUPERSEDED
            row.resolution_reason = reason
            row.resolved_by_revision_id = None
            row.resolved_at = resolved_at
        self._session.flush()
        return len(rows)

    def supersede_for_requirements(
        self,
        requirement_ids: Iterable[str],
        *,
        approval_revision_id: str,
        resolution_reason: str,
        resolved_by_revision_id: str | None = None,
    ) -> int:
        rows = self.active_by_requirement_ids(
            requirement_ids,
            approval_revision_id=approval_revision_id,
        )
        resolved_at = utc_now()
        for row in rows.values():
            row.status = PLANNING_WINDOW_OVERRIDE_SUPERSEDED
            row.resolution_reason = _text(resolution_reason) or "OPERATIONAL_ENTRY_CHANGED"
            row.resolved_by_revision_id = resolved_by_revision_id
            row.resolved_at = resolved_at
        if rows:
            self._session.flush()
        return len(rows)

    @staticmethod
    def _revision_entries(
        revision: RequestApprovalRevision,
    ) -> dict[str, dict[str, object]]:
        payload = json.loads(revision.payload_text)
        authorization = payload.get("authorization", {})
        entries = authorization.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("Le snapshot approuvé ne contient pas une liste d'entrées valide.")
        result: dict[str, dict[str, object]] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("Le snapshot approuvé contient une entrée invalide.")
            identity = _text(raw.get("identity"))
            if identity:
                result[identity] = dict(raw)
        return result

    def reconcile_for_new_revision(
        self,
        request_id: str,
        *,
        previous_revision_id: str,
        new_revision: RequestApprovalRevision,
    ) -> int:
        rows = list(
            self._session.scalars(
                select(PlanningWindowOverride)
                .where(
                    PlanningWindowOverride.workforce_request_id == request_id,
                    PlanningWindowOverride.approval_revision_id == previous_revision_id,
                    PlanningWindowOverride.status == PLANNING_WINDOW_OVERRIDE_ACTIVE,
                )
                .order_by(PlanningWindowOverride.created_at, PlanningWindowOverride.id)
            ).all()
        )
        if not rows:
            return 0

        entries = self._revision_entries(new_revision)
        resolved_at = utc_now()
        for row in rows:
            entry = entries.get(row.approved_entry_key)
            if entry is None:
                row.status = PLANNING_WINDOW_OVERRIDE_SUPERSEDED
                row.resolution_reason = "ENTRY_NOT_PRESENT"
            else:
                start_value = _text(entry.get("start_date"))
                end_value = _text(entry.get("end_date"))
                if not start_value or not end_value:
                    raise ValueError(
                        "La nouvelle révision contient une entrée sans fenêtre approuvée valide."
                    )
                new_start = date.fromisoformat(start_value)
                new_end = date.fromisoformat(end_value)
                if planning_window_covers(
                    outer_start=new_start,
                    outer_end=new_end,
                    inner_start=row.effective_start_date,
                    inner_end=row.effective_end_date,
                ):
                    row.status = PLANNING_WINDOW_OVERRIDE_ABSORBED
                    row.resolution_reason = "REAPPROVAL_ABSORBED"
                else:
                    row.status = PLANNING_WINDOW_OVERRIDE_SUPERSEDED
                    row.resolution_reason = "REAPPROVAL_NOT_COVERED"
            row.resolved_by_revision_id = new_revision.id
            row.resolved_at = resolved_at

        self._session.flush()
        return len(rows)
