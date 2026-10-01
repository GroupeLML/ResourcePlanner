from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .commands import (
    AllocationDropEvaluateCommand,
    AllocationDuplicateCommand,
    AllocationExtendMoveCommand,
    AllocationSplitCommand,
)


class PlanningCommandPort(Protocol):
    """Command boundary for one authoritative planning rebuild."""

    def rebuild(self) -> Mapping[str, Any]: ...


class AllocationCommandPort(Protocol):
    """Command boundary for manual shifts and operational assignment mutations."""

    def create_manual(
        self,
        segment_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
        overallocation_policy: str | None = None,
    ) -> str: ...

    def update_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
        overallocation_policy: str | None = None,
    ) -> None: ...

    def move_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        outside_standard_hours: bool = False,
    ) -> None: ...

    def release_manual(self, allocation_id: str) -> None: ...

    def delete_manual(self, allocation_id: str) -> None: ...

    def assign_segment(self, segment_id: str, technician: str) -> Mapping[str, Any]: ...


class CompositeAllocationCommandPort(Protocol):
    """Atomic contextual planning commands for one Shift transaction."""

    def split(self, command: AllocationSplitCommand) -> Mapping[str, Any]: ...

    def duplicate(self, command: AllocationDuplicateCommand) -> Mapping[str, Any]: ...

    def evaluate_drop(self, command: AllocationDropEvaluateCommand) -> Mapping[str, Any]: ...

    def extend_and_move(self, command: AllocationExtendMoveCommand) -> Mapping[str, Any]: ...


class ApprovedDemandSyncPort(Protocol):
    """Synchronize approved authorization and its active operational choices."""

    def sync_approved(
        self,
        demand_number: str,
        *,
        approved_request_version: int | None = None,
    ) -> None: ...

    def sync_operational_choices(self, demand_number: str) -> None: ...
