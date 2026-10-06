from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
import json

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.application.project_managers import ProjectManagerResolutionService
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
)

from .delivery_models import DeliveryItemRow, DeliveryPlanRow
from .identity_models import AppUser
from .models import Project, WorkPackage
from .project_manager_resolution_repository import SqlProjectManagerResolutionRepository
from .verification_execution_models import (
    VerificationEvidenceLinkRow,
    VerificationExecutorAssignmentRow,
    VerificationTestExecutionRow,
)
from .verification_models import (
    StoryVerificationDecisionRequirementRow,
    StoryVerificationDecisionRow,
    VerificationRequirementRow,
    VerificationRetestRequestRow,
    VerificationScopeRow,
)
from .verification_repository import SqlVerificationRepository


class SqlVerificationExecutionRepository:
    """Persistence adapter for #363D; caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._verification = SqlVerificationRepository(session)

    def work_package_exists(self, work_package_id: str) -> bool:
        return self._session.get(WorkPackage, str(work_package_id).strip()) is not None

    def get_scope_for_work_package(
        self, work_package_id: str
    ) -> VerificationScope | None:
        return self._verification.get_scope_for_work_package(work_package_id)

    def acquire_document_read_guards(self, work_package_id: str) -> bool:
        """Hold a coherent document snapshot in ADR-023 lock order.

        SQL Server gets update/serializable row locks. Other dialects ignore the
        MSSQL hints while retaining the same read ordering for local tests.
        """

        normalized = str(work_package_id).strip()
        work_package = self._session.scalar(
            select(WorkPackage.id)
            .where(WorkPackage.id == normalized)
            .with_hint(
                WorkPackage,
                "WITH (UPDLOCK, HOLDLOCK)",
                dialect_name="mssql",
            )
        )
        if work_package is None:
            return False
        tuple(
            self._session.scalars(
                select(DeliveryPlanRow.id)
                .where(DeliveryPlanRow.work_package_id == normalized)
                .order_by(DeliveryPlanRow.id)
                .with_hint(
                    DeliveryPlanRow,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
            ).all()
        )
        tuple(
            self._session.scalars(
                select(VerificationScopeRow.id)
                .where(VerificationScopeRow.work_package_id == normalized)
                .with_hint(
                    VerificationScopeRow,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
            ).all()
        )
        return True

    @staticmethod
    def _serialized_dt(value: object | None) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else None

    def document_context(self, work_package_id: str) -> dict[str, object]:
        normalized = str(work_package_id).strip()
        work_package = self._session.get(WorkPackage, normalized)
        if work_package is None:
            raise KeyError(f"WorkPackage not found: {normalized}")
        project = self._session.get(Project, work_package.project_id)

        plan_ids = tuple(
            self._session.scalars(
                select(DeliveryPlanRow.id)
                .where(DeliveryPlanRow.work_package_id == normalized)
                .order_by(DeliveryPlanRow.created_at, DeliveryPlanRow.id)
            ).all()
        )
        stories: dict[str, dict[str, object]] = {}
        if plan_ids:
            story_rows = tuple(
                self._session.scalars(
                    select(DeliveryItemRow)
                    .where(
                        DeliveryItemRow.delivery_plan_id.in_(plan_ids),
                        DeliveryItemRow.item_type == "STORY",
                    )
                    .order_by(DeliveryItemRow.created_at, DeliveryItemRow.id)
                ).all()
            )
            epic_ids = tuple(
                sorted({row.parent_id for row in story_rows if row.parent_id})
            )
            epic_rows = (
                {
                    row.id: row
                    for row in self._session.scalars(
                        select(DeliveryItemRow).where(
                            DeliveryItemRow.id.in_(epic_ids),
                            DeliveryItemRow.item_type == "EPIC",
                        )
                    ).all()
                }
                if epic_ids
                else {}
            )
            for row in story_rows:
                epic = epic_rows.get(row.parent_id) if row.parent_id else None
                stories[row.id] = {
                    "id": row.id,
                    "title": row.title,
                    "epic_id": epic.id if epic is not None else None,
                    "epic_title": epic.title if epic is not None else None,
                }

        scope = self._verification.get_scope_for_work_package(normalized)
        decisions: list[dict[str, object]] = []
        if scope is not None:
            decision_rows = tuple(
                self._session.scalars(
                    select(StoryVerificationDecisionRow)
                    .where(
                        StoryVerificationDecisionRow.verification_scope_id
                        == scope.id
                    )
                    .order_by(
                        StoryVerificationDecisionRow.story_id,
                        StoryVerificationDecisionRow.created_at,
                        StoryVerificationDecisionRow.id,
                    )
                ).all()
            )
            decision_ids = tuple(row.id for row in decision_rows)
            requirement_ids_by_decision: dict[str, list[str]] = {
                decision_id: [] for decision_id in decision_ids
            }
            if decision_ids:
                links = self._session.execute(
                    select(
                        StoryVerificationDecisionRequirementRow.decision_id,
                        StoryVerificationDecisionRequirementRow.requirement_id,
                    )
                    .where(
                        StoryVerificationDecisionRequirementRow.decision_id.in_(
                            decision_ids
                        )
                    )
                    .order_by(
                        StoryVerificationDecisionRequirementRow.decision_id,
                        StoryVerificationDecisionRequirementRow.requirement_id,
                    )
                ).all()
                for decision_id, requirement_id in links:
                    requirement_ids_by_decision[str(decision_id)].append(
                        str(requirement_id)
                    )
            decisions = [
                {
                    "id": row.id,
                    "story_id": row.story_id,
                    "kind": row.kind,
                    "justification": row.justification,
                    "created_at": self._serialized_dt(row.created_at),
                    "requirement_ids": requirement_ids_by_decision.get(row.id, []),
                }
                for row in decision_rows
            ]

        return {
            "project": {
                "id": project.id if project is not None else work_package.project_id,
                "number": project.number if project is not None else None,
                "name": project.name if project is not None else None,
                "client": project.client if project is not None else None,
            },
            "work_package": {
                "id": work_package.id,
                "code": work_package.code,
                "name": work_package.name,
            },
            "stories": stories,
            "story_decisions": decisions,
        }

    def get_requirement(
        self, requirement_id: str
    ) -> VerificationRequirement | None:
        return self._verification.get_requirement(requirement_id)

    def get_scope_for_requirement(
        self, requirement_id: str
    ) -> tuple[VerificationScope, VerificationRequirement] | None:
        requirement = self.get_requirement(requirement_id)
        if requirement is None:
            return None
        scope = self._verification.get_scope(requirement.verification_scope_id)
        if scope is None:
            return None
        return scope, requirement

    def get_revision(self, revision_id: str):
        return self._verification.get_revision(revision_id)

    def list_requirements(
        self, scope_id: str
    ) -> tuple[VerificationRequirement, ...]:
        rows = self._session.scalars(
            select(VerificationRequirementRow)
            .where(
                VerificationRequirementRow.verification_scope_id
                == str(scope_id).strip()
            )
            .order_by(
                VerificationRequirementRow.created_at,
                VerificationRequirementRow.id,
            )
        ).all()
        return tuple(
            VerificationRequirement(
                id=row.id,
                verification_scope_id=row.verification_scope_id,
                story_id=row.story_id,
                phase=VerificationPhase(row.phase),
                current_revision_id=row.current_revision_id,
                state=VerificationRequirementState(row.state),
                withdrawal_reason=row.withdrawal_reason,
            )
            for row in rows
        )

    def project_scope_authorized(
        self, work_package_id: str, actor_user_id: str
    ) -> bool:
        work_package = self._session.get(
            WorkPackage, str(work_package_id).strip()
        )
        if work_package is None:
            return False
        resolved = ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(self._session)
        ).resolve_projects((work_package.project_id,)).get(work_package.project_id)
        if resolved is None:
            return False
        managers = tuple(
            manager
            for manager in (resolved.primary, *resolved.co_managers)
            if manager is not None
        )
        actor = str(actor_user_id or "").strip()
        return any(
            manager.app_user_id == actor
            and manager.user_active is True
            and manager.contact_active is not False
            for manager in managers
        )

    def active_user_exists(self, user_id: str) -> bool:
        row = self._session.get(AppUser, str(user_id).strip())
        return bool(row is not None and row.active)

    def has_active_assignment(
        self, requirement_id: str, executor_user_id: str
    ) -> bool:
        return (
            self._session.scalar(
                select(VerificationExecutorAssignmentRow.id)
                .where(
                    VerificationExecutorAssignmentRow.requirement_id
                    == str(requirement_id).strip(),
                    VerificationExecutorAssignmentRow.executor_user_id
                    == str(executor_user_id).strip(),
                    VerificationExecutorAssignmentRow.active == true(),
                )
                .limit(1)
            )
            is not None
        )

    def has_any_active_assignment(
        self, scope_id: str, executor_user_id: str
    ) -> bool:
        return (
            self._session.scalar(
                select(VerificationExecutorAssignmentRow.id)
                .join(
                    VerificationRequirementRow,
                    VerificationRequirementRow.id
                    == VerificationExecutorAssignmentRow.requirement_id,
                )
                .where(
                    VerificationRequirementRow.verification_scope_id
                    == str(scope_id).strip(),
                    VerificationExecutorAssignmentRow.executor_user_id
                    == str(executor_user_id).strip(),
                    VerificationExecutorAssignmentRow.active == true(),
                )
                .limit(1)
            )
            is not None
        )

    def assign_executor(
        self,
        assignment: VerificationExecutorAssignment,
    ) -> VerificationExecutorAssignment:
        existing = self._session.scalar(
            select(VerificationExecutorAssignmentRow)
            .where(
                VerificationExecutorAssignmentRow.requirement_id
                == assignment.requirement_id,
                VerificationExecutorAssignmentRow.executor_user_id
                == assignment.executor_user_id,
                VerificationExecutorAssignmentRow.active == true(),
            )
            .order_by(VerificationExecutorAssignmentRow.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return VerificationExecutorAssignment(
                id=existing.id,
                requirement_id=existing.requirement_id,
                executor_user_id=existing.executor_user_id,
                assigned_by_user_id=existing.assigned_by_user_id,
            )
        self._session.add(
            VerificationExecutorAssignmentRow(
                id=assignment.id,
                requirement_id=assignment.requirement_id,
                executor_user_id=assignment.executor_user_id,
                assigned_by_user_id=assignment.assigned_by_user_id,
                active=True,
            )
        )
        self._session.flush()
        return assignment

    def unassign_executor(
        self,
        requirement_id: str,
        executor_user_id: str,
        *,
        ended_by_user_id: str,
    ) -> bool:
        row = self._session.scalar(
            select(VerificationExecutorAssignmentRow)
            .where(
                VerificationExecutorAssignmentRow.requirement_id
                == str(requirement_id).strip(),
                VerificationExecutorAssignmentRow.executor_user_id
                == str(executor_user_id).strip(),
                VerificationExecutorAssignmentRow.active == true(),
            )
            .order_by(VerificationExecutorAssignmentRow.created_at.desc())
            .limit(1)
        )
        if row is None:
            return False
        from .base import utc_now

        row.active = False
        row.ended_by_user_id = str(ended_by_user_id).strip()
        row.ended_at = utc_now()
        self._session.flush()
        return True

    def list_active_executor_ids(
        self, requirement_id: str
    ) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(VerificationExecutorAssignmentRow.executor_user_id)
                .where(
                    VerificationExecutorAssignmentRow.requirement_id
                    == str(requirement_id).strip(),
                    VerificationExecutorAssignmentRow.active == true(),
                )
                .order_by(VerificationExecutorAssignmentRow.executor_user_id)
            ).all()
        )

    def latest_execution_sequence(
        self,
        requirement_id: str,
        *,
        revision_id: str | None = None,
    ) -> int:
        query = select(func.max(VerificationTestExecutionRow.sequence)).where(
            VerificationTestExecutionRow.requirement_id
            == str(requirement_id).strip()
        )
        if revision_id is not None:
            query = query.where(
                VerificationTestExecutionRow.revision_id
                == str(revision_id).strip()
            )
        return int(self._session.scalar(query) or 0)

    def add_execution(self, execution: VerificationExecution) -> None:
        if not self.has_active_assignment(
            execution.requirement_id,
            execution.executor_user_id,
        ):
            raise ValueError("Executor is not actively assigned to this requirement")
        self._session.add(
            VerificationTestExecutionRow(
                id=execution.id,
                requirement_id=execution.requirement_id,
                revision_id=execution.revision_id,
                sequence=execution.sequence,
                result=execution.result.value,
                executor_user_id=execution.executor_user_id,
                executed_at=execution.executed_at,
                measurements_json=json.dumps(
                    dict(execution.measurements or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                comments=execution.comments,
            )
        )
        self._session.flush()

    def list_executions(
        self, requirement_id: str
    ) -> tuple[VerificationExecution, ...]:
        rows = self._session.scalars(
            select(VerificationTestExecutionRow)
            .where(
                VerificationTestExecutionRow.requirement_id
                == str(requirement_id).strip()
            )
            .order_by(VerificationTestExecutionRow.sequence)
        ).all()
        executions: list[VerificationExecution] = []
        for row in rows:
            try:
                measurements = json.loads(row.measurements_json or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Verification execution measurements are invalid: {row.id}"
                ) from exc
            if not isinstance(measurements, dict):
                raise ValueError(
                    f"Verification execution measurements are invalid: {row.id}"
                )
            executions.append(
                VerificationExecution(
                    id=row.id,
                    requirement_id=row.requirement_id,
                    revision_id=row.revision_id,
                    sequence=row.sequence,
                    result=row.result,
                    executor_user_id=row.executor_user_id,
                    executed_at=row.executed_at,
                    measurements=measurements,
                    comments=row.comments,
                    recorded_at=row.recorded_at,
                )
            )
        return tuple(executions)

    def get_execution_context(
        self, execution_id: str
    ) -> tuple[VerificationScope, VerificationRequirement, VerificationExecution] | None:
        row = self._session.get(
            VerificationTestExecutionRow, str(execution_id).strip()
        )
        if row is None:
            return None
        context = self.get_scope_for_requirement(row.requirement_id)
        if context is None:
            return None
        execution = next(
            (
                candidate
                for candidate in self.list_executions(row.requirement_id)
                if candidate.id == row.id
            ),
            None,
        )
        if execution is None:
            return None
        scope, requirement = context
        return scope, requirement, execution

    def add_evidence(self, evidence: VerificationEvidenceLink) -> None:
        if self._session.get(
            VerificationTestExecutionRow, evidence.execution_id
        ) is None:
            raise KeyError(f"Verification execution not found: {evidence.execution_id}")
        self._session.add(
            VerificationEvidenceLinkRow(
                id=evidence.id,
                execution_id=evidence.execution_id,
                url=evidence.url,
                label=evidence.label,
                provenance=evidence.provenance,
                added_by_user_id=evidence.added_by_user_id,
            )
        )
        self._session.flush()

    def list_evidence(
        self, execution_id: str
    ) -> tuple[VerificationEvidenceLink, ...]:
        rows = self._session.scalars(
            select(VerificationEvidenceLinkRow)
            .where(
                VerificationEvidenceLinkRow.execution_id
                == str(execution_id).strip()
            )
            .order_by(
                VerificationEvidenceLinkRow.created_at,
                VerificationEvidenceLinkRow.id,
            )
        ).all()
        return tuple(
            VerificationEvidenceLink(
                id=row.id,
                execution_id=row.execution_id,
                url=row.url,
                label=row.label,
                provenance=row.provenance,
                added_by_user_id=row.added_by_user_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    def add_retest_request(self, request: VerificationRetestRequest) -> None:
        self._verification.add_retest_request(request)

    def latest_retest_after_sequence(
        self,
        requirement_id: str,
        revision_id: str,
    ) -> int:
        return int(
            self._session.scalar(
                select(func.max(VerificationRetestRequestRow.after_execution_sequence))
                .where(
                    VerificationRetestRequestRow.requirement_id
                    == str(requirement_id).strip(),
                    VerificationRetestRequestRow.revision_id
                    == str(revision_id).strip(),
                )
            )
            or 0
        )

    def story_decision_coverage(
        self,
        work_package_id: str,
        scope_id: str | None,
    ) -> dict[str, object]:
        done_story_ids = tuple(
            self._session.scalars(
                select(DeliveryItemRow.id)
                .join(
                    DeliveryPlanRow,
                    DeliveryPlanRow.id == DeliveryItemRow.delivery_plan_id,
                )
                .where(
                    DeliveryPlanRow.work_package_id
                    == str(work_package_id).strip(),
                    DeliveryItemRow.item_type == "STORY",
                    DeliveryItemRow.status == "DONE",
                )
                .order_by(DeliveryItemRow.id)
            ).all()
        )
        documented: set[str] = set()
        if scope_id is not None and done_story_ids:
            documented.update(
                self._session.scalars(
                    select(StoryVerificationDecisionRow.story_id)
                    .where(
                        StoryVerificationDecisionRow.verification_scope_id
                        == str(scope_id).strip(),
                        StoryVerificationDecisionRow.story_id.in_(done_story_ids),
                    )
                    .distinct()
                ).all()
            )
        undocumented = tuple(
            story_id for story_id in done_story_ids if story_id not in documented
        )
        total = len(done_story_ids)
        documented_count = total - len(undocumented)
        return {
            "done_story_count": total,
            "documented_done_story_count": documented_count,
            "undocumented_done_story_ids": list(undocumented),
            "complete": not undocumented,
            "coverage_rate": documented_count / total if total else None,
        }

    def compare_and_increment_version(
        self,
        scope_id: str,
        *,
        expected_verification_version: int,
    ) -> int:
        return self._verification.compare_and_increment_version(
            scope_id,
            expected_verification_version=expected_verification_version,
        )

    def append_history(self, **kwargs: Any) -> str:
        return self._verification.append_history(**kwargs)

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
        return self._verification.replay_or_execute_mutation(
            scope_id=scope_id,
            actor_user_id=actor_user_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            action=action,
        )
