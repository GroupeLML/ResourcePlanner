from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ...application.command_ports import CompositeAllocationCommandPort, PlanningCommandPort
from ...application.commands import (
    AllocationDropEvaluateCommand,
    AllocationDuplicateCommand,
    AllocationExtendMoveCommand,
    AllocationSplitCommand,
    AllocationWindowOverrideMoveCommand,
    PlanningWindowOverrideExtendCommand,
)
from ...application.errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationValidationError,
)
from ...application.idempotency import request_fingerprint
from ...application.security import (
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_OVERRIDE_PLANNING_WINDOW,
)
from ...application.repository_ports import PlanningAuthorizationPort, PlanningMutationVersionPort
from ...domain.approval_envelope import envelope_entry_identity_from_stable_key
from ...domain.manual_overallocation import (
    INCREASE_PLANNED,
    KEEP_EXCEPTION,
    TOLERANCE_HOURS,
    normalize_overallocation_policy,
)
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
)
from .asset_service import SqlAssetService
from .idempotency import SqlCommandIdempotencyAdapter
from .identity_models import AppUser
from .base import new_id
from .command_adapters import INACTIVE_REQUIREMENT_STATUSES, SqlPlanningCommandAdapter
from .models import ORIGIN_REQUEST, Resource, ResourceRequirement, Shift, WorkforceRequest
from .operational_choice_models import RequestOperationalState
from .overallocation import (
    _segment_metrics,
    append_overallocation_audit,
    evaluate_projected_manual_state,
    validate_projected_manual_state,
)
from .planning_audit import ENTITY_SHIFT, SqlPlanningAuditJournal
from .planning_version import SqlPlanningMutationVersionRepository
from .planning_window_override_repository import SqlPlanningWindowOverrideRepository
from .segment_asset_guard import assert_segment_asset_mutation_compatible


_HOUR = Decimal("0.01")


def _text(value: object) -> str:
    return str(value or "").strip()


def _hours(value: object) -> Decimal:
    return Decimal(str(value).replace(",", ".")).quantize(_HOUR)


