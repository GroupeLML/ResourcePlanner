from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from app.domain.verification import (
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementState,
    VerificationRetestRequest,
    VerificationScope,
)
from app.domain.verification_execution import (
    VerificationEvidenceLink,
    VerificationExecution,
    VerificationExecutorAssignment,
    VerificationMeasure,
    normalize_verification_measurements,
    project_requirement_status,
)

from .errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from .security import AuthPrincipal
from .verification_contracts import VerificationAction
from .verification_security import authorized_verification_actions_for


class VerificationExecutionRepositoryPort(Protocol):
    def work_package_exists(self, work_package_id: str) -> bool: ...
    def get_scope_for_work_package(self, work_package_id: str) -> VerificationScope | None: ...
    def get_requirement(self, requirement_id: str) -> VerificationRequirement | None: ...
    def get_scope_for_requirement(
        self, requirement_id: str
    ) -> tuple[VerificationScope, VerificationRequirement] | None: ...
    def get_revision(self, revision_id: str): ...
    def list_requirements(self, scope_id: str) -> tuple[VerificationRequirement, ...]: ...
    def project_scope_authorized(self, work_package_id: str, actor_user_id: str) -> bool: ...
    def active_user_exists(self, user_id: str) -> bool: ...
    def has_active_assignment(self, requirement_id: str, executor_user_id: str) -> bool: ...
    def has_any_active_assignment(self, scope_id: str, executor_user_id: str) -> bool: ...
    def assign_executor(
        self, assignment: VerificationExecutorAssignment
    ) -> VerificationExecutorAssignment: ...
    def unassign_executor(
        self,
        requirement_id: str,
        executor_user_id: str,
        *,
        ended_by_user_id: str,
    ) -> bool: ...
    def list_active_executor_ids(self, requirement_id: str) -> tuple[str, ...]: ...
    def latest_execution_sequence(
        self, requirement_id: str, *, revision_id: str | None = None
    ) -> int: ...
    def add_execution(self, execution: VerificationExecution) -> None: ...
    def list_executions(self, requirement_id: str) -> tuple[VerificationExecution, ...]: ...
    def get_execution_context(
        self, execution_id: str
    ) -> tuple[VerificationScope, VerificationRequirement, VerificationExecution] | None: ...
    def add_evidence(self, evidence: VerificationEvidenceLink) -> None: ...
    def list_evidence(self, execution_id: str) -> tuple[VerificationEvidenceLink, ...]: ...
    def add_retest_request(self, request: VerificationRetestRequest) -> None: ...
    def latest_retest_after_sequence(self, requirement_id: str, revision_id: str) -> int: ...
    def story_decision_coverage(
        self, work_package_id: str, scope_id: str | None
    ) -> dict[str, object]: ...
    def compare_and_increment_version(
        self, scope_id: str, *, expected_verification_version: int
    ) -> int: ...
    def append_history(self, **kwargs: Any) -> str: ...
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


