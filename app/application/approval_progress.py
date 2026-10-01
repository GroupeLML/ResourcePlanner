from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..domain.approval_cycles import APPROVAL_CYCLE_STATE_OPEN
from .approval_cycles import ApprovalCycleService
from .approval_voting import ApprovalDecisionRecord, project_approval_quorum
from .security import PERMISSION_APPROVE_DEMANDS


@dataclass(frozen=True, slots=True)
class ApprovalUserSummaryRecord:
    app_user_id: str
    display_name: str
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovalApproverProgressReadModel:
    app_user_id: str
    display_name: str
    active: bool
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalDecisionProgressReadModel:
    app_user_id: str
    display_name: str
    decision: str
    action_id: str
    decided_at: object | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequirementProgressReadModel:
    requirement_id: str
    request_line_id: str
    task_catalog_item_id: str | None
    approval_scope_id: str | None
    proposed_resource_id: str | None
    routing_sources: tuple[str, ...]
    satisfied: bool
    actor_can_approve: bool
    approvers: tuple[ApprovalApproverProgressReadModel, ...]
    decisions: tuple[ApprovalDecisionProgressReadModel, ...]


@dataclass(frozen=True, slots=True)
class ApprovalCycleProgressReadModel:
    approval_cycle_id: str
    state: str
    submitted_request_version: int
    submitted_at: object
    invalidated_at: object | None
    invalidation_reason: str | None
    completed_at: object | None
    approval_revision_id: str | None
    total_requirements: int
    satisfied_requirements: int
    quorum_complete: bool
    actor_approvable_requirement_ids: tuple[str, ...]
    actor_approvable_request_line_ids: tuple[str, ...]
    requirements: tuple[ApprovalRequirementProgressReadModel, ...]


class ApprovalProgressRepositoryPort(Protocol):
    def list_decisions(self, cycle_id: str) -> tuple[ApprovalDecisionRecord, ...]: ...

    def list_approval_users(
        self,
        user_ids: Sequence[str],
    ) -> tuple[ApprovalUserSummaryRecord, ...]: ...

    def list_actor_approvable_demand_numbers(
        self,
        demand_numbers: Sequence[str],
        *,
        current_user_id: str,
    ) -> tuple[str, ...]: ...


class ApprovalProgressService:
    """Read-only #276D projection over the immutable approval-cycle snapshot."""

    def __init__(
        self,
        cycles: ApprovalCycleService,
        repository: ApprovalProgressRepositoryPort,
    ) -> None:
        self._cycles = cycles
        self._repository = repository

    def actor_approvable_demand_numbers(
        self,
        demand_numbers: Sequence[str],
        *,
        current_user_id: str | None,
        permissions: Sequence[str],
    ) -> tuple[str, ...]:
        actor_id = str(current_user_id or "").strip()
        if not actor_id or PERMISSION_APPROVE_DEMANDS not in permissions:
            return ()
        return self._repository.list_actor_approvable_demand_numbers(
            demand_numbers,
            current_user_id=actor_id,
        )

    def get(
        self,
        demand_number: str,
        *,
        current_user_id: str | None,
        permissions: Sequence[str],
    ) -> ApprovalCycleProgressReadModel | None:
        request = self._cycles.get_request(demand_number)
        if request is None:
            return None

        cycle = self._cycles.get_active_cycle(request.id)
        if cycle is None:
            cycle = self._cycles.get_latest_cycle(request.id)
        if cycle is None:
            return None

        decisions = self._repository.list_decisions(cycle.id)
        quorum = project_approval_quorum(cycle, decisions)
        all_user_ids = tuple(
            dict.fromkeys(
                [
                    approver.app_user_id
                    for requirement in cycle.requirements
                    for approver in requirement.approvers
                ]
                + [decision.app_user_id for decision in decisions]
            )
        )
        users = {
            row.app_user_id: row
            for row in self._repository.list_approval_users(all_user_ids)
        }

        actor_id = str(current_user_id or "").strip() or None
        actor = users.get(actor_id) if actor_id is not None else None
        actor_authorized = bool(
            actor_id
            and actor is not None
            and actor.active
            and PERMISSION_APPROVE_DEMANDS in permissions
            and cycle.state == APPROVAL_CYCLE_STATE_OPEN
            and request.status == "Soumise"
        )

        decisions_by_requirement: dict[str, list[ApprovalDecisionRecord]] = {}
        for decision in decisions:
            decisions_by_requirement.setdefault(decision.requirement_id, []).append(decision)

        satisfied = set(quorum.satisfied_requirement_ids)
        projected_requirements: list[ApprovalRequirementProgressReadModel] = []
        actor_requirement_ids: list[str] = []
        actor_line_ids: list[str] = []

        for requirement in cycle.requirements:
            frozen_actor_ids = {item.app_user_id for item in requirement.approvers}
            can_approve = bool(
                actor_authorized
                and requirement.id not in satisfied
                and actor_id in frozen_actor_ids
            )
            if can_approve:
                actor_requirement_ids.append(requirement.id)
                actor_line_ids.append(requirement.request_line_id)

            approvers = tuple(
                ApprovalApproverProgressReadModel(
                    app_user_id=item.app_user_id,
                    display_name=(
                        users[item.app_user_id].display_name
                        if item.app_user_id in users
                        else item.app_user_id
                    ),
                    active=(
                        users[item.app_user_id].active
                        if item.app_user_id in users
                        else False
                    ),
                    sources=item.sources,
                )
                for item in requirement.approvers
            )
            requirement_decisions = tuple(
                ApprovalDecisionProgressReadModel(
                    app_user_id=decision.app_user_id,
                    display_name=(
                        users[decision.app_user_id].display_name
                        if decision.app_user_id in users
                        else decision.app_user_id
                    ),
                    decision=decision.decision,
                    action_id=decision.action_id,
                    decided_at=decision.decided_at,
                    comment=decision.comment,
                )
                for decision in decisions_by_requirement.get(requirement.id, ())
            )
            projected_requirements.append(
                ApprovalRequirementProgressReadModel(
                    requirement_id=requirement.id,
                    request_line_id=requirement.request_line_id,
                    task_catalog_item_id=requirement.task_catalog_item_id,
                    approval_scope_id=requirement.approval_scope_id,
                    proposed_resource_id=requirement.proposed_resource_id,
                    routing_sources=requirement.routing_sources,
                    satisfied=requirement.id in satisfied,
                    actor_can_approve=can_approve,
                    approvers=approvers,
                    decisions=requirement_decisions,
                )
            )

        return ApprovalCycleProgressReadModel(
            approval_cycle_id=cycle.id,
            state=cycle.state,
            submitted_request_version=cycle.submitted_request_version,
            submitted_at=cycle.submitted_at,
            invalidated_at=cycle.invalidated_at,
            invalidation_reason=cycle.invalidation_reason,
            completed_at=cycle.completed_at,
            approval_revision_id=cycle.approved_revision_id,
            total_requirements=quorum.total_requirements,
            satisfied_requirements=quorum.satisfied_requirements,
            quorum_complete=quorum.complete,
            actor_approvable_requirement_ids=tuple(actor_requirement_ids),
            actor_approvable_request_line_ids=tuple(actor_line_ids),
            requirements=tuple(projected_requirements),
        )