class SqlCompositeAllocationCommandAdapter(CompositeAllocationCommandPort):
    """Atomic split/duplicate implementation; caller owns the surrounding transaction."""

    def __init__(
        self,
        session: Session,
        *,
        planning: PlanningCommandPort | None = None,
        authorization: PlanningAuthorizationPort | None = None,
        versioning: PlanningMutationVersionPort | None = None,
        journal: SqlPlanningAuditJournal | None = None,
        actor_user_id: str | None = None,
        permissions: Sequence[str] | None = None,
    ) -> None:
        self._session = session
        self._versioning = versioning or SqlPlanningMutationVersionRepository(session)
        self._actor_user_id = _text(actor_user_id) or None
        self._permissions = frozenset(_text(value) for value in (permissions or ()) if _text(value))
        self._planning = planning or SqlPlanningCommandAdapter(
            session,
            versioning=self._versioning,
        )
        self._authorization = authorization
        self._journal = journal or SqlPlanningAuditJournal(session)

    def _can_override_planning_window(self) -> bool:
        if (
            not self._actor_user_id
            or PERMISSION_MANAGE_PLANNING not in self._permissions
            or PERMISSION_OVERRIDE_PLANNING_WINDOW not in self._permissions
        ):
            return False
        actor = self._session.get(AppUser, self._actor_user_id)
        return bool(actor is not None and actor.active)

    def _require_window_override_authority(self) -> str:
        if (
            PERMISSION_MANAGE_PLANNING not in self._permissions
            or PERMISSION_OVERRIDE_PLANNING_WINDOW not in self._permissions
        ):
            raise ApplicationAuthorizationError(
                "Les permissions manage_planning et override_planning_window sont requises.",
                code="planning_window_override_permission_denied",
            )
        actor_id = _text(self._actor_user_id)
        actor = self._session.get(AppUser, actor_id) if actor_id else None
        if actor is None or not actor.active:
            raise ApplicationAuthorizationError(
                "Une identité AppUser active et stable est requise pour déroger à la fenêtre Planning.",
                code="planning_window_override_actor_required",
            )
        return actor.id

    def _requirement_by_identifier(self, identifier: str) -> ResourceRequirement:
        wanted = _text(identifier)
        requirement = self._session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.id == wanted)
                | (ResourceRequirement.legacy_segment_id == wanted)
            )
        )
        if requirement is None:
            raise KeyError(f"Segment {wanted} introuvable")
        if requirement.status in INACTIVE_REQUIREMENT_STATUSES:
            raise ValueError("Le segment ciblé n'est plus actif.")
        return requirement

    def _planning_window_context(
        self,
        requirement: ResourceRequirement,
        *,
        expected_approval_revision_id: str,
        expected_operational_version: int | None,
    ) -> tuple[str, str, Any, Any, int | None]:
        if requirement.origin != ORIGIN_REQUEST or not requirement.workforce_request_id:
            raise ApplicationValidationError(
                "La dérogation de fenêtre s'applique uniquement à un segment REQUEST approuvé.",
                code="planning_window_override_request_required",
                context={"segment_id": self._segment_reference(requirement)},
            )
        approval_revision_id, operational_version = self._authorization_context(
            requirement,
            expected_approval_revision_id=expected_approval_revision_id,
            expected_operational_version=expected_operational_version,
            require_operational_version=False,
        )
        if not approval_revision_id or self._authorization is None:
            raise ApplicationConflictError(
                "L'autorisation approuvée active est requise pour la dérogation.",
                code="planning_authorization_unknown",
            )
        decision = self._authorization.operational_window_authorization(
            self._segment_reference(requirement),
            requirement.start_date,
            expected_approval_revision_id=expected_approval_revision_id,
        )
        approved_entry_key = _text(decision.get("approved_entry_key"))
        approved_start = decision.get("approved_start")
        approved_end = decision.get("approved_end")
        if (
            approval_revision_id != _text(decision.get("approval_revision_id"))
            or approved_entry_key != _text(requirement.approved_entry_key)
            or approved_start is None
            or approved_end is None
        ):
            raise ApplicationConflictError(
                "Le segment ne correspond plus à l'entrée approuvée active.",
                code="planning_authorization_revision_conflict",
                context={
                    "segment_id": self._segment_reference(requirement),
                    "expected_approval_revision_id": expected_approval_revision_id,
                    "current_approval_revision_id": decision.get("approval_revision_id"),
                    "approved_entry_key": decision.get("approved_entry_key"),
                },
            )

        override = SqlPlanningWindowOverrideRepository(
            self._session
        ).active_by_requirement_ids(
            (requirement.id,),
            approval_revision_id=approval_revision_id,
        ).get(requirement.id)
        effective = SqlPlanningWindowOverrideRepository.effective_window_for_requirement(
            requirement,
            override,
            approval_revision_id=approval_revision_id,
            approved_entry_key=approved_entry_key,
            approved_start_date=approved_start,
            approved_end_date=approved_end,
        )
        if (
            requirement.start_date != effective.start_date
            or requirement.end_date != effective.end_date
        ):
            raise ApplicationConflictError(
                "La fenêtre matérialisée du segment n'est plus cohérente avec son autorisation effective.",
                code="planning_window_override_state_conflict",
                context={
                    "segment_id": self._segment_reference(requirement),
                    "effective_start": effective.start_date.isoformat(),
                    "effective_end": effective.end_date.isoformat(),
                    "segment_start": requirement.start_date.isoformat(),
                    "segment_end": requirement.end_date.isoformat(),
                },
            )
        return (
            approval_revision_id,
            approved_entry_key,
            approved_start,
            approved_end,
            operational_version,
        )

    def _audit_window_override(
        self,
        *,
        override_id: str,
        requirement: ResourceRequirement,
        before_start: Any,
        before_end: Any,
        approved_start: Any,
        approved_end: Any,
        reason: str,
        correlation_id: str,
        operation: str,
    ) -> None:
        self._journal.append(
            entity_type="PLANNING_WINDOW_OVERRIDE",
            entity_id=override_id,
            entity_reference=self._segment_reference(requirement),
            parent_reference=requirement.workforce_request_id,
            action=operation,
            before={
                "effective_start_date": before_start.isoformat(),
                "effective_end_date": before_end.isoformat(),
            },
            after={
                "approved_start_date": approved_start.isoformat(),
                "approved_end_date": approved_end.isoformat(),
                "effective_start_date": requirement.start_date.isoformat(),
                "effective_end_date": requirement.end_date.isoformat(),
                "approval_revision_id": requirement.approval_revision_id,
                "approved_entry_key": requirement.approved_entry_key,
                "planning_version": self._versioning.current_version(),
                "reason": _text(reason),
                "correlation_id": _text(correlation_id),
            },
        )

    def _shift(self, identifier: str) -> Shift:
        wanted = _text(identifier)
        shift = self._session.scalar(
            select(Shift).where(
                (Shift.id == wanted) | (Shift.legacy_allocation_id == wanted)
            )
        )
        if shift is None:
            raise KeyError(f"Allocation {wanted} introuvable")
        return shift

    def _requirement(self, shift: Shift) -> ResourceRequirement:
        requirement = self._session.get(
            ResourceRequirement,
            shift.resource_requirement_id,
        )
        if requirement is None:
            raise KeyError(f"Segment {shift.resource_requirement_id} introuvable")
        if requirement.status in INACTIVE_REQUIREMENT_STATUSES:
            raise ValueError("Le segment de ce quart n'est plus actif.")
        return requirement

    def _resource(self, resource_id: str) -> Resource:
        wanted = _text(resource_id)
        if not wanted:
            raise ApplicationValidationError(
                "Un identifiant stable de ressource est requis.",
                code="allocation_resource_required",
            )
        resource = self._session.get(Resource, wanted)
        if resource is None:
            raise KeyError(f"Ressource {wanted} introuvable")
        if not resource.active:
            raise ValueError(f"La ressource {wanted} est inactive.")
        return resource

    def _locked_hours(self, requirement_id: str) -> Decimal:
        value = self._session.scalar(
            select(func.coalesce(func.sum(Shift.hours), 0)).where(
                Shift.resource_requirement_id == requirement_id,
                Shift.locked == true(),
            )
        )
        return _hours(value or 0)

    @staticmethod
    def _reference(shift: Shift) -> str:
        return _text(shift.legacy_allocation_id) or shift.id

    @staticmethod
    def _segment_reference(requirement: ResourceRequirement) -> str:
        return _text(requirement.legacy_segment_id) or requirement.id

    @staticmethod
    def _is_non_counted_proposal(shift: Shift) -> bool:
        return (
            _text(shift.source).upper() == "AUTO"
            and _text(shift.allocation_type) == MISSING_ALLOCATION_TYPE
        )

    def _authorization_context(
        self,
        requirement: ResourceRequirement,
        *,
        expected_approval_revision_id: str | None,
        expected_operational_version: int | None,
        require_operational_version: bool,
    ) -> tuple[str | None, int | None]:
        if requirement.origin != ORIGIN_REQUEST or not requirement.workforce_request_id:
            return None, None

        expected_revision = _text(expected_approval_revision_id)
        if not expected_revision:
            raise ApplicationValidationError(
                "La révision approuvée attendue est requise pour ce besoin.",
                code="allocation_approval_revision_required",
                context={"segment_id": self._segment_reference(requirement)},
            )

        reference = self._session.get(
            RequestApprovalReference,
            requirement.workforce_request_id,
        )
        active_revision = _text(reference.active_revision_id) if reference is not None else ""
        if (
            reference is None
            or reference.status != APPROVAL_REFERENCE_CAPTURED
            or not active_revision
            or expected_revision != active_revision
            or _text(requirement.approval_revision_id) != active_revision
            or requirement.approval_reference_status != APPROVAL_REFERENCE_CAPTURED
            or not _text(requirement.approved_entry_key)
        ):
            raise ApplicationConflictError(
                "L'autorisation approuvée du besoin a changé depuis sa lecture.",
                code="planning_authorization_unknown",
                context={
                    "segment_id": self._segment_reference(requirement),
                    "expected_approval_revision_id": expected_revision,
                    "current_approval_revision_id": active_revision or None,
                    "approved_entry_key": _text(requirement.approved_entry_key) or None,
                },
            )

        state = self._session.get(
            RequestOperationalState,
            requirement.workforce_request_id,
        )
        if state is not None and _text(state.approval_revision_id) != active_revision:
            raise ApplicationConflictError(
                "Les choix opérationnels ne correspondent plus à la révision approuvée active.",
                code="operational_choice_version_conflict",
                context={
                    "expected_approval_revision_id": active_revision,
                    "operational_approval_revision_id": state.approval_revision_id,
                },
            )
        current_operational = max(int(state.version or 1), 1) if state is not None else 1

        if require_operational_version and expected_operational_version is None:
            raise ApplicationValidationError(
                "La version opérationnelle attendue est requise pour augmenter le budget.",
                code="allocation_operational_version_required",
                context={"segment_id": self._segment_reference(requirement)},
            )
        if (
            expected_operational_version is not None
            and int(expected_operational_version) != current_operational
        ):
            raise ApplicationConflictError(
                "Les choix opérationnels ont été modifiés depuis leur lecture.",
                code="operational_choice_version_conflict",
                context={
                    "expected_version": int(expected_operational_version),
                    "current_version": current_operational,
                },
            )
        return active_revision, current_operational

    def _current_operational_version(
        self,
        requirement: ResourceRequirement,
    ) -> int | None:
        if not requirement.workforce_request_id:
            return None
        state = self._session.get(
            RequestOperationalState,
            requirement.workforce_request_id,
        )
        return max(int(state.version or 1), 1) if state is not None else 1

    def _validate_source(self, shift: Shift) -> None:
        if self._is_non_counted_proposal(shift):
            raise ApplicationValidationError(
                "Une proposition « Hors horaire requis » non comptabilisée ne peut pas être partagée ou dupliquée.",
                code="allocation_source_not_counted",
                context={"allocation_id": self._reference(shift)},
            )

    def _audit(
        self,
        *,
        operation: str,
        source_before: Mapping[str, object],
        source: Shift,
        target: Shift,
        requirement: ResourceRequirement,
        before_planned: Decimal,
        before_locked: Decimal,
        after_locked: Decimal,
        policy: str | None,
        approval_revision_id: str | None,
        expected_planning_version: int,
        operational_version: int | None,
        expected_operational_version: int | None,
        correlation_id: str | None,
        auto_source_converted: bool,
    ) -> None:
        current = self._journal.shift_snapshot(source.id)
        if current is None:
            raise RuntimeError("Le quart source est introuvable après la mutation composite.")
        entity_id, entity_reference, parent, source_after = current
        after = dict(source_after)
        after.update(
            {
                "composite_operation": operation,
                "target_allocation_id": self._reference(target),
                "target_shift_id": target.id,
                "target_resource_id": target.resource_id,
                "target_work_date": target.work_date.isoformat(),
                "target_hours": float(target.hours),
                "requirement_id": requirement.id,
                "planned_hours_before": float(before_planned),
                "planned_hours_after": float(requirement.planned_hours),
                "locked_hours_before": float(before_locked),
                "locked_hours_after": float(after_locked),
                "excess_hours_before": round(
                    max(float(before_locked - before_planned), 0.0),
                    2,
                ),
                "excess_hours_after": round(
                    max(float(after_locked - requirement.planned_hours), 0.0),
                    2,
                ),
                "overallocation_policy": policy,
                "approval_revision_id": approval_revision_id,
                "expected_planning_version": expected_planning_version,
                "planning_version": self._versioning.current_version(),
                "expected_operational_version": expected_operational_version,
                "operational_version": operational_version,
                "correlation_id": _text(correlation_id) or None,
                "auto_source_converted": auto_source_converted,
            }
        )
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action=(
                "Partage atomique de quart"
                if operation == "SPLIT"
                else "Duplication atomique de quart"
            ),
            before=source_before,
            after=after,
        )

    def _result(
        self,
        *,
        operation: str,
        source: Shift,
        target: Shift,
        requirement: ResourceRequirement,
        approval_revision_id: str | None,
        auto_source_converted: bool,
    ) -> Mapping[str, Any]:
        locked = self._locked_hours(requirement.id)
        planned = _hours(requirement.planned_hours)
        return {
            "operation": operation,
            "source_allocation_id": self._reference(source),
            "target_allocation_id": self._reference(target),
            "source_shift_id": source.id,
            "target_shift_id": target.id,
            "segment_id": self._segment_reference(requirement),
            "requirement_id": requirement.id,
            "source_hours": float(source.hours),
            "target_hours": float(target.hours),
            "planned_hours": float(planned),
            "locked_hours": float(locked),
            "excess_hours": round(max(float(locked - planned), 0.0), 2),
            "planning_version": self._versioning.current_version(),
            "approval_revision_id": approval_revision_id,
            "operational_version": self._current_operational_version(requirement),
            "auto_source_converted": auto_source_converted,
            "asset_assignment_policy": "SOURCE_RETAINS",
            "target_asset_assignment_inherited": False,
        }

    def _drop_request_context(
        self,
        requirement: ResourceRequirement,
    ) -> tuple[str | None, int | None]:
        if not requirement.workforce_request_id:
            return None, None
        request = self._session.get(WorkforceRequest, requirement.workforce_request_id)
        if request is None:
            return None, None
        return (
            _text(request.legacy_demand_number) or request.id,
            max(int(request.aggregate_version or 1), 1),
        )

    def evaluate_drop(
        self,
        command: AllocationDropEvaluateCommand,
    ) -> Mapping[str, Any]:
        """Read-only backend classification for one DnD target."""

        source = self._shift(command.allocation_id)
        requirement = self._requirement(source)
        self._validate_source(source)
        target_resource = self._resource(command.resource_id)
        target_day = command.day

        current_start = requirement.start_date
        current_end = requirement.end_date
        proposed_start = min(current_start, target_day)
        proposed_end = max(current_end, target_day)
        before_locked = self._locked_hours(requirement.id)
        source_hours = _hours(source.hours)
        projected_locked = (
            before_locked
            - (source_hours if source.locked else Decimal("0"))
            + source_hours
        )

        _day, impact, availability = evaluate_projected_manual_state(
            self._session,
            requirement,
            target_resource,
            target_day,
            command.outside_standard_hours,
            current_locked_hours=before_locked,
            projected_locked_hours=projected_locked,
            window_start=proposed_start,
            window_end=proposed_end,
        )
        demand_number, request_version = self._drop_request_context(requirement)

        approval_revision_id: str | None = None
        approved_entry_key: str | None = None
        request_line_id: str | None = None
        period_key: str | None = None
        approved_start = None
        approved_end = None
        within_authorization = requirement.origin != ORIGIN_REQUEST
        authorization_reason = "NOT_APPLICABLE"

        if requirement.origin == ORIGIN_REQUEST:
            if self._authorization is None:
                authorization_reason = "APPROVAL_REFERENCE_UNKNOWN"
            else:
                decision = self._authorization.operational_window_authorization(
                    self._segment_reference(requirement),
                    target_day,
                )
                approval_revision_id = _text(
                    decision.get("approval_revision_id")
                ) or None
                approved_entry_key = _text(
                    decision.get("approved_entry_key")
                ) or None
                if approved_entry_key:
                    identity = envelope_entry_identity_from_stable_key(approved_entry_key)
                    request_line_id = identity.line_id
                    period_key = identity.period_key
                approved_start = decision.get("approved_start")
                approved_end = decision.get("approved_end")
                within_authorization = bool(decision.get("authorized"))
                authorization_reason = (
                    "WITHIN_APPROVED_ENTRY"
                    if within_authorization
                    else "WINDOW_EXTENSION_REAPPROVAL_REQUIRED"
                )

        inside_current = current_start <= target_day <= current_end
        if (
            command.include_planning_window_override_options
            and inside_current
            and requirement.origin == ORIGIN_REQUEST
            and not within_authorization
            and approval_revision_id
            and approved_entry_key
            and approved_start is not None
            and approved_end is not None
        ):
            active_override = SqlPlanningWindowOverrideRepository(
                self._session
            ).active_by_requirement_ids(
                (requirement.id,),
                approval_revision_id=approval_revision_id,
            ).get(requirement.id)
            if active_override is not None:
                effective = SqlPlanningWindowOverrideRepository.effective_window_for_requirement(
                    requirement,
                    active_override,
                    approval_revision_id=approval_revision_id,
                    approved_entry_key=approved_entry_key,
                    approved_start_date=approved_start,
                    approved_end_date=approved_end,
                )
                if effective.start_date <= target_day <= effective.end_date:
                    authorization_reason = "WITHIN_ACTIVE_PLANNING_WINDOW_OVERRIDE"
        actions: list[dict[str, object]] = []
        if inside_current:
            actions.extend(
                (
                    {
                        "code": "MOVE",
                        "label": "Déplacer",
                        "enabled": True,
                        "required_parameters": [],
                    },
                    {
                        "code": "SPLIT",
                        "label": "Partager",
                        "enabled": True,
                        "required_parameters": ["transfer_hours"],
                    },
                    {
                        "code": "DUPLICATE",
                        "label": "Dupliquer",
                        "enabled": True,
                        "required_parameters": [],
                    },
                )
            )
        elif within_authorization:
            actions.append(
                {
                    "code": "EXTEND_AND_MOVE",
                    "label": "Étendre la période et déplacer",
                    "enabled": True,
                    "required_parameters": ["confirm_window_extension"],
                }
            )
        else:
            if (
                command.include_planning_window_override_options
                and requirement.origin == ORIGIN_REQUEST
                and self._can_override_planning_window()
            ):
                authorization_reason = "PLANNING_WINDOW_OVERRIDE_AVAILABLE"
                actions.append(
                    {
                        "code": "OVERRIDE_WINDOW_AND_MOVE",
                        "label": "Déroger à la fenêtre et déplacer",
                        "enabled": True,
                        "required_parameters": [
                            "reason",
                            "expected_planning_version",
                            "expected_approval_revision_id",
                            "idempotency_key",
                        ],
                    }
                )
            actions.append(
                {
                    "code": "PROPOSE_WINDOW_EXTENSION",
                    "label": "Soumettre l'extension de période",
                    "enabled": True,
                    "required_parameters": ["expected_request_version"],
                }
            )
        actions.append(
            {
                "code": "CANCEL",
                "label": "Annuler",
                "enabled": True,
                "required_parameters": [],
            }
        )

        warnings: list[dict[str, object]] = []
        if availability.override_required:
            warnings.append(
                {
                    "code": "OUTSIDE_STANDARD_HOURS_REQUIRED",
                    "message": (
                        "La ressource n'est pas disponible selon son horaire standard "
                        "cette journée; une autorisation hors horaire est requise."
                    ),
                }
            )
        if impact.increases_exception:
            warnings.append(
                {
                    "code": "OVERALLOCATION_CHOICE_REQUIRED",
                    "message": (
                        "Le verrouillage final augmenterait la surallocation; une "
                        "politique explicite est requise à l'exécution."
                    ),
                    "excess_hours": impact.projected_excess_hours,
                }
            )

        return {
            "allocation_id": self._reference(source),
            "source_shift_id": source.id,
            "segment_id": self._segment_reference(requirement),
            "requirement_id": requirement.id,
            "origin": requirement.origin,
            "source_resource_id": source.resource_id,
            "target_resource_id": target_resource.id,
            "target_day": target_day.isoformat(),
            "current_window": {
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
            },
            "proposed_window": {
                "start": proposed_start.isoformat(),
                "end": proposed_end.isoformat(),
            },
            "planning_version": self._versioning.current_version(),
            "approval_revision_id": approval_revision_id,
            "approved_entry_key": approved_entry_key,
            "request_line_id": request_line_id,
            "period_key": period_key,
            "approved_window": (
                {
                    "start": approved_start.isoformat(),
                    "end": approved_end.isoformat(),
                }
                if approved_start is not None and approved_end is not None
                else None
            ),
            "request_number": demand_number,
            "request_version": request_version,
            "operational_version": self._current_operational_version(requirement),
            "authorization_decision": authorization_reason,
            "availability_hours": availability.state.hours,
            "planned_hours": float(requirement.planned_hours),
            "current_locked_hours": float(before_locked),
            "projected_locked_hours": float(projected_locked),
            "projected_excess_hours": impact.projected_excess_hours,
            "actions": actions,
            "warnings": warnings,
        }

    def extend_and_move(
        self,
        command: AllocationExtendMoveCommand,
    ) -> Mapping[str, Any]:
        """Atomically widen an already-authorized window and execute the move."""

        self._versioning.acquire(command.expected_planning_version)
        source = self._shift(command.allocation_id)
        requirement = self._requirement(source)
        self._validate_source(source)
        target_resource = self._resource(command.resource_id)
        target_day = command.day
        if requirement.start_date <= target_day <= requirement.end_date:
            raise ApplicationValidationError(
                "La cible est déjà dans la fenêtre du besoin; utilise le déplacement simple.",
                code="allocation_window_extension_not_required",
                context={"allocation_id": self._reference(source)},
            )

        before_start = requirement.start_date
        before_end = requirement.end_date
        proposed_start = min(before_start, target_day)
        proposed_end = max(before_end, target_day)
        before_locked = self._locked_hours(requirement.id)
        source_hours = _hours(source.hours)
        projected_locked = (
            before_locked
            - (source_hours if source.locked else Decimal("0"))
            + source_hours
        )
        policy = normalize_overallocation_policy(command.overallocation_policy)

        approval_revision_id: str | None = None
        if requirement.origin == ORIGIN_REQUEST:
            approval_revision_id, _ = self._authorization_context(
                requirement,
                expected_approval_revision_id=command.expected_approval_revision_id,
                expected_operational_version=command.expected_operational_version,
                require_operational_version=policy == INCREASE_PLANNED,
            )
            if self._authorization is None:
                raise ApplicationConflictError(
                    "L'autorisation de fenêtre n'est pas disponible.",
                    code="planning_authorization_unknown",
                )
            decision = self._authorization.operational_window_authorization(
                self._segment_reference(requirement),
                target_day,
                expected_approval_revision_id=command.expected_approval_revision_id,
            )
            if not bool(decision.get("authorized")):
                raise ApplicationConflictError(
                    "La date cible dépasse l'entrée approuvée active. "
                    "L'extension doit passer par la demande candidate.",
                    code="planning_authorization_revision_required",
                    context={
                        "segment_id": self._segment_reference(requirement),
                        "approval_revision_id": decision.get("approval_revision_id"),
                        "approved_entry_key": decision.get("approved_entry_key"),
                        "target_day": target_day.isoformat(),
                    },
                )

        _day, impact, availability = evaluate_projected_manual_state(
            self._session,
            requirement,
            target_resource,
            target_day,
            command.outside_standard_hours,
            current_locked_hours=before_locked,
            projected_locked_hours=projected_locked,
            window_start=proposed_start,
            window_end=proposed_end,
        )
        if not availability.allowed:
            raise ValueError(
                "La ressource n'est pas disponible selon son horaire standard cette journée. "
                "Autorise explicitement le quart hors horaire pour continuer."
            )
        if impact.increases_exception and policy is None:
            raise ApplicationValidationError(
                "Ce déplacement augmenterait la surallocation. Choisis explicitement "
                "de conserver l'exception ou d'augmenter les heures prévues.",
                code="allocation_overallocation_choice_required",
                context={
                    "segment_id": self._segment_reference(requirement),
                    "planned_hours": impact.planned_hours,
                    "projected_locked_hours": impact.projected_locked_hours,
                    "excess_hours": impact.projected_excess_hours,
                },
            )
        if (
            policy == INCREASE_PLANNED
            and impact.projected_excess_hours > TOLERANCE_HOURS
        ):
            if self._authorization is not None:
                self._authorization.authorize_planned_hours(
                    self._segment_reference(requirement),
                    impact.projected_locked_hours,
                    explicit_increase=True,
                    expected_operational_version=command.expected_operational_version,
                )
            requirement.planned_hours = _hours(impact.projected_locked_hours)
        elif impact.increases_exception and policy != KEEP_EXCEPTION:
            raise ApplicationValidationError(
                "Une décision explicite est requise pour la surallocation manuelle.",
                code="allocation_overallocation_choice_required",
            )

        shift_before_row = self._journal.shift_snapshot(source.id)
        requirement_before_row = self._journal.requirement_snapshot(requirement.id)
        requirement_before_metrics = _segment_metrics(self._session, requirement.id)
        if shift_before_row is None or requirement_before_row is None:
            raise RuntimeError("L'état initial du déplacement atomique est introuvable.")

        shift_before = dict(shift_before_row[3])
        requirement_before = dict(requirement_before_row[2])
        requirement.start_date = proposed_start
        requirement.end_date = proposed_end
        source.resource_id = target_resource.id
        source.work_date = target_day
        source.source = "MANUAL"
        source.locked = True
        source.outside_standard_hours = availability.override_applied
        SqlAssetService(
            self._session,
            actor="planning",
        ).synchronize_shift_ad_hoc_assignment(shift=source)
        self._session.flush()
        self._planning.rebuild()

        persisted = self._session.get(Shift, source.id)
        if persisted is None:
            raise RuntimeError("Le recalcul a perdu le quart déplacé.")
        if (
            persisted.resource_requirement_id != requirement.id
            or persisted.resource_id != target_resource.id
            or persisted.work_date != target_day
            or not persisted.locked
            or _text(persisted.source).upper() != "MANUAL"
            or requirement.start_date != proposed_start
            or requirement.end_date != proposed_end
        ):
            raise RuntimeError("Les invariants de l'extension + déplacement ne sont pas respectés.")

        append_overallocation_audit(
            self._session,
            self._journal,
            self._segment_reference(requirement),
            requirement_before_row,
            requirement_before_metrics,
            policy=policy,
        )
        current_req = self._journal.requirement_snapshot(requirement.id)
        current_shift = self._journal.shift_snapshot(persisted.id)
        if current_req is not None:
            req_after = dict(current_req[2])
            req_after.update(
                {
                    "correlation_id": _text(command.correlation_id) or None,
                    "expected_planning_version": command.expected_planning_version,
                    "planning_version": self._versioning.current_version(),
                    "approval_revision_id": approval_revision_id,
                }
            )
            self._journal.append(
                entity_type="SEGMENT",
                entity_id=current_req[0],
                entity_reference=current_req[1],
                action="Extension atomique de fenêtre",
                before=requirement_before,
                after=req_after,
            )
        if current_shift is not None:
            shift_after = dict(current_shift[3])
            shift_after.update(
                {
                    "correlation_id": _text(command.correlation_id) or None,
                    "expected_planning_version": command.expected_planning_version,
                    "planning_version": self._versioning.current_version(),
                    "approval_revision_id": approval_revision_id,
                }
            )
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=current_shift[0],
                entity_reference=current_shift[1],
                parent_reference=current_shift[2],
                action="Déplacement atomique après extension",
                before=shift_before,
                after=shift_after,
            )

        return {
            "operation": "EXTEND_AND_MOVE",
            "source_allocation_id": self._reference(persisted),
            "target_allocation_id": self._reference(persisted),
            "source_shift_id": persisted.id,
            "target_shift_id": persisted.id,
            "segment_id": self._segment_reference(requirement),
            "requirement_id": requirement.id,
            "source_hours": float(persisted.hours),
            "target_hours": float(persisted.hours),
            "planned_hours": float(requirement.planned_hours),
            "locked_hours": float(self._locked_hours(requirement.id)),
            "excess_hours": round(
                max(
                    float(self._locked_hours(requirement.id) - _hours(requirement.planned_hours)),
                    0.0,
                ),
                2,
            ),
            "planning_version": self._versioning.current_version(),
            "approval_revision_id": approval_revision_id,
            "operational_version": self._current_operational_version(requirement),
            "auto_source_converted": _text(shift_before.get("source")).upper() == "AUTO",
        }

    def extend_planning_window(
        self,
        command: PlanningWindowOverrideExtendCommand,
    ) -> Mapping[str, Any]:
        actor_user_id = self._require_window_override_authority()
        payload = {
            "segment_id": command.segment_id,
            "start_date": command.start_date.isoformat(),
            "end_date": command.end_date.isoformat(),
            "reason": _text(command.reason),
            "expected_planning_version": command.expected_planning_version,
            "expected_approval_revision_id": command.expected_approval_revision_id,
            "expected_operational_version": command.expected_operational_version,
        }
        correlation_id = _text(command.correlation_id) or _text(command.idempotency_key)
        return SqlCommandIdempotencyAdapter(
            self._session,
            actor_name=actor_user_id,
        ).replay_or_execute(
            scope="planning_window_override.extend_segment",
            key=command.idempotency_key,
            request_fingerprint=request_fingerprint(payload),
            action=lambda: self._extend_planning_window_once(
                command,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            ),
        )

    def _extend_planning_window_once(
        self,
        command: PlanningWindowOverrideExtendCommand,
        *,
        actor_user_id: str,
        correlation_id: str,
    ) -> Mapping[str, Any]:
        self._versioning.acquire(command.expected_planning_version)
        requirement = self._requirement_by_identifier(command.segment_id)
        (
            approval_revision_id,
            approved_entry_key,
            approved_start,
            approved_end,
            operational_version,
        ) = self._planning_window_context(
            requirement,
            expected_approval_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
        )
        before_start = requirement.start_date
        before_end = requirement.end_date
        if (
            command.start_date > before_start
            or command.end_date < before_end
            or (
                command.start_date == before_start
                and command.end_date == before_end
            )
        ):
            raise ApplicationValidationError(
                "La commande doit élargir la fenêtre opérationnelle actuelle sans la réduire.",
                code="planning_window_override_widening_required",
                context={
                    "current_start": before_start.isoformat(),
                    "current_end": before_end.isoformat(),
                },
            )

        assert_segment_asset_mutation_compatible(
            self._session,
            requirement,
            target_start_date=command.start_date,
            target_end_date=command.end_date,
        )
        before_row = self._journal.requirement_snapshot(requirement.id)
        override = SqlPlanningWindowOverrideRepository(self._session).apply_widening(
            requirement,
            approval_revision_id=approval_revision_id,
            approved_entry_key=approved_entry_key,
            approved_start_date=approved_start,
            approved_end_date=approved_end,
            effective_start_date=command.start_date,
            effective_end_date=command.end_date,
            actor_user_id=actor_user_id,
            reason=command.reason,
            correlation_id=correlation_id,
        )
        requirement.start_date = command.start_date
        requirement.end_date = command.end_date
        self._session.flush()
        self._planning.rebuild()

        persisted = self._session.get(ResourceRequirement, requirement.id)
        if (
            persisted is None
            or persisted.start_date != command.start_date
            or persisted.end_date != command.end_date
        ):
            raise RuntimeError(
                "Le recalcul n'a pas conservé la dérogation de fenêtre Planning."
            )
        invalid_locked = self._session.scalar(
            select(func.count())
            .select_from(Shift)
            .where(
                Shift.resource_requirement_id == persisted.id,
                Shift.locked == true(),
                (
                    (Shift.work_date < persisted.start_date)
                    | (Shift.work_date > persisted.end_date)
                ),
            )
        )
        if int(invalid_locked or 0) > 0:
            raise RuntimeError(
                "Un quart verrouillé se trouve hors de la fenêtre effective finale."
            )

        self._audit_window_override(
            override_id=override.id,
            requirement=persisted,
            before_start=before_start,
            before_end=before_end,
            approved_start=approved_start,
            approved_end=approved_end,
            reason=command.reason,
            correlation_id=correlation_id,
            operation="Dérogation explicite de fenêtre Planning",
        )
        if before_row is not None:
            current = self._journal.requirement_snapshot(persisted.id)
            if current is not None:
                after = dict(current[2])
                after.update(
                    {
                        "planning_window_override_id": override.id,
                        "correlation_id": correlation_id,
                        "planning_version": self._versioning.current_version(),
                    }
                )
                self._journal.append(
                    entity_type="SEGMENT",
                    entity_id=current[0],
                    entity_reference=current[1],
                    action="Extension opérationnelle dérogée",
                    before=dict(before_row[2]),
                    after=after,
                )

        return {
            "operation": "OVERRIDE_WINDOW",
            "segment_id": self._segment_reference(persisted),
            "requirement_id": persisted.id,
            "override_id": override.id,
            "approved_window": {
                "start": approved_start.isoformat(),
                "end": approved_end.isoformat(),
            },
            "effective_window": {
                "start": persisted.start_date.isoformat(),
                "end": persisted.end_date.isoformat(),
            },
            "planning_version": self._versioning.current_version(),
            "approval_revision_id": approval_revision_id,
            "operational_version": operational_version,
        }

    def override_and_move(
        self,
        command: AllocationWindowOverrideMoveCommand,
    ) -> Mapping[str, Any]:
        actor_user_id = self._require_window_override_authority()
        payload = {
            "allocation_id": command.allocation_id,
            "resource_id": command.resource_id,
            "day": command.day.isoformat(),
            "reason": _text(command.reason),
            "expected_planning_version": command.expected_planning_version,
            "expected_approval_revision_id": command.expected_approval_revision_id,
            "expected_operational_version": command.expected_operational_version,
            "outside_standard_hours": command.outside_standard_hours,
            "overallocation_policy": command.overallocation_policy,
        }
        correlation_id = _text(command.correlation_id) or _text(command.idempotency_key)
        return SqlCommandIdempotencyAdapter(
            self._session,
            actor_name=actor_user_id,
        ).replay_or_execute(
            scope="planning_window_override.move_shift",
            key=command.idempotency_key,
            request_fingerprint=request_fingerprint(payload),
            action=lambda: self._override_and_move_once(
                command,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            ),
        )

    def _override_and_move_once(
        self,
        command: AllocationWindowOverrideMoveCommand,
        *,
        actor_user_id: str,
        correlation_id: str,
    ) -> Mapping[str, Any]:
        self._versioning.acquire(command.expected_planning_version)
        source = self._shift(command.allocation_id)
        requirement = self._requirement(source)
        self._validate_source(source)
        target_resource = self._resource(command.resource_id)
        target_day = command.day
        (
            approval_revision_id,
            approved_entry_key,
            approved_start,
            approved_end,
            operational_version,
        ) = self._planning_window_context(
            requirement,
            expected_approval_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
        )
        if requirement.start_date <= target_day <= requirement.end_date:
            raise ApplicationValidationError(
                "La date cible est déjà dans la fenêtre effective; utilise le déplacement normal.",
                code="planning_window_override_not_required",
            )
        if approved_start <= target_day <= approved_end:
            raise ApplicationValidationError(
                "La date cible reste dans l'entrée approuvée; utilise l'extension autorisée existante.",
                code="planning_window_override_not_required",
            )

        before_start = requirement.start_date
        before_end = requirement.end_date
        proposed_start = min(before_start, target_day)
        proposed_end = max(before_end, target_day)
        assert_segment_asset_mutation_compatible(
            self._session,
            requirement,
            target_start_date=proposed_start,
            target_end_date=proposed_end,
        )

        before_locked = self._locked_hours(requirement.id)
        source_hours = _hours(source.hours)
        projected_locked = (
            before_locked
            - (source_hours if source.locked else Decimal("0"))
            + source_hours
        )
        policy = normalize_overallocation_policy(command.overallocation_policy)
        if policy == INCREASE_PLANNED:
            raise ApplicationValidationError(
                "Une dérogation de dates ne modifie jamais le budget ou les heures prévues.",
                code="planning_window_override_budget_immutable",
            )
        _day, impact, availability = evaluate_projected_manual_state(
            self._session,
            requirement,
            target_resource,
            target_day,
            command.outside_standard_hours,
            current_locked_hours=before_locked,
            projected_locked_hours=projected_locked,
            window_start=proposed_start,
            window_end=proposed_end,
        )
        if not availability.allowed:
            raise ApplicationValidationError(
                "La ressource n'est pas disponible selon son horaire standard cette journée. "
                "Autorise explicitement le quart hors horaire pour continuer.",
                code="allocation_outside_standard_hours_confirmation_required",
            )
        if impact.increases_exception and policy != KEEP_EXCEPTION:
            raise ApplicationValidationError(
                "Ce déplacement augmenterait la surallocation; KEEP_EXCEPTION doit être confirmé explicitement.",
                code="allocation_overallocation_choice_required",
                context={
                    "segment_id": self._segment_reference(requirement),
                    "planned_hours": impact.planned_hours,
                    "projected_locked_hours": impact.projected_locked_hours,
                    "excess_hours": impact.projected_excess_hours,
                },
            )

        shift_before_row = self._journal.shift_snapshot(source.id)
        requirement_before_row = self._journal.requirement_snapshot(requirement.id)
        requirement_before_metrics = _segment_metrics(self._session, requirement.id)
        if shift_before_row is None or requirement_before_row is None:
            raise RuntimeError(
                "L'état initial du déplacement dérogé est introuvable."
            )
        shift_before = dict(shift_before_row[3])

        override = SqlPlanningWindowOverrideRepository(self._session).apply_widening(
            requirement,
            approval_revision_id=approval_revision_id,
            approved_entry_key=approved_entry_key,
            approved_start_date=approved_start,
            approved_end_date=approved_end,
            effective_start_date=proposed_start,
            effective_end_date=proposed_end,
            actor_user_id=actor_user_id,
            reason=command.reason,
            correlation_id=correlation_id,
        )
        requirement.start_date = proposed_start
        requirement.end_date = proposed_end
        source.resource_id = target_resource.id
        source.work_date = target_day
        source.source = "MANUAL"
        source.locked = True
        source.outside_standard_hours = availability.override_applied
        SqlAssetService(
            self._session,
            actor=actor_user_id,
        ).synchronize_shift_ad_hoc_assignment(shift=source)
        self._session.flush()
        self._planning.rebuild()

        persisted = self._session.get(Shift, source.id)
        current_requirement = self._session.get(ResourceRequirement, requirement.id)
        if (
            persisted is None
            or current_requirement is None
            or persisted.resource_requirement_id != requirement.id
            or persisted.resource_id != target_resource.id
            or persisted.work_date != target_day
            or not persisted.locked
            or _text(persisted.source).upper() != "MANUAL"
            or current_requirement.start_date != proposed_start
            or current_requirement.end_date != proposed_end
        ):
            raise RuntimeError(
                "Les invariants du déplacement avec dérogation ne sont pas respectés."
            )

        append_overallocation_audit(
            self._session,
            self._journal,
            self._segment_reference(current_requirement),
            requirement_before_row,
            requirement_before_metrics,
            policy=policy,
        )
        self._audit_window_override(
            override_id=override.id,
            requirement=current_requirement,
            before_start=before_start,
            before_end=before_end,
            approved_start=approved_start,
            approved_end=approved_end,
            reason=command.reason,
            correlation_id=correlation_id,
            operation="Dérogation de fenêtre et déplacement atomique",
        )
        current_req = self._journal.requirement_snapshot(current_requirement.id)
        current_shift = self._journal.shift_snapshot(persisted.id)
        if current_req is not None:
            req_after = dict(current_req[2])
            req_after.update(
                {
                    "planning_window_override_id": override.id,
                    "correlation_id": correlation_id,
                    "expected_planning_version": command.expected_planning_version,
                    "planning_version": self._versioning.current_version(),
                    "approval_revision_id": approval_revision_id,
                }
            )
            self._journal.append(
                entity_type="SEGMENT",
                entity_id=current_req[0],
                entity_reference=current_req[1],
                action="Extension opérationnelle dérogée",
                before=dict(requirement_before_row[2]),
                after=req_after,
            )
        if current_shift is not None:
            shift_after = dict(current_shift[3])
            shift_after.update(
                {
                    "planning_window_override_id": override.id,
                    "correlation_id": correlation_id,
                    "expected_planning_version": command.expected_planning_version,
                    "planning_version": self._versioning.current_version(),
                    "approval_revision_id": approval_revision_id,
                }
            )
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=current_shift[0],
                entity_reference=current_shift[1],
                parent_reference=current_shift[2],
                action="Déplacement atomique avec dérogation",
                before=shift_before,
                after=shift_after,
            )

        return {
            "operation": "OVERRIDE_WINDOW_AND_MOVE",
            "segment_id": self._segment_reference(current_requirement),
            "requirement_id": current_requirement.id,
            "override_id": override.id,
            "approved_window": {
                "start": approved_start.isoformat(),
                "end": approved_end.isoformat(),
            },
            "effective_window": {
                "start": current_requirement.start_date.isoformat(),
                "end": current_requirement.end_date.isoformat(),
            },
            "planning_version": self._versioning.current_version(),
            "approval_revision_id": approval_revision_id,
            "operational_version": operational_version,
            "allocation_id": self._reference(persisted),
            "shift_id": persisted.id,
            "resource_id": persisted.resource_id,
            "work_date": persisted.work_date.isoformat(),
            "auto_source_converted": _text(shift_before.get("source")).upper() == "AUTO",
        }

    def split(self, command: AllocationSplitCommand) -> Mapping[str, Any]:
        self._versioning.acquire(command.expected_planning_version)
        source = self._shift(command.allocation_id)
        requirement = self._requirement(source)
        self._validate_source(source)
        target_resource = self._resource(command.resource_id)

        source_before_row = self._journal.shift_snapshot(source.id)
        if source_before_row is None:
            raise RuntimeError("Le quart source est introuvable avant le partage.")
        source_before = dict(source_before_row[3])
        requirement_before_snapshot = self._journal.requirement_snapshot(requirement.id)
        requirement_before_metrics = _segment_metrics(self._session, requirement.id)
        source_hours = _hours(source.hours)
        transfer = _hours(command.transfer_hours)
        if transfer <= 0 or transfer >= source_hours:
            raise ApplicationValidationError(
                "Le partage exige un transfert strictement inférieur aux heures du quart source; utilise le déplacement pour un transfert intégral.",
                code="allocation_split_hours_invalid",
                context={
                    "source_hours": float(source_hours),
                    "transfer_hours": float(transfer),
                },
            )

        before_planned = _hours(requirement.planned_hours)
        before_locked = self._locked_hours(requirement.id)
        projected_locked = (
            before_locked
            - (source_hours if source.locked else Decimal("0"))
            + source_hours
        )
        policy = normalize_overallocation_policy(command.overallocation_policy)
        approval_revision_id, _ = self._authorization_context(
            requirement,
            expected_approval_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
            require_operational_version=policy == INCREASE_PLANNED,
        )
        target_outside = (
            bool(source.outside_standard_hours)
            if command.outside_standard_hours is None
            else bool(command.outside_standard_hours)
        )
        target_day, _impact, availability = validate_projected_manual_state(
            self._session,
            requirement,
            target_resource,
            command.day,
            target_outside,
            current_locked_hours=before_locked,
            projected_locked_hours=projected_locked,
            overallocation_policy=policy,
            authorization=self._authorization,
            expected_operational_version=command.expected_operational_version,
        )

        auto_source_converted = _text(source.source).upper() == "AUTO"
        source.hours = source_hours - transfer
        source.source = "MANUAL"
        source.locked = True
        target = Shift(
            legacy_allocation_id=f"MAN-{new_id()}",
            resource_requirement_id=requirement.id,
            resource_id=target_resource.id,
            work_date=target_day,
            hours=transfer,
            allocation_type=source.allocation_type or requirement.planning_type,
            source="MANUAL",
            locked=True,
            outside_standard_hours=availability.override_applied,
            confirmation=source.confirmation,
            note=source.note,
            operational_responsible_override_contact_id=(
                source.operational_responsible_override_contact_id
            ),
        )
        self._session.add(target)
        self._session.flush()
        self._planning.rebuild()

        persisted_source = self._session.get(Shift, source.id)
        persisted_target = self._session.get(Shift, target.id)
        if persisted_source is None or persisted_target is None:
            raise RuntimeError("Le recalcul a perdu un quart du partage atomique.")
        if (
            persisted_source.resource_requirement_id != requirement.id
            or persisted_target.resource_requirement_id != requirement.id
            or not persisted_source.locked
            or not persisted_target.locked
            or _text(persisted_source.source).upper() != "MANUAL"
            or _text(persisted_target.source).upper() != "MANUAL"
            or persisted_target.operational_responsible_override_contact_id
            != persisted_source.operational_responsible_override_contact_id
            or _hours(persisted_source.hours) + _hours(persisted_target.hours) != source_hours
        ):
            raise RuntimeError("Les invariants du partage atomique ne sont pas respectés.")

        after_locked = self._locked_hours(requirement.id)
        operational_version = self._current_operational_version(requirement)
        append_overallocation_audit(
            self._session,
            self._journal,
            self._segment_reference(requirement),
            requirement_before_snapshot,
            requirement_before_metrics,
            policy=policy,
        )
        self._audit(
            operation="SPLIT",
            source_before=source_before,
            source=persisted_source,
            target=persisted_target,
            requirement=requirement,
            before_planned=before_planned,
            before_locked=before_locked,
            after_locked=after_locked,
            policy=policy,
            approval_revision_id=approval_revision_id,
            expected_planning_version=command.expected_planning_version,
            operational_version=operational_version,
            expected_operational_version=command.expected_operational_version,
            correlation_id=command.correlation_id,
            auto_source_converted=auto_source_converted,
        )
        return self._result(
            operation="SPLIT",
            source=persisted_source,
            target=persisted_target,
            requirement=requirement,
            approval_revision_id=approval_revision_id,
            auto_source_converted=auto_source_converted,
        )

    def duplicate(self, command: AllocationDuplicateCommand) -> Mapping[str, Any]:
        self._versioning.acquire(command.expected_planning_version)
        source = self._shift(command.allocation_id)
        requirement = self._requirement(source)
        self._validate_source(source)
        target_resource = self._resource(command.resource_id)

        source_before_row = self._journal.shift_snapshot(source.id)
        if source_before_row is None:
            raise RuntimeError("Le quart source est introuvable avant la duplication.")
        source_before = dict(source_before_row[3])
        requirement_before_snapshot = self._journal.requirement_snapshot(requirement.id)
        requirement_before_metrics = _segment_metrics(self._session, requirement.id)
        source_hours = _hours(source.hours)
        before_planned = _hours(requirement.planned_hours)
        before_locked = self._locked_hours(requirement.id)
        projected_locked = (
            before_locked
            - (source_hours if source.locked else Decimal("0"))
            + source_hours
            + source_hours
        )
        policy = normalize_overallocation_policy(command.overallocation_policy)
        approval_revision_id, _ = self._authorization_context(
            requirement,
            expected_approval_revision_id=command.expected_approval_revision_id,
            expected_operational_version=command.expected_operational_version,
            require_operational_version=policy == INCREASE_PLANNED,
        )
        target_outside = (
            bool(source.outside_standard_hours)
            if command.outside_standard_hours is None
            else bool(command.outside_standard_hours)
        )
        target_day, _impact, availability = validate_projected_manual_state(
            self._session,
            requirement,
            target_resource,
            command.day,
            target_outside,
            current_locked_hours=before_locked,
            projected_locked_hours=projected_locked,
            overallocation_policy=policy,
            authorization=self._authorization,
            expected_operational_version=command.expected_operational_version,
        )

        auto_source_converted = _text(source.source).upper() == "AUTO"
        source.source = "MANUAL"
        source.locked = True
        target = Shift(
            legacy_allocation_id=f"MAN-{new_id()}",
            resource_requirement_id=requirement.id,
            resource_id=target_resource.id,
            work_date=target_day,
            hours=source_hours,
            allocation_type=source.allocation_type or requirement.planning_type,
            source="MANUAL",
            locked=True,
            outside_standard_hours=availability.override_applied,
            confirmation=source.confirmation,
            note=source.note,
            operational_responsible_override_contact_id=(
                source.operational_responsible_override_contact_id
            ),
        )
        self._session.add(target)
        self._session.flush()
        self._planning.rebuild()

        persisted_source = self._session.get(Shift, source.id)
        persisted_target = self._session.get(Shift, target.id)
        if persisted_source is None or persisted_target is None:
            raise RuntimeError("Le recalcul a perdu un quart de la duplication atomique.")
        if (
            persisted_source.resource_requirement_id != requirement.id
            or persisted_target.resource_requirement_id != requirement.id
            or not persisted_source.locked
            or not persisted_target.locked
            or _text(persisted_source.source).upper() != "MANUAL"
            or _text(persisted_target.source).upper() != "MANUAL"
            or persisted_target.operational_responsible_override_contact_id
            != persisted_source.operational_responsible_override_contact_id
            or _hours(persisted_source.hours) != source_hours
            or _hours(persisted_target.hours) != source_hours
        ):
            raise RuntimeError("Les invariants de la duplication atomique ne sont pas respectés.")

        after_locked = self._locked_hours(requirement.id)
        operational_version = self._current_operational_version(requirement)
        append_overallocation_audit(
            self._session,
            self._journal,
            self._segment_reference(requirement),
            requirement_before_snapshot,
            requirement_before_metrics,
            policy=policy,
        )
        self._audit(
            operation="DUPLICATE",
            source_before=source_before,
            source=persisted_source,
            target=persisted_target,
            requirement=requirement,
            before_planned=before_planned,
            before_locked=before_locked,
            after_locked=after_locked,
            policy=policy,
            approval_revision_id=approval_revision_id,
            expected_planning_version=command.expected_planning_version,
            operational_version=operational_version,
            expected_operational_version=command.expected_operational_version,
            correlation_id=command.correlation_id,
            auto_source_converted=auto_source_converted,
        )
        return self._result(
            operation="DUPLICATE",
            source=persisted_source,
            target=persisted_target,
            requirement=requirement,
            approval_revision_id=approval_revision_id,
            auto_source_converted=auto_source_converted,
        )
