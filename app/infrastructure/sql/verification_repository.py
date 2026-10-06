from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.application.errors import ApplicationConflictError
from app.application.idempotency import normalize_idempotency_key, request_fingerprint
from app.application.verification_contracts import (
    VerificationStorySourceReadModel,
    validate_story_source_for_scope,
)
from app.domain.delivery import DeliveryItemType
from app.domain.verification import (
    StoryVerificationDecision,
    VerificationDecisionKind,
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationRequirementState,
    VerificationRetestRequest,
    VerificationScope,
    adopt_requirement_revision,
    validate_requirement_revision_chain,
    validate_retest_request,
    validate_story_verification_decision,
    withdraw_requirement,
)

from .base import utc_now
from .delivery_models import DeliveryItemRow, DeliveryPlanRow
from .idempotency import SqlCommandIdempotencyAdapter
from .identity_models import AppUser
from .models import WorkPackage
from .verification_models import (
    StoryVerificationDecisionRequirementRow,
    StoryVerificationDecisionRow,
    VerificationChangeHistory,
    VerificationRequirementRevisionRow,
    VerificationRequirementRow,
    VerificationRetestRequestRow,
    VerificationScopeRow,
)


class VerificationVersionConflict(ApplicationConflictError):
    default_code = "verification_version_conflict"


def _scope_from_row(row: VerificationScopeRow) -> VerificationScope:
    return VerificationScope(
        id=row.id,
        work_package_id=row.work_package_id,
        lead_user_id=row.lead_user_id,
        verification_version=row.verification_version,
    )


def _requirement_from_row(row: VerificationRequirementRow) -> VerificationRequirement:
    return VerificationRequirement(
        id=row.id,
        verification_scope_id=row.verification_scope_id,
        story_id=row.story_id,
        phase=VerificationPhase(row.phase),
        current_revision_id=row.current_revision_id,
        state=VerificationRequirementState(row.state),
        withdrawal_reason=row.withdrawal_reason,
    )


def _revision_from_row(
    row: VerificationRequirementRevisionRow,
) -> VerificationRequirementRevision:
    try:
        prerequisites = json.loads(row.prerequisites_json or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Verification revision prerequisites are invalid: {row.id}"
        ) from exc
    if not isinstance(prerequisites, list):
        raise ValueError(
            f"Verification revision prerequisites are invalid: {row.id}"
        )
    return VerificationRequirementRevision(
        id=row.id,
        requirement_id=row.requirement_id,
        revision_number=row.revision_number,
        objective=row.objective,
        method=row.method,
        expected_result=row.expected_result,
        prerequisites=tuple(str(value) for value in prerequisites),
        criticality=row.criticality,
    )


def _retest_from_row(row: VerificationRetestRequestRow) -> VerificationRetestRequest:
    return VerificationRetestRequest(
        id=row.id,
        requirement_id=row.requirement_id,
        revision_id=row.revision_id,
        after_execution_sequence=row.after_execution_sequence,
        reason=row.reason,
    )


