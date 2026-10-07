from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from datetime import date, datetime
from typing import Any, ContextManager
from uuid import uuid4

from ..domain.demand_periods import DemandPeriodDefinition, validate_period_definitions
from ..domain.request_lines import default_legacy_hours
from .approval_cycles import ApprovalCycleService
from .approval_voting import ApprovalVoteCommand, ApprovalVoteOutcome, ApprovalVoteService
from .command_ports import ApprovedDemandSyncPort, PlanningCommandPort
from .commands import (
    DemandAlternativeSelectCommand,
    DemandOperationalConfirmationCommand,
    DemandApproveCommand,
    DemandCancelCommand,
    DemandCancellationAcceptCommand,
    DemandCancellationRejectCommand,
    DemandCancellationRequestCommand,
    DemandCorrectionCommand,
    DemandCreateCommand,
    DemandPeriodsReplaceCommand,
    DemandSubmitCommand,
    DemandUpdateCommand,
)
from ..domain.approval_envelope import (
    DECISION_APPROVAL_REFERENCE_UNKNOWN,
    DECISION_EXPLICIT_EXCEPTION_REQUIRED,
    DECISION_INVALID,
    DECISION_REAPPROVAL_REQUIRED,
    REASON_DELEGATED_TOLERANCE,
    EnvelopeDecision,
)
from .errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationOperationError,
    ApplicationValidationError,
    call_application_port,
)
from .demand_requesters import (
    DemandRequesterDirectoryPort,
    is_admissible_requester,
)
from .demand_workflow_policy import (
    ACTION_APPROVE,
    ACTION_CANCEL,
    ACTION_CORRECTION,
    ACTION_ACCEPT_CANCELLATION,
    ACTION_REJECT_CANCELLATION,
    ACTION_REQUEST_CANCELLATION,
    ACTION_MODIFY,
    ACTION_SUBMIT,
    DemandWorkflowBlock,
    DemandWorkflowReadModel,
    assert_demand_action,
    demand_workflow_state,
)
from .security import (
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_MANAGE_DEMANDS,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_PROJECT_MANAGER,
)
from .query_models import DemandCancellationMaterializationReadModel
from .query_ports import PlannerQueryPort
from .read_models import DemandOperationalChoiceReadModel, DemandPeriodReadModel
from .repository_ports import (
    DemandApprovalEnvelopePolicyPort,
    DemandOperationalChoiceRepositoryPort,
    DemandPeriodRepositoryPort,
    DemandRepositoryPort,
    PlanningMutationVersionPort,
)


BUSINESS_DEMAND_FIELDS = frozenset(
    {
        "NumeroProjet",
        "NomProjet",
        "Client",
        "ChargeProjet",
        "SourceEffortID",
        "TaskCode",
        "TypeDemande",
        "Priorite",
        "Confirmation",
        "DateDebutSouhaitee",
        "DateFinSouhaitee",
        "Description",
        "SiteClient",
        "Lieu",
        "NombreRessources",
        "CompetencesRequises",
        "TempsEstimeHeures",
        "TempsEstimeJours",
        "TechnicienPropose",
        "RequestLines",
    }
)

LEGACY_UNKNOWN_REAPPROVAL_FIELDS = BUSINESS_DEMAND_FIELDS - {
    "Description",
    "Confirmation",
    "TechnicienPropose",
}


