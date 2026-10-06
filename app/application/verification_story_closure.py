from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from app.domain.delivery import (
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryItemType,
    DeliveryPlan,
    DeliveryPlanStatus,
    transition_story_status,
)
from app.domain.verification import (
    StoryVerificationDecision,
    VerificationDecisionKind,
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationScope,
)

from .delivery_contracts import DeliveryAction
from .delivery_security import authorized_delivery_actions_for
from .errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from .security import AuthPrincipal
from .verification_contracts import (
    VerificationAction,
    VerificationActorContext,
    VerificationAuthorityContext,
    verification_actions_for,
)


@dataclass(frozen=True, slots=True)
class StoryRequirementDefinition:
    phase: VerificationPhase
    objective: str
    method: str
    expected_result: str
    prerequisites: tuple[str, ...] = ()
    criticality: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", VerificationPhase(self.phase))
        for field_name in ("objective", "method", "expected_result"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        prerequisites = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in self.prerequisites
                if str(value).strip()
            )
        )
        object.__setattr__(self, "prerequisites", prerequisites)
        criticality = str(self.criticality or "").strip() or None
        object.__setattr__(self, "criticality", criticality)


@dataclass(frozen=True, slots=True)
class StoryVerificationDecisionInput:
    kind: VerificationDecisionKind
    justification: str | None = None
    existing_requirement_ids: tuple[str, ...] = ()
    new_requirements: tuple[StoryRequirementDefinition, ...] = ()

    def __post_init__(self) -> None:
        kind = VerificationDecisionKind(self.kind)
        object.__setattr__(self, "kind", kind)
        justification = str(self.justification or "").strip() or None
        object.__setattr__(self, "justification", justification)
        requirement_ids = tuple(
            dict.fromkeys(
                str(requirement_id).strip()
                for requirement_id in self.existing_requirement_ids
                if str(requirement_id).strip()
            )
        )
        object.__setattr__(self, "existing_requirement_ids", requirement_ids)
        object.__setattr__(self, "new_requirements", tuple(self.new_requirements))
        if kind is VerificationDecisionKind.NO_TEST_REQUIRED:
            if justification is None:
                raise ValueError(
                    "NO_TEST_REQUIRED requires an explicit justification"
                )
            if requirement_ids or self.new_requirements:
                raise ValueError(
                    "NO_TEST_REQUIRED cannot reference Verification requirements"
                )
        elif not requirement_ids and not self.new_requirements:
            raise ValueError(
                "TESTS_DEFINED requires at least one existing or new requirement"
            )


class StoryClosureDeliveryRepositoryPort(Protocol):
    def get_plan(self, plan_id: str) -> DeliveryPlan | None: ...
    def get_item(self, item_id: str) -> DeliveryItem | None: ...
    def save_item(self, item: DeliveryItem) -> None: ...
    def guard_version(
        self, plan_id: str, *, expected_delivery_version: int
    ) -> None: ...
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


