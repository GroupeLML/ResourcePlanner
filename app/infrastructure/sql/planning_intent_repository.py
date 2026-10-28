from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...domain.effective_need import resolve_effective_slot_need
from .operational_choice_models import (
    PLANNING_INTENT_AUTO,
    PLANNING_TARGET_INHERIT,
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
)


def stable_planning_slot_key(
    *,
    workforce_request_id: str,
    approved_entry_key: str,
    slot_index: int,
    source_request_line_id: str | None,
    source_period_id: str | None,
) -> str:
    """Stable, display-independent ADR-034 slot identity."""
    return json.dumps(
        [
            str(workforce_request_id),
            str(source_request_line_id or ""),
            str(approved_entry_key),
            str(source_period_id or ""),
            int(slot_index),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SqlRequestRequirementPlanningIntentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_key(self, slot_identity_key: str) -> RequestRequirementPlanningIntent | None:
        return self.session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.slot_identity_key == slot_identity_key
            )
        )

    def ensure(
        self,
        *,
        workforce_request_id: str,
        approved_entry_key: str,
        slot_index: int,
        source_request_line_id: str | None,
        source_period_id: str | None,
        approval_revision_id: str | None,
        current_resource_requirement_id: str | None,
    ) -> RequestRequirementPlanningIntent:
        key = stable_planning_slot_key(
            workforce_request_id=workforce_request_id,
            approved_entry_key=approved_entry_key,
            slot_index=slot_index,
            source_request_line_id=source_request_line_id,
            source_period_id=source_period_id,
        )
        row = self.by_key(key)
        if row is None:
            row = RequestRequirementPlanningIntent(
                slot_identity_key=key,
                workforce_request_id=workforce_request_id,
                source_request_line_id=source_request_line_id,
                approval_revision_id=approval_revision_id,
                approved_entry_key=approved_entry_key,
                source_period_id=source_period_id,
                slot_index=slot_index,
                current_resource_requirement_id=current_resource_requirement_id,
                intent_mode=PLANNING_INTENT_AUTO,
                target_mode=PLANNING_TARGET_INHERIT,
                target_resource_id=None,
            )
            self.session.add(row)
            self.session.flush()
            return row

        # Reapproval/rematerialization updates provenance/current binding, but not
        # the stable identity or previously chosen operational intent.
        row.approval_revision_id = approval_revision_id
        row.current_resource_requirement_id = current_resource_requirement_id
        return row

    def active_reduction_total(self, planning_intent_id: str) -> Decimal:
        total = self.session.scalar(
            select(func.coalesce(func.sum(RequestCompletionReduction.reduction_hours), 0)).where(
                RequestCompletionReduction.planning_intent_id == planning_intent_id,
                RequestCompletionReduction.reconciliation_state == "ACTIVE",
            )
        )
        return Decimal(str(total or 0)).quantize(Decimal("0.01"))

    def effective_need(
        self,
        *,
        planning_intent_id: str,
        nominal_slot_hours: object,
    ) -> Decimal:
        return resolve_effective_slot_need(
            nominal_slot_hours,
            (self.active_reduction_total(planning_intent_id),),
        )
