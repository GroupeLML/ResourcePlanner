from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OperationalResponsibilityOverrideMutationResult:
    """Transport-neutral result for one explicit operational-responsibility override."""

    entity_type: str
    entity_id: str
    override_contact_id: str | None
    project_override_version: int | None = None
    planning_version: int | None = None
    auto_source_converted: bool = False


class OperationalResponsibilityMutationPort(Protocol):
    """Command boundary for explicit Project/Requirement/Shift responsibility overrides."""

    def set_project_override(
        self,
        project_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult: ...

    def set_requirement_override(
        self,
        requirement_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_planning_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult: ...

    def set_shift_override(
        self,
        shift_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_planning_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult: ...
