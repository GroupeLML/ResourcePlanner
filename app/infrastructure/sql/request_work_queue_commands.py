from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
)
from ...application.request_work_queue import (
    PermanentlyReduceShiftHoursCommand,
    RequestWorkQueueCommandPort,
    ReturnSegmentToWorkQueueCommand,
    ReturnShiftToWorkQueueCommand,
)
from ...application.security import (
    AuthPrincipal,
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_READ_DEMANDS,
    permissions_for_roles,
)
from ...application.user_view_context import UserViewContextService
from ...domain.effective_need import allocate_entry_budget
from .approval_cycle_models import ApprovalRequirement, RequestApprovalCycle
from .approval_scope_models import ApprovalScope, ApprovalScopeApprover
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .asset_service import SqlAssetService
from .segment_asset_guard import assert_segment_asset_mutation_compatible
from .base import new_id
from .command_adapters import SqlPlanningCommandAdapter
from .identity_models import AppUser
from .models import ORIGIN_REQUEST, ResourceRequirement, Shift, WorkforceRequest
from .operational_choice_models import (
    PLANNING_INTENT_PRESERVE_EXISTING,
    PLANNING_INTENT_QUEUED,
    PLANNING_TARGET_EXPLICIT_NONE,
    PlanningWorkQueueRelease,
    RequestCompletionReduction,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
)
from .planning_audit import ENTITY_SEGMENT, ENTITY_SHIFT, SqlPlanningAuditJournal
from .planning_intent_repository import SqlRequestRequirementPlanningIntentRepository
from .planning_version import SqlPlanningMutationVersionRepository
from .request_version import acquire_request_aggregate_version
from .user_view_context_repository import SqlUserViewContextRepository


CENT = Decimal("0.01")
TOLERANCE = Decimal("0.001")


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


