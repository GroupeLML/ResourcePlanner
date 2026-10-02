from __future__ import annotations

from datetime import datetime
from typing import Protocol


PROJECT_CO_MANAGER_ADDED = "PROJECT_CO_MANAGER_ADDED"
PROJECT_CO_MANAGER_REMOVED = "PROJECT_CO_MANAGER_REMOVED"


class ProjectCoManagerRecord:
    __slots__ = (
        "project_id",
        "business_contact_id",
        "created_at",
        "created_by_user_id",
    )

    def __init__(
        self,
        *,
        project_id: str,
        business_contact_id: str,
        created_at: datetime,
        created_by_user_id: str,
    ) -> None:
        self.project_id = project_id
        self.business_contact_id = business_contact_id
        self.created_at = created_at
        self.created_by_user_id = created_by_user_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectCoManagerRecord):
            return NotImplemented
        return (
            self.project_id,
            self.business_contact_id,
            self.created_at,
            self.created_by_user_id,
        ) == (
            other.project_id,
            other.business_contact_id,
            other.created_at,
            other.created_by_user_id,
        )

    def __repr__(self) -> str:
        return (
            "ProjectCoManagerRecord("
            f"project_id={self.project_id!r}, "
            f"business_contact_id={self.business_contact_id!r}, "
            f"created_at={self.created_at!r}, "
            f"created_by_user_id={self.created_by_user_id!r})"
        )


class ProjectCoManagerMutationResult:
    __slots__ = ("project_id", "business_contact_id", "version", "action")

    def __init__(
        self,
        *,
        project_id: str,
        business_contact_id: str,
        version: int,
        action: str,
    ) -> None:
        self.project_id = project_id
        self.business_contact_id = business_contact_id
        self.version = int(version)
        self.action = action

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectCoManagerMutationResult):
            return NotImplemented
        return (
            self.project_id,
            self.business_contact_id,
            self.version,
            self.action,
        ) == (
            other.project_id,
            other.business_contact_id,
            other.version,
            other.action,
        )

    def __repr__(self) -> str:
        return (
            "ProjectCoManagerMutationResult("
            f"project_id={self.project_id!r}, "
            f"business_contact_id={self.business_contact_id!r}, "
            f"version={self.version!r}, "
            f"action={self.action!r})"
        )


class ProjectCoManagerRepositoryPort(Protocol):
    def list_co_managers(self, project_id: str) -> tuple[ProjectCoManagerRecord, ...]: ...

    def add_co_manager(
        self,
        project_id: str,
        business_contact_id: str,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> ProjectCoManagerMutationResult: ...

    def remove_co_manager(
        self,
        project_id: str,
        business_contact_id: str,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> ProjectCoManagerMutationResult: ...
