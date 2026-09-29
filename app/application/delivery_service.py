from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence
from uuid import uuid4

from app.domain.delivery import (
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryItemType,
    DeliveryPlan,
    DeliveryPlanStatus,
    reestimate_story,
    transition_delivery_plan,
    transition_story_status,
    update_story_remaining_hours,
    validate_delivery_hierarchy,
)

from .delivery_contracts import DeliveryAction
from .delivery_security import authorized_delivery_actions_for
from .errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from .security import AuthPrincipal


class DeliveryRepositoryPort(Protocol):
    def get_plan(self, plan_id: str) -> DeliveryPlan | None: ...
    def get_non_archived_plan_for_work_package(
        self, work_package_id: str
    ) -> DeliveryPlan | None: ...
    def get_item(self, item_id: str) -> DeliveryItem | None: ...
    def list_items(self, plan_id: str) -> Sequence[DeliveryItem]: ...
    def work_package_exists(self, work_package_id: str) -> bool: ...
    def user_exists(self, user_id: str) -> bool: ...
    def add_plan(self, plan: DeliveryPlan) -> None: ...
    def add_item(self, item: DeliveryItem) -> None: ...
    def save_plan(self, plan: DeliveryPlan) -> None: ...
    def save_item(self, item: DeliveryItem) -> None: ...
    def compare_and_increment_version(
        self, plan_id: str, *, expected_delivery_version: int
    ) -> int: ...
    def append_history(
        self,
        *,
        plan_id: str,
        action: str,
        delivery_version: int,
        item_id: str | None = None,
        actor_user_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: object | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


class DeliveryService:
    def __init__(self, repository: DeliveryRepositoryPort) -> None:
        self._repository = repository

    def _plan(self, plan_id: str) -> DeliveryPlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise ApplicationNotFoundError(
                "DeliveryPlan introuvable.",
                code="delivery_plan_not_found",
                context={"delivery_plan_id": plan_id},
            )
        return plan

    def _item(self, item_id: str) -> DeliveryItem:
        item = self._repository.get_item(item_id)
        if item is None:
            raise ApplicationNotFoundError(
                "DeliveryItem introuvable.",
                code="delivery_item_not_found",
                context={"delivery_item_id": item_id},
            )
        return item

    @staticmethod
    def _require_identity(principal: AuthPrincipal) -> str:
        user_id = str(principal.local_user_id or "").strip()
        if not user_id:
            raise ApplicationAuthorizationError(
                "Une identité AppUser stable est requise pour modifier Delivery.",
                code="delivery_identity_required",
            )
        return user_id

    @staticmethod
    def _require_action(
        principal: AuthPrincipal,
        plan: DeliveryPlan,
        action: DeliveryAction,
        item: DeliveryItem | None = None,
    ) -> None:
        actions = authorized_delivery_actions_for(principal, plan, item)
        if action not in actions:
            raise ApplicationAuthorizationError(
                "Action Delivery non autorisée.",
                code="delivery_action_denied",
                context={
                    "required_action": action.value,
                    "delivery_plan_id": plan.id,
                    "delivery_item_id": item.id if item is not None else None,
                },
            )

    @staticmethod
    def _require_any_action(
        principal: AuthPrincipal,
        plan: DeliveryPlan,
        actions: tuple[DeliveryAction, ...],
        item: DeliveryItem | None = None,
    ) -> None:
        allowed = authorized_delivery_actions_for(principal, plan, item)
        if not allowed.intersection(actions):
            raise ApplicationAuthorizationError(
                "Action Delivery non autorisée.",
                code="delivery_action_denied",
                context={
                    "required_any_action": [action.value for action in actions],
                    "delivery_plan_id": plan.id,
                    "delivery_item_id": item.id if item is not None else None,
                },
            )

    @staticmethod
    def _ensure_editable(plan: DeliveryPlan) -> None:
        if plan.status is DeliveryPlanStatus.ARCHIVED:
            raise ApplicationConflictError(
                "Un DeliveryPlan archivé est en lecture seule.",
                code="delivery_plan_archived",
                context={"delivery_plan_id": plan.id},
            )

    @staticmethod
    def _check_expected(plan: DeliveryPlan, expected_delivery_version: int) -> None:
        if expected_delivery_version < 1:
            raise ApplicationValidationError(
                "expected_delivery_version doit être positif.",
                code="delivery_version_invalid",
            )
        if plan.delivery_version != expected_delivery_version:
            raise ApplicationConflictError(
                "Le board Delivery a été modifié par une autre opération.",
                code="delivery_version_conflict",
                context={
                    "delivery_plan_id": plan.id,
                    "expected_delivery_version": expected_delivery_version,
                    "current_delivery_version": plan.delivery_version,
                },
            )

    def _bump(self, plan: DeliveryPlan, expected_delivery_version: int) -> int:
        self._check_expected(plan, expected_delivery_version)
        try:
            return self._repository.compare_and_increment_version(
                plan.id,
                expected_delivery_version=expected_delivery_version,
            )
        except RuntimeError as exc:
            raise ApplicationConflictError(
                "Le board Delivery a été modifié par une autre opération.",
                code="delivery_version_conflict",
                context={
                    "delivery_plan_id": plan.id,
                    "expected_delivery_version": expected_delivery_version,
                },
            ) from exc

    def _validate_user(self, user_id: str | None, *, field: str) -> str | None:
        normalized = _text(user_id)
        if normalized is not None and not self._repository.user_exists(normalized):
            raise ApplicationValidationError(
                "AppUser Delivery introuvable.",
                code="delivery_user_not_found",
                context={"field": field, "user_id": normalized},
            )
        return normalized

    def board(self, plan_id: str, principal: AuthPrincipal) -> dict[str, object]:
        plan = self._plan(plan_id)
        items = tuple(self._repository.list_items(plan.id))
        plan_actions = sorted(
            action.value for action in authorized_delivery_actions_for(principal, plan)
        )
        return {
            "plan": self._plan_payload(plan),
            "actions": plan_actions,
            "items": [
                {
                    **self._item_payload(item),
                    "actions": sorted(
                        action.value
                        for action in authorized_delivery_actions_for(
                            principal, plan, item
                        )
                    ),
                }
                for item in items
            ],
        }

    def board_for_work_package(
        self, work_package_id: str, principal: AuthPrincipal
    ) -> dict[str, object] | None:
        plan = self._repository.get_non_archived_plan_for_work_package(work_package_id)
        if plan is None:
            return None
        return self.board(plan.id, principal)

    def create_plan(
        self,
        *,
        work_package_id: str,
        lead_user_id: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        if not self._repository.work_package_exists(work_package_id):
            raise ApplicationValidationError(
                "WorkPackage introuvable.",
                code="delivery_work_package_not_found",
                context={"work_package_id": work_package_id},
            )
        if self._repository.get_non_archived_plan_for_work_package(work_package_id):
            raise ApplicationConflictError(
                "Un DeliveryPlan non archivé existe déjà pour ce WorkPackage.",
                code="delivery_plan_exists",
                context={"work_package_id": work_package_id},
            )
        lead_id = self._validate_user(lead_user_id, field="lead_user_id")
        plan = DeliveryPlan(
            id=str(uuid4()),
            work_package_id=work_package_id,
            lead_user_id=lead_id,
        )
        self._require_action(
            principal, plan, DeliveryAction.MANAGE_PLAN_LIFECYCLE
        )
        if lead_id is not None:
            self._require_action(principal, plan, DeliveryAction.ASSIGN_TEAM_LEAD)
        self._repository.add_plan(plan)
        self._repository.append_history(
            plan_id=plan.id,
            action="PLAN_CREATED",
            delivery_version=1,
            actor_user_id=actor_user_id,
            details={"work_package_id": work_package_id, "lead_user_id": lead_id},
        )
        return self.board(plan.id, principal)

    def set_plan_status(
        self,
        plan_id: str,
        *,
        target_status: DeliveryPlanStatus,
        expected_delivery_version: int,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        plan = self._plan(plan_id)
        self._require_action(
            principal, plan, DeliveryAction.MANAGE_PLAN_LIFECYCLE
        )
        self._check_expected(plan, expected_delivery_version)
        target = DeliveryPlanStatus(target_status)
        if plan.status is target:
            return self.board(plan.id, principal)
        try:
            changed = transition_delivery_plan(plan, target, occurred_at=_utc_now())
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc), code="delivery_plan_transition_invalid"
            ) from exc
        next_version = self._bump(plan, expected_delivery_version)
        changed = replace(changed, delivery_version=next_version)
        self._repository.save_plan(changed)
        self._repository.append_history(
            plan_id=plan.id,
            action=f"PLAN_{target.value}",
            delivery_version=next_version,
            actor_user_id=actor_user_id,
            details={"from_status": plan.status.value, "to_status": target.value},
        )
        return self.board(plan.id, principal)

    def set_lead(
        self,
        plan_id: str,
        *,
        lead_user_id: str | None,
        expected_delivery_version: int,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        plan = self._plan(plan_id)
        self._ensure_editable(plan)
        self._require_action(principal, plan, DeliveryAction.ASSIGN_TEAM_LEAD)
        lead_id = self._validate_user(lead_user_id, field="lead_user_id")
        self._check_expected(plan, expected_delivery_version)
        if lead_id == plan.lead_user_id:
            return self.board(plan.id, principal)
        next_version = self._bump(plan, expected_delivery_version)
        changed = replace(
            plan,
            lead_user_id=lead_id,
            delivery_version=next_version,
            updated_at=_utc_now(),
        )
        self._repository.save_plan(changed)
        self._repository.append_history(
            plan_id=plan.id,
            action="PLAN_LEAD_CHANGED",
            delivery_version=next_version,
            actor_user_id=actor_user_id,
            details={"from_user_id": plan.lead_user_id, "to_user_id": lead_id},
        )
        return self.board(plan.id, principal)

    def create_item(
        self,
        plan_id: str,
        *,
        item_type: DeliveryItemType,
        title: str,
        parent_id: str | None,
        description: str | None,
        priority: str | None,
        status: DeliveryItemStatus,
        assignee_user_id: str | None,
        current_estimate_hours: float | None,
        remaining_hours: float | None,
        due_date,
        position: int | None,
        sprint: str | None,
        expected_delivery_version: int,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        plan = self._plan(plan_id)
        self._ensure_editable(plan)
        self._require_action(principal, plan, DeliveryAction.MANAGE_STRUCTURE)
        item_kind = DeliveryItemType(item_type)
        assignee = self._validate_user(
            assignee_user_id, field="assignee_user_id"
        )
        if assignee is not None:
            self._require_action(principal, plan, DeliveryAction.ASSIGN_STORIES)
        items = tuple(self._repository.list_items(plan.id))
        next_position = (
            position
            if position is not None
            else max((item.position for item in items), default=-1) + 1
        )
        try:
            item = DeliveryItem(
                id=str(uuid4()),
                delivery_plan_id=plan.id,
                item_type=item_kind,
                title=title,
                parent_id=_text(parent_id),
                description=_text(description),
                priority=_text(priority),
                assignee_user_id=assignee,
                current_estimate_hours=current_estimate_hours,
                remaining_hours=remaining_hours,
                due_date=due_date,
                position=next_position,
                sprint=_text(sprint),
            )
            if item_kind is DeliveryItemType.STORY and status is not DeliveryItemStatus.BACKLOG:
                item = transition_story_status(item, status)
            elif item_kind is DeliveryItemType.EPIC and status is not DeliveryItemStatus.BACKLOG:
                raise ValueError("EPIC status is not mutable in the MVP")
            validate_delivery_hierarchy(plan, (*items, item))
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc), code="delivery_item_invalid"
            ) from exc
        self._check_expected(plan, expected_delivery_version)
        next_version = self._bump(plan, expected_delivery_version)
        self._repository.add_item(item)
        self._repository.append_history(
            plan_id=plan.id,
            item_id=item.id,
            action="ITEM_CREATED",
            delivery_version=next_version,
            actor_user_id=actor_user_id,
            details={
                "item_type": item.item_type.value,
                "title": item.title,
                "parent_id": item.parent_id,
            },
        )
        return self.board(plan.id, principal)

    def update_item(
        self,
        item_id: str,
        *,
        changes: Mapping[str, object],
        expected_delivery_version: int,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        item = self._item(item_id)
        plan = self._plan(item.delivery_plan_id)
        self._ensure_editable(plan)
        allowed_fields = {
            "title",
            "parent_id",
            "description",
            "priority",
            "status",
            "assignee_user_id",
            "current_estimate_hours",
            "remaining_hours",
            "due_date",
            "position",
            "sprint",
        }
        unknown = set(changes) - allowed_fields
        if unknown:
            raise ApplicationValidationError(
                "Champs Delivery non supportés.",
                code="delivery_item_fields_invalid",
                context={"fields": sorted(unknown)},
            )
        if not changes:
            return self.board(plan.id, principal)

        structure_fields = {"title", "parent_id", "description"}
        if structure_fields.intersection(changes):
            self._require_action(principal, plan, DeliveryAction.MANAGE_STRUCTURE)
        if "position" in changes:
            self._require_action(principal, plan, DeliveryAction.ORDER_ITEMS)
        if "assignee_user_id" in changes:
            self._require_action(principal, plan, DeliveryAction.ASSIGN_STORIES)
        if "current_estimate_hours" in changes:
            self._require_action(principal, plan, DeliveryAction.ESTIMATE_STORIES)
        if "sprint" in changes:
            self._require_action(principal, plan, DeliveryAction.MANAGE_SPRINT)
        if {"priority", "due_date"}.intersection(changes):
            self._require_any_action(
                principal,
                plan,
                (
                    DeliveryAction.SET_PLAN_PRIORITY_DUE_DATE,
                    DeliveryAction.MANAGE_STRUCTURE,
                ),
                item,
            )
        if "status" in changes:
            self._require_any_action(
                principal,
                plan,
                (
                    DeliveryAction.MANAGE_STORY_STATUS_BLOCKING,
                    DeliveryAction.UPDATE_OWN_STORY_STATUS,
                ),
                item,
            )
        if "remaining_hours" in changes:
            self._require_any_action(
                principal,
                plan,
                (
                    DeliveryAction.ESTIMATE_STORIES,
                    DeliveryAction.UPDATE_OWN_REMAINING_HOURS,
                ),
                item,
            )

        changed = item
        try:
            if structure_fields.intersection(changes) or {"priority", "due_date", "position", "sprint", "assignee_user_id"}.intersection(changes):
                values: dict[str, object] = {}
                for field in (
                    "title",
                    "parent_id",
                    "description",
                    "priority",
                    "due_date",
                    "position",
                    "sprint",
                ):
                    if field in changes:
                        value = changes[field]
                        values[field] = (
                            _text(value)
                            if field in {"parent_id", "description", "priority", "sprint"}
                            else value
                        )
                if "assignee_user_id" in changes:
                    values["assignee_user_id"] = self._validate_user(
                        changes["assignee_user_id"], field="assignee_user_id"
                    )
                changed = replace(changed, **values)
            if "current_estimate_hours" in changes:
                changed = reestimate_story(
                    changed, changes["current_estimate_hours"]  # type: ignore[arg-type]
                )
            if "remaining_hours" in changes:
                changed = update_story_remaining_hours(
                    changed, changes["remaining_hours"]  # type: ignore[arg-type]
                )
            if "status" in changes:
                changed = transition_story_status(
                    changed,
                    DeliveryItemStatus(str(changes["status"])),
                )
            all_items = [
                changed if existing.id == changed.id else existing
                for existing in self._repository.list_items(plan.id)
            ]
            validate_delivery_hierarchy(plan, tuple(all_items))
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                str(exc), code="delivery_item_invalid"
            ) from exc

        self._check_expected(plan, expected_delivery_version)
        if changed == item:
            return self.board(plan.id, principal)
        next_version = self._bump(plan, expected_delivery_version)
        self._repository.save_item(changed)
        self._repository.append_history(
            plan_id=plan.id,
            item_id=item.id,
            action="ITEM_UPDATED",
            delivery_version=next_version,
            actor_user_id=actor_user_id,
            details={"fields": sorted(changes)},
        )
        return self.board(plan.id, principal)

    def document_blockage(
        self,
        item_id: str,
        *,
        note: str,
        expected_delivery_version: int,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        actor_user_id = self._require_identity(principal)
        item = self._item(item_id)
        plan = self._plan(item.delivery_plan_id)
        self._ensure_editable(plan)
        self._require_any_action(
            principal,
            plan,
            (
                DeliveryAction.MANAGE_STORY_STATUS_BLOCKING,
                DeliveryAction.DOCUMENT_OWN_BLOCKAGE,
            ),
            item,
        )
        normalized_note = str(note or "").strip()
        if not normalized_note:
            raise ApplicationValidationError(
                "La note de blocage est requise.",
                code="delivery_blockage_note_required",
            )
        next_version = self._bump(plan, expected_delivery_version)
        self._repository.append_history(
            plan_id=plan.id,
            item_id=item.id,
            action="BLOCKAGE_DOCUMENTED",
            delivery_version=next_version,
            actor_user_id=actor_user_id,
            details={"note": normalized_note},
        )
        return self.board(plan.id, principal)

    @staticmethod
    def _plan_payload(plan: DeliveryPlan) -> dict[str, object]:
        return {
            "id": plan.id,
            "work_package_id": plan.work_package_id,
            "status": plan.status.value,
            "lead_user_id": plan.lead_user_id,
            "delivery_version": plan.delivery_version,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "activated_at": plan.activated_at,
            "archived_at": plan.archived_at,
        }

    @staticmethod
    def _item_payload(item: DeliveryItem) -> dict[str, object]:
        return {
            "id": item.id,
            "delivery_plan_id": item.delivery_plan_id,
            "parent_id": item.parent_id,
            "item_type": item.item_type.value,
            "title": item.title,
            "description": item.description,
            "priority": item.priority,
            "status": item.status.value,
            "assignee_user_id": item.assignee_user_id,
            "current_estimate_hours": item.current_estimate_hours,
            "reference_estimate_hours": item.reference_estimate_hours,
            "remaining_hours": item.remaining_hours,
            "due_date": item.due_date,
            "position": item.position,
            "sprint": item.sprint,
        }
