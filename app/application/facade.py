from __future__ import annotations

from .allocation_service import AllocationService
from .composite_allocation_service import CompositeAllocationService
from .commands import (
    AllocationDropEvaluateCommand,
    AllocationDuplicateCommand,
    AllocationExtendMoveCommand,
    AllocationWindowOverrideMoveCommand,
    AllocationSplitCommand,
    PlanningWindowOverrideExtendCommand,
    AllocationWindowExtensionProposalCommand,
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
    ManualAllocationCreateCommand,
    ManualAllocationDeleteCommand,
    ManualAllocationMoveCommand,
    ManualAllocationReleaseCommand,
    ManualAllocationUpdateCommand,
    PlanningRebuildCommand,
    QuickShiftCreateCommand,
    SegmentAssignCommand,
    SegmentCancelCommand,
    SegmentCreateCommand,
    SegmentUpdateCommand,
    WorkPackageCreateCommand,
    WorkPackageUpdateCommand,
)
from .approval_voting import ApprovalVoteCommand, ApprovalVoteOutcome
from .demand_service import DemandService
from .demand_workflow_policy import DemandWorkflowReadModel
from .errors import ApplicationConflictError, ApplicationOperationError
from .planning_service import PlanningService
from .repository_ports import PlanningMutationVersionPort
from .quick_shift_service import QuickShiftService
from .resource_admin import (
    AvailabilityRuleCreateCommand,
    AvailabilityRuleMutationResult,
    AvailabilityRuleUpdateCommand,
    ResourceAdminService,
    ResourceCreateCommand,
    ResourceMutationResult,
    ResourceReorderCommand,
    ResourceUpdateCommand,
)
from .results import (
    AllocationMutationResult,
    CompositeAllocationMutationResult,
    DemandAlternativeSelectionResult,
    DemandCancellationMutationResult,
    DemandMutationResult,
    DemandOperationalConfirmationResult,
    DemandPeriodsMutationResult,
    PlanningDropEvaluationResult,
    PlanningWindowOverrideMutationResult,
    PlanningResult,
    QuickShiftCreatedResult,
    SegmentMutationResult,
    WorkPackageMutationResult,
)
from .segment_service import SegmentService
from .work_package_service import WorkPackageService
from .work_package_weekly_load import (
    WorkPackageWeeklyLoadProposalResult,
    WorkPackageWeeklyLoadReplaceCommand,
)


def _identifier(value: object) -> str:
    return str(value or "").strip()


