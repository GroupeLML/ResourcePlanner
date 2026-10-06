"""Application contracts for Verification from ADR-023 / issue #363A.

These policies are conceptual only. Later #363 slices bind them to persisted RBAC,
Delivery reads, SQL transactions, assignments, executions, and HTTP routes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.domain.delivery import DeliveryItemType
from app.domain.verification import VerificationScope


class VerificationAction(StrEnum):
    VIEW_SCOPE = "VIEW_SCOPE"
    PILOT_SCOPE = "PILOT_SCOPE"
    RECORD_STORY_DECISION = "RECORD_STORY_DECISION"
    DEFINE_STORY_REQUIREMENTS = "DEFINE_STORY_REQUIREMENTS"
    REVISE_REQUIREMENTS = "REVISE_REQUIREMENTS"
    ASSIGN_EXECUTORS = "ASSIGN_EXECUTORS"
    WITHDRAW_REQUIREMENTS = "WITHDRAW_REQUIREMENTS"
    REQUEST_RETEST = "REQUEST_RETEST"
    RECORD_RESULT = "RECORD_RESULT"
    ADD_EVIDENCE = "ADD_EVIDENCE"


@dataclass(frozen=True, slots=True)
class VerificationStorySourceReadModel:
    story_id: str
    delivery_plan_id: str
    work_package_id: str
    item_type: DeliveryItemType
    delivery_status: str

    def __post_init__(self) -> None:
        for field_name in ("story_id", "delivery_plan_id", "work_package_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} requires a stable identifier")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "item_type", DeliveryItemType(self.item_type))
        status = str(self.delivery_status or "").strip()
        if not status:
            raise ValueError("delivery_status is required")
        object.__setattr__(self, "delivery_status", status)


class VerificationDeliveryReadPort(Protocol):
    def get_story_source(
        self,
        story_id: str,
    ) -> VerificationStorySourceReadModel | None:
        ...


@dataclass(frozen=True, slots=True)
class VerificationActorContext:
    user_id: str
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        user_id = str(self.user_id or "").strip()
        if not user_id:
            raise ValueError("Verification actor requires a stable AppUser id")
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(
            self,
            "roles",
            tuple(
                dict.fromkeys(
                    str(role).strip().upper()
                    for role in self.roles
                    if str(role).strip()
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationAuthorityContext:
    story_closure_authorized: bool = False
    project_scope_authorized: bool = False
    assigned_executor: bool = False
    execution_authorized: bool = False


def validate_story_source_for_scope(
    scope: VerificationScope,
    source: VerificationStorySourceReadModel,
) -> None:
    if source.item_type is not DeliveryItemType.STORY:
        raise ValueError("Verification source must be a Delivery STORY")
    if source.work_package_id != scope.work_package_id:
        raise ValueError("Verification Story must belong to the same WorkPackage")


def verification_actions_for(
    actor: VerificationActorContext,
    scope: VerificationScope,
    authority: VerificationAuthorityContext = VerificationAuthorityContext(),
) -> frozenset[VerificationAction]:
    actions: set[VerificationAction] = set()
    roles = set(actor.roles)

    if "ADMIN" in roles:
        return frozenset(VerificationAction)

    if authority.project_scope_authorized:
        actions.update(
            {
                VerificationAction.VIEW_SCOPE,
                VerificationAction.PILOT_SCOPE,
            }
        )

    if scope.lead_user_id == actor.user_id:
        actions.update(
            {
                VerificationAction.VIEW_SCOPE,
                VerificationAction.DEFINE_STORY_REQUIREMENTS,
                VerificationAction.REVISE_REQUIREMENTS,
                VerificationAction.ASSIGN_EXECUTORS,
                VerificationAction.WITHDRAW_REQUIREMENTS,
                VerificationAction.REQUEST_RETEST,
            }
        )

    if authority.story_closure_authorized:
        actions.update(
            {
                VerificationAction.VIEW_SCOPE,
                VerificationAction.RECORD_STORY_DECISION,
                VerificationAction.DEFINE_STORY_REQUIREMENTS,
            }
        )

    if authority.assigned_executor and authority.execution_authorized:
        actions.update(
            {
                VerificationAction.VIEW_SCOPE,
                VerificationAction.RECORD_RESULT,
                VerificationAction.ADD_EVIDENCE,
            }
        )

    return frozenset(actions)
