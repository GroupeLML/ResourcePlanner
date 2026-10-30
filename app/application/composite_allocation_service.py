from __future__ import annotations

from .command_ports import CompositeAllocationCommandPort
from .commands import (
    AllocationDropEvaluateCommand,
    AllocationDuplicateCommand,
    AllocationExtendMoveCommand,
    AllocationWindowOverrideMoveCommand,
    AllocationSplitCommand,
    PlanningWindowOverrideExtendCommand,
    SegmentDatedDropCommand,
    SegmentPlacementCutoffResetCommand,
)
from .errors import ApplicationOperationError, call_application_port
from .results import (
    CompositeAllocationMutationResult,
    PlanningDropEvaluationResult,
    PlanningWindowOverrideMutationResult,
    SegmentPlacementMutationResult,
)


class CompositeAllocationService:
    """Application boundary for contextual atomic Shift mutations."""

    def __init__(self, commands: CompositeAllocationCommandPort) -> None:
        self._commands = commands

    @staticmethod
    def _available(commands: CompositeAllocationCommandPort | None) -> CompositeAllocationCommandPort:
        if commands is None:
            raise ApplicationOperationError(
                "Les commandes composites de quart ne sont pas configurées.",
                code="composite_allocation_commands_unavailable",
            )
        return commands

    def split_command(
        self,
        command: AllocationSplitCommand,
    ) -> CompositeAllocationMutationResult:
        result = call_application_port(
            lambda: self._commands.split(command),
            code_prefix="allocation_split",
            context={"allocation_id": command.allocation_id},
        )
        return CompositeAllocationMutationResult.from_mapping(result)

    def duplicate_command(
        self,
        command: AllocationDuplicateCommand,
    ) -> CompositeAllocationMutationResult:
        result = call_application_port(
            lambda: self._commands.duplicate(command),
            code_prefix="allocation_duplicate",
            context={"allocation_id": command.allocation_id},
        )
        return CompositeAllocationMutationResult.from_mapping(result)


    def evaluate_drop_command(
        self,
        command: AllocationDropEvaluateCommand,
    ) -> PlanningDropEvaluationResult:
        result = call_application_port(
            lambda: self._commands.evaluate_drop(command),
            code_prefix="allocation_drop_evaluate",
            context={"allocation_id": command.allocation_id},
        )
        return PlanningDropEvaluationResult.from_mapping(result)

    def extend_and_move_command(
        self,
        command: AllocationExtendMoveCommand,
    ) -> CompositeAllocationMutationResult:
        result = call_application_port(
            lambda: self._commands.extend_and_move(command),
            code_prefix="allocation_extend_move",
            context={"allocation_id": command.allocation_id},
        )
        return CompositeAllocationMutationResult.from_mapping(result)


    def extend_planning_window_command(
        self,
        command: PlanningWindowOverrideExtendCommand,
    ) -> PlanningWindowOverrideMutationResult:
        result = call_application_port(
            lambda: self._commands.extend_planning_window(command),
            code_prefix="planning_window_override_extend",
            context={"segment_id": command.segment_id},
        )
        return PlanningWindowOverrideMutationResult.from_mapping(result)

    def override_and_move_command(
        self,
        command: AllocationWindowOverrideMoveCommand,
    ) -> PlanningWindowOverrideMutationResult:
        result = call_application_port(
            lambda: self._commands.override_and_move(command),
            code_prefix="planning_window_override_move",
            context={"allocation_id": command.allocation_id},
        )
        return PlanningWindowOverrideMutationResult.from_mapping(result)

    def assign_segment_from_drop_command(
        self,
        command: SegmentDatedDropCommand,
    ) -> SegmentPlacementMutationResult:
        result = call_application_port(
            lambda: self._commands.assign_segment_from_drop(command),
            code_prefix="segment_dated_drop",
            context={
                "segment_id": command.segment_id,
                "resource_id": command.resource_id,
                "dropped_date": command.dropped_date.isoformat(),
            },
        )
        return SegmentPlacementMutationResult.from_mapping(result)

    def reset_segment_placement_cutoff_command(
        self,
        command: SegmentPlacementCutoffResetCommand,
    ) -> SegmentPlacementMutationResult:
        result = call_application_port(
            lambda: self._commands.reset_segment_placement_cutoff(command),
            code_prefix="segment_placement_cutoff_reset",
            context={"segment_id": command.segment_id},
        )
        return SegmentPlacementMutationResult.from_mapping(result)