class ApplicationFacade:
    """Stable use-case surface for UI/API adapters."""

    def __init__(
        self,
        *,
        demands: DemandService,
        segments: SegmentService,
        allocations: AllocationService,
        quick_shifts: QuickShiftService,
        composite_allocations: CompositeAllocationService | None = None,
        planning: PlanningService,
        work_packages: WorkPackageService | None = None,
        resource_admin: ResourceAdminService | None = None,
        planning_versions: PlanningMutationVersionPort | None = None,
    ) -> None:
        self._demands = demands
        self._segments = segments
        self._allocations = allocations
        self._quick_shifts = quick_shifts
        self._composite_allocations = composite_allocations
        self._planning = planning
        self._work_packages = work_packages
        self._resource_admin = resource_admin
        self._planning_versions = planning_versions

    def _acquire_planning_version(self, expected_version: int | None = None) -> int | None:
        if self._planning_versions is None:
            return None
        return self._planning_versions.acquire(expected_version)

    def _work_package_service(self) -> WorkPackageService:
        if self._work_packages is None:
            raise ApplicationOperationError(
                "Les commandes WorkPackage ne sont pas configurées dans cet adaptateur.",
                code="work_package_commands_unavailable",
            )
        return self._work_packages

    def _composite_allocation_service(self) -> CompositeAllocationService:
        if self._composite_allocations is None:
            raise ApplicationOperationError(
                "Les commandes composites de quart ne sont pas configurées.",
                code="composite_allocation_commands_unavailable",
            )
        return self._composite_allocations

    def _resource_admin_service(self) -> ResourceAdminService:
        if self._resource_admin is None:
            raise ApplicationOperationError(
                "L'administration des ressources n'est pas configurée dans cet adaptateur.",
                code="resource_admin_unavailable",
            )
        return self._resource_admin

    def create_work_package(
        self,
        command: WorkPackageCreateCommand,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().create_command(command)

    def update_work_package(
        self,
        command: WorkPackageUpdateCommand,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().update_command(command)

    def close_work_package(
        self,
        reference: str,
        *,
        expected_version: int,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().close(
            reference,
            expected_version=expected_version,
        )

    def cancel_work_package(
        self,
        reference: str,
        *,
        expected_version: int,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().cancel(
            reference,
            expected_version=expected_version,
        )

    def reopen_work_package(
        self,
        reference: str,
        *,
        expected_version: int,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().reopen(
            reference,
            expected_version=expected_version,
        )

    def propose_work_package_weekly_loads(
        self,
        reference: str,
    ) -> WorkPackageWeeklyLoadProposalResult:
        return self._work_package_service().propose_weekly_loads(reference)

    def replace_work_package_weekly_loads(
        self,
        command: WorkPackageWeeklyLoadReplaceCommand,
    ) -> WorkPackageMutationResult:
        return self._work_package_service().replace_weekly_loads(command)

    def create_resource(self, command: ResourceCreateCommand) -> ResourceMutationResult:
        self._acquire_planning_version()
        return self._resource_admin_service().create_resource(command)

    def update_resource(self, command: ResourceUpdateCommand) -> ResourceMutationResult:
        self._acquire_planning_version()
        return self._resource_admin_service().update_resource(command)

    def manual_resource_order_positions(self) -> dict[str, int]:
        return self._resource_admin_service().manual_order_positions()

    def reorder_resource(self, command: ResourceReorderCommand) -> ResourceMutationResult:
        # Personal manual display order does not change shifts, capacity or planning decisions.
        return self._resource_admin_service().reorder_resource(command)

    def create_availability_rule(
        self,
        command: AvailabilityRuleCreateCommand,
    ) -> AvailabilityRuleMutationResult:
        self._acquire_planning_version()
        return self._resource_admin_service().create_availability_rule(command)

    def update_availability_rule(
        self,
        command: AvailabilityRuleUpdateCommand,
    ) -> AvailabilityRuleMutationResult:
        self._acquire_planning_version()
        return self._resource_admin_service().update_availability_rule(command)

    def deactivate_availability_rule(self, rule_id: str) -> AvailabilityRuleMutationResult:
        self._acquire_planning_version()
        return self._resource_admin_service().deactivate_availability_rule(rule_id)

    def create_demand(self, command: DemandCreateCommand) -> DemandMutationResult:
        number = self._demands.create_command(command)
        return DemandMutationResult(
            demand_number=_identifier(number),
            status="Soumise" if command.submit else "Brouillon",
        )

    def update_demand(self, command: DemandUpdateCommand) -> DemandMutationResult:
        reapproval = self._demands.modify_command(command)
        return DemandMutationResult(
            demand_number=_identifier(command.number),
            status="Soumise" if reapproval else None,
            reapproval_required=reapproval,
        )

    def replace_demand_periods(
        self,
        command: DemandPeriodsReplaceCommand,
    ) -> DemandPeriodsMutationResult:
        periods, reapproval = self._demands.replace_periods_command(command)
        return DemandPeriodsMutationResult(
            demand_number=_identifier(command.number),
            period_count=len(periods),
            status="Soumise" if reapproval else None,
            reapproval_required=reapproval,
        )

    def select_demand_alternative(
        self,
        command: DemandAlternativeSelectCommand,
    ) -> DemandAlternativeSelectionResult:
        summary = self._demands.select_alternative_command(command)
        state = self._demands.operational_choice_state(command.number)
        return DemandAlternativeSelectionResult(
            demand_number=_identifier(command.number),
            alternative_group=_identifier(command.alternative_group),
            period_id=_identifier(command.period_id),
            planning=PlanningResult.from_mapping(summary) if summary is not None else None,
            operational_version=state.version if state is not None else None,
        )

    def set_demand_operational_confirmation(
        self,
        command: DemandOperationalConfirmationCommand,
    ) -> DemandOperationalConfirmationResult:
        summary = self._demands.set_operational_confirmation_command(command)
        state = self._demands.operational_choice_state(command.number)
        if state is None:
            raise ApplicationOperationError(
                "L'état opérationnel actif est introuvable après confirmation.",
                code="operational_choice_state_missing",
            )
        return DemandOperationalConfirmationResult(
            demand_number=_identifier(command.number),
            confirmation=_identifier(command.confirmation),
            operational_version=state.version,
            planning=PlanningResult.from_mapping(summary),
        )

    def demand_workflow_state(self, number: str) -> DemandWorkflowReadModel:
        return self._demands.workflow_state(number)

    def submit_demand(self, command: DemandSubmitCommand) -> DemandMutationResult:
        self._demands.submit_command(command)
        return DemandMutationResult(_identifier(command.number), status="Soumise")

    def approve_demand(self, command: DemandApproveCommand) -> DemandMutationResult:
        """Compatibility facade result for non-HTTP callers.

        The SQL HTTP path uses approve_demand_quorum so 276C can expose partial
        quorum state without changing the long-standing facade contract.
        """
        outcome = self._demands.approve_command(command)
        if isinstance(outcome, ApprovalVoteOutcome):
            return DemandMutationResult(
                _identifier(command.number),
                status=outcome.status,
                planning=(
                    PlanningResult.from_mapping(outcome.planning)
                    if outcome.planning is not None
                    else None
                ),
            )
        return DemandMutationResult(
            _identifier(command.number),
            status="En planification",
            planning=PlanningResult.from_mapping(outcome),
        )

    def approve_demand_quorum(
        self,
        command: DemandApproveCommand,
    ) -> ApprovalVoteOutcome:
        outcome = self._demands.approve_command(command)
        if not isinstance(outcome, ApprovalVoteOutcome):
            raise ApplicationOperationError(
                "Le workflow de quorum est indisponible dans cette composition.",
                code="approval_quorum_unavailable",
                context={"demand_number": command.number},
            )
        return outcome

    def vote_demand_approval(
        self,
        command: ApprovalVoteCommand,
    ) -> ApprovalVoteOutcome:
        return self._demands.vote_approval_command(command)

    def request_demand_correction(
        self,
        command: DemandCorrectionCommand,
    ) -> DemandMutationResult:
        self._demands.request_correction_command(command)
        return DemandMutationResult(_identifier(command.number), status="À corriger")

    def request_demand_cancellation(
        self,
        command: DemandCancellationRequestCommand,
    ) -> DemandCancellationMutationResult:
        status, cancellation_request_id = self._demands.request_cancellation_command(
            command
        )
        return DemandCancellationMutationResult(
            _identifier(command.number),
            status=status,
            cancellation_request_id=cancellation_request_id,
            cancellation_state="PENDING",
        )

    def accept_demand_cancellation(
        self,
        command: DemandCancellationAcceptCommand,
    ) -> DemandCancellationMutationResult:
        summary = self._demands.accept_cancellation_command(command)
        return DemandCancellationMutationResult(
            demand_number=_identifier(command.number),
            status="Annulée",
            cancellation_request_id=_identifier(command.cancellation_request_id),
            cancellation_state="ACCEPTED",
            planning_version=int(summary.get("planning_version") or 0) or None,
            request_version=int(summary.get("request_version") or 0) or None,
            deleted_human_shifts=int(summary.get("deleted_human_shifts") or 0),
            deleted_asset_allocations=int(summary.get("deleted_asset_allocations") or 0),
            deleted_ad_hoc_asset_requirements=int(
                summary.get("deleted_ad_hoc_asset_requirements") or 0
            ),
            deleted_segment_asset_requirements=int(
                summary.get("deleted_segment_asset_requirements") or 0
            ),
            cancelled_workforce_requirements=int(
                summary.get("cancelled_workforce_requirements") or 0
            ),
            cancelled_asset_requirements=int(
                summary.get("cancelled_asset_requirements") or 0
            ),
            released_locked_human_shifts=int(
                summary.get("released_locked_human_shifts") or 0
            ),
            released_locked_asset_allocations=int(
                summary.get("released_locked_asset_allocations") or 0
            ),
        )

    def reject_demand_cancellation(
        self,
        command: DemandCancellationRejectCommand,
    ) -> DemandCancellationMutationResult:
        status = self._demands.reject_cancellation_command(command)
        return DemandCancellationMutationResult(
            _identifier(command.number),
            status=status,
            cancellation_request_id=_identifier(command.cancellation_request_id),
            cancellation_state="REJECTED",
        )

    def cancel_demand(self, command: DemandCancelCommand) -> DemandMutationResult:
        self._demands.cancel_command(command)
        return DemandMutationResult(_identifier(command.number), status="Annulée")

    def create_segment(self, command: SegmentCreateCommand) -> SegmentMutationResult:
        self._acquire_planning_version()
        identifier, summary = self._segments.create_command(command)
        return SegmentMutationResult(
            segment_id=_identifier(identifier),
            action="created",
            planning=PlanningResult.from_mapping(summary),
        )

    def update_segment(self, command: SegmentUpdateCommand) -> SegmentMutationResult:
        self._acquire_planning_version()
        summary = self._segments.update_command(command)
        return SegmentMutationResult(
            segment_id=_identifier(command.segment_id),
            action="updated",
            planning=PlanningResult.from_mapping(summary),
        )

    def cancel_segment(self, command: SegmentCancelCommand) -> SegmentMutationResult:
        self._acquire_planning_version()
        summary = self._segments.cancel_command(command)
        return SegmentMutationResult(
            segment_id=_identifier(command.segment_id),
            action="cancelled",
            planning=PlanningResult.from_mapping(summary),
        )

    def assign_segment(self, command: SegmentAssignCommand) -> SegmentMutationResult:
        self._acquire_planning_version()
        summary = self._allocations.assign_segment_command(command)
        return SegmentMutationResult(
            segment_id=_identifier(command.segment_id),
            action="assigned",
            planning=PlanningResult.from_mapping(summary),
            technician=_identifier(command.technician) or None,
        )

    def create_allocation(
        self,
        command: ManualAllocationCreateCommand,
    ) -> AllocationMutationResult:
        self._acquire_planning_version()
        identifier = self._allocations.create_manual_command(command)
        return AllocationMutationResult(_identifier(identifier), action="created")

    def update_allocation(
        self,
        command: ManualAllocationUpdateCommand,
    ) -> AllocationMutationResult:
        self._acquire_planning_version()
        self._allocations.update_manual_command(command)
        return AllocationMutationResult(_identifier(command.allocation_id), action="updated")

    def move_allocation(
        self,
        command: ManualAllocationMoveCommand,
    ) -> AllocationMutationResult:
        self._acquire_planning_version(command.expected_planning_version)
        self._allocations.move_manual_command(command)
        return AllocationMutationResult(_identifier(command.allocation_id), action="moved")

    def release_allocation(
        self,
        command: ManualAllocationReleaseCommand,
    ) -> AllocationMutationResult:
        self._acquire_planning_version(command.expected_planning_version)
        self._allocations.release_manual_command(command)
        return AllocationMutationResult(_identifier(command.allocation_id), action="released")

    def delete_allocation(
        self,
        command: ManualAllocationDeleteCommand,
    ) -> AllocationMutationResult:
        self._acquire_planning_version(command.expected_planning_version)
        self._allocations.delete_manual_command(command)
        return AllocationMutationResult(_identifier(command.allocation_id), action="deleted")

    def evaluate_allocation_drop(
        self,
        command: AllocationDropEvaluateCommand,
    ) -> PlanningDropEvaluationResult:
        return self._composite_allocation_service().evaluate_drop_command(command)

    def extend_and_move_allocation(
        self,
        command: AllocationExtendMoveCommand,
    ) -> CompositeAllocationMutationResult:
        return self._composite_allocation_service().extend_and_move_command(command)


    def extend_planning_window_override(
        self,
        command: PlanningWindowOverrideExtendCommand,
    ) -> PlanningWindowOverrideMutationResult:
        return self._composite_allocation_service().extend_planning_window_command(command)

    def override_and_move_allocation(
        self,
        command: AllocationWindowOverrideMoveCommand,
    ) -> PlanningWindowOverrideMutationResult:
        return self._composite_allocation_service().override_and_move_command(command)

    def propose_allocation_window_extension(
        self,
        command: AllocationWindowExtensionProposalCommand,
    ) -> DemandMutationResult:
        evaluation = self._composite_allocation_service().evaluate_drop_command(
            AllocationDropEvaluateCommand(
                allocation_id=command.allocation_id,
                resource_id=command.resource_id,
                day=command.day,
                outside_standard_hours=command.outside_standard_hours,
            )
        )
        if (
            not evaluation.approval_revision_id
            or evaluation.approval_revision_id != command.expected_approval_revision_id
        ):
            raise ApplicationConflictError(
                "L'autorisation approuvée a changé depuis l'évaluation du déplacement.",
                code="planning_authorization_revision_conflict",
                context={
                    "expected_approval_revision_id": command.expected_approval_revision_id,
                    "current_approval_revision_id": evaluation.approval_revision_id,
                },
            )
        if not any(
            str(action.get("code") or "") == "PROPOSE_WINDOW_EXTENSION"
            and bool(action.get("enabled", True))
            for action in evaluation.actions
        ):
            raise ApplicationConflictError(
                "La cible ne requiert plus une proposition d'extension candidate.",
                code="allocation_window_proposal_stale",
                context={
                    "allocation_id": command.allocation_id,
                    "authorization_decision": evaluation.authorization_decision,
                },
            )
        if not evaluation.request_number:
            raise ApplicationConflictError(
                "Le quart n'est plus relié à une demande candidate.",
                code="allocation_window_proposal_source_missing",
                context={"allocation_id": command.allocation_id},
            )

        reapproval = self._demands.extend_candidate_window(
            evaluation.request_number,
            request_line_id=evaluation.request_line_id,
            period_key=evaluation.period_key,
            target_day=command.day,
            expected_request_version=command.expected_request_version,
        )
        return DemandMutationResult(
            demand_number=evaluation.request_number,
            status="Soumise" if reapproval else "En planification",
            reapproval_required=reapproval,
        )

    def split_allocation(
        self,
        command: AllocationSplitCommand,
    ) -> CompositeAllocationMutationResult:
        return self._composite_allocation_service().split_command(command)

    def duplicate_allocation(
        self,
        command: AllocationDuplicateCommand,
    ) -> CompositeAllocationMutationResult:
        return self._composite_allocation_service().duplicate_command(command)

    def create_quick_shift(
        self,
        command: QuickShiftCreateCommand,
    ) -> QuickShiftCreatedResult:
        self._acquire_planning_version()
        result = self._quick_shifts.create_command(command)
        return QuickShiftCreatedResult(
            segment_id=_identifier(result.segment_id),
            allocation_id=_identifier(result.allocation_id),
        )

    def rebuild_planning(self, command: PlanningRebuildCommand) -> PlanningResult:
        self._acquire_planning_version()
        return PlanningResult.from_mapping(self._planning.rebuild_command(command))