class VerificationExecutionService:
    def __init__(self, repository: VerificationExecutionRepositoryPort) -> None:
        self._repository = repository

    @staticmethod
    def _actor(principal: AuthPrincipal) -> str:
        actor = str(principal.local_user_id or "").strip()
        if not actor:
            raise ApplicationAuthorizationError(
                "Une identité AppUser stable est requise pour Verification.",
                code="verification_identity_required",
            )
        return actor

    def _actions(
        self,
        scope: VerificationScope,
        principal: AuthPrincipal,
        *,
        requirement_id: str | None = None,
    ) -> frozenset[VerificationAction]:
        actor = self._actor(principal)
        return authorized_verification_actions_for(
            principal,
            scope,
            project_scope_authorized=self._repository.project_scope_authorized(
                scope.work_package_id, actor
            ),
            assigned_executor=(
                requirement_id is not None
                and self._repository.has_active_assignment(requirement_id, actor)
            ),
        )

    def _require_requirement_action(
        self,
        requirement_id: str,
        principal: AuthPrincipal,
        action: VerificationAction,
    ) -> tuple[str, VerificationScope, VerificationRequirement]:
        actor = self._actor(principal)
        context = self._repository.get_scope_for_requirement(requirement_id)
        if context is None:
            raise ApplicationNotFoundError(
                "Exigence Verification introuvable.",
                code="verification_requirement_not_found",
                context={"requirement_id": requirement_id},
            )
        scope, requirement = context
        if action not in self._actions(
            scope, principal, requirement_id=requirement.id
        ):
            raise ApplicationAuthorizationError(
                "Action Verification non autorisée.",
                code="verification_action_denied",
                context={
                    "required_action": action.value,
                    "requirement_id": requirement.id,
                },
            )
        return actor, scope, requirement

    def _next_version(
        self,
        scope: VerificationScope,
        expected_verification_version: int,
    ) -> int:
        try:
            return self._repository.compare_and_increment_version(
                scope.id,
                expected_verification_version=expected_verification_version,
            )
        except RuntimeError as exc:
            raise ApplicationConflictError(
                "Le périmètre Verification a été modifié depuis sa lecture.",
                code="verification_version_conflict",
                context={
                    "verification_scope_id": scope.id,
                    "expected_verification_version": expected_verification_version,
                },
            ) from exc

    def assign_executor(
        self,
        requirement_id: str,
        *,
        executor_user_id: str,
        expected_verification_version: int,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor, scope, requirement = self._require_requirement_action(
            requirement_id, principal, VerificationAction.ASSIGN_EXECUTORS
        )
        executor = str(executor_user_id or "").strip()
        if not executor or not self._repository.active_user_exists(executor):
            raise ApplicationValidationError(
                "L'exécutant doit être un AppUser actif.",
                code="verification_executor_invalid",
                context={"executor_user_id": executor},
            )
        payload = {
            "requirement_id": requirement.id,
            "executor_user_id": executor,
            "expected_verification_version": expected_verification_version,
        }

        def action() -> dict[str, Any]:
            if self._repository.has_active_assignment(requirement.id, executor):
                return {
                    "requirement_id": requirement.id,
                    "executor_user_id": executor,
                    "verification_version": scope.verification_version,
                    "already_assigned": True,
                }
            version = self._next_version(scope, expected_verification_version)
            assignment = self._repository.assign_executor(
                VerificationExecutorAssignment(
                    id=str(uuid4()),
                    requirement_id=requirement.id,
                    executor_user_id=executor,
                    assigned_by_user_id=actor,
                )
            )
            self._repository.append_history(
                scope_id=scope.id,
                entity_type="VerificationRequirement",
                entity_id=requirement.id,
                story_id=requirement.story_id,
                actor_user_id=actor,
                action="EXECUTOR_ASSIGNED",
                verification_version=version,
                details={"executor_user_id": executor, "assignment_id": assignment.id},
            )
            return {
                "requirement_id": requirement.id,
                "executor_user_id": executor,
                "assignment_id": assignment.id,
                "verification_version": version,
                "already_assigned": False,
            }

        return self._repository.replay_or_execute_mutation(
            scope_id=scope.id,
            actor_user_id=actor,
            command_scope=f"assign_executor:{requirement.id}",
            idempotency_key=idempotency_key,
            request_payload=payload,
            action=action,
        )

    def unassign_executor(
        self,
        requirement_id: str,
        *,
        executor_user_id: str,
        reason: str,
        expected_verification_version: int,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor, scope, requirement = self._require_requirement_action(
            requirement_id, principal, VerificationAction.ASSIGN_EXECUTORS
        )
        executor = str(executor_user_id or "").strip()
        normalized_reason = str(reason or "").strip()
        if not executor or not normalized_reason:
            raise ApplicationValidationError(
                "L'exécutant et le motif de retrait sont requis.",
                code="verification_unassignment_invalid",
            )
        payload = {
            "requirement_id": requirement.id,
            "executor_user_id": executor,
            "reason": normalized_reason,
            "expected_verification_version": expected_verification_version,
        }

        def action() -> dict[str, Any]:
            if not self._repository.has_active_assignment(requirement.id, executor):
                return {
                    "requirement_id": requirement.id,
                    "executor_user_id": executor,
                    "verification_version": scope.verification_version,
                    "already_unassigned": True,
                }
            version = self._next_version(scope, expected_verification_version)
            self._repository.unassign_executor(
                requirement.id, executor, ended_by_user_id=actor
            )
            self._repository.append_history(
                scope_id=scope.id,
                entity_type="VerificationRequirement",
                entity_id=requirement.id,
                story_id=requirement.story_id,
                actor_user_id=actor,
                action="EXECUTOR_UNASSIGNED",
                verification_version=version,
                details={"executor_user_id": executor, "reason": normalized_reason},
            )
            return {
                "requirement_id": requirement.id,
                "executor_user_id": executor,
                "verification_version": version,
                "already_unassigned": False,
            }

        return self._repository.replay_or_execute_mutation(
            scope_id=scope.id,
            actor_user_id=actor,
            command_scope=f"unassign_executor:{requirement.id}",
            idempotency_key=idempotency_key,
            request_payload=payload,
            action=action,
        )

    def record_execution(
        self,
        requirement_id: str,
        *,
        result: str,
        executed_at: datetime | None,
        measurements: Mapping[str, VerificationMeasure] | None,
        comments: str | None,
        expected_verification_version: int,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor, scope, requirement = self._require_requirement_action(
            requirement_id, principal, VerificationAction.RECORD_RESULT
        )
        if requirement.state is not VerificationRequirementState.ACTIVE:
            raise ApplicationValidationError(
                "Une exigence retirée ne peut pas être exécutée.",
                code="verification_requirement_withdrawn",
                context={"requirement_id": requirement.id},
            )
        try:
            normalized_measurements = normalize_verification_measurements(measurements)
            execution = VerificationExecution(
                id=str(uuid4()),
                requirement_id=requirement.id,
                revision_id=requirement.current_revision_id,
                sequence=1,
                result=str(result).strip().upper(),
                executor_user_id=actor,
                executed_at=executed_at,
                measurements=normalized_measurements,
                comments=comments,
            )
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc), code="verification_execution_invalid"
            ) from exc

        payload = {
            "requirement_id": requirement.id,
            "result": execution.result.value,
            "executed_at": executed_at.isoformat() if executed_at else None,
            "measurements": normalized_measurements,
            "comments": execution.comments,
            "expected_verification_version": expected_verification_version,
        }

        def action() -> dict[str, Any]:
            version = self._next_version(scope, expected_verification_version)
            sequence = self._repository.latest_execution_sequence(requirement.id) + 1
            persisted = VerificationExecution(
                id=execution.id,
                requirement_id=requirement.id,
                revision_id=requirement.current_revision_id,
                sequence=sequence,
                result=execution.result,
                executor_user_id=actor,
                executed_at=execution.executed_at,
                measurements=normalized_measurements,
                comments=execution.comments,
            )
            self._repository.add_execution(persisted)
            self._repository.append_history(
                scope_id=scope.id,
                entity_type="TestExecution",
                entity_id=persisted.id,
                story_id=requirement.story_id,
                actor_user_id=actor,
                action="TEST_EXECUTION_RECORDED",
                verification_version=version,
                details={
                    "requirement_id": requirement.id,
                    "revision_id": requirement.current_revision_id,
                    "sequence": sequence,
                    "result": persisted.result.value,
                },
            )
            return {
                "execution_id": persisted.id,
                "requirement_id": requirement.id,
                "revision_id": requirement.current_revision_id,
                "sequence": sequence,
                "result": persisted.result.value,
                "verification_version": version,
            }

        return self._repository.replay_or_execute_mutation(
            scope_id=scope.id,
            actor_user_id=actor,
            command_scope=f"record_execution:{requirement.id}",
            idempotency_key=idempotency_key,
            request_payload=payload,
            action=action,
        )

    def add_evidence_link(
        self,
        execution_id: str,
        *,
        url: str,
        label: str | None,
        provenance: str | None,
        expected_verification_version: int,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor = self._actor(principal)
        context = self._repository.get_execution_context(execution_id)
        if context is None:
            raise ApplicationNotFoundError(
                "Exécution Verification introuvable.",
                code="verification_execution_not_found",
                context={"execution_id": execution_id},
            )
        scope, requirement, execution = context
        if VerificationAction.ADD_EVIDENCE not in self._actions(
            scope, principal, requirement_id=requirement.id
        ):
            raise ApplicationAuthorizationError(
                "Ajout de preuve Verification non autorisé.",
                code="verification_action_denied",
                context={"required_action": VerificationAction.ADD_EVIDENCE.value},
            )
        try:
            evidence = VerificationEvidenceLink(
                id=str(uuid4()),
                execution_id=execution.id,
                url=url,
                label=label,
                provenance=str(provenance or "").strip() or "external_https",
                added_by_user_id=actor,
            )
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc), code="verification_evidence_invalid"
            ) from exc
        payload = {
            "execution_id": execution.id,
            "url": evidence.url,
            "label": evidence.label,
            "provenance": evidence.provenance,
            "expected_verification_version": expected_verification_version,
        }

        def action() -> dict[str, Any]:
            version = self._next_version(scope, expected_verification_version)
            self._repository.add_evidence(evidence)
            self._repository.append_history(
                scope_id=scope.id,
                entity_type="TestExecution",
                entity_id=execution.id,
                story_id=requirement.story_id,
                actor_user_id=actor,
                action="EVIDENCE_LINK_ADDED",
                verification_version=version,
                details={
                    "evidence_id": evidence.id,
                    "url": evidence.url,
                    "provenance": evidence.provenance,
                },
            )
            return {
                "evidence_id": evidence.id,
                "execution_id": execution.id,
                "url": evidence.url,
                "verification_version": version,
            }

        return self._repository.replay_or_execute_mutation(
            scope_id=scope.id,
            actor_user_id=actor,
            command_scope=f"add_evidence:{execution.id}",
            idempotency_key=idempotency_key,
            request_payload=payload,
            action=action,
        )

    def request_retest(
        self,
        requirement_id: str,
        *,
        reason: str,
        expected_verification_version: int,
        idempotency_key: str | None,
        principal: AuthPrincipal,
    ) -> dict[str, Any]:
        actor, scope, requirement = self._require_requirement_action(
            requirement_id, principal, VerificationAction.REQUEST_RETEST
        )
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ApplicationValidationError(
                "Un motif de retest est requis.",
                code="verification_retest_reason_required",
            )
        latest = self._repository.latest_execution_sequence(
            requirement.id, revision_id=requirement.current_revision_id
        )
        if latest < 1:
            raise ApplicationValidationError(
                "Un retest exige une exécution préalable de la révision courante.",
                code="verification_retest_without_execution",
            )
        payload = {
            "requirement_id": requirement.id,
            "revision_id": requirement.current_revision_id,
            "after_execution_sequence": latest,
            "reason": normalized_reason,
            "expected_verification_version": expected_verification_version,
        }

        def action() -> dict[str, Any]:
            version = self._next_version(scope, expected_verification_version)
            request = VerificationRetestRequest(
                id=str(uuid4()),
                requirement_id=requirement.id,
                revision_id=requirement.current_revision_id,
                after_execution_sequence=latest,
                reason=normalized_reason,
            )
            self._repository.add_retest_request(request)
            self._repository.append_history(
                scope_id=scope.id,
                entity_type="VerificationRequirement",
                entity_id=requirement.id,
                story_id=requirement.story_id,
                actor_user_id=actor,
                action="RETEST_REQUESTED",
                verification_version=version,
                details={
                    "retest_request_id": request.id,
                    "revision_id": request.revision_id,
                    "after_execution_sequence": latest,
                    "reason": normalized_reason,
                },
            )
            return {
                "retest_request_id": request.id,
                "requirement_id": requirement.id,
                "revision_id": request.revision_id,
                "after_execution_sequence": latest,
                "verification_version": version,
            }

        return self._repository.replay_or_execute_mutation(
            scope_id=scope.id,
            actor_user_id=actor,
            command_scope=f"request_retest:{requirement.id}",
            idempotency_key=idempotency_key,
            request_payload=payload,
            action=action,
        )

    @staticmethod
    def _dt(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def package(
        self, work_package_id: str, *, principal: AuthPrincipal
    ) -> dict[str, Any]:
        actor = self._actor(principal)
        if not self._repository.work_package_exists(work_package_id):
            raise ApplicationNotFoundError(
                "WorkPackage introuvable.",
                code="work_package_not_found",
                context={"work_package_id": work_package_id},
            )
        scope = self._repository.get_scope_for_work_package(work_package_id)
        conceptual_scope = scope or VerificationScope(
            id=f"pending:{work_package_id}",
            work_package_id=work_package_id,
        )
        actions = authorized_verification_actions_for(
            principal,
            conceptual_scope,
            project_scope_authorized=self._repository.project_scope_authorized(
                work_package_id, actor
            ),
            assigned_executor=(
                scope is not None
                and self._repository.has_any_active_assignment(scope.id, actor)
            ),
        )
        if VerificationAction.VIEW_SCOPE not in actions:
            raise ApplicationAuthorizationError(
                "Consultation du périmètre Verification non autorisée.",
                code="verification_scope_view_denied",
                context={"work_package_id": work_package_id},
            )

        coverage = self._repository.story_decision_coverage(
            work_package_id, scope.id if scope is not None else None
        )
        phase_summary = {
            phase.value: {
                "active": 0,
                "PASS": 0,
                "FAIL": 0,
                "BLOCKED": 0,
                "NOT_RUN": 0,
            }
            for phase in VerificationPhase
        }
        requirement_payloads: list[dict[str, Any]] = []
        pass_count = 0
        active_count = 0
        withdrawn_count = 0

        for requirement in (
            self._repository.list_requirements(scope.id)
            if scope is not None
            else ()
        ):
            revision = self._repository.get_revision(
                requirement.current_revision_id
            )
            executions = self._repository.list_executions(requirement.id)
            retest_after = self._repository.latest_retest_after_sequence(
                requirement.id, requirement.current_revision_id
            )
            active = requirement.state is VerificationRequirementState.ACTIVE
            projected = (
                project_requirement_status(
                    current_revision_id=requirement.current_revision_id,
                    executions=executions,
                    retest_after_sequence=retest_after,
                )
                if active
                else None
            )
            if active and projected is not None:
                active_count += 1
                phase = phase_summary[requirement.phase.value]
                phase["active"] += 1
                phase[projected.value] += 1
                if projected.value == "PASS":
                    pass_count += 1
            else:
                withdrawn_count += 1

            history: list[dict[str, Any]] = []
            for execution in executions:
                history.append(
                    {
                        "id": execution.id,
                        "revision_id": execution.revision_id,
                        "sequence": execution.sequence,
                        "result": execution.result.value,
                        "executor_user_id": execution.executor_user_id,
                        "executed_at": self._dt(execution.executed_at),
                        "recorded_at": self._dt(execution.recorded_at),
                        "measurements": dict(execution.measurements or {}),
                        "comments": execution.comments,
                        "evidence_links": [
                            {
                                "id": evidence.id,
                                "url": evidence.url,
                                "label": evidence.label,
                                "provenance": evidence.provenance,
                                "added_by_user_id": evidence.added_by_user_id,
                                "created_at": self._dt(evidence.created_at),
                            }
                            for evidence in self._repository.list_evidence(
                                execution.id
                            )
                        ],
                    }
                )
            current_candidates = [
                execution
                for execution in executions
                if execution.revision_id == requirement.current_revision_id
                and execution.sequence > retest_after
            ]
            latest_current = (
                max(current_candidates, key=lambda item: item.sequence)
                if current_candidates
                else None
            )
            requirement_payloads.append(
                {
                    "id": requirement.id,
                    "story_id": requirement.story_id,
                    "phase": requirement.phase.value,
                    "state": requirement.state.value,
                    "withdrawal_reason": requirement.withdrawal_reason,
                    "current_revision": (
                        {
                            "id": revision.id,
                            "revision_number": revision.revision_number,
                            "objective": revision.objective,
                            "method": revision.method,
                            "expected_result": revision.expected_result,
                            "prerequisites": list(revision.prerequisites),
                            "criticality": revision.criticality,
                        }
                        if revision is not None
                        else None
                    ),
                    "assigned_executor_ids": list(
                        self._repository.list_active_executor_ids(requirement.id)
                    ),
                    "projected_status": (
                        projected.value if projected is not None else None
                    ),
                    "retest_after_sequence": retest_after,
                    "latest_execution_id": (
                        latest_current.id if latest_current is not None else None
                    ),
                    "executions": history,
                }
            )

        return {
            "work_package_id": work_package_id,
            "verification_scope_id": scope.id if scope is not None else None,
            "verification_version": (
                scope.verification_version if scope is not None else None
            ),
            "lead_user_id": scope.lead_user_id if scope is not None else None,
            "allowed_actions": sorted(action.value for action in actions),
            "story_decision_coverage": coverage,
            "phases": phase_summary,
            "active_requirement_count": active_count,
            "withdrawn_requirement_count": withdrawn_count,
            "pass_rate": pass_count / active_count if active_count else None,
            "no_tests_defined": active_count == 0,
            "requirements": requirement_payloads,
        }