class SqlRequestWorkQueueCommandAdapter(RequestWorkQueueCommandPort):
    """ADR-034 authoritative REQUEST shift/segment return and need-reduction commands."""

    def __init__(
        self,
        session: Session,
        *,
        planning: SqlPlanningCommandAdapter | None = None,
        versioning: SqlPlanningMutationVersionRepository | None = None,
        journal: SqlPlanningAuditJournal | None = None,
        actor_user_id: str | None,
        actor_name: str = "api",
        permissions: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
    ) -> None:
        self._session = session
        self._versioning = versioning or SqlPlanningMutationVersionRepository(session)
        self._planning = planning or SqlPlanningCommandAdapter(
            session,
            versioning=self._versioning,
        )
        self._journal = journal or SqlPlanningAuditJournal(
            session,
            actor_name=_text(actor_name) or "api",
        )
        self._actor_user_id = _text(actor_user_id) or None
        self._actor_name = _text(actor_name) or "api"
        self._roles = tuple(_text(role).upper() for role in (roles or ()) if _text(role))
        self._permissions = set(_text(p) for p in (permissions or ()) if _text(p))

    def _actor(self, *, reduction: bool = False) -> AppUser:
        if not self._actor_user_id:
            raise ApplicationAuthorizationError(
                "Une identité AppUser stable est requise pour cette commande.",
                code="planning_actor_identity_required",
            )
        actor = self._session.get(AppUser, self._actor_user_id)
        if actor is None or not actor.active:
            raise ApplicationAuthorizationError(
                "L'utilisateur RessourcePlanner actif est introuvable.",
                code="planning_actor_inactive",
            )
        permissions = set(self._permissions)
        if not permissions:
            permissions.update(permissions_for_roles(self._roles))
        required = {PERMISSION_MANAGE_PLANNING}
        if reduction:
            required.update({PERMISSION_READ_DEMANDS, PERMISSION_APPROVE_DEMANDS})
        missing = sorted(required - permissions)
        if missing:
            raise ApplicationAuthorizationError(
                "Les permissions requises pour cette commande ne sont pas satisfaites.",
                code="planning_command_permission_denied",
                context={"missing_permissions": missing},
            )
        return actor

    def _principal(self, actor: AppUser) -> AuthPrincipal:
        roles = self._roles
        if not roles:
            try:
                raw = json.loads(actor.roles_json or "[]")
                roles = tuple(str(value) for value in raw if str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                roles = ()
        return AuthPrincipal.from_roles(
            local_user_id=actor.id,
            issuer=_text(actor.issuer) or "local",
            subject=_text(actor.subject) or actor.id,
            display_name=_text(actor.display_name) or actor.id,
            email=_text(actor.email) or None,
            employee_external_id=_text(actor.employee_external_id) or None,
            roles=roles,
            auth_mode="internal",
        )

    def _assert_planning_scope(
        self,
        actor: AppUser,
        requirement: ResourceRequirement,
    ) -> None:
        scope = UserViewContextService(
            SqlUserViewContextRepository(self._session)
        ).resolve_project_scope(self._principal(actor))
        if scope.project_ids is not None and requirement.project_id not in set(scope.project_ids):
            raise ApplicationAuthorizationError(
                "Le besoin n'est pas visible dans le périmètre Planning courant.",
                code="planning_scope_denied",
                context={"project_id": requirement.project_id, "requirement_id": requirement.id},
            )

    def _shift(self, identifier: str) -> Shift:
        wanted = _text(identifier)
        row = self._session.scalar(
            select(Shift).where(
                (Shift.id == wanted) | (Shift.legacy_allocation_id == wanted)
            )
        )
        if row is None:
            raise ApplicationNotFoundError(
                "Le quart ciblé n'existe plus.",
                code="request_shift_not_found",
                context={"allocation_id": wanted},
            )
        return row

    def _requirement(self, identifier: str) -> ResourceRequirement:
        wanted = _text(identifier)
        row = self._session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.id == wanted)
                | (ResourceRequirement.legacy_segment_id == wanted)
            )
        )
        if row is None:
            raise ApplicationNotFoundError(
                "Le segment ciblé est introuvable.",
                code="request_requirement_not_found",
                context={"segment_id": wanted},
            )
        if row.origin != ORIGIN_REQUEST or not row.workforce_request_id:
            raise ApplicationConflictError(
                "Cette commande ADR-034 s'applique uniquement aux quarts issus d'une demande approuvée.",
                code="request_work_queue_origin_invalid",
                context={"requirement_id": row.id, "origin": row.origin},
            )
        return row

    @staticmethod
    def _assert_return_lifecycle(requirement: ResourceRequirement) -> None:
        if _text(requirement.status) in {"Terminé", "Annulé"}:
            raise ApplicationConflictError(
                "Un besoin terminal ne peut pas retourner du travail dans la File de Travail.",
                code="request_work_queue_terminal_requirement",
                context={
                    "requirement_id": requirement.id,
                    "status": requirement.status,
                },
            )

    @staticmethod
    def _assert_reduction_lifecycle(requirement: ResourceRequirement) -> None:
        if _text(requirement.status) == "Annulé":
            raise ApplicationConflictError(
                "Un besoin annulé ne peut pas recevoir une réduction de complétion.",
                code="request_reduction_terminal_requirement",
                context={
                    "requirement_id": requirement.id,
                    "status": requirement.status,
                },
            )

    def _request(self, requirement: ResourceRequirement) -> WorkforceRequest:
        request = self._session.get(WorkforceRequest, requirement.workforce_request_id)
        if request is None:
            raise ApplicationConflictError(
                "La demande source du segment est introuvable.",
                code="planning_authorization_unknown",
                context={"requirement_id": requirement.id},
            )
        if _text(request.cancellation_state).upper() == "ACCEPTED":
            raise ApplicationConflictError(
                "Une demande dont l'annulation est acceptée n'a plus de besoin courant à modifier.",
                code="request_current_need_terminal",
                context={"demand_number": _text(request.legacy_demand_number) or request.id},
            )
        return request

    def _acquire_operational_guard(
        self,
        *,
        request_id: str,
        approval_revision_id: str,
        expected_version: int,
    ) -> RequestOperationalState:
        result = self._session.execute(
            update(RequestOperationalState)
            .where(
                RequestOperationalState.workforce_request_id == request_id,
                RequestOperationalState.approval_revision_id == approval_revision_id,
                RequestOperationalState.version == int(expected_version),
            )
            .values(version=RequestOperationalState.version)
        )
        if int(result.rowcount or 0) != 1:
            current = self._session.get(RequestOperationalState, request_id)
            raise ApplicationConflictError(
                "L'état opérationnel de la demande a changé depuis la lecture.",
                code="operational_choice_version_conflict",
                context={
                    "expected_version": int(expected_version),
                    "current_version": int(current.version or 0) if current is not None else None,
                },
            )
        self._session.flush()
        state = self._session.get(RequestOperationalState, request_id)
        if state is None:
            raise ApplicationConflictError(
                "L'état opérationnel de la demande est introuvable.",
                code="operational_choice_version_conflict",
            )
        return state

    def _approval_and_operational(
        self,
        requirement: ResourceRequirement,
        request: WorkforceRequest,
        *,
        expected_revision_id: str,
        expected_operational_version: int,
    ) -> tuple[RequestApprovalRevision, RequestOperationalState]:
        reference = self._session.get(RequestApprovalReference, request.id)
        expected = _text(expected_revision_id)
        if (
            reference is None
            or reference.status != APPROVAL_REFERENCE_CAPTURED
            or not reference.active_revision_id
            or reference.active_revision_id != expected
            or requirement.approval_revision_id != expected
            or requirement.approval_reference_status != APPROVAL_REFERENCE_CAPTURED
        ):
            raise ApplicationConflictError(
                "L'autorisation approuvée active a changé depuis la lecture.",
                code="planning_authorization_revision_conflict",
                context={
                    "expected_approval_revision_id": expected,
                    "current_approval_revision_id": (
                        reference.active_revision_id if reference is not None else None
                    ),
                },
            )
        revision = self._session.get(RequestApprovalRevision, expected)
        if revision is None:
            raise ApplicationConflictError(
                "La révision approuvée active est introuvable.",
                code="planning_authorization_unknown",
            )
        state = self._acquire_operational_guard(
            request_id=request.id,
            approval_revision_id=expected,
            expected_version=expected_operational_version,
        )
        return revision, state

    def _intent(self, requirement: ResourceRequirement) -> RequestRequirementPlanningIntent:
        rows = self._session.scalars(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id == requirement.id
            )
        ).all()
        if len(rows) != 1:
            raise ApplicationConflictError(
                "L'identité stable du slot approuvé est absente ou ambiguë.",
                code="planning_slot_identity_unknown",
                context={"requirement_id": requirement.id, "match_count": len(rows)},
            )
        return rows[0]

    @staticmethod
    def _entry(revision: RequestApprovalRevision, entry_key: str) -> dict[str, object]:
        payload = json.loads(revision.payload_text)
        rows = payload.get("authorization", {}).get("entries", [])
        matches = [
            dict(row)
            for row in rows
            if isinstance(row, Mapping) and _text(row.get("identity")) == _text(entry_key)
        ]
        if len(matches) != 1:
            raise ApplicationConflictError(
                "L'entrée approuvée du slot est absente ou ambiguë.",
                code="planning_authorization_entry_unknown",
                context={"approved_entry_key": entry_key},
            )
        return matches[0]

    def _current_need(
        self,
        intent: RequestRequirementPlanningIntent,
        revision: RequestApprovalRevision,
        state: RequestOperationalState,
    ) -> Decimal:
        entry = self._entry(revision, intent.approved_entry_key)
        approved_total = _decimal(entry.get("hours"))
        try:
            overrides = json.loads(state.budget_overrides_text or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            overrides = {}
        total = _decimal(overrides.get(intent.approved_entry_key, approved_total))
        slot_count = max(int(entry.get("slot_count") or 1), 1)
        if intent.slot_index < 0 or intent.slot_index >= slot_count:
            raise ApplicationConflictError(
                "La topologie approuvée ne correspond plus au slot stable.",
                code="reduction_rebase_required",
                context={"slot_index": intent.slot_index, "slot_count": slot_count},
            )
        nominal = allocate_entry_budget(total, slot_count)[intent.slot_index]
        return SqlRequestRequirementPlanningIntentRepository(self._session).effective_need(
            planning_intent_id=intent.id,
            nominal_slot_hours=nominal,
        )

    def _covered_hours(
        self,
        requirement_id: str,
        *,
        excluding_shift_id: str | None = None,
    ) -> Decimal:
        statement = select(func.coalesce(func.sum(Shift.hours), 0)).where(
            Shift.resource_requirement_id == requirement_id
        )
        if excluding_shift_id:
            statement = statement.where(Shift.id != excluding_shift_id)
        return _decimal(self._session.scalar(statement) or 0)

    def _assert_not_overcovered(
        self,
        requirement: ResourceRequirement,
        effective_need: Decimal,
    ) -> None:
        covered = self._covered_hours(requirement.id)
        if covered > effective_need + TOLERANCE:
            raise ApplicationConflictError(
                "Le segment est en surcouverture et doit être régularisé avant cette commande.",
                code="request_work_queue_overcoverage_conflict",
                context={
                    "requirement_id": requirement.id,
                    "effective_need": float(effective_need),
                    "covered_hours": float(covered),
                },
            )

    def _assert_reduction_approval_authority(
        self,
        actor: AppUser,
        request: WorkforceRequest,
        intent: RequestRequirementPlanningIntent,
        revision: RequestApprovalRevision,
    ) -> None:
        if not intent.source_request_line_id:
            raise ApplicationAuthorizationError(
                "L'autorité d'approbation de cette entrée legacy ne peut pas être établie sans ambiguïté.",
                code="reduction_approval_authority_unknown",
            )
        cycles = self._session.scalars(
            select(RequestApprovalCycle).where(
                RequestApprovalCycle.workforce_request_id == request.id,
                RequestApprovalCycle.approved_revision_id == revision.id,
            )
        ).all()
        if len(cycles) != 1:
            raise ApplicationAuthorizationError(
                "Le cycle qui a produit l'autorisation active est absent ou ambigu.",
                code="reduction_approval_authority_unknown",
                context={"approval_cycle_count": len(cycles)},
            )
        cycle = cycles[0]
        relevant = tuple(
            self._session.scalars(
                select(ApprovalRequirement).where(
                    ApprovalRequirement.approval_cycle_id == cycle.id,
                    ApprovalRequirement.request_line_id == intent.source_request_line_id,
                )
            ).all()
        )
        if not relevant:
            raise ApplicationAuthorizationError(
                "Aucune exigence d'approbation ne correspond à la source autorisée.",
                code="reduction_approval_authority_unknown",
                context={"request_line_id": intent.source_request_line_id},
            )

        # The completed-cycle approver list is historical evidence only. ADR-034
        # requires current eligibility in the ApprovalScope that authorized the
        # active approved subject.
        scope_ids = {
            _text(requirement.approval_scope_id)
            for requirement in relevant
            if _text(requirement.approval_scope_id)
        }
        if len(scope_ids) != 1:
            raise ApplicationAuthorizationError(
                "Le périmètre d'approbation courant de la source est absent ou ambigu.",
                code="reduction_approval_authority_unknown",
                context={
                    "request_line_id": intent.source_request_line_id,
                    "approval_scope_ids": sorted(scope_ids),
                },
            )
        scope_id = next(iter(scope_ids))
        scope = self._session.get(ApprovalScope, scope_id)
        current_membership = self._session.get(
            ApprovalScopeApprover,
            (scope_id, actor.id),
        )
        if scope is None or not bool(scope.active) or current_membership is None:
            raise ApplicationAuthorizationError(
                "L'utilisateur n'est plus admissible dans le périmètre d'approbation courant.",
                code="reduction_approval_authority_denied",
                context={
                    "request_line_id": intent.source_request_line_id,
                    "approval_scope_id": scope_id,
                },
            )

    @staticmethod
    def _shift_snapshot(shift: Shift) -> str:
        return json.dumps(
            {
                "shift_id": shift.id,
                "allocation_id": shift.legacy_allocation_id,
                "resource_requirement_id": shift.resource_requirement_id,
                "resource_id": shift.resource_id,
                "work_date": shift.work_date.isoformat(),
                "hours": format(_decimal(shift.hours), "f"),
                "allocation_type": shift.allocation_type,
                "source": shift.source,
                "locked": bool(shift.locked),
                "outside_standard_hours": bool(shift.outside_standard_hours),
                "confirmation": shift.confirmation,
                "note": shift.note,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _result(
        self,
        *,
        operation: str,
        requirement: ResourceRequirement,
        request: WorkforceRequest,
        planning_version: int,
        revision: RequestApprovalRevision,
        state: RequestOperationalState,
        effective_need_before: Decimal,
        effective_need_after: Decimal,
        removed_shift_count: int,
        removed_hours: Decimal,
        shift_id: str | None = None,
        release_id: str | None = None,
        reduction_id: str | None = None,
        request_version: int | None = None,
        no_op: bool = False,
    ) -> dict[str, Any]:
        covered_after = self._covered_hours(requirement.id)
        return {
            "operation": operation,
            "requirement_id": requirement.id,
            "segment_id": _text(requirement.legacy_segment_id) or requirement.id,
            "demand_number": _text(request.legacy_demand_number) or request.id,
            "planning_version": planning_version,
            "approval_revision_id": revision.id,
            "operational_version": int(state.version or 1),
            "request_version": request_version,
            "shift_id": shift_id,
            "release_id": release_id,
            "reduction_id": reduction_id,
            "removed_shift_count": removed_shift_count,
            "removed_hours": float(removed_hours),
            "effective_need_before": float(effective_need_before),
            "effective_need_after": float(effective_need_after),
            "covered_hours_after": float(covered_after),
            "remaining_hours_after": float(max(effective_need_after - covered_after, Decimal("0.00"))),
            "no_op": no_op,
        }

    def return_shift(self, command: ReturnShiftToWorkQueueCommand) -> Mapping[str, Any]:
        planning_version = self._versioning.acquire(command.expected_planning_version)
        actor = self._actor()
        shift = self._shift(command.allocation_id)
        requirement = self._requirement(shift.resource_requirement_id)
        self._assert_return_lifecycle(requirement)
        self._assert_planning_scope(actor, requirement)
        request = self._request(requirement)
        revision, state = self._approval_and_operational(
            requirement,
            request,
            expected_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
        )
        intent = self._intent(requirement)
        effective_need = self._current_need(intent, revision, state)
        self._assert_not_overcovered(requirement, effective_need)

        survivor_ids = tuple(
            self._session.scalars(
                select(Shift.id).where(
                    Shift.resource_requirement_id == requirement.id,
                    Shift.id != shift.id,
                )
            ).all()
        )
        release = PlanningWorkQueueRelease(
            planning_intent_id=intent.id,
            resource_requirement_id=requirement.id,
            original_shift_id=shift.id,
            workforce_request_id=request.id,
            approval_revision_id=revision.id,
            approved_entry_key=intent.approved_entry_key,
            resource_id=shift.resource_id,
            work_date=shift.work_date,
            hours=_decimal(shift.hours),
            shift_snapshot_text=self._shift_snapshot(shift),
            actor_user_id=actor.id,
            command_id=command.command_id,
            active=True,
        )
        intent.intent_mode = PLANNING_INTENT_PRESERVE_EXISTING
        self._session.add(release)

        previous = self._journal.shift_snapshot(shift.id)
        SqlAssetService(self._session, actor=self._actor_name).delete_shift_ad_hoc_assignment(
            shift=shift
        )
        self._session.delete(shift)
        self._session.flush()
        self._planning.rebuild(preserve_shift_ids=survivor_ids)
        if previous is not None:
            entity_id, entity_reference, parent_reference, before = previous
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=parent_reference,
                action="Retour quart à la File de Travail",
                before=before,
                after={"planning_intent": PLANNING_INTENT_PRESERVE_EXISTING, "release_id": release.id},
            )
        return self._result(
            operation="RETURN_SHIFT_TO_WORK_QUEUE",
            requirement=requirement,
            request=request,
            planning_version=planning_version,
            revision=revision,
            state=state,
            effective_need_before=effective_need,
            effective_need_after=effective_need,
            removed_shift_count=1,
            removed_hours=release.hours,
            shift_id=release.original_shift_id,
            release_id=release.id,
        )

    def return_segment(self, command: ReturnSegmentToWorkQueueCommand) -> Mapping[str, Any]:
        planning_version = self._versioning.acquire(command.expected_planning_version)
        actor = self._actor()
        requirement = self._requirement(command.segment_id)
        self._assert_return_lifecycle(requirement)
        self._assert_planning_scope(actor, requirement)
        request = self._request(requirement)
        revision, state = self._approval_and_operational(
            requirement,
            request,
            expected_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
        )
        intent = self._intent(requirement)
        effective_need = self._current_need(intent, revision, state)
        shifts = list(
            self._session.scalars(
                select(Shift)
                .where(Shift.resource_requirement_id == requirement.id)
                .order_by(Shift.work_date, Shift.id)
            ).all()
        )
        if (
            intent.intent_mode == PLANNING_INTENT_QUEUED
            and intent.target_mode == PLANNING_TARGET_EXPLICIT_NONE
            and not shifts
        ):
            return self._result(
                operation="RETURN_SEGMENT_TO_WORK_QUEUE",
                requirement=requirement,
                request=request,
                planning_version=planning_version,
                revision=revision,
                state=state,
                effective_need_before=effective_need,
                effective_need_after=effective_need,
                removed_shift_count=0,
                removed_hours=Decimal("0.00"),
                no_op=True,
            )

        before = self._journal.requirement_snapshot(requirement.id)
        removed_hours = sum((_decimal(row.hours) for row in shifts), Decimal("0.00"))
        intent.intent_mode = PLANNING_INTENT_QUEUED
        intent.target_mode = PLANNING_TARGET_EXPLICIT_NONE
        intent.target_resource_id = None
        requirement.assigned_resource_id = None
        assets = SqlAssetService(self._session, actor=self._actor_name)
        for shift in shifts:
            assets.delete_shift_ad_hoc_assignment(shift=shift)
            self._session.delete(shift)
        self._session.flush()
        self._planning.rebuild()
        if before is not None:
            entity_id, entity_reference, before_values = before
            after = self._journal.requirement_snapshot(requirement.id)
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                action="Retour segment à la File de Travail",
                before=before_values,
                after=(after[2] if after is not None else {"planning_intent": PLANNING_INTENT_QUEUED}),
            )
        return self._result(
            operation="RETURN_SEGMENT_TO_WORK_QUEUE",
            requirement=requirement,
            request=request,
            planning_version=planning_version,
            revision=revision,
            state=state,
            effective_need_before=effective_need,
            effective_need_after=effective_need,
            removed_shift_count=len(shifts),
            removed_hours=removed_hours,
        )

    def permanently_reduce_shift(
        self,
        command: PermanentlyReduceShiftHoursCommand,
    ) -> Mapping[str, Any]:
        planning_version = self._versioning.acquire(command.expected_planning_version)
        actor = self._actor(reduction=True)
        shift = self._shift(command.allocation_id)
        requirement = self._requirement(shift.resource_requirement_id)
        self._assert_reduction_lifecycle(requirement)
        self._assert_planning_scope(actor, requirement)
        request = self._request(requirement)
        request_version = acquire_request_aggregate_version(
            self._session,
            request,
            command.expected_request_version,
        )
        revision, state = self._approval_and_operational(
            requirement,
            request,
            expected_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
        )
        intent = self._intent(requirement)
        self._assert_reduction_approval_authority(actor, request, intent, revision)
        effective_need = self._current_need(intent, revision, state)
        self._assert_not_overcovered(requirement, effective_need)

        hours = _decimal(shift.hours)
        effective_after = max(effective_need - hours, Decimal("0.00"))
        survivors = self._covered_hours(requirement.id, excluding_shift_id=shift.id)
        if survivors > effective_after + TOLERANCE:
            raise ApplicationConflictError(
                "Les quarts survivants dépasseraient le besoin courant après la réduction.",
                code="request_reduction_survivor_conflict",
                context={
                    "effective_need_before": float(effective_need),
                    "effective_need_after": float(effective_after),
                    "survivor_hours": float(survivors),
                },
            )
        if effective_after == 0:
            assert_segment_asset_mutation_compatible(
                self._session,
                requirement,
                removing=True,
            )

        reduction = RequestCompletionReduction(
            planning_intent_id=intent.id,
            workforce_request_id=request.id,
            historical_approval_revision_id=revision.id,
            source_request_line_id=intent.source_request_line_id,
            approved_entry_key=intent.approved_entry_key,
            resource_requirement_id=requirement.id,
            original_shift_id=shift.id,
            shift_snapshot_text=self._shift_snapshot(shift),
            reduction_hours=hours,
            effective_need_before=effective_need,
            effective_need_after=effective_after,
            actor_user_id=actor.id,
            command_id=command.command_id,
            reconciliation_state="ACTIVE",
        )
        self._session.add(reduction)
        survivor_ids = tuple(
            self._session.scalars(
                select(Shift.id).where(
                    Shift.resource_requirement_id == requirement.id,
                    Shift.id != shift.id,
                )
            ).all()
        )
        previous = self._journal.shift_snapshot(shift.id)
        SqlAssetService(self._session, actor=self._actor_name).delete_shift_ad_hoc_assignment(
            shift=shift
        )
        self._session.delete(shift)
        if effective_after > 0:
            requirement.planned_hours = effective_after
        else:
            requirement.planned_hours = Decimal("0.00")
            # Zero effective need is completion, not cancellation. Preserve an
            # existing terminal completion and otherwise transition to Terminé.
            requirement.status = "Terminé"
        self._session.flush()
        self._planning.rebuild(preserve_shift_ids=survivor_ids)
        if previous is not None:
            entity_id, entity_reference, parent_reference, before = previous
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=parent_reference,
                action="Suppression définitive quart — travail non requis",
                before=before,
                after={
                    "reduction_id": reduction.id,
                    "effective_need_before": float(effective_need),
                    "effective_need_after": float(effective_after),
                },
            )
        return self._result(
            operation="PERMANENTLY_REDUCE_SHIFT_HOURS",
            requirement=requirement,
            request=request,
            planning_version=planning_version,
            revision=revision,
            state=state,
            effective_need_before=effective_need,
            effective_need_after=effective_after,
            removed_shift_count=1,
            removed_hours=hours,
            shift_id=reduction.original_shift_id,
            reduction_id=reduction.id,
            request_version=request_version,
        )
