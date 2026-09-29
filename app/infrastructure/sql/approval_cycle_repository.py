from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ...application.approval_progress import ApprovalUserSummaryRecord
from ...application.approval_voting import (
    APPROVAL_DECISION_APPROVE,
    ApprovalDecisionRecord,
    ApprovalQuorumProjection,
)
from ...application.approval_cycles import (
    ApprovalApproverSnapshot,
    ApprovalCycleRecord,
    ApprovalCycleRequestRecord,
    ApprovalCycleRoutingInput,
    ApprovalCycleSubjectRecord,
    ApprovalRequirementRecord,
    ApprovalRequirementSnapshot,
)
from ...application.errors import ApplicationConflictError
from ...domain.approval_cycles import (
    APPROVAL_CYCLE_STATE_COMPLETED,
    APPROVAL_CYCLE_STATE_INVALIDATED,
    APPROVAL_CYCLE_STATE_OPEN,
)
from .approval_cycle_models import (
    ApprovalDecision,
    ApprovalRequirement,
    ApprovalRequirementApprover,
    RequestApprovalCycle,
)
from .approval_revision_models import RequestApprovalReference
from .approval_revision_repository import SqlRequestApprovalRevisionRepository
from .approval_scope_models import TaskApprovalScopeMapping
from .base import new_id, utc_now
from .identity_models import AppUser
from .models import (
    RequestLine,
    WorkforceRequest,
    WorkforceRequestHistory,
)
from .request_version import acquire_request_aggregate_version


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _decode_sources(value: str | None) -> tuple[str, ...]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = []
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({str(item) for item in raw if str(item)}))


