"""Pure Verification contracts and rules from ADR-023 / issue #363A.

Verification owns test intent and lifecycle only. It has no dependency on
Planning entities, persistence, HTTP, or global RBAC infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Sequence


class VerificationDecisionKind(StrEnum):
    NO_TEST_REQUIRED = "NO_TEST_REQUIRED"
    TESTS_DEFINED = "TESTS_DEFINED"


class VerificationPhase(StrEnum):
    FAT = "FAT"
    SAT = "SAT"
    COMMISSIONING = "COMMISSIONING"


class VerificationRequirementState(StrEnum):
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class VerificationExecutionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class VerificationProjectedStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


def _required_id(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} requires a stable identifier")
    return normalized


def _optional_id(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _required_text(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _optional_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _text_tuple(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values if str(value).strip())
    return tuple(dict.fromkeys(normalized))


@dataclass(frozen=True, slots=True)
class VerificationScope:
    id: str
    work_package_id: str
    lead_user_id: str | None = None
    verification_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "VerificationScope.id"))
        object.__setattr__(
            self,
            "work_package_id",
            _required_id(self.work_package_id, "VerificationScope.work_package_id"),
        )
        object.__setattr__(self, "lead_user_id", _optional_id(self.lead_user_id))
        if self.verification_version < 1:
            raise ValueError("verification_version must be at least 1")


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    id: str
    verification_scope_id: str
    story_id: str
    phase: VerificationPhase
    current_revision_id: str
    state: VerificationRequirementState = VerificationRequirementState.ACTIVE
    withdrawal_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _required_id(self.id, "VerificationRequirement.id")
        )
        object.__setattr__(
            self,
            "verification_scope_id",
            _required_id(
                self.verification_scope_id,
                "VerificationRequirement.verification_scope_id",
            ),
        )
        object.__setattr__(
            self,
            "story_id",
            _required_id(self.story_id, "VerificationRequirement.story_id"),
        )
        object.__setattr__(
            self,
            "current_revision_id",
            _required_id(
                self.current_revision_id,
                "VerificationRequirement.current_revision_id",
            ),
        )
        object.__setattr__(self, "phase", VerificationPhase(self.phase))
        object.__setattr__(self, "state", VerificationRequirementState(self.state))
        reason = _optional_text(self.withdrawal_reason)
        if self.state is VerificationRequirementState.WITHDRAWN and reason is None:
            raise ValueError("withdrawal_reason is required for a withdrawn requirement")
        if self.state is VerificationRequirementState.ACTIVE and reason is not None:
            raise ValueError("active requirement cannot carry a withdrawal_reason")
        object.__setattr__(self, "withdrawal_reason", reason)


@dataclass(frozen=True, slots=True)
class VerificationRequirementRevision:
    id: str
    requirement_id: str
    revision_number: int
    objective: str
    method: str
    expected_result: str
    prerequisites: tuple[str, ...] = ()
    criticality: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _required_id(self.id, "VerificationRequirementRevision.id")
        )
        object.__setattr__(
            self,
            "requirement_id",
            _required_id(
                self.requirement_id,
                "VerificationRequirementRevision.requirement_id",
            ),
        )
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        object.__setattr__(
            self,
            "objective",
            _required_text(self.objective, "VerificationRequirementRevision.objective"),
        )
        object.__setattr__(
            self,
            "method",
            _required_text(self.method, "VerificationRequirementRevision.method"),
        )
        object.__setattr__(
            self,
            "expected_result",
            _required_text(
                self.expected_result,
                "VerificationRequirementRevision.expected_result",
            ),
        )
        object.__setattr__(self, "prerequisites", _text_tuple(self.prerequisites))
        object.__setattr__(self, "criticality", _optional_text(self.criticality))


@dataclass(frozen=True, slots=True)
class StoryVerificationDecision:
    id: str
    verification_scope_id: str
    story_id: str
    kind: VerificationDecisionKind
    justification: str | None = None
    requirement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _required_id(self.id, "StoryVerificationDecision.id")
        )
        object.__setattr__(
            self,
            "verification_scope_id",
            _required_id(
                self.verification_scope_id,
                "StoryVerificationDecision.verification_scope_id",
            ),
        )
        object.__setattr__(
            self,
            "story_id",
            _required_id(self.story_id, "StoryVerificationDecision.story_id"),
        )
        object.__setattr__(self, "kind", VerificationDecisionKind(self.kind))
        object.__setattr__(
            self,
            "requirement_ids",
            tuple(
                dict.fromkeys(
                    _required_id(
                        requirement_id,
                        "StoryVerificationDecision.requirement_id",
                    )
                    for requirement_id in self.requirement_ids
                )
            ),
        )
        justification = _optional_text(self.justification)
        if self.kind is VerificationDecisionKind.NO_TEST_REQUIRED:
            if justification is None:
                raise ValueError(
                    "NO_TEST_REQUIRED requires an explicit justification"
                )
            if self.requirement_ids:
                raise ValueError(
                    "NO_TEST_REQUIRED cannot reference active requirements"
                )
        elif not self.requirement_ids:
            raise ValueError("TESTS_DEFINED requires at least one requirement")
        object.__setattr__(self, "justification", justification)


@dataclass(frozen=True, slots=True)
class VerificationRetestRequest:
    id: str
    requirement_id: str
    revision_id: str
    after_execution_sequence: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _required_id(self.id, "VerificationRetestRequest.id")
        )
        object.__setattr__(
            self,
            "requirement_id",
            _required_id(
                self.requirement_id,
                "VerificationRetestRequest.requirement_id",
            ),
        )
        object.__setattr__(
            self,
            "revision_id",
            _required_id(self.revision_id, "VerificationRetestRequest.revision_id"),
        )
        if self.after_execution_sequence < 0:
            raise ValueError("after_execution_sequence cannot be negative")
        object.__setattr__(
            self,
            "reason",
            _required_text(self.reason, "VerificationRetestRequest.reason"),
        )


def validate_requirement_revision_chain(
    requirement: VerificationRequirement,
    revisions: Sequence[VerificationRequirementRevision],
) -> None:
    if not revisions:
        raise ValueError("VerificationRequirement requires at least one revision")
    ids: set[str] = set()
    by_number: dict[int, VerificationRequirementRevision] = {}
    for revision in revisions:
        if revision.requirement_id != requirement.id:
            raise ValueError(
                f"Revision {revision.id} belongs to another VerificationRequirement"
            )
        if revision.id in ids:
            raise ValueError(
                f"Duplicate VerificationRequirementRevision id: {revision.id}"
            )
        if revision.revision_number in by_number:
            raise ValueError(
                "Duplicate VerificationRequirement revision number: "
                f"{revision.revision_number}"
            )
        ids.add(revision.id)
        by_number[revision.revision_number] = revision
    expected_numbers = list(range(1, len(revisions) + 1))
    if sorted(by_number) != expected_numbers:
        raise ValueError("VerificationRequirement revisions must be contiguous from 1")
    current = by_number[max(by_number)]
    if requirement.current_revision_id != current.id:
        raise ValueError(
            "current_revision_id must reference the latest "
            "VerificationRequirementRevision"
        )


def adopt_requirement_revision(
    requirement: VerificationRequirement,
    current_revision: VerificationRequirementRevision,
    next_revision: VerificationRequirementRevision,
) -> VerificationRequirement:
    if requirement.state is not VerificationRequirementState.ACTIVE:
        raise ValueError("Cannot revise a withdrawn VerificationRequirement")
    if current_revision.id != requirement.current_revision_id:
        raise ValueError("current_revision does not match current_revision_id")
    if (
        current_revision.requirement_id != requirement.id
        or next_revision.requirement_id != requirement.id
    ):
        raise ValueError("Requirement revisions must belong to the same requirement")
    if next_revision.id == current_revision.id:
        raise ValueError("A revision requires a new stable identifier")
    if next_revision.revision_number != current_revision.revision_number + 1:
        raise ValueError("Requirement revision numbers must advance by exactly one")
    return replace(requirement, current_revision_id=next_revision.id)


def withdraw_requirement(
    requirement: VerificationRequirement,
    *,
    reason: str,
) -> VerificationRequirement:
    normalized_reason = _required_text(reason, "withdrawal reason")
    if requirement.state is VerificationRequirementState.WITHDRAWN:
        if requirement.withdrawal_reason == normalized_reason:
            return requirement
        raise ValueError("VerificationRequirement is already withdrawn")
    return replace(
        requirement,
        state=VerificationRequirementState.WITHDRAWN,
        withdrawal_reason=normalized_reason,
    )


def validate_story_verification_decision(
    scope: VerificationScope,
    decision: StoryVerificationDecision,
    requirements: Sequence[VerificationRequirement],
    revisions: Sequence[VerificationRequirementRevision],
) -> None:
    if decision.verification_scope_id != scope.id:
        raise ValueError("Story verification decision belongs to another scope")
    if decision.kind is VerificationDecisionKind.NO_TEST_REQUIRED:
        return

    requirement_by_id = {
        requirement.id: requirement for requirement in requirements
    }
    revision_by_requirement: dict[
        str, list[VerificationRequirementRevision]
    ] = {}
    for revision in revisions:
        revision_by_requirement.setdefault(revision.requirement_id, []).append(revision)

    for requirement_id in decision.requirement_ids:
        requirement = requirement_by_id.get(requirement_id)
        if requirement is None:
            raise ValueError(
                "Story verification decision references unknown requirement: "
                f"{requirement_id}"
            )
        if requirement.verification_scope_id != scope.id:
            raise ValueError(
                f"VerificationRequirement {requirement.id} belongs to another scope"
            )
        if requirement.story_id != decision.story_id:
            raise ValueError(
                f"VerificationRequirement {requirement.id} belongs to another Story"
            )
        if requirement.state is not VerificationRequirementState.ACTIVE:
            raise ValueError(
                f"VerificationRequirement {requirement.id} is not active"
            )
        validate_requirement_revision_chain(
            requirement,
            revision_by_requirement.get(requirement.id, ()),
        )


def validate_retest_request(
    requirement: VerificationRequirement,
    request: VerificationRetestRequest,
) -> None:
    if requirement.state is not VerificationRequirementState.ACTIVE:
        raise ValueError("Cannot request a retest for a withdrawn requirement")
    if request.requirement_id != requirement.id:
        raise ValueError("Retest request belongs to another requirement")
    if request.revision_id != requirement.current_revision_id:
        raise ValueError("Retest must target the current requirement revision")
