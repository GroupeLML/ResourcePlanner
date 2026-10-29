from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Any

from .errors import ApplicationValidationError
from .results import ApplicationResult


def _required_text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApplicationValidationError(
            f"{field} est requis.",
            code="request_work_queue_field_required",
            context={"field": field},
        )
    return text


def _positive_version(value: object, *, field: str) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ApplicationValidationError(
            f"{field} doit être une version positive.",
            code="request_work_queue_version_invalid",
            context={"field": field, "value": value},
        ) from exc
    if version < 1:
        raise ApplicationValidationError(
            f"{field} doit être une version positive.",
            code="request_work_queue_version_invalid",
            context={"field": field, "value": value},
        )
    return version


@dataclass(frozen=True, slots=True)
class ReturnShiftToWorkQueueCommand:
    allocation_id: str
    expected_planning_version: int
    expected_approval_revision_id: str
    expected_operational_version: int
    command_id: str

    def __post_init__(self) -> None:
        _required_text(self.allocation_id, field="allocation_id")
        _required_text(self.expected_approval_revision_id, field="expected_approval_revision_id")
        _required_text(self.command_id, field="command_id")
        _positive_version(self.expected_planning_version, field="expected_planning_version")
        _positive_version(self.expected_operational_version, field="expected_operational_version")


@dataclass(frozen=True, slots=True)
class ReturnSegmentToWorkQueueCommand:
    segment_id: str
    expected_planning_version: int
    expected_approval_revision_id: str
    expected_operational_version: int
    command_id: str

    def __post_init__(self) -> None:
        _required_text(self.segment_id, field="segment_id")
        _required_text(self.expected_approval_revision_id, field="expected_approval_revision_id")
        _required_text(self.command_id, field="command_id")
        _positive_version(self.expected_planning_version, field="expected_planning_version")
        _positive_version(self.expected_operational_version, field="expected_operational_version")


@dataclass(frozen=True, slots=True)
class PermanentlyReduceShiftHoursCommand:
    allocation_id: str
    expected_planning_version: int
    expected_request_version: int
    expected_approval_revision_id: str
    expected_operational_version: int
    command_id: str

    def __post_init__(self) -> None:
        _required_text(self.allocation_id, field="allocation_id")
        _required_text(self.expected_approval_revision_id, field="expected_approval_revision_id")
        _required_text(self.command_id, field="command_id")
        _positive_version(self.expected_planning_version, field="expected_planning_version")
        _positive_version(self.expected_request_version, field="expected_request_version")
        _positive_version(self.expected_operational_version, field="expected_operational_version")


@dataclass(frozen=True, slots=True)
class RequestWorkQueueMutationResult(ApplicationResult):
    operation: str
    requirement_id: str
    segment_id: str
    demand_number: str
    planning_version: int
    approval_revision_id: str
    operational_version: int
    request_version: int | None = None
    shift_id: str | None = None
    release_id: str | None = None
    reduction_id: str | None = None
    removed_shift_count: int = 0
    removed_hours: float = 0.0
    effective_need_before: float = 0.0
    effective_need_after: float = 0.0
    covered_hours_after: float = 0.0
    remaining_hours_after: float = 0.0
    no_op: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RequestWorkQueueMutationResult":
        source = dict(values)
        return cls(
            operation=str(source.get("operation") or ""),
            requirement_id=str(source.get("requirement_id") or ""),
            segment_id=str(source.get("segment_id") or ""),
            demand_number=str(source.get("demand_number") or ""),
            planning_version=int(source.get("planning_version") or 0),
            approval_revision_id=str(source.get("approval_revision_id") or ""),
            operational_version=int(source.get("operational_version") or 0),
            request_version=(
                int(source["request_version"])
                if source.get("request_version") is not None
                else None
            ),
            shift_id=(str(source["shift_id"]) if source.get("shift_id") is not None else None),
            release_id=(str(source["release_id"]) if source.get("release_id") is not None else None),
            reduction_id=(str(source["reduction_id"]) if source.get("reduction_id") is not None else None),
            removed_shift_count=int(source.get("removed_shift_count") or 0),
            removed_hours=float(source.get("removed_hours") or 0.0),
            effective_need_before=float(source.get("effective_need_before") or 0.0),
            effective_need_after=float(source.get("effective_need_after") or 0.0),
            covered_hours_after=float(source.get("covered_hours_after") or 0.0),
            remaining_hours_after=float(source.get("remaining_hours_after") or 0.0),
            no_op=bool(source.get("no_op")),
        )


@dataclass(frozen=True, slots=True)
class RequestWorkQueuePreviewResult(ApplicationResult):
    allocation_id: str
    requirement_id: str
    segment_id: str
    demand_number: str
    source_request_line_id: str | None
    planning_version: int
    request_version: int
    approval_revision_id: str
    operational_version: int
    shift_hours: float
    effective_need: float
    covered_hours: float
    remaining_hours: float
    return_shift_allowed: bool
    return_shift_block_code: str | None
    return_segment_allowed: bool
    return_segment_block_code: str | None
    permanent_reduction_allowed: bool
    permanent_reduction_block_code: str | None
    effective_need_after_reduction: float
    remaining_after_return_shift: float
    remaining_after_return_segment: float
    remaining_after_reduction: float

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RequestWorkQueuePreviewResult":
        source = dict(values)
        return cls(**{
            field: source.get(field)
            for field in cls.__dataclass_fields__
        })


class RequestWorkQueueCommandPort(Protocol):
    def preview_shift(self, allocation_id: str) -> Mapping[str, Any]: ...

    def preview_shift(self, allocation_id: str) -> RequestWorkQueuePreviewResult:
        return RequestWorkQueuePreviewResult.from_mapping(
            self._commands.preview_shift(allocation_id)
        )

    def return_shift(
        self,
        command: ReturnShiftToWorkQueueCommand,
    ) -> Mapping[str, Any]: ...

    def return_segment(
        self,
        command: ReturnSegmentToWorkQueueCommand,
    ) -> Mapping[str, Any]: ...

    def permanently_reduce_shift(
        self,
        command: PermanentlyReduceShiftHoursCommand,
    ) -> Mapping[str, Any]: ...


class RequestWorkQueueService:
    def __init__(self, commands: RequestWorkQueueCommandPort) -> None:
        self._commands = commands

    def return_shift(
        self,
        command: ReturnShiftToWorkQueueCommand,
    ) -> RequestWorkQueueMutationResult:
        return RequestWorkQueueMutationResult.from_mapping(
            self._commands.return_shift(command)
        )

    def return_segment(
        self,
        command: ReturnSegmentToWorkQueueCommand,
    ) -> RequestWorkQueueMutationResult:
        return RequestWorkQueueMutationResult.from_mapping(
            self._commands.return_segment(command)
        )

    def permanently_reduce_shift(
        self,
        command: PermanentlyReduceShiftHoursCommand,
    ) -> RequestWorkQueueMutationResult:
        return RequestWorkQueueMutationResult.from_mapping(
            self._commands.permanently_reduce_shift(command)
        )
