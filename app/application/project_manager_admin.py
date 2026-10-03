from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import ApplicationAuthorizationError, ApplicationNotFoundError
from .project_co_managers import (
    ProjectCoManagerMutationResult,
    ProjectCoManagerRepositoryPort,
)
from .project_managers import EffectiveProjectManager, ProjectManagerResolutionService


@dataclass(frozen=True, slots=True)
class ProjectManagerAdminProjectRecord:
    project_id: str
    project_number: str


@dataclass(frozen=True, slots=True)
class ProjectManagersReadModel:
    project_id: str
    project_number: str
    co_managers_version: int
    primary: EffectiveProjectManager | None
    co_managers: tuple[EffectiveProjectManager, ...]
    diagnostics: tuple[str, ...]


class ProjectManagerAdminProjectRepositoryPort(Protocol):
    def get_project(
        self,
        project_number: str,
    ) -> ProjectManagerAdminProjectRecord | None: ...


def _required_text(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ApplicationNotFoundError(
            "Projet introuvable." if field == "project_number" else f"{field} introuvable.",
            code="project_not_found" if field == "project_number" else "not_found",
            context={field: normalized},
        )
    return normalized


def _actor_user_id(value: object) -> str:
    actor_user_id = str(value or "").strip()
    if not actor_user_id:
        raise ApplicationAuthorizationError(
            "Une identité locale authentifiée est requise pour administrer les co-chargés.",
            code="project_co_manager_actor_required",
        )
    return actor_user_id


class ProjectManagerAdminService:
    """Application façade for canonical project-manager reads and RP nominations."""

    def __init__(
        self,
        project_repository: ProjectManagerAdminProjectRepositoryPort,
        resolution_service: ProjectManagerResolutionService,
        co_manager_repository: ProjectCoManagerRepositoryPort,
    ) -> None:
        self._projects = project_repository
        self._resolution = resolution_service
        self._co_managers = co_manager_repository

    def _project(self, project_number: str) -> ProjectManagerAdminProjectRecord:
        number = _required_text(project_number, field="project_number")
        project = self._projects.get_project(number)
        if project is None:
            raise ApplicationNotFoundError(
                f"Projet {number} introuvable.",
                code="project_not_found",
                context={"project_number": number},
            )
        return project

    def get_managers(self, project_number: str) -> ProjectManagersReadModel:
        project = self._project(project_number)
        managers = self._resolution.resolve_project(project.project_id)
        return ProjectManagersReadModel(
            project_id=project.project_id,
            project_number=project.project_number,
            co_managers_version=managers.co_managers_version,
            primary=managers.primary,
            co_managers=managers.co_managers,
            diagnostics=managers.diagnostics,
        )

    def add_co_manager(
        self,
        project_number: str,
        business_contact_id: str,
        *,
        actor_user_id: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> ProjectCoManagerMutationResult:
        project = self._project(project_number)
        return self._co_managers.add_co_manager(
            project.project_id,
            business_contact_id,
            actor_user_id=_actor_user_id(actor_user_id),
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def remove_co_manager(
        self,
        project_number: str,
        business_contact_id: str,
        *,
        actor_user_id: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> ProjectCoManagerMutationResult:
        project = self._project(project_number)
        return self._co_managers.remove_co_manager(
            project.project_id,
            business_contact_id,
            actor_user_id=_actor_user_id(actor_user_id),
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