class DemandService:
    """Application service for the workforce-demand lifecycle."""

    def __init__(
        self,
        demands: DemandRepositoryPort,
        planning: PlanningCommandPort,
        approved_sync: ApprovedDemandSyncPort,
        *,
        periods: DemandPeriodRepositoryPort | None = None,
        operational_choices: DemandOperationalChoiceRepositoryPort | None = None,
        approval_envelope_policy: DemandApprovalEnvelopePolicyPort | None = None,
        requester_directory: DemandRequesterDirectoryPort | None = None,
        current_user: str = "",
        current_user_id: str | None = None,
        permissions: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        planning_versions: PlanningMutationVersionPort | None = None,
        queries: PlannerQueryPort | None = None,
        approval_cycles: ApprovalCycleService | None = None,
        approval_votes: ApprovalVoteService | None = None,
        batch: Callable[[str], ContextManager[Any]] | None = None,
    ) -> None:
        self._demands = demands
        self._planning = planning
        self._approved_sync = approved_sync
        self._periods = periods
        self._operational_choices = operational_choices
        self._approval_envelope_policy = approval_envelope_policy
        self._requester_directory = requester_directory
        self._current_user = str(current_user or "")
        self._current_user_id = str(current_user_id or "").strip() or None
        self._permissions = tuple(permissions) if permissions is not None else (
            PERMISSION_MANAGE_DEMANDS,
            PERMISSION_APPROVE_DEMANDS,
        )
        self._roles = tuple(str(role).strip().upper() for role in (roles or ()) if str(role).strip())
        self._planning_versions = planning_versions
        self._queries = queries
        self._approval_cycles = approval_cycles
        self._approval_votes = approval_votes
        self._batch = batch

    def _context(self, label: str) -> ContextManager[Any]:
        return self._batch(label) if self._batch is not None else nullcontext()

    def _initialize_approval_cycle_after_submission(self, number: str) -> None:
        if self._approval_cycles is None:
            return
        request = self._approval_cycles.get_request(number)
        if request is None or request.status != "Soumise":
            return
        if self._approval_cycles.get_active_cycle(request.id) is not None:
            return
        self._approval_cycles.initialize_cycle(
            request.id,
            expected_version=request.aggregate_version,
        )

    def _invalidate_active_approval_cycle(self, number: str, *, reason: str) -> None:
        if self._approval_cycles is None:
            return
        request = self._approval_cycles.get_request(number)
        if request is None:
            return
        cycle = self._approval_cycles.get_active_cycle(request.id)
        if cycle is None:
            return
        self._approval_cycles.invalidate_cycle(
            request.id,
            expected_version=request.aggregate_version,
            reason=reason,
        )

    def _refresh_submitted_approval_cycle_if_subject_changed(
        self,
        number: str,
        *,
        reason: str,
    ) -> None:
        """Replace an OPEN snapshot when a still-submitted subject is edited.

        New submissions own an explicit 276B cycle. Candidate edits made before the
        first approval vote must therefore refresh that snapshot instead of leaving a
        stale cycle that can only fail later at approval time. Legacy submitted
        requests without a cycle remain explicit-initialization only.
        """
        if self._approval_cycles is None:
            return
        request = self._approval_cycles.get_request(number)
        if request is None or request.status != "Soumise":
            return
        cycle = self._approval_cycles.get_active_cycle(request.id)
        if cycle is None:
            return
        if (
            self._approval_cycles.current_subject_fingerprint(request.id)
            == cycle.subject_fingerprint
        ):
            return
        self._approval_cycles.invalidate_cycle(
            request.id,
            expected_version=request.aggregate_version,
            reason=reason,
        )
        refreshed = self._approval_cycles.get_request(request.id)
        if refreshed is None:
            raise ApplicationOperationError(
                "La demande est introuvable après invalidation du cycle.",
                code="approval_cycle_request_missing_after_invalidation",
                context={"demand_number": number},
            )
        self._approval_cycles.initialize_cycle(
            request.id,
            expected_version=refreshed.aggregate_version,
        )

    @staticmethod
    def _required_identifier(value: object, *, entity: str) -> str:
        identifier = str(value or "").strip()
        if not identifier:
            raise ApplicationValidationError(
                f"L'identifiant de {entity} est requis.",
                code=f"{entity}_id_required",
            )
        return identifier

    @staticmethod
    def _validate_window(start: date | None, end: date | None) -> None:
        if start is None:
            raise ApplicationValidationError(
                "La date de début est requise.",
                code="demand_start_required",
                context={"field": "desired_start"},
            )
        if end is not None and end < start:
            raise ApplicationValidationError(
                "La date de fin ne peut pas précéder la date de début.",
                code="demand_date_window_invalid",
                context={"start": start.isoformat(), "end": end.isoformat()},
            )

    def _period_repository(self) -> DemandPeriodRepositoryPort:
        if self._periods is None:
            raise ApplicationOperationError(
                "La gestion détaillée des périodes n'est pas disponible dans ce runtime.",
                code="demand_periods_unavailable",
            )
        return self._periods

    def _operational_choice_repository(
        self,
    ) -> DemandOperationalChoiceRepositoryPort:
        if self._operational_choices is None:
            raise ApplicationOperationError(
                "Les choix opérationnels versionnés ne sont pas disponibles dans ce runtime.",
                code="operational_choices_unavailable",
            )
        return self._operational_choices

    def operational_choice_state(
        self,
        number: str,
    ) -> DemandOperationalChoiceReadModel | None:
        identifier = self._required_identifier(number, entity="demand")
        return call_application_port(
            lambda: self._operational_choice_repository().state_for_demand(
                identifier
            ),
            code_prefix="operational_choice_lookup",
            context={"demand_number": identifier},
        )

    @staticmethod
    def _period_line_scope(existing: Any, request_line_id: str | None) -> str | None:
        if bool(existing.line_mode):
            wanted = str(request_line_id or "").strip()
            if not wanted:
                raise ApplicationConflictError(
                    "Les périodes d'une demande multi-lignes doivent cibler une ligne.",
                    code="demand_line_period_scope_required",
                    context={"demand_number": existing.number},
                )
            line = next(
                (
                    row
                    for row in existing.lines
                    if row.active and str(row.line_id) == wanted
                ),
                None,
            )
            if line is None:
                raise ApplicationNotFoundError(
                    f"Ligne {wanted} introuvable pour la demande {existing.number}.",
                    code="demand_line_not_found",
                    context={
                        "demand_number": existing.number,
                        "request_line_id": wanted,
                    },
                )
            if line.kind not in {"WORKFORCE", "ASSET"}:
                raise ApplicationValidationError(
                    "Type de ligne non supporté pour les périodes.",
                    code="demand_line_kind_unsupported",
                    context={"line_id": line.line_id, "kind": line.kind},
                )
            return wanted

        if request_line_id is not None:
            raise ApplicationConflictError(
                "Les demandes historiques à une ligne utilisent les endpoints de périodes de la demande.",
                code="demand_legacy_period_scope_invalid",
                context={"demand_number": existing.number},
            )
        return None

    @staticmethod
    def _period_signature_from_definition(period: DemandPeriodDefinition) -> tuple[Any, ...]:
        return (
            period.period_id,
            period.start_date,
            period.end_date,
            float(period.hours) if period.hours is not None else None,
            period.kind,
            period.alternative_group,
            period.confirmation,
            period.confirmation_mode,
            period.proposed_resource,
            period.proposed_resource_mode,
            period.same_as_period_id,
            int(period.resource_count),
            period.desired_active_days,
            period.note,
        )

    @staticmethod
    def _period_signature_from_read_model(period: DemandPeriodReadModel) -> tuple[Any, ...]:
        return (
            period.period_id,
            period.start_date,
            period.end_date,
            float(period.hours) if period.hours is not None else None,
            period.kind,
            period.alternative_group,
            period.confirmation,
            period.proposed_resource,
            int(period.resource_count),
            period.desired_active_days,
            period.note or "",
        )

    def _demand_or_not_found(self, number: str):
        existing = call_application_port(
            lambda: self._demands.get(number),
            code_prefix="demand_lookup",
            context={"demand_number": number},
        )
        if existing is None:
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable",
                code="demand_not_found",
                context={"demand_number": number},
            )
        return existing

    def _workflow_business_blocks(
        self,
        existing: Any,
    ) -> Mapping[str, DemandWorkflowBlock]:
        return {}

    def _cancellation_materialization(
        self,
        number: str,
    ) -> DemandCancellationMaterializationReadModel:
        if self._queries is None:
            return DemandCancellationMaterializationReadModel(demand_number=number)
        reader = getattr(self._queries, "demand_cancellation_materialization", None)
        if not callable(reader):
            return DemandCancellationMaterializationReadModel(demand_number=number)
        return call_application_port(
            lambda: reader(number),
            code_prefix="demand_cancellation_materialization",
            context={"demand_number": number},
        )

    def workflow_state(self, number: str) -> DemandWorkflowReadModel:
        identifier = self._required_identifier(number, entity="demand")
        existing = self._demand_or_not_found(identifier)
        return demand_workflow_state(
            existing,
            permissions=self._permissions,
            business_blocks=self._workflow_business_blocks(existing),
            materialization=self._cancellation_materialization(identifier),
        )

    @staticmethod
    def _assert_expected_version(existing: Any, expected_version: int | None) -> None:
        if expected_version is None:
            return
        if int(expected_version) != int(existing.version):
            raise ApplicationConflictError(
                "La demande a été modifiée depuis sa lecture.",
                code="demand_version_conflict",
                context={
                    "demand_number": existing.number,
                    "expected_version": int(expected_version),
                    "current_version": int(existing.version),
                },
            )

    def _assert_workflow_action(
        self,
        existing: Any,
        action: str,
        *,
        expected_version: int | None = None,
    ) -> None:
        self._assert_expected_version(existing, expected_version)
        materialization = (
            self._cancellation_materialization(existing.number)
            if action
            in {
                ACTION_CANCEL,
                ACTION_REQUEST_CANCELLATION,
                ACTION_REJECT_CANCELLATION,
            }
            else None
        )
        assert_demand_action(
            existing,
            action,
            permissions=self._permissions,
            business_blocks=self._workflow_business_blocks(existing),
            materialization=materialization,
        )

    def _envelope_actor_role(self) -> str | None:
        for role in (ROLE_ADMIN, ROLE_COORDINATOR, ROLE_PROJECT_MANAGER):
            if role in self._roles:
                return role
        return None

    def _canonical_requester(self, requested_user_id: object):
        """Resolve and authorize a stable requester for canonical Web callers."""

        if self._requester_directory is None or self._current_user_id is None:
            return None

        current = self._requester_directory.get_by_id(self._current_user_id)
        if current is None or not current.active:
            raise ApplicationAuthorizationError(
                "L'identité applicative courante ne peut pas porter une demande.",
                code="demand_requester_actor_unavailable",
            )

        requested = str(requested_user_id or "").strip() or None
        can_delegate = ROLE_ADMIN in self._roles or ROLE_COORDINATOR in self._roles
        if can_delegate:
            target_id = requested or self._current_user_id
        else:
            if requested is not None and requested != self._current_user_id:
                raise ApplicationAuthorizationError(
                    "Un chargé de projet ne peut pas créer ou modifier une demande au nom d'un autre demandeur.",
                    code="demand_requester_impersonation_forbidden",
                    context={
                        "actor_user_id": self._current_user_id,
                        "requested_user_id": requested,
                    },
                )
            target_id = self._current_user_id

        target = self._requester_directory.get_by_id(target_id)
        if target is None or not is_admissible_requester(target):
            raise ApplicationValidationError(
                "Le demandeur sélectionné n'est pas admissible.",
                code="demand_requester_not_admissible",
                context={"requester_user_id": target_id},
            )
        return target

    def _apply_delegated_budget_changes(
        self,
        number: str,
        decision: EnvelopeDecision,
    ) -> None:
        if decision.reason != REASON_DELEGATED_TOLERANCE:
            return
        if self._operational_choices is None:
            raise ApplicationOperationError(
                "Les budgets opérationnels versionnés ne sont pas disponibles.",
                code="operational_budget_unavailable",
                context={"demand_number": number},
            )

        changed = False
        for change in decision.changes:
            if (
                change.code != REASON_DELEGATED_TOLERANCE
                or not change.entry_key
                or change.candidate_hours is None
            ):
                continue
            call_application_port(
                lambda change=change: self._operational_choices.set_budget_override(
                    number,
                    change.entry_key or "",
                    float(change.candidate_hours),
                ),
                code_prefix="operational_budget_override",
                context={
                    "demand_number": number,
                    "approved_entry_key": change.entry_key,
                },
            )
            changed = True

        if not changed:
            return
        sync_operational = getattr(
            self._approved_sync,
            "sync_operational_choices",
            None,
        )
        if not callable(sync_operational):
            raise ApplicationOperationError(
                "La synchronisation des budgets opérationnels n'est pas disponible.",
                code="operational_budget_sync_unavailable",
                context={"demand_number": number},
            )
        call_application_port(
            lambda: sync_operational(number),
            code_prefix="operational_budget_sync",
            context={"demand_number": number},
        )
        call_application_port(
            self._planning.rebuild,
            code_prefix="operational_budget_rebuild",
            context={"demand_number": number},
        )

    def _candidate_decision_mutates_active_plan(
        self,
        decision: EnvelopeDecision,
        *,
        legacy_unknown_requires_reapproval: bool,
    ) -> bool:
        needs_approval = decision.decision == DECISION_REAPPROVAL_REQUIRED
        if decision.decision == DECISION_APPROVAL_REFERENCE_UNKNOWN:
            needs_approval = legacy_unknown_requires_reapproval
        if needs_approval and PERMISSION_APPROVE_DEMANDS in self._permissions:
            return True
        return (
            decision.reason == REASON_DELEGATED_TOLERANCE
            and any(
                change.code == REASON_DELEGATED_TOLERANCE
                and change.entry_key
                and change.candidate_hours is not None
                for change in decision.changes
            )
        )

    def _evaluate_candidate_for_handling(
        self,
        number: str,
        *,
        code_prefix: str,
        context: Mapping[str, Any],
        legacy_unknown_requires_reapproval: bool,
    ) -> EnvelopeDecision:
        policy = self._approval_envelope_policy
        if policy is None:
            raise ApplicationOperationError(
                "La politique d'enveloppe approuvée n'est pas disponible.",
                code="approval_envelope_policy_unavailable",
                context={"demand_number": number},
            )
        decision = call_application_port(
            lambda: policy.evaluate_candidate(
                number,
                actor_role=self._envelope_actor_role(),
            ),
            code_prefix=code_prefix,
            context=context,
        )
        if (
            self._planning_versions is not None
            and self._candidate_decision_mutates_active_plan(
                decision,
                legacy_unknown_requires_reapproval=legacy_unknown_requires_reapproval,
            )
        ):
            self._planning_versions.acquire()
            decision = call_application_port(
                lambda: policy.evaluate_candidate(
                    number,
                    actor_role=self._envelope_actor_role(),
                ),
                code_prefix=f"{code_prefix}_guarded",
                context=context,
            )
        return decision

    def _handle_candidate_envelope_decision(
        self,
        number: str,
        decision: EnvelopeDecision,
        *,
        legacy_unknown_requires_reapproval: bool,
    ) -> bool:
        policy = self._approval_envelope_policy
        if policy is None:
            return legacy_unknown_requires_reapproval

        needs_approval = decision.decision == DECISION_REAPPROVAL_REQUIRED
        if decision.decision == DECISION_APPROVAL_REFERENCE_UNKNOWN:
            needs_approval = legacy_unknown_requires_reapproval
        elif decision.decision == DECISION_INVALID:
            detail = next(
                (change.detail for change in decision.changes if change.detail),
                "La proposition candidate ne forme pas une enveloppe valide.",
            )
            raise ApplicationValidationError(
                detail,
                code="approval_envelope_invalid",
                context={"demand_number": number, "decision": decision.to_dict()},
            )
        elif decision.decision == DECISION_EXPLICIT_EXCEPTION_REQUIRED:
            raise ApplicationConflictError(
                "La modification exige une dérogation explicite.",
                code="approval_envelope_exception_required",
                context={"demand_number": number, "decision": decision.to_dict()},
            )

        if not needs_approval:
            call_application_port(
                lambda: policy.record_candidate_decision(number, decision),
                code_prefix="approval_envelope_decision_audit",
                context={"demand_number": number},
            )
            self._apply_delegated_budget_changes(number, decision)
            return False

        if self._approval_cycles is None or self._approval_votes is None:
            if PERMISSION_APPROVE_DEMANDS in self._permissions:
                call_application_port(
                    lambda: policy.stamp_direct_approval(
                        number,
                        decision,
                        actor_name=self._current_user,
                    ),
                    code_prefix="approval_envelope_direct_approval",
                    context={"demand_number": number},
                )
                call_application_port(
                    lambda: self._approved_sync.sync_approved(number),
                    code_prefix="approval_envelope_direct_sync",
                    context={"demand_number": number},
                )
                call_application_port(
                    self._planning.rebuild,
                    code_prefix="approval_envelope_direct_rebuild",
                    context={"demand_number": number},
                )
                return False
        call_application_port(
            lambda: policy.mark_reapproval_required(number, decision),
            code_prefix="approval_envelope_reapproval",
            context={"demand_number": number},
        )
        self._initialize_approval_cycle_after_submission(number)

        # ADR-003/004 direct approval remains valid, but 276C routes it through the
        # frozen per-line quorum instead of stamping/materializing around it. The
        # current approver satisfies every eligible requirement in one atomic action;
        # if other scopes remain, the request stays Soumise with a partial vote.
        if (
            PERMISSION_APPROVE_DEMANDS in self._permissions
            and self._approval_cycles is not None
            and self._approval_votes is not None
        ):
            request = self._approval_cycles.get_request(number)
            cycle = (
                self._approval_cycles.get_active_cycle(request.id)
                if request is not None
                else None
            )
            if request is not None and cycle is not None:
                try:
                    outcome = self._approval_votes.approve_all_eligible(
                        workforce_request_id=request.id,
                        approval_cycle_id=cycle.id,
                        expected_request_version=request.aggregate_version,
                        comment="Autorisation élargie par approbateur",
                    )
                except ApplicationAuthorizationError as exc:
                    if exc.code not in {
                        "approval_actor_identity_required",
                        "approval_actor_not_eligible",
                        "approval_actor_inactive",
                    }:
                        raise
                else:
                    if outcome.status == "En planification":
                        record_direct = getattr(
                            policy,
                            "record_direct_approval",
                            None,
                        )
                        if callable(record_direct):
                            call_application_port(
                                lambda: record_direct(
                                    number,
                                    decision,
                                    actor_name=self._current_user,
                                ),
                                code_prefix="approval_envelope_direct_audit",
                                context={"demand_number": number},
                            )
                    return outcome.status != "En planification"
        return True

    def create_command(self, command: DemandCreateCommand) -> str:
        values = command.to_repository_values()
        canonical_requester = self._canonical_requester(
            values.get("RequesterUserId")
        )
        if canonical_requester is not None:
            values["RequesterUserId"] = canonical_requester.user_id
            values["Demandeur"] = canonical_requester.display_name
        with self._context("create demand"):
            number = call_application_port(
                lambda: self._demands.create(values, submit=bool(command.submit)),
                code_prefix="demand_create",
                context={"project_number": command.project_number},
            )

        normalized = str(number or "").strip()
        if not normalized:
            raise ApplicationOperationError(
                "La création de la demande n'a retourné aucun numéro.",
                code="demand_create_id_missing",
                context={"project_number": command.project_number},
            )
        if command.submit:
            self._initialize_approval_cycle_after_submission(normalized)
        return normalized

    def modify_command(self, command: DemandUpdateCommand) -> bool:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)

        data = command.to_repository_values()
        expected_version = data.pop("ExpectedVersion", None)
        if self._requester_directory is not None and self._current_user_id is not None:
            if "RequesterUserId" in data:
                canonical_requester = self._canonical_requester(
                    data.get("RequesterUserId")
                )
                if canonical_requester is not None:
                    data["RequesterUserId"] = canonical_requester.user_id
                    data["Demandeur"] = canonical_requester.display_name
            elif "Demandeur" in data:
                raise ApplicationValidationError(
                    "Utilise requester_user_id pour modifier le demandeur.",
                    code="demand_requester_id_required",
                    context={"demand_number": number},
                )
        self._assert_workflow_action(
            existing,
            ACTION_MODIFY,
            expected_version=expected_version,
        )
        if "RequestLines" in data:
            if expected_version is None:
                raise ApplicationValidationError(
                    "expected_version est requis pour modifier les lignes d'une demande.",
                    code="demand_version_required",
                    context={"demand_number": number},
                )
            data["ExpectedVersion"] = int(expected_version)
        if "RequestLines" not in data and not existing.line_mode:
            final_count = int(data.get("NombreRessources", existing.resource_count) or 1)
            final_hours = data.get("TempsEstimeHeures", existing.estimated_hours)
            if final_hours is not None:
                numeric_hours = float(final_hours)
                if numeric_hours <= 0:
                    raise ApplicationValidationError(
                        "Les heures doivent être supérieures à zéro lorsqu'elles sont renseignées.",
                        code="demand_estimated_hours_invalid",
                        context={"estimated_hours": numeric_hours},
                    )
                if numeric_hours < final_count * 0.01:
                    raise ApplicationValidationError(
                        "Les heures totales sont insuffisantes pour produire un besoin positif par ressource.",
                        code="demand_hours_split_invalid",
                        context={
                            "estimated_hours": numeric_hours,
                            "resource_count": final_count,
                        },
                    )
        if "NumeroProjet" in data and not str(data["NumeroProjet"] or "").strip():
            raise ApplicationValidationError(
                "Le projet est requis.",
                code="demand_project_required",
                context={"field": "project_number"},
            )
        if "RequestLines" not in data and not existing.line_mode:
            start = data.get("DateDebutSouhaitee", existing.desired_start)
            end = data.get("DateFinSouhaitee", existing.desired_end)
            self._validate_window(
                start if isinstance(start, date) else None,
                end if isinstance(end, date) else None,
            )

        was_approved = existing.status == "En planification"
        envelope_relevant_change = bool(
            BUSINESS_DEMAND_FIELDS.intersection(data)
        )
        fallback_reapproval_required = (
            was_approved
            and envelope_relevant_change
        )
        legacy_unknown_requires_reapproval = (
            was_approved
            and bool(LEGACY_UNKNOWN_REAPPROVAL_FIELDS.intersection(data))
        )

        audit_comment = str(command.comment or "").strip()
        if self._approval_envelope_policy is None and fallback_reapproval_required:
            data["Statut"] = "Soumise"
            data["ApprouvePar"] = None
            data["DateApprobation"] = None
            data["CommentaireApprobation"] = (
                "Demande modifiée après approbation — nouvelle approbation requise"
            )
            suffix = "Nouvelle approbation requise; la planification existante est conservée"
            audit_comment = f"{audit_comment} · {suffix}" if audit_comment else suffix

        with self._context("modify demand"):
            call_application_port(
                lambda: self._demands.update(
                    number,
                    data,
                    action="Modification",
                    comment=audit_comment,
                ),
                code_prefix="demand_update",
                context={"demand_number": number},
            )
            if (
                was_approved
                and envelope_relevant_change
                and self._approval_envelope_policy is not None
            ):
                decision = self._evaluate_candidate_for_handling(
                    number,
                    code_prefix="approval_envelope_compare",
                    context={"demand_number": number},
                    legacy_unknown_requires_reapproval=legacy_unknown_requires_reapproval,
                )
                return self._handle_candidate_envelope_decision(
                    number,
                    decision,
                    legacy_unknown_requires_reapproval=legacy_unknown_requires_reapproval,
                )
            if existing.status == "Soumise" and envelope_relevant_change:
                self._refresh_submitted_approval_cycle_if_subject_changed(
                    number,
                    reason="SUBMITTED_SUBJECT_MODIFIED",
                )
        return fallback_reapproval_required

    def replace_periods_command(
        self,
        command: DemandPeriodsReplaceCommand,
    ) -> tuple[Sequence[DemandPeriodReadModel], bool]:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)
        if command.expected_request_version is None:
            raise ApplicationValidationError(
                "expected_request_version est requis pour remplacer les périodes.",
                code="demand_version_required",
                context={"demand_number": number},
            )
        self._assert_workflow_action(
            existing,
            ACTION_MODIFY,
            expected_version=command.expected_request_version,
        )
        request_line_id = self._period_line_scope(
            existing,
            command.request_line_id,
        )
        periods = self._period_repository()

        try:
            definitions = tuple(item.to_definition() for item in command.periods)
            line = next((row for row in existing.lines if row.line_id == request_line_id), None)
            validate_period_definitions(definitions, allow_unbudgeted=bool(line and line.kind == "ASSET"))
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc),
                code="demand_periods_invalid",
                context={
                    "demand_number": number,
                    "request_line_id": request_line_id,
                },
            ) from exc

        current = call_application_port(
            lambda: (
                periods.list_for_demand(number)
                if request_line_id is None
                else periods.list_for_demand(
                    number,
                    request_line_id=request_line_id,
                )
            ),
            code_prefix="demand_periods_lookup",
            context={
                "demand_number": number,
                "request_line_id": request_line_id,
            },
        )
        old_signature = tuple(
            self._period_signature_from_read_model(row) for row in current
        )
        new_signature = tuple(
            self._period_signature_from_definition(row) for row in definitions
        )
        if old_signature == new_signature:
            return tuple(current), False

        was_approved = existing.status == "En planification"
        reapproval_required = was_approved
        status_update: dict[str, Any] = {}
        comment = (
            f"Périodes détaillées de la ligne {request_line_id} modifiées"
            if request_line_id is not None
            else "Périodes détaillées de la demande modifiées"
        )
        if self._approval_envelope_policy is None and reapproval_required:
            status_update = {
                "Statut": "Soumise",
                "ApprouvePar": None,
                "DateApprobation": None,
                "CommentaireApprobation": (
                    "Enveloppe de périodes modifiée après approbation — nouvelle approbation requise"
                ),
            }
            comment += "; nouvelle approbation requise et planification existante conservée"

        with self._context("replace demand periods"):
            updated = call_application_port(
                lambda: (
                    periods.replace_for_demand(number, definitions)
                    if request_line_id is None
                    else periods.replace_for_demand(
                        number,
                        definitions,
                        request_line_id=request_line_id,
                    )
                ),
                code_prefix="demand_periods_replace",
                context={
                    "demand_number": number,
                    "request_line_id": request_line_id,
                },
            )
            call_application_port(
                lambda: self._demands.update(
                    number,
                    {
                        **status_update,
                        "ExpectedVersion": command.expected_request_version,
                    },
                    action="Modification périodes",
                    comment=comment,
                ),
                code_prefix="demand_periods_audit",
                context={
                    "demand_number": number,
                    "request_line_id": request_line_id,
                },
            )
            if was_approved and self._approval_envelope_policy is not None:
                decision = self._evaluate_candidate_for_handling(
                    number,
                    code_prefix="approval_envelope_period_compare",
                    context={
                        "demand_number": number,
                        "request_line_id": request_line_id,
                    },
                    legacy_unknown_requires_reapproval=True,
                )
                reapproval_required = self._handle_candidate_envelope_decision(
                    number,
                    decision,
                    legacy_unknown_requires_reapproval=True,
                )
            elif existing.status == "Soumise":
                self._refresh_submitted_approval_cycle_if_subject_changed(
                    number,
                    reason="SUBMITTED_PERIODS_MODIFIED",
                )
        return tuple(updated), reapproval_required

    def extend_candidate_window(
        self,
        number: str,
        *,
        request_line_id: str | None,
        period_key: str | None,
        target_day: date,
        expected_request_version: int,
    ) -> bool:
        """Widen only the candidate source represented by one approved entry.

        The exact line/period is mutated in place so sibling lines, alternatives,
        selections, effort and stable identities remain untouched. The existing #13
        envelope policy then decides whether the candidate needs reapproval or can be
        approved directly. No materialized Shift is moved here.
        """

        identifier = self._required_identifier(number, entity="demand")
        existing = self._demand_or_not_found(identifier)
        self._assert_workflow_action(
            existing,
            ACTION_MODIFY,
            expected_version=expected_request_version,
        )
        was_approved = existing.status == "En planification"
        wanted_period = str(period_key or "").strip() or None
        wanted_line = str(request_line_id or "").strip() or None
        comment = (
            "Extension de fenêtre candidate depuis le planning; "
            "aucun déplacement n'est exécuté automatiquement"
        )

        with self._context("extend candidate demand window"):
            if wanted_period is not None:
                scoped_line = self._period_line_scope(existing, wanted_line)
                changed = call_application_port(
                    lambda: self._period_repository().extend_window(
                        identifier,
                        wanted_period,
                        target_day,
                        request_line_id=scoped_line,
                    ),
                    code_prefix="demand_period_window_extend",
                    context={
                        "demand_number": identifier,
                        "request_line_id": scoped_line,
                        "period_key": wanted_period,
                    },
                )
                if changed:
                    call_application_port(
                        lambda: self._demands.update(
                            identifier,
                            {"ExpectedVersion": expected_request_version},
                            action="Extension période candidate",
                            comment=comment,
                        ),
                        code_prefix="demand_period_window_audit",
                        context={
                            "demand_number": identifier,
                            "request_line_id": scoped_line,
                            "period_key": wanted_period,
                        },
                    )
            else:
                changed = call_application_port(
                    lambda: self._demands.extend_candidate_window(
                        identifier,
                        target_day,
                        request_line_id=wanted_line,
                        expected_version=expected_request_version,
                        action="Extension fenêtre candidate",
                        comment=comment,
                    ),
                    code_prefix="demand_window_extend",
                    context={
                        "demand_number": identifier,
                        "request_line_id": wanted_line,
                    },
                )

            if not changed:
                return existing.status != "En planification"
            if not was_approved:
                return False

            if self._approval_envelope_policy is None:
                call_application_port(
                    lambda: self._demands.update(
                        identifier,
                        {
                            "Statut": "Soumise",
                            "ApprouvePar": None,
                            "DateApprobation": None,
                            "CommentaireApprobation": (
                                "Enveloppe de fenêtre modifiée après approbation — "
                                "nouvelle approbation requise"
                            ),
                        },
                        action="Extension fenêtre candidate",
                        comment=f"{comment}; nouvelle approbation requise",
                    ),
                    code_prefix="demand_window_reapproval_fallback",
                    context={"demand_number": identifier},
                )
                return True

            decision = self._evaluate_candidate_for_handling(
                identifier,
                code_prefix="approval_envelope_window_compare",
                context={
                    "demand_number": identifier,
                    "request_line_id": wanted_line,
                    "period_key": wanted_period,
                },
                legacy_unknown_requires_reapproval=True,
            )
            return self._handle_candidate_envelope_decision(
                identifier,
                decision,
                legacy_unknown_requires_reapproval=True,
            )

    def select_alternative_command(
        self,
        command: DemandAlternativeSelectCommand,
    ) -> Mapping[str, Any] | None:
        number = self._required_identifier(command.number, entity="demand")
        group = self._required_identifier(
            command.alternative_group,
            entity="alternative_group",
        )
        period_id = self._required_identifier(command.period_id, entity="period")
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(existing, ACTION_MODIFY)
        request_line_id = self._period_line_scope(
            existing,
            command.request_line_id,
        )

        use_operational = bool(command.operational) or (
            existing.status == "En planification"
            and self._operational_choices is not None
            and self.operational_choice_state(number) is not None
        )
        if use_operational:
            with self._context("select operational alternative"):
                call_application_port(
                    lambda: self._operational_choice_repository().select_alternative(
                        number,
                        group,
                        period_id,
                        request_line_id=request_line_id,
                        expected_version=command.expected_operational_version,
                    ),
                    code_prefix="operational_alternative_select",
                    context={
                        "demand_number": number,
                        "request_line_id": request_line_id,
                        "alternative_group": group,
                        "period_id": period_id,
                    },
                )
                sync_operational = getattr(
                    self._approved_sync,
                    "sync_operational_choices",
                    None,
                )
                if not callable(sync_operational):
                    raise ApplicationOperationError(
                        "La synchronisation des choix opérationnels n'est pas disponible.",
                        code="operational_choice_sync_unavailable",
                    )
                call_application_port(
                    lambda: sync_operational(number),
                    code_prefix="operational_choice_sync",
                    context={
                        "demand_number": number,
                        "alternative_group": group,
                    },
                )
                summary = call_application_port(
                    self._planning.rebuild,
                    code_prefix="operational_choice_rebuild",
                    context={
                        "demand_number": number,
                        "alternative_group": group,
                    },
                )
            return dict(summary)

        periods = self._period_repository()
        selections = call_application_port(
            lambda: (
                periods.selections_for_demand(number)
                if request_line_id is None
                else periods.selections_for_demand(
                    number,
                    request_line_id=request_line_id,
                )
            ),
            code_prefix="demand_period_selection_lookup",
            context={
                "demand_number": number,
                "request_line_id": request_line_id,
                "alternative_group": group,
            },
        )
        if str(selections.get(group) or "").strip() == period_id:
            return None

        with self._context("select demand alternative"):
            call_application_port(
                lambda: (
                    periods.select_alternative(number, group, period_id)
                    if request_line_id is None
                    else periods.select_alternative(
                        number,
                        group,
                        period_id,
                        request_line_id=request_line_id,
                    )
                ),
                code_prefix="demand_period_select",
                context={
                    "demand_number": number,
                    "request_line_id": request_line_id,
                    "alternative_group": group,
                    "period_id": period_id,
                },
            )
            if existing.status == "Soumise":
                self._refresh_submitted_approval_cycle_if_subject_changed(
                    number,
                    reason="SUBMITTED_ALTERNATIVE_SELECTION_CHANGED",
                )
        return None

    def set_operational_confirmation_command(
        self,
        command: DemandOperationalConfirmationCommand,
    ) -> Mapping[str, Any]:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(existing, ACTION_MODIFY)
        request_line_id = self._period_line_scope(
            existing,
            command.request_line_id,
        )
        with self._context("set operational confirmation"):
            call_application_port(
                lambda: self._operational_choice_repository().set_confirmation(
                    number,
                    command.confirmation,
                    request_line_id=request_line_id,
                    period_id=command.period_id,
                    expected_version=command.expected_operational_version,
                ),
                code_prefix="operational_confirmation",
                context={
                    "demand_number": number,
                    "request_line_id": request_line_id,
                    "period_id": command.period_id,
                },
            )
            sync_operational = getattr(
                self._approved_sync,
                "sync_operational_choices",
                None,
            )
            if not callable(sync_operational):
                raise ApplicationOperationError(
                    "La synchronisation des choix opérationnels n'est pas disponible.",
                    code="operational_choice_sync_unavailable",
                )
            call_application_port(
                lambda: sync_operational(number),
                code_prefix="operational_confirmation_sync",
                context={"demand_number": number},
            )
            summary = call_application_port(
                self._planning.rebuild,
                code_prefix="operational_confirmation_rebuild",
                context={"demand_number": number},
            )
        return dict(summary)

    def submit_command(self, command: DemandSubmitCommand) -> None:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_SUBMIT,
            expected_version=command.expected_version,
        )
        active_lines = (
            tuple(line for line in existing.lines if line.active)
            if existing.line_mode
            else ()
        )
        if active_lines:
            for line in active_lines:
                if line.kind not in {"WORKFORCE", "ASSET"}:
                    raise ApplicationValidationError(
                        "Type de ligne non supporté.",
                        code="demand_line_kind_unsupported",
                        context={"line_id": line.line_id, "kind": line.kind},
                    )
                if line.desired_start is None:
                    raise ApplicationValidationError(
                        "Chaque ligne doit avoir une date de début avant soumission.",
                        code="demand_line_start_required",
                        context={"line_id": line.line_id},
                    )
                if line.kind == "WORKFORCE" and (
                    line.estimated_hours is None or line.estimated_hours <= 0
                ):
                    raise ApplicationValidationError(
                        "Chaque ligne WORKFORCE doit avoir un effort résolu avant soumission.",
                        code="demand_line_effort_required",
                        context={"line_id": line.line_id},
                    )
                if line.kind == "ASSET" and not str(line.asset_type_id or "").strip():
                    raise ApplicationValidationError(
                        "Chaque ligne ASSET doit préciser un type d'actif avant soumission.",
                        code="demand_asset_type_required",
                        context={"line_id": line.line_id},
                    )
        submit_updates: dict[str, Any] = {"Statut": "Soumise"}
        has_period_effort = False
        if (
            existing is not None
            and not existing.line_mode
            and existing.estimated_hours is None
            and existing.estimated_days is None
            and self._periods is not None
        ):
            period_rows = call_application_port(
                lambda: self._periods.list_for_demand(number),
                code_prefix="demand_submit_periods",
                context={"demand_number": number},
            )
            has_period_effort = bool(period_rows)
        if (
            existing is not None
            and not existing.line_mode
            and existing.estimated_hours is None
            and existing.estimated_days is None
            and not has_period_effort
        ):
            raise ApplicationValidationError(
                "Une demande soumise doit préciser des heures, un nombre de jours ou des périodes détaillées.",
                code="demand_effort_required",
                context={"demand_number": number},
            )
        if (
            existing is not None
            and not existing.line_mode
            and existing.estimated_hours is None
            and existing.estimated_days is not None
        ):
            submit_updates["TempsEstimeHeures"] = default_legacy_hours(
                estimated_hours=None,
                estimated_days=existing.estimated_days,
                resource_count=existing.resource_count,
            )
            submit_updates["RequestLineHoursSource"] = "DEFAULT_8H"
        with self._context("submit demand"):
            call_application_port(
                lambda: self._demands.update(
                    number,
                    submit_updates,
                    action="Soumission",
                    comment="Demande soumise pour approbation",
                ),
                code_prefix="demand_submit",
                context={"demand_number": number},
            )
            self._initialize_approval_cycle_after_submission(number)

    def approve_command(self, command: DemandApproveCommand) -> ApprovalVoteOutcome | dict[str, Any]:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_APPROVE,
            expected_version=command.expected_version,
        )
        if self._approval_cycles is None or self._approval_votes is None:
            # Compatibility for transport-neutral/unit compositions that do not
            # include the SQL approval aggregate. The production SQL facade always
            # composes 276C and therefore never enters this branch.
            comment = str(command.comment or "")
            with self._context("approve demand"):
                call_application_port(
                    lambda: self._demands.update(
                        number,
                        {
                            "Statut": "En planification",
                            "ApprouvePar": self._current_user,
                            "DateApprobation": datetime.now(),
                            "CommentaireApprobation": comment,
                        },
                        action="Approbation",
                        comment=comment or "Demande approuvée",
                    ),
                    code_prefix="demand_approve",
                    context={"demand_number": number},
                )
                call_application_port(
                    lambda: self._approved_sync.sync_approved(number),
                    code_prefix="demand_approval_sync",
                    context={"demand_number": number},
                )
                summary = call_application_port(
                    self._planning.rebuild,
                    code_prefix="demand_approval_rebuild",
                    context={"demand_number": number},
                )
            return dict(summary)
        request = self._approval_cycles.get_request(number)
        if request is None:
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable",
                code="demand_not_found",
                context={"demand_number": number},
            )
        cycle = self._approval_cycles.get_active_cycle(request.id)
        if cycle is None:
            # Deliberately do not auto-initialize here: a legacy submitted request
            # requires the explicit 276B initialization contract.
            self._approval_votes.approve_all_eligible(
                workforce_request_id=request.id,
                approval_cycle_id="",
                expected_request_version=(
                    int(command.expected_version)
                    if command.expected_version is not None
                    else request.aggregate_version
                ),
                comment=str(command.comment or ""),
                expected_planning_version=command.expected_planning_version,
            )
            raise AssertionError("unreachable")
        outcome = self._approval_votes.approve_all_eligible(
            workforce_request_id=request.id,
            approval_cycle_id=cycle.id,
            expected_request_version=(
                int(command.expected_version)
                if command.expected_version is not None
                else request.aggregate_version
            ),
            comment=str(command.comment or ""),
            expected_planning_version=command.expected_planning_version,
        )
        return outcome

    def vote_approval_command(
        self,
        command: ApprovalVoteCommand,
    ) -> ApprovalVoteOutcome:
        if self._approval_votes is None:
            raise ApplicationOperationError(
                "Le workflow de quorum d'approbation n'est pas configuré.",
                code="approval_quorum_unavailable",
                context={"demand_number": command.workforce_request_id},
            )
        return self._approval_votes.vote(command)

    def request_correction_command(self, command: DemandCorrectionCommand) -> None:
        number = self._required_identifier(command.number, entity="demand")
        reason = str(command.comment or "").strip()
        if not reason:
            raise ApplicationValidationError(
                "Un commentaire de correction est requis.",
                code="demand_correction_comment_required",
                context={"demand_number": number},
            )
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_CORRECTION,
            expected_version=command.expected_version,
        )
        with self._context("request demand correction"):
            call_application_port(
                lambda: self._demands.update(
                    number,
                    {
                        "Statut": "À corriger",
                        "CommentaireApprobation": reason,
                    },
                    action="Retour pour correction",
                    comment=reason,
                ),
                code_prefix="demand_correction",
                context={"demand_number": number},
            )
            self._invalidate_active_approval_cycle(
                number,
                reason="RETURNED_FOR_CORRECTION",
            )

    def request_cancellation_command(
        self,
        command: DemandCancellationRequestCommand,
    ) -> tuple[str, str]:
        number = self._required_identifier(command.number, entity="demand")
        reason = str(command.reason or "").strip()
        if not reason:
            raise ApplicationValidationError(
                "Un motif d'annulation est requis.",
                code="cancellation_reason_required",
                context={"demand_number": number},
            )
        if command.expected_version is None:
            raise ApplicationValidationError(
                "expected_version est requis pour demander l'annulation.",
                code="demand_version_required",
                context={"demand_number": number},
            )
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_REQUEST_CANCELLATION,
            expected_version=command.expected_version,
        )
        cancellation_request_id = str(uuid4())
        with self._context("request demand cancellation"):
            call_application_port(
                lambda: self._demands.request_cancellation(
                    number,
                    cancellation_request_id=cancellation_request_id,
                    reason=reason,
                    expected_version=int(command.expected_version),
                ),
                code_prefix="demand_cancellation_request",
                context={
                    "demand_number": number,
                    "cancellation_request_id": cancellation_request_id,
                },
            )
            self._invalidate_active_approval_cycle(
                number,
                reason="CANCELLATION_REQUESTED",
            )
        return existing.status, cancellation_request_id

    def accept_cancellation_command(
        self,
        command: DemandCancellationAcceptCommand,
    ) -> Mapping[str, Any]:
        number = self._required_identifier(command.number, entity="demand")
        cycle_id = self._required_identifier(
            command.cancellation_request_id,
            entity="cancellation_request",
        )
        correlation_id = self._required_identifier(
            command.correlation_id,
            entity="correlation",
        )
        comment = str(command.comment or "").strip()
        if not comment:
            raise ApplicationValidationError(
                "Un commentaire de résolution est requis.",
                code="cancellation_resolution_comment_required",
                context={"demand_number": number},
            )
        if self._planning_versions is None:
            raise ApplicationOperationError(
                "La garde de version du planning n'est pas configurée.",
                code="planning_version_unavailable",
                context={"demand_number": number},
            )

        # ADR-006: acquire the global mutation guard before any decision read.
        planning_version = self._planning_versions.acquire(
            int(command.expected_planning_version)
        )
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_ACCEPT_CANCELLATION,
            expected_version=command.expected_version,
        )
        if existing.cancellation_request_id != cycle_id:
            raise ApplicationConflictError(
                "La demande d'annulation à résoudre n'est plus active.",
                code="cancellation_cycle_conflict",
                context={
                    "demand_number": number,
                    "expected_cancellation_request_id": cycle_id,
                    "current_cancellation_request_id": existing.cancellation_request_id,
                },
            )

        with self._context("accept demand cancellation"):
            persisted = call_application_port(
                lambda: self._demands.accept_cancellation(
                    number,
                    cancellation_request_id=cycle_id,
                    comment=comment,
                    expected_version=int(command.expected_version),
                    planning_version=int(planning_version),
                    correlation_id=correlation_id,
                ),
                code_prefix="demand_cancellation_accept",
                context={
                    "demand_number": number,
                    "cancellation_request_id": cycle_id,
                    "correlation_id": correlation_id,
                },
            )
        return {
            **dict(persisted),
            "status": "Annulée",
            "planning_version": int(planning_version),
        }

    def reject_cancellation_command(
        self,
        command: DemandCancellationRejectCommand,
    ) -> str:
        number = self._required_identifier(command.number, entity="demand")
        cycle_id = self._required_identifier(
            command.cancellation_request_id,
            entity="cancellation_request",
        )
        comment = str(command.comment or "").strip()
        if not comment:
            raise ApplicationValidationError(
                "Un commentaire de résolution est requis.",
                code="cancellation_resolution_comment_required",
                context={"demand_number": number},
            )
        if command.expected_version is None:
            raise ApplicationValidationError(
                "expected_version est requis pour refuser l'annulation.",
                code="demand_version_required",
                context={"demand_number": number},
            )
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_REJECT_CANCELLATION,
            expected_version=command.expected_version,
        )
        if existing.cancellation_request_id != cycle_id:
            raise ApplicationConflictError(
                "La demande d'annulation à résoudre n'est plus active.",
                code="cancellation_cycle_conflict",
                context={
                    "demand_number": number,
                    "expected_cancellation_request_id": cycle_id,
                    "current_cancellation_request_id": existing.cancellation_request_id,
                },
            )
        with self._context("reject demand cancellation"):
            call_application_port(
                lambda: self._demands.reject_cancellation(
                    number,
                    cancellation_request_id=cycle_id,
                    comment=comment,
                    expected_version=int(command.expected_version),
                ),
                code_prefix="demand_cancellation_reject",
                context={
                    "demand_number": number,
                    "cancellation_request_id": cycle_id,
                },
            )
        return existing.status

    def cancel_command(self, command: DemandCancelCommand) -> None:
        number = self._required_identifier(command.number, entity="demand")
        existing = self._demand_or_not_found(number)
        self._assert_workflow_action(
            existing,
            ACTION_CANCEL,
            expected_version=command.expected_version,
        )
        cancel_materialized = getattr(self._approved_sync, "cancel_materialized", None)
        with self._context("cancel demand"):
            if callable(cancel_materialized):
                call_application_port(
                    lambda: cancel_materialized(number),
                    code_prefix="demand_cancel_materialized",
                    context={"demand_number": number},
                )
            call_application_port(
                lambda: self._demands.update(
                    number,
                    {
                        "Statut": "Annulée",
                        **(
                            {"ExpectedVersion": int(command.expected_version)}
                            if command.expected_version is not None
                            else {}
                        ),
                    },
                    action="Annulation",
                    comment="Demande annulée",
                ),
                code_prefix="demand_cancel",
                context={"demand_number": number},
            )
            self._invalidate_active_approval_cycle(
                number,
                reason="DEMAND_CANCELLED",
            )
            if callable(cancel_materialized):
                call_application_port(
                    self._planning.rebuild,
                    code_prefix="demand_cancel_rebuild",
                    context={"demand_number": number},
                )

    def create(self, data: Mapping[str, Any], *, submit: bool = False) -> str:
        """Compatibility adapter for current NiceGUI callers."""

        return self.create_command(DemandCreateCommand.from_mapping(data, submit=submit))

    def modify(
        self,
        number: str,
        updates: Mapping[str, Any],
        comment: str = "Demande modifiée dans l'application",
    ) -> bool:
        return self.modify_command(
            DemandUpdateCommand.from_mapping(number, updates, comment=comment)
        )

    def submit(self, number: str) -> None:
        self.submit_command(DemandSubmitCommand(number))

    def approve(self, number: str, comment: str = "") -> ApprovalVoteOutcome | dict[str, Any]:
        return self.approve_command(DemandApproveCommand(number, comment))

    def request_correction(self, number: str, comment: str) -> None:
        self.request_correction_command(DemandCorrectionCommand(number, comment))

    def cancel(self, number: str) -> None:
        self.cancel_command(DemandCancelCommand(number))