class StoryClosureVerificationRepositoryPort(Protocol):
    def get_scope_for_work_package(
        self, work_package_id: str
    ) -> VerificationScope | None: ...
    def guard_work_package_dependency(self, work_package_id: str) -> None: ...
    def add_scope(
        self,
        scope: VerificationScope,
        *,
        guard_work_package: bool = True,
    ) -> None: ...
    def compare_and_increment_version(
        self,
        scope_id: str,
        *,
        expected_verification_version: int,
    ) -> int: ...
    def add_requirement(
        self,
        requirement: VerificationRequirement,
        initial_revision: VerificationRequirementRevision,
    ) -> None: ...
    def add_story_decision(self, decision: StoryVerificationDecision) -> None: ...
    def has_story_decision(self, scope_id: str, story_id: str) -> bool: ...
    def append_history(
        self,
        *,
        scope_id: str,
        entity_type: str,
        entity_id: str,
        actor_user_id: str,
        action: str,
        verification_version: int,
        story_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str: ...
    def replay_or_execute_mutation(
        self,
        *,
        scope_id: str,
        actor_user_id: str,
        command_scope: str,
        idempotency_key: str | None,
        request_payload: Mapping[str, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]: ...


class StoryVerificationClosureService:
    """Coordinate Story closure with Verification inside one caller transaction."""

    _CLOSURE_ACTIONS = frozenset(
        {
            DeliveryAction.MANAGE_STORY_STATUS_BLOCKING,
            DeliveryAction.UPDATE_OWN_STORY_STATUS,
        }
    )

    def __init__(
        self,
        delivery: StoryClosureDeliveryRepositoryPort,
        verification: StoryClosureVerificationRepositoryPort,
    ) -> None:
        self._delivery = delivery
        self._verification = verification

    @staticmethod
    def _require_identity(principal: AuthPrincipal) -> str:
        actor_user_id = str(principal.local_user_id or "").strip()
        if not actor_user_id:
            raise ApplicationAuthorizationError(
                "Une identité AppUser stable est requise pour fermer une Story.",
                code="delivery_identity_required",
            )
        return actor_user_id

    def _authorized_context(
        self,
        story_id: str,
        principal: AuthPrincipal,
    ) -> tuple[str, DeliveryItem, DeliveryPlan]:
        actor_user_id = self._require_identity(principal)
        story = self._delivery.get_item(story_id)
        if story is None:
            raise ApplicationNotFoundError(
                "DeliveryItem introuvable.",
                code="delivery_item_not_found",
                context={"delivery_item_id": story_id},
            )
        if story.item_type is not DeliveryItemType.STORY:
            raise ApplicationValidationError(
                "La décision Verification de fermeture cible uniquement une Story.",
                code="verification_story_required",
                context={"delivery_item_id": story.id},
            )
        plan = self._delivery.get_plan(story.delivery_plan_id)
        if plan is None:
            raise ApplicationNotFoundError(
                "DeliveryPlan introuvable.",
                code="delivery_plan_not_found",
                context={"delivery_plan_id": story.delivery_plan_id},
            )

        delivery_actions = authorized_delivery_actions_for(principal, plan, story)
        if not delivery_actions.intersection(self._CLOSURE_ACTIONS):
            raise ApplicationAuthorizationError(
                "Action Delivery non autorisée.",
                code="delivery_action_denied",
                context={
                    "required_any_action": sorted(
                        action.value for action in self._CLOSURE_ACTIONS
                    ),
                    "delivery_plan_id": plan.id,
                    "delivery_item_id": story.id,
                },
            )

        conceptual_scope = (
            self._verification.get_scope_for_work_package(plan.work_package_id)
            or VerificationScope(
                id=f"pending:{plan.work_package_id}",
                work_package_id=plan.work_package_id,
                lead_user_id=plan.lead_user_id,
            )
        )
        verification_actions = verification_actions_for(
            VerificationActorContext(actor_user_id, tuple(principal.roles)),
            conceptual_scope,
            VerificationAuthorityContext(story_closure_authorized=True),
        )
        if VerificationAction.RECORD_STORY_DECISION not in verification_actions:
            raise ApplicationAuthorizationError(
                "Décision Verification non autorisée pour cette fermeture.",
                code="verification_action_denied",
                context={"story_id": story.id},
            )
        return actor_user_id, story, plan

    @staticmethod
    def _payload(
        *,
        story_id: str,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        decision: StoryVerificationDecisionInput,
        historical: bool,
    ) -> dict[str, Any]:
        return {
            "story_id": story_id,
            "expected_delivery_version": expected_delivery_version,
            "expected_verification_version": expected_verification_version,
            "historical": historical,
            "decision": {
                "kind": decision.kind.value,
                "justification": decision.justification,
                "existing_requirement_ids": list(decision.existing_requirement_ids),
                "new_requirements": [
                    {
                        "phase": requirement.phase.value,
                        "objective": requirement.objective,
                        "method": requirement.method,
                        "expected_result": requirement.expected_result,
                        "prerequisites": list(requirement.prerequisites),
                        "criticality": requirement.criticality,
                    }
                    for requirement in decision.new_requirements
                ],
            },
        }

    def complete_story(
        self,
        story_id: str,
        *,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        decision: StoryVerificationDecisionInput,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor_user_id, story, plan = self._authorized_context(story_id, principal)
        return self._execute_idempotent(
            actor_user_id=actor_user_id,
            story=story,
            plan=plan,
            expected_delivery_version=expected_delivery_version,
            expected_verification_version=expected_verification_version,
            decision=decision,
            idempotency_key=idempotency_key,
            historical=False,
        )

    def record_historical_decision(
        self,
        story_id: str,
        *,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        decision: StoryVerificationDecisionInput,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor_user_id, story, plan = self._authorized_context(story_id, principal)
        return self._execute_idempotent(
            actor_user_id=actor_user_id,
            story=story,
            plan=plan,
            expected_delivery_version=expected_delivery_version,
            expected_verification_version=expected_verification_version,
            decision=decision,
            idempotency_key=idempotency_key,
            historical=True,
        )

    def _execute_idempotent(
        self,
        *,
        actor_user_id: str,
        story: DeliveryItem,
        plan: DeliveryPlan,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        decision: StoryVerificationDecisionInput,
        idempotency_key: str | None,
        historical: bool,
    ) -> dict[str, Any]:
        payload = self._payload(
            story_id=story.id,
            expected_delivery_version=expected_delivery_version,
            expected_verification_version=expected_verification_version,
            decision=decision,
            historical=historical,
        )

        try:
            return self._verification.replay_or_execute_mutation(
                scope_id=plan.work_package_id,
                actor_user_id=actor_user_id,
                command_scope=(
                    "historical_story_decision"
                    if historical
                    else "story_close"
                ),
                idempotency_key=idempotency_key,
                request_payload=payload,
                action=lambda: self._execute_atomic(
                    actor_user_id=actor_user_id,
                    story_id=story.id,
                    delivery_plan_id=plan.id,
                    work_package_id=plan.work_package_id,
                    expected_delivery_version=expected_delivery_version,
                    expected_verification_version=expected_verification_version,
                    decision=decision,
                    historical=historical,
                ),
            )
        except ApplicationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            message = str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc)
            raise ApplicationValidationError(
                message or "Décision Verification invalide.",
                code="verification_story_decision_invalid",
                context={"story_id": story.id},
            ) from exc

    def _execute_atomic(
        self,
        *,
        actor_user_id: str,
        story_id: str,
        delivery_plan_id: str,
        work_package_id: str,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        decision: StoryVerificationDecisionInput,
        historical: bool,
    ) -> dict[str, Any]:
        # ADR-023 lock order: WorkPackage -> Delivery -> Verification.
        self._verification.guard_work_package_dependency(work_package_id)

        try:
            if historical:
                self._delivery.guard_version(
                    delivery_plan_id,
                    expected_delivery_version=expected_delivery_version,
                )
                next_delivery_version = expected_delivery_version
            else:
                next_delivery_version = self._delivery.compare_and_increment_version(
                    delivery_plan_id,
                    expected_delivery_version=expected_delivery_version,
                )
        except RuntimeError as exc:
            raise ApplicationConflictError(
                "Le board Delivery a été modifié par une autre opération.",
                code="delivery_version_conflict",
                context={
                    "delivery_plan_id": delivery_plan_id,
                    "expected_delivery_version": expected_delivery_version,
                },
            ) from exc

        scope = self._verification.get_scope_for_work_package(work_package_id)
        scope_created = scope is None
        if scope is None:
            if expected_verification_version is not None:
                raise ApplicationConflictError(
                    "Le périmètre Verification attendu n'existe plus dans cet état.",
                    code="verification_version_conflict",
                    context={
                        "work_package_id": work_package_id,
                        "expected_verification_version": expected_verification_version,
                        "current_verification_version": None,
                    },
                )
            current_plan = self._delivery.get_plan(delivery_plan_id)
            if current_plan is None:
                raise ApplicationNotFoundError(
                    "DeliveryPlan introuvable.",
                    code="delivery_plan_not_found",
                    context={"delivery_plan_id": delivery_plan_id},
                )
            scope = VerificationScope(
                id=str(uuid4()),
                work_package_id=work_package_id,
                lead_user_id=current_plan.lead_user_id,
                verification_version=1,
            )
            self._verification.add_scope(scope, guard_work_package=False)
            next_verification_version = 1
        else:
            if expected_verification_version is None:
                raise ApplicationConflictError(
                    "Le périmètre Verification existe alors que la commande attendait son absence.",
                    code="verification_version_conflict",
                    context={
                        "verification_scope_id": scope.id,
                        "expected_verification_version": None,
                        "current_verification_version": scope.verification_version,
                    },
                )
            next_verification_version = self._verification.compare_and_increment_version(
                scope.id,
                expected_verification_version=expected_verification_version,
            )

        current_plan = self._delivery.get_plan(delivery_plan_id)
        current_story = self._delivery.get_item(story_id)
        if current_plan is None or current_story is None:
            raise ApplicationNotFoundError(
                "Story Delivery introuvable pendant la fermeture.",
                code="delivery_item_not_found",
                context={"delivery_item_id": story_id},
            )
        if current_plan.work_package_id != work_package_id:
            raise ApplicationConflictError(
                "La Story a changé de WorkPackage pendant la commande.",
                code="verification_story_scope_conflict",
                context={"story_id": story_id, "work_package_id": work_package_id},
            )
        if current_story.item_type is not DeliveryItemType.STORY:
            raise ApplicationValidationError(
                "La décision Verification cible uniquement une Story.",
                code="verification_story_required",
                context={"delivery_item_id": story_id},
            )

        if historical:
            if current_story.status is not DeliveryItemStatus.DONE:
                raise ApplicationConflictError(
                    "La reprise historique exige une Story déjà terminée.",
                    code="historical_story_not_done",
                    context={
                        "story_id": story_id,
                        "current_status": current_story.status.value,
                    },
                )
            if self._verification.has_story_decision(scope.id, story_id):
                raise ApplicationConflictError(
                    "Cette Story possède déjà une décision Verification.",
                    code="story_verification_already_documented",
                    context={"story_id": story_id},
                )
            completed_story = current_story
        else:
            if current_plan.status is DeliveryPlanStatus.ARCHIVED:
                raise ApplicationConflictError(
                    "Un DeliveryPlan archivé est en lecture seule.",
                    code="delivery_plan_archived",
                    context={"delivery_plan_id": current_plan.id},
                )
            if current_story.status is DeliveryItemStatus.DONE:
                raise ApplicationConflictError(
                    "La Story est déjà terminée; utilisez la reprise historique "
                    "seulement si sa décision manque.",
                    code="story_already_done",
                    context={"story_id": story_id},
                )
            completed_story = transition_story_status(
                current_story,
                DeliveryItemStatus.DONE,
            )

        new_requirement_ids: list[str] = []
        for definition in decision.new_requirements:
            requirement_id = str(uuid4())
            revision_id = str(uuid4())
            requirement = VerificationRequirement(
                id=requirement_id,
                verification_scope_id=scope.id,
                story_id=story_id,
                phase=definition.phase,
                current_revision_id=revision_id,
            )
            revision = VerificationRequirementRevision(
                id=revision_id,
                requirement_id=requirement_id,
                revision_number=1,
                objective=definition.objective,
                method=definition.method,
                expected_result=definition.expected_result,
                prerequisites=definition.prerequisites,
                criticality=definition.criticality,
            )
            self._verification.add_requirement(requirement, revision)
            new_requirement_ids.append(requirement_id)

        requirement_ids = tuple(
            dict.fromkeys(
                (*decision.existing_requirement_ids, *new_requirement_ids)
            )
        )
        story_decision = StoryVerificationDecision(
            id=str(uuid4()),
            verification_scope_id=scope.id,
            story_id=story_id,
            kind=decision.kind,
            justification=decision.justification,
            requirement_ids=requirement_ids,
        )
        self._verification.add_story_decision(story_decision)

        if not historical:
            self._delivery.save_item(completed_story)

        if scope_created:
            self._verification.append_history(
                scope_id=scope.id,
                entity_type="SCOPE",
                entity_id=scope.id,
                story_id=story_id,
                actor_user_id=actor_user_id,
                action="VERIFICATION_SCOPE_CREATED",
                verification_version=next_verification_version,
                details={"work_package_id": work_package_id},
            )
        for requirement_id in new_requirement_ids:
            self._verification.append_history(
                scope_id=scope.id,
                entity_type="REQUIREMENT",
                entity_id=requirement_id,
                story_id=story_id,
                actor_user_id=actor_user_id,
                action="REQUIREMENT_DEFINED_AT_STORY_CLOSURE",
                verification_version=next_verification_version,
                details={"decision_kind": decision.kind.value},
            )
        self._verification.append_history(
            scope_id=scope.id,
            entity_type="STORY_DECISION",
            entity_id=story_decision.id,
            story_id=story_id,
            actor_user_id=actor_user_id,
            action=(
                "HISTORICAL_STORY_DECISION_RECORDED"
                if historical
                else "STORY_DECISION_RECORDED"
            ),
            verification_version=next_verification_version,
            details={"requirement_ids": list(requirement_ids)},
        )

        if not historical:
            self._delivery.append_history(
                plan_id=delivery_plan_id,
                item_id=story_id,
                actor_user_id=actor_user_id,
                action="STORY_COMPLETED_WITH_VERIFICATION",
                delivery_version=next_delivery_version,
                details={
                    "verification_scope_id": scope.id,
                    "verification_decision_id": story_decision.id,
                    "verification_decision_kind": decision.kind.value,
                },
            )

        return {
            "story_id": story_id,
            "delivery_plan_id": delivery_plan_id,
            "work_package_id": work_package_id,
            "story_status": completed_story.status.value,
            "delivery_version": next_delivery_version,
            "verification_scope_id": scope.id,
            "verification_version": next_verification_version,
            "decision_id": story_decision.id,
            "decision_kind": story_decision.kind.value,
            "requirement_ids": list(story_decision.requirement_ids),
            "historical": historical,
        }
