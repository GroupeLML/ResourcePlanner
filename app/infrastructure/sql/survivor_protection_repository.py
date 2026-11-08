from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from .base import utc_now
from .models import Resource, ResourceRequirement, Shift
from .operational_choice_models import (
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
    RequestShiftSurvivorProtection,
)


class DurableSurvivorRow(NamedTuple):
    protection: RequestShiftSurvivorProtection
    shift: Shift
    requirement: ResourceRequirement
    resource: Resource


class SqlRequestShiftSurvivorProtectionRepository:
    """Persistence authority for #793 durable survivor placement protection."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _assert_same_slot(
        intent: RequestRequirementPlanningIntent,
        shift: Shift,
    ) -> None:
        requirement_id = str(intent.current_resource_requirement_id or "")
        if not requirement_id or shift.resource_requirement_id != requirement_id:
            raise RuntimeError(
                "request_survivor_protection_slot_mismatch: le Shift protégé "
                "n'appartient pas au ResourceRequirement courant du PlanningIntent."
            )

    def protect_survivors(
        self,
        *,
        intent: RequestRequirementPlanningIntent,
        reduction: RequestCompletionReduction,
        survivors: Iterable[Shift],
    ) -> tuple[RequestShiftSurvivorProtection, ...]:
        if reduction.planning_intent_id != intent.id:
            raise RuntimeError(
                "request_survivor_protection_reduction_slot_mismatch: "
                "la réduction et le PlanningIntent diffèrent."
            )

        protected: list[RequestShiftSurvivorProtection] = []
        for shift in survivors:
            self._assert_same_slot(intent, shift)
            existing = self.session.scalar(
                select(RequestShiftSurvivorProtection)
                .where(
                    RequestShiftSurvivorProtection.shift_id == shift.id,
                    RequestShiftSurvivorProtection.active == true(),
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.planning_intent_id != intent.id:
                    raise RuntimeError(
                        "request_survivor_protection_cross_slot: un Shift ne peut "
                        "être protégé activement par deux slots différents."
                    )
                # Preserve truthful provenance from the first reduction that protected
                # this still-surviving physical Shift.
                protected.append(existing)
                continue

            row = RequestShiftSurvivorProtection(
                planning_intent_id=intent.id,
                shift_id=shift.id,
                original_shift_id=shift.id,
                originating_reduction_id=reduction.id,
                active=True,
            )
            self.session.add(row)
            protected.append(row)

        self.session.flush()
        return tuple(protected)

    def retire_shift_protection(
        self,
        *,
        intent: RequestRequirementPlanningIntent,
        shift: Shift,
        reason: str,
        release_live_reference: bool,
    ) -> RequestShiftSurvivorProtection | None:
        row = self.session.scalar(
            select(RequestShiftSurvivorProtection)
            .where(
                RequestShiftSurvivorProtection.shift_id == shift.id,
                RequestShiftSurvivorProtection.active == true(),
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.planning_intent_id != intent.id:
            raise RuntimeError(
                "request_survivor_protection_cross_slot: impossible de retirer "
                "un lien actif appartenant à un autre slot."
            )
        self._assert_same_slot(intent, shift)
        row.active = False
        row.retired_at = utc_now()
        row.retired_reason = str(reason or "SHIFT_CHANGED")[:64]
        if release_live_reference:
            # Preserve immutable historical identity while releasing the live FK
            # before canonical Shift deletion.
            row.shift_id = None
        self.session.flush()
        return row

    def retire_deleted_shift(
        self,
        *,
        intent: RequestRequirementPlanningIntent,
        shift: Shift,
        reason: str,
    ) -> RequestShiftSurvivorProtection | None:
        return self.retire_shift_protection(
            intent=intent,
            shift=shift,
            reason=reason,
            release_live_reference=True,
        )

    def active_rows(self) -> tuple[DurableSurvivorRow, ...]:
        rows = self.session.execute(
            select(
                RequestShiftSurvivorProtection,
                RequestRequirementPlanningIntent,
                Shift,
                ResourceRequirement,
                Resource,
            )
            .join(
                RequestRequirementPlanningIntent,
                RequestShiftSurvivorProtection.planning_intent_id
                == RequestRequirementPlanningIntent.id,
            )
            .join(
                Shift,
                RequestShiftSurvivorProtection.shift_id == Shift.id,
            )
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .join(Resource, Shift.resource_id == Resource.id)
            .where(RequestShiftSurvivorProtection.active == true())
            .order_by(
                RequestShiftSurvivorProtection.planning_intent_id,
                RequestShiftSurvivorProtection.shift_id,
            )
        ).all()

        result: list[DurableSurvivorRow] = []
        for protection, intent, shift, requirement, resource in rows:
            if protection.shift_id is None:
                raise RuntimeError(
                    "request_survivor_protection_orphan: un lien actif doit "
                    "pointer vers un Shift réel."
                )
            self._assert_same_slot(intent, shift)
            if requirement.id != intent.current_resource_requirement_id:
                raise RuntimeError(
                    "request_survivor_protection_requirement_mismatch: "
                    "le requirement du Shift n'est plus celui du slot."
                )
            result.append(
                DurableSurvivorRow(
                    protection=protection,
                    shift=shift,
                    requirement=requirement,
                    resource=resource,
                )
            )
        return tuple(result)

    def active_for_intent(
        self,
        planning_intent_id: str,
    ) -> tuple[RequestShiftSurvivorProtection, ...]:
        return tuple(
            self.session.scalars(
                select(RequestShiftSurvivorProtection)
                .where(
                    RequestShiftSurvivorProtection.planning_intent_id
                    == planning_intent_id,
                    RequestShiftSurvivorProtection.active == true(),
                )
                .order_by(RequestShiftSurvivorProtection.original_shift_id)
            ).all()
        )