class SqlApprovalCycleRepository:
    def __init__(
        self,
        session: Session,
        *,
        actor_user_id: str | None = None,
        actor_name: str = "",
    ) -> None:
        self._session = session
        self._actor_user_id = _optional_text(actor_user_id)
        self._actor_name = _optional_text(actor_name)

    def get_request(
        self,
        request_id: str,
    ) -> ApprovalCycleRequestRecord | None:
        wanted = _text(request_id)
        row = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.id == wanted)
                | (WorkforceRequest.legacy_demand_number == wanted)
            )
        )
        if row is None:
            return None
        return ApprovalCycleRequestRecord(
            id=row.id,
            number=_optional_text(row.legacy_demand_number) or row.id,
            status=_text(row.status),
            aggregate_version=max(int(row.aggregate_version or 1), 1),
            project_id=row.project_id,
            priority=_optional_text(row.priority),
            site_client=_optional_text(row.site_client),
            location=_optional_text(row.location),
            line_mode=bool(row.line_mode),
            approved_at=row.approved_at,
        )

    def list_active_line_ids(self, request_id: str) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(RequestLine.id)
                .where(
                    RequestLine.workforce_request_id == _text(request_id),
                    RequestLine.active == true(),
                )
                .order_by(RequestLine.id)
            ).all()
        )

    def read_subject(self, request_id: str) -> ApprovalCycleSubjectRecord:
        request = self._session.get(WorkforceRequest, _text(request_id))
        if request is None:
            raise KeyError(f"Demande {request_id} introuvable")
        envelope = SqlRequestApprovalRevisionRepository(
            self._session
        ).candidate_envelope(request)
        return ApprovalCycleSubjectRecord(
            request_id=request.id,
            project_id=request.project_id,
            priority=_optional_text(request.priority),
            site_client=_optional_text(request.site_client),
            location=_optional_text(request.location),
            line_mode=bool(request.line_mode),
            authorization_entries=tuple(
                entry.authorization_payload()
                for entry in envelope.entries
            ),
        )

    def read_routing_inputs(
        self,
        request_id: str,
    ) -> tuple[ApprovalCycleRoutingInput, ...]:
        lines = self._session.scalars(
            select(RequestLine)
            .where(
                RequestLine.workforce_request_id == _text(request_id),
                RequestLine.active == true(),
            )
            .order_by(RequestLine.id)
        ).all()
        task_ids = {
            line.task_catalog_item_id
            for line in lines
            if line.task_catalog_item_id
        }
        mappings: dict[str, list[str]] = defaultdict(list)
        if task_ids:
            rows = self._session.execute(
                select(
                    TaskApprovalScopeMapping.task_catalog_item_id,
                    TaskApprovalScopeMapping.approval_scope_id,
                )
                .where(
                    TaskApprovalScopeMapping.task_catalog_item_id.in_(
                        task_ids
                    )
                )
                .order_by(
                    TaskApprovalScopeMapping.task_catalog_item_id,
                    TaskApprovalScopeMapping.approval_scope_id,
                )
            ).all()
            for task_id, scope_id in rows:
                mappings[task_id].append(scope_id)

        return tuple(
            ApprovalCycleRoutingInput(
                request_line_id=line.id,
                task_catalog_item_id=line.task_catalog_item_id,
                approval_scope_ids=tuple(
                    sorted(
                        set(
                            mappings.get(
                                line.task_catalog_item_id or "",
                                [],
                            )
                        )
                    )
                ),
                proposed_resource_id=_optional_text(
                    line.proposed_resource_id
                ),
            )
            for line in lines
        )

    def _cycle_requirements(
        self,
        cycle_id: str,
    ) -> tuple[ApprovalRequirementRecord, ...]:
        requirements = self._session.scalars(
            select(ApprovalRequirement)
            .where(
                ApprovalRequirement.approval_cycle_id == cycle_id
            )
            .order_by(
                ApprovalRequirement.request_line_id,
                ApprovalRequirement.id,
            )
        ).all()
        requirement_ids = {row.id for row in requirements}
        approvers_by_requirement: dict[
            str,
            list[ApprovalApproverSnapshot],
        ] = defaultdict(list)
        if requirement_ids:
            rows = self._session.scalars(
                select(ApprovalRequirementApprover)
                .where(
                    ApprovalRequirementApprover.requirement_id.in_(
                        requirement_ids
                    )
                )
                .order_by(
                    ApprovalRequirementApprover.requirement_id,
                    ApprovalRequirementApprover.app_user_id,
                )
            ).all()
            for row in rows:
                approvers_by_requirement[row.requirement_id].append(
                    ApprovalApproverSnapshot(
                        app_user_id=row.app_user_id,
                        sources=_decode_sources(row.sources_text),
                    )
                )

        return tuple(
            ApprovalRequirementRecord(
                id=row.id,
                request_line_id=row.request_line_id,
                task_catalog_item_id=row.task_catalog_item_id,
                approval_scope_id=row.approval_scope_id,
                proposed_resource_id=row.proposed_resource_id,
                routing_sources=_decode_sources(
                    row.routing_sources_text
                ),
                approvers=tuple(
                    approvers_by_requirement.get(row.id, [])
                ),
            )
            for row in requirements
        )

    def _record(
        self,
        cycle: RequestApprovalCycle,
    ) -> ApprovalCycleRecord:
        return ApprovalCycleRecord(
            id=cycle.id,
            workforce_request_id=cycle.workforce_request_id,
            submitted_request_version=int(
                cycle.submitted_request_version
            ),
            state=cycle.state,
            subject_fingerprint=cycle.subject_fingerprint,
            initialization_reason=cycle.initialization_reason,
            submitted_at=cycle.submitted_at,
            invalidated_at=cycle.invalidated_at,
            invalidation_reason=cycle.invalidation_reason,
            completed_at=cycle.completed_at,
            approved_revision_id=cycle.approved_revision_id,
            requirements=self._cycle_requirements(cycle.id),
        )

    def get_active_cycle(
        self,
        request_id: str,
    ) -> ApprovalCycleRecord | None:
        rows = self._session.scalars(
            select(RequestApprovalCycle)
            .where(
                RequestApprovalCycle.workforce_request_id
                == _text(request_id),
                RequestApprovalCycle.state
                == APPROVAL_CYCLE_STATE_OPEN,
            )
            .order_by(
                RequestApprovalCycle.submitted_at.desc(),
                RequestApprovalCycle.id.desc(),
            )
            .limit(2)
        ).all()
        if len(rows) > 1:
            raise ApplicationConflictError(
                "Plus d'un cycle d'approbation actif existe pour la demande.",
                code="approval_cycle_multiple_active",
                context={"workforce_request_id": _text(request_id)},
            )
        return self._record(rows[0]) if rows else None

    def get_latest_cycle(
        self,
        request_id: str,
    ) -> ApprovalCycleRecord | None:
        row = self._session.scalar(
            select(RequestApprovalCycle)
            .where(
                RequestApprovalCycle.workforce_request_id
                == _text(request_id)
            )
            .order_by(
                RequestApprovalCycle.submitted_at.desc(),
                RequestApprovalCycle.id.desc(),
            )
            .limit(1)
        )
        return self._record(row) if row is not None else None

    def has_any_cycle(self, request_id: str) -> bool:
        count = self._session.scalar(
            select(func.count(RequestApprovalCycle.id)).where(
                RequestApprovalCycle.workforce_request_id
                == _text(request_id)
            )
        )
        return bool(count)

    def _append_history(
        self,
        request: WorkforceRequest,
        *,
        action: str,
        comment: str | None,
        cycle_id: str,
        extra_details: dict[str, object] | None = None,
    ) -> None:
        details = {
            "aggregate_version": int(
                request.aggregate_version or 1
            ),
            "line_mode": bool(request.line_mode),
            "approval_cycle_id": cycle_id,
        }
        if extra_details:
            details.update(extra_details)
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=action,
                status=request.status,
                comment=_optional_text(comment),
                details=json.dumps(
                    details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_user_id=self._actor_user_id,
                actor_name=self._actor_name,
                occurred_at=utc_now(),
            )
        )

    def create_cycle(
        self,
        *,
        request_id: str,
        submitted_request_version: int,
        expected_version: int,
        subject_fingerprint: str,
        initialization_reason: str,
        requirements: Sequence[ApprovalRequirementSnapshot],
    ) -> ApprovalCycleRecord:
        request = self._session.get(
            WorkforceRequest,
            _text(request_id),
        )
        if request is None:
            raise KeyError(f"Demande {request_id} introuvable")
        if self.get_active_cycle(request.id) is not None:
            raise ApplicationConflictError(
                "Un cycle d'approbation actif existe déjà pour cette demande.",
                code="approval_cycle_already_active",
                context={"workforce_request_id": request.id},
            )
        if int(submitted_request_version) != int(expected_version):
            raise ApplicationConflictError(
                "La version soumise ne correspond pas à la version attendue.",
                code="approval_cycle_submitted_version_conflict",
                context={
                    "submitted_request_version": int(
                        submitted_request_version
                    ),
                    "expected_version": int(expected_version),
                },
            )

        acquire_request_aggregate_version(
            self._session,
            request,
            expected_version,
        )
        cycle = RequestApprovalCycle(
            id=new_id(),
            workforce_request_id=request.id,
            submitted_request_version=int(
                submitted_request_version
            ),
            state=APPROVAL_CYCLE_STATE_OPEN,
            subject_fingerprint=_text(subject_fingerprint),
            initialization_reason=_text(
                initialization_reason
            ),
            submitted_at=utc_now(),
        )
        self._session.add(cycle)
        self._session.flush()

        for snapshot in requirements:
            requirement = ApprovalRequirement(
                id=new_id(),
                approval_cycle_id=cycle.id,
                request_line_id=snapshot.request_line_id,
                task_catalog_item_id=snapshot.task_catalog_item_id,
                approval_scope_id=snapshot.approval_scope_id,
                proposed_resource_id=snapshot.proposed_resource_id,
                routing_sources_text=json.dumps(
                    sorted(set(snapshot.routing_sources)),
                    separators=(",", ":"),
                ),
            )
            self._session.add(requirement)
            for approver in snapshot.approvers:
                self._session.add(
                    ApprovalRequirementApprover(
                        requirement_id=requirement.id,
                        app_user_id=approver.app_user_id,
                        sources_text=json.dumps(
                            sorted(set(approver.sources)),
                            separators=(",", ":"),
                        ),
                    )
                )

        self._append_history(
            request,
            action="Initialisation cycle approbation",
            comment=None,
            cycle_id=cycle.id,
            extra_details={
                "submitted_request_version": int(
                    submitted_request_version
                ),
                "subject_fingerprint": cycle.subject_fingerprint,
                "initialization_reason": cycle.initialization_reason,
                "requirement_count": len(requirements),
            },
        )
        self._session.flush()
        self._session.refresh(cycle)
        return self._record(cycle)

    def actor_is_active(self, app_user_id: str) -> bool:
        actor = self._session.get(AppUser, _text(app_user_id))
        return bool(actor is not None and actor.active)

    def list_decisions(
        self,
        cycle_id: str,
    ) -> tuple[ApprovalDecisionRecord, ...]:
        rows = self._session.scalars(
            select(ApprovalDecision)
            .join(
                ApprovalRequirement,
                ApprovalRequirement.id == ApprovalDecision.requirement_id,
            )
            .where(ApprovalRequirement.approval_cycle_id == _text(cycle_id))
            .order_by(
                ApprovalDecision.decided_at,
                ApprovalDecision.id,
            )
        ).all()
        return tuple(
            ApprovalDecisionRecord(
                requirement_id=row.requirement_id,
                app_user_id=row.app_user_id,
                decision=_text(row.decision).upper(),
                action_id=row.action_id,
                decided_at=row.decided_at,
                comment=_optional_text(row.comment),
            )
            for row in rows
        )

    def list_approval_users(
        self,
        user_ids: Sequence[str],
    ) -> tuple[ApprovalUserSummaryRecord, ...]:
        identifiers = tuple(
            dict.fromkeys(_text(value) for value in user_ids if _text(value))
        )
        if not identifiers:
            return ()
        rows = self._session.scalars(
            select(AppUser)
            .where(AppUser.id.in_(identifiers))
            .order_by(AppUser.display_name, AppUser.id)
        ).all()
        return tuple(
            ApprovalUserSummaryRecord(
                app_user_id=row.id,
                display_name=_text(row.display_name) or row.id,
                active=bool(row.active),
            )
            for row in rows
        )

    def acquire_request_version(
        self,
        request_id: str,
        expected_version: int,
    ) -> int:
        request = self._session.get(WorkforceRequest, _text(request_id))
        if request is None:
            raise KeyError(f"Demande {request_id} introuvable")
        acquire_request_aggregate_version(
            self._session,
            request,
            int(expected_version),
        )
        self._session.refresh(request)
        return max(int(request.aggregate_version or 1), 1)

    def append_decisions(
        self,
        *,
        cycle_id: str,
        requirement_ids: Sequence[str],
        app_user_id: str,
        decision: str,
        comment: str,
        action_id: str,
    ) -> None:
        cycle = self._session.get(RequestApprovalCycle, _text(cycle_id))
        if cycle is None:
            raise KeyError(f"Cycle d'approbation {cycle_id} introuvable")
        if cycle.state != APPROVAL_CYCLE_STATE_OPEN:
            raise ApplicationConflictError(
                "Le cycle d'approbation n'est plus actif.",
                code="approval_cycle_not_open",
                context={"approval_cycle_id": cycle.id, "state": cycle.state},
            )
        requirement_set = {
            row.id
            for row in self._session.scalars(
                select(ApprovalRequirement).where(
                    ApprovalRequirement.approval_cycle_id == cycle.id,
                    ApprovalRequirement.id.in_(tuple(requirement_ids)),
                )
            ).all()
        }
        if requirement_set != set(requirement_ids):
            raise ApplicationConflictError(
                "Une exigence ciblée n'appartient plus au cycle actif.",
                code="approval_vote_target_invalid",
                context={"approval_cycle_id": cycle.id},
            )
        for requirement_id in sorted(requirement_set):
            self._session.add(
                ApprovalDecision(
                    id=new_id(),
                    requirement_id=requirement_id,
                    app_user_id=_text(app_user_id),
                    decision=_text(decision).upper() or APPROVAL_DECISION_APPROVE,
                    decided_at=utc_now(),
                    comment=_optional_text(comment),
                    action_id=_text(action_id),
                )
            )
        self._session.flush()

    def mark_request_approved(
        self,
        request_id: str,
        *,
        actor_name: str,
        comment: str,
    ) -> None:
        request = self._session.get(WorkforceRequest, _text(request_id))
        if request is None:
            raise KeyError(f"Demande {request_id} introuvable")
        if request.status != "Soumise":
            raise ApplicationConflictError(
                "La demande n'est plus soumise au moment de la finalisation.",
                code="approval_request_not_submitted",
                context={"status": request.status},
            )
        request.status = "En planification"
        request.approved_by_name = _optional_text(actor_name)
        request.approved_at = utc_now()
        request.approval_comment = _optional_text(comment)
        self._session.flush()

    def active_revision_id(self, request_id: str) -> str | None:
        reference = self._session.get(
            RequestApprovalReference,
            _text(request_id),
        )
        if reference is None:
            return None
        return _optional_text(reference.active_revision_id)

    def complete_cycle(
        self,
        *,
        cycle_id: str,
        approval_revision_id: str,
        action_id: str,
        planning_version: int | None,
    ) -> None:
        cycle = self._session.get(RequestApprovalCycle, _text(cycle_id))
        if cycle is None:
            raise KeyError(f"Cycle d'approbation {cycle_id} introuvable")
        if cycle.state != APPROVAL_CYCLE_STATE_OPEN:
            raise ApplicationConflictError(
                "Le cycle d'approbation a déjà été finalisé ou invalidé.",
                code="approval_cycle_not_open",
                context={
                    "approval_cycle_id": cycle.id,
                    "state": cycle.state,
                    "approved_revision_id": cycle.approved_revision_id,
                },
            )
        if cycle.approved_revision_id is not None:
            raise ApplicationConflictError(
                "Le cycle possède déjà une révision approuvée.",
                code="approval_cycle_revision_already_bound",
                context={
                    "approval_cycle_id": cycle.id,
                    "approved_revision_id": cycle.approved_revision_id,
                },
            )
        cycle.state = APPROVAL_CYCLE_STATE_COMPLETED
        cycle.completed_at = utc_now()
        cycle.approved_revision_id = _text(approval_revision_id)
        request = self._session.get(WorkforceRequest, cycle.workforce_request_id)
        if request is None:
            raise KeyError(f"Demande {cycle.workforce_request_id} introuvable")
        self._append_history(
            request,
            action="Finalisation cycle approbation",
            comment="Quorum complet; cycle finalisé.",
            cycle_id=cycle.id,
            extra_details={
                "action_id": _text(action_id),
                "approved_revision_id": cycle.approved_revision_id,
                "planning_version": planning_version,
            },
        )
        self._session.flush()

    def append_vote_audit(
        self,
        *,
        request_id: str,
        cycle_id: str,
        action_id: str,
        requirement_ids: Sequence[str],
        old_request_version: int,
        new_request_version: int,
        quorum: ApprovalQuorumProjection,
        planning_version: int | None = None,
        approval_revision_id: str | None = None,
    ) -> None:
        request = self._session.get(WorkforceRequest, _text(request_id))
        if request is None:
            raise KeyError(f"Demande {request_id} introuvable")
        action = (
            "Quorum approbation complété"
            if quorum.complete
            else (
                "Vote approbation multi-lignes"
                if len(tuple(requirement_ids)) > 1
                else "Vote approbation partiel"
            )
        )
        self._append_history(
            request,
            action=action,
            comment=None,
            cycle_id=_text(cycle_id),
            extra_details={
                "action_id": _text(action_id),
                "app_user_id": self._actor_user_id,
                "requirement_ids": sorted(set(requirement_ids)),
                "old_aggregate_version": int(old_request_version),
                "new_aggregate_version": int(new_request_version),
                "total_requirements": int(quorum.total_requirements),
                "satisfied_requirements": int(quorum.satisfied_requirements),
                "remaining_requirement_ids": list(quorum.remaining_requirement_ids),
                "quorum_complete": bool(quorum.complete),
                "approved_revision_id": _optional_text(approval_revision_id),
                "planning_version": planning_version,
            },
        )
        self._session.flush()

    def invalidate_cycle(
        self,
        *,
        cycle_id: str,
        expected_version: int,
        reason: str,
    ) -> ApprovalCycleRecord:
        cycle = self._session.get(
            RequestApprovalCycle,
            _text(cycle_id),
        )
        if cycle is None:
            raise KeyError(
                f"Cycle d'approbation {cycle_id} introuvable"
            )
        if cycle.state != APPROVAL_CYCLE_STATE_OPEN:
            raise ApplicationConflictError(
                "Le cycle d'approbation n'est plus actif.",
                code="approval_cycle_not_open",
                context={
                    "approval_cycle_id": cycle.id,
                    "state": cycle.state,
                },
            )
        request = self._session.get(
            WorkforceRequest,
            cycle.workforce_request_id,
        )
        if request is None:
            raise KeyError(
                f"Demande {cycle.workforce_request_id} introuvable"
            )

        acquire_request_aggregate_version(
            self._session,
            request,
            expected_version,
        )
        cycle.state = APPROVAL_CYCLE_STATE_INVALIDATED
        cycle.invalidated_at = utc_now()
        cycle.invalidation_reason = _text(reason)
        self._append_history(
            request,
            action="Invalidation cycle approbation",
            comment=_text(reason),
            cycle_id=cycle.id,
            extra_details={
                "submitted_request_version": int(
                    cycle.submitted_request_version
                ),
                "subject_fingerprint": cycle.subject_fingerprint,
                "invalidation_reason": cycle.invalidation_reason,
            },
        )
        self._session.flush()
        self._session.refresh(cycle)
        return self._record(cycle)
