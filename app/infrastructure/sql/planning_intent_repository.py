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
    # ADR-034: approved_entry_key already embeds the stable logical line/period
    # identity. Physical RequestLine/Period row ids are provenance only and must
    # never fork the durable slot when a compatible reapproval rematerializes them.
    return json.dumps(
        [
            str(workforce_request_id),
            str(approved_entry_key),
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
        logical_rows = list(
            self.session.scalars(
                select(RequestRequirementPlanningIntent).where(
                    RequestRequirementPlanningIntent.workforce_request_id
                    == workforce_request_id,
                    RequestRequirementPlanningIntent.approved_entry_key
                    == approved_entry_key,
                    RequestRequirementPlanningIntent.slot_index == int(slot_index),
                )
            ).all()
        )
        if row is None and logical_rows:
            if len(logical_rows) != 1:
                raise ValueError(
                    "planning_slot_identity_ambiguous: plusieurs intentions durables "
                    "existent pour le même slot logique."
                )
            row = logical_rows[0]
            row.slot_identity_key = key
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

        # Reapproval/rematerialization updates physical provenance/current binding,
        # but never creates a second logical slot or resets operational intent.
        if current_resource_requirement_id:
            other = self.session.scalar(
                select(RequestRequirementPlanningIntent).where(
                    RequestRequirementPlanningIntent.current_resource_requirement_id
                    == current_resource_requirement_id,
                    RequestRequirementPlanningIntent.id != row.id,
                )
            )
            if other is not None:
                raise ValueError(
                    "planning_slot_binding_ambiguous: un ResourceRequirement ne peut "
                    "être lié qu'à une seule intention durable."
                )
        row.source_request_line_id = source_request_line_id
        row.source_period_id = source_period_id
        row.approval_revision_id = approval_revision_id
        row.current_resource_requirement_id = current_resource_requirement_id
        self.session.flush()
        return row

    def current_effective_need_for_requirement(
        self,
        requirement_id: str,
        *,
        fallback_hours: object,
    ) -> Decimal:
        intent = self.session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == str(requirement_id)
            )
        )
        if intent is None:
            return Decimal(str(fallback_hours)).quantize(Decimal("0.01"))
        active_after = self.session.scalar(
            select(func.min(RequestCompletionReduction.effective_need_after)).where(
                RequestCompletionReduction.planning_intent_id == intent.id,
                RequestCompletionReduction.reconciliation_state == "ACTIVE",
            )
        )
        if active_after is None:
            return Decimal(str(fallback_hours)).quantize(Decimal("0.01"))
        return Decimal(str(active_after)).quantize(Decimal("0.01"))

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