class SqlVerificationRepository:
    """Additive Verification persistence primitives for ADR-023 / issue #363B.

    The caller owns the outer transaction. Keyed mutations reuse the shared durable
    command receipt; unkeyed mutations use a SAVEPOINT so a caught failure cannot
    accidentally commit a Verification CAS without its business rows and audit.
    Story closure remains outside this repository until #363C.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_scope(self, scope_id: str) -> VerificationScope | None:
        row = self._session.get(VerificationScopeRow, str(scope_id).strip())
        return _scope_from_row(row) if row is not None else None

    def get_scope_for_work_package(
        self, work_package_id: str
    ) -> VerificationScope | None:
        row = self._session.scalar(
            select(VerificationScopeRow).where(
                VerificationScopeRow.work_package_id == str(work_package_id).strip()
            )
        )
        return _scope_from_row(row) if row is not None else None

    def get_requirement(self, requirement_id: str) -> VerificationRequirement | None:
        row = self._session.get(VerificationRequirementRow, str(requirement_id).strip())
        return _requirement_from_row(row) if row is not None else None

    def get_revision(
        self, revision_id: str
    ) -> VerificationRequirementRevision | None:
        row = self._session.get(
            VerificationRequirementRevisionRow, str(revision_id).strip()
        )
        return _revision_from_row(row) if row is not None else None

    def list_revisions(
        self, requirement_id: str
    ) -> tuple[VerificationRequirementRevision, ...]:
        rows = self._session.scalars(
            select(VerificationRequirementRevisionRow)
            .where(
                VerificationRequirementRevisionRow.requirement_id
                == str(requirement_id).strip()
            )
            .order_by(VerificationRequirementRevisionRow.revision_number)
        ).all()
        return tuple(_revision_from_row(row) for row in rows)

    def list_requirements_for_story(
        self, scope_id: str, story_id: str
    ) -> tuple[VerificationRequirement, ...]:
        rows = self._session.scalars(
            select(VerificationRequirementRow)
            .where(
                VerificationRequirementRow.verification_scope_id
                == str(scope_id).strip(),
                VerificationRequirementRow.story_id == str(story_id).strip(),
            )
            .order_by(
                VerificationRequirementRow.created_at,
                VerificationRequirementRow.id,
            )
        ).all()
        return tuple(_requirement_from_row(row) for row in rows)

    def _guard_work_package_dependency(self, work_package_id: str) -> None:
        identifier = str(work_package_id or "").strip()
        if not identifier:
            raise KeyError("WorkPackage id is required")
        result = self._session.execute(
            update(WorkPackage)
            .where(WorkPackage.id == identifier)
            .values(
                version=WorkPackage.version,
                updated_at=WorkPackage.updated_at,
            )
        )
        if int(result.rowcount or 0) != 1:
            raise KeyError(f"WorkPackage not found: {identifier}")
        self._session.flush()

    def _story_source(
        self, scope: VerificationScope, story_id: str
    ) -> VerificationStorySourceReadModel:
        row = self._session.execute(
            select(DeliveryItemRow, DeliveryPlanRow)
            .join(
                DeliveryPlanRow,
                DeliveryPlanRow.id == DeliveryItemRow.delivery_plan_id,
            )
            .where(DeliveryItemRow.id == str(story_id).strip())
        ).one_or_none()
        if row is None:
            raise KeyError(f"DeliveryItem not found: {story_id}")
        item, plan = row
        source = VerificationStorySourceReadModel(
            story_id=item.id,
            delivery_plan_id=plan.id,
            work_package_id=plan.work_package_id,
            item_type=DeliveryItemType(item.item_type),
            delivery_status=item.status,
        )
        validate_story_source_for_scope(scope, source)
        return source

    def add_scope(self, scope: VerificationScope) -> None:
        self._guard_work_package_dependency(scope.work_package_id)
        if (
            scope.lead_user_id is not None
            and self._session.get(AppUser, scope.lead_user_id) is None
        ):
            raise KeyError(f"AppUser not found: {scope.lead_user_id}")
        self._session.add(
            VerificationScopeRow(
                id=scope.id,
                work_package_id=scope.work_package_id,
                lead_user_id=scope.lead_user_id,
                verification_version=scope.verification_version,
            )
        )
        self._session.flush()

    def compare_and_increment_version(
        self,
        scope_id: str,
        *,
        expected_verification_version: int,
    ) -> int:
        if expected_verification_version < 1:
            raise ValueError("expected_verification_version must be at least 1")
        next_version = expected_verification_version + 1
        result = self._session.execute(
            update(VerificationScopeRow)
            .where(
                VerificationScopeRow.id == str(scope_id).strip(),
                VerificationScopeRow.verification_version
                == expected_verification_version,
            )
            .values(
                verification_version=next_version,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) == 1:
            self._session.flush()
            return next_version
        current = self._session.scalar(
            select(VerificationScopeRow.verification_version).where(
                VerificationScopeRow.id == str(scope_id).strip()
            )
        )
        if current is None:
            raise KeyError(f"VerificationScope not found: {scope_id}")
        raise VerificationVersionConflict(
            "Le périmètre Verification a été modifié depuis sa lecture.",
            context={
                "verification_scope_id": str(scope_id).strip(),
                "expected_verification_version": expected_verification_version,
                "current_verification_version": int(current),
            },
        )

    def add_requirement(
        self,
        requirement: VerificationRequirement,
        initial_revision: VerificationRequirementRevision,
    ) -> None:
        scope = self.get_scope(requirement.verification_scope_id)
        if scope is None:
            raise KeyError(
                f"VerificationScope not found: {requirement.verification_scope_id}"
            )
        self._story_source(scope, requirement.story_id)
        validate_requirement_revision_chain(requirement, (initial_revision,))
        self._session.add(
            VerificationRequirementRow(
                id=requirement.id,
                verification_scope_id=requirement.verification_scope_id,
                story_id=requirement.story_id,
                phase=requirement.phase.value,
                current_revision_id=requirement.current_revision_id,
                state=requirement.state.value,
                withdrawal_reason=requirement.withdrawal_reason,
            )
        )
        self._session.add(
            VerificationRequirementRevisionRow(
                id=initial_revision.id,
                requirement_id=initial_revision.requirement_id,
                revision_number=initial_revision.revision_number,
                objective=initial_revision.objective,
                method=initial_revision.method,
                expected_result=initial_revision.expected_result,
                prerequisites_json=json.dumps(
                    list(initial_revision.prerequisites),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                criticality=initial_revision.criticality,
            )
        )
        self._session.flush()

    def add_revision(
        self,
        requirement_id: str,
        next_revision: VerificationRequirementRevision,
    ) -> VerificationRequirement:
        row = self._session.get(
            VerificationRequirementRow, str(requirement_id).strip()
        )
        if row is None:
            raise KeyError(f"VerificationRequirement not found: {requirement_id}")
        requirement = _requirement_from_row(row)
        current = self.get_revision(requirement.current_revision_id)
        if current is None:
            raise ValueError(
                "VerificationRequirement current revision is missing: "
                f"{requirement.current_revision_id}"
            )
        revised = adopt_requirement_revision(requirement, current, next_revision)
        self._session.add(
            VerificationRequirementRevisionRow(
                id=next_revision.id,
                requirement_id=next_revision.requirement_id,
                revision_number=next_revision.revision_number,
                objective=next_revision.objective,
                method=next_revision.method,
                expected_result=next_revision.expected_result,
                prerequisites_json=json.dumps(
                    list(next_revision.prerequisites),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                criticality=next_revision.criticality,
            )
        )
        row.current_revision_id = revised.current_revision_id
        self._session.flush()
        return revised

    def withdraw_requirement(
        self, requirement_id: str, *, reason: str
    ) -> VerificationRequirement:
        row = self._session.get(
            VerificationRequirementRow, str(requirement_id).strip()
        )
        if row is None:
            raise KeyError(f"VerificationRequirement not found: {requirement_id}")
        withdrawn = withdraw_requirement(_requirement_from_row(row), reason=reason)
        row.state = withdrawn.state.value
        row.withdrawal_reason = withdrawn.withdrawal_reason
        self._session.flush()
        return withdrawn

    def add_story_decision(self, decision: StoryVerificationDecision) -> None:
        scope = self.get_scope(decision.verification_scope_id)
        if scope is None:
            raise KeyError(
                f"VerificationScope not found: {decision.verification_scope_id}"
            )
        self._story_source(scope, decision.story_id)
        requirements: list[VerificationRequirement] = []
        revisions: list[VerificationRequirementRevision] = []
        for requirement_id in decision.requirement_ids:
            requirement = self.get_requirement(requirement_id)
            if requirement is None:
                raise KeyError(
                    f"VerificationRequirement not found: {requirement_id}"
                )
            requirements.append(requirement)
            revisions.extend(self.list_revisions(requirement.id))
        validate_story_verification_decision(
            scope,
            decision,
            requirements,
            revisions,
        )
        self._session.add(
            StoryVerificationDecisionRow(
                id=decision.id,
                verification_scope_id=decision.verification_scope_id,
                story_id=decision.story_id,
                kind=decision.kind.value,
                justification=decision.justification,
            )
        )
        self._session.flush()
        for requirement_id in decision.requirement_ids:
            self._session.add(
                StoryVerificationDecisionRequirementRow(
                    decision_id=decision.id,
                    requirement_id=requirement_id,
                )
            )
        self._session.flush()

    def get_story_decision(
        self, decision_id: str
    ) -> StoryVerificationDecision | None:
        row = self._session.get(
            StoryVerificationDecisionRow, str(decision_id).strip()
        )
        if row is None:
            return None
        requirement_ids = tuple(
            self._session.scalars(
                select(StoryVerificationDecisionRequirementRow.requirement_id)
                .where(
                    StoryVerificationDecisionRequirementRow.decision_id == row.id
                )
                .order_by(StoryVerificationDecisionRequirementRow.requirement_id)
            ).all()
        )
        return StoryVerificationDecision(
            id=row.id,
            verification_scope_id=row.verification_scope_id,
            story_id=row.story_id,
            kind=VerificationDecisionKind(row.kind),
            justification=row.justification,
            requirement_ids=requirement_ids,
        )

    def add_retest_request(self, request: VerificationRetestRequest) -> None:
        requirement = self.get_requirement(request.requirement_id)
        if requirement is None:
            raise KeyError(
                f"VerificationRequirement not found: {request.requirement_id}"
            )
        validate_retest_request(requirement, request)
        revision = self.get_revision(request.revision_id)
        if revision is None or revision.requirement_id != requirement.id:
            raise ValueError("Retest revision is not persisted for this requirement")
        self._session.add(
            VerificationRetestRequestRow(
                id=request.id,
                requirement_id=request.requirement_id,
                revision_id=request.revision_id,
                after_execution_sequence=request.after_execution_sequence,
                reason=request.reason,
            )
        )
        self._session.flush()

    def get_retest_request(
        self, request_id: str
    ) -> VerificationRetestRequest | None:
        row = self._session.get(
            VerificationRetestRequestRow, str(request_id).strip()
        )
        return _retest_from_row(row) if row is not None else None

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
    ) -> str:
        normalized_entity_type = str(entity_type or "").strip()
        normalized_entity_id = str(entity_id or "").strip()
        normalized_actor = str(actor_user_id or "").strip()
        normalized_action = str(action or "").strip()
        if not all(
            (normalized_entity_type, normalized_entity_id, normalized_actor, normalized_action)
        ):
            raise ValueError("Verification audit identifiers and action are required")
        if verification_version < 1:
            raise ValueError("verification_version must be at least 1")
        if self._session.get(AppUser, normalized_actor) is None:
            raise KeyError(f"AppUser not found: {normalized_actor}")
        row = VerificationChangeHistory(
            verification_scope_id=str(scope_id).strip(),
            entity_type=normalized_entity_type,
            entity_id=normalized_entity_id,
            story_id=str(story_id).strip() if story_id else None,
            actor_user_id=normalized_actor,
            action=normalized_action,
            verification_version=verification_version,
            details_json=json.dumps(
                dict(details or {}),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ),
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def replay_or_execute_mutation(
        self,
        *,
        scope_id: str,
        actor_user_id: str,
        command_scope: str,
        idempotency_key: str | None,
        request_payload: Mapping[str, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_scope_id = str(scope_id or "").strip()
        normalized_actor = str(actor_user_id or "").strip()
        normalized_command = str(command_scope or "").strip()
        if not normalized_scope_id or not normalized_actor or not normalized_command:
            raise ValueError(
                "Verification mutation scope, actor and command scope are required"
            )
        key = normalize_idempotency_key(idempotency_key)
        if key is None:
            with self._session.begin_nested():
                return action()
        return SqlCommandIdempotencyAdapter(
            self._session,
            actor_name=f"verification:{normalized_actor}",
        ).replay_or_execute(
            scope=f"verification:{normalized_scope_id}:{normalized_command}",
            key=key,
            request_fingerprint=request_fingerprint(request_payload),
            action=action,
        )
