from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import ApplicationValidationError
from .query_models import ResourceReadModel
from .security import (
    AuthPrincipal,
    PERMISSION_READ,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)


LINK_STATUS_UNLINKED = "UNLINKED"
LINK_STATUS_RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
LINK_STATUS_LINKED = "LINKED"

SCOPE_MINE = "mine"
SCOPE_GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class PersonalResourceResolution:
    employee_external_id: str | None
    link_status: str
    resource: ResourceReadModel | None


@dataclass(frozen=True, slots=True)
class ResolvedUserRelations:
    employee_external_id: str | None
    resource_link_status: str
    resource: ResourceReadModel | None
    managed_project_ids: tuple[str, ...]
    participating_project_ids: tuple[str, ...]

    @property
    def personal_project_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.managed_project_ids, *self.participating_project_ids))
        )


@dataclass(frozen=True, slots=True)
class ProjectScopeResolution:
    scope: str
    project_ids: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class DemandScopeResolution:
    scope: str
    project_ids: tuple[str, ...] | None
    demand_ids: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class UserViewResourceReadModel:
    id: str | None
    link_status: str
    active: bool | None


@dataclass(frozen=True, slots=True)
class UserViewRelationsReadModel:
    managed_project_count: int
    participating_project_count: int
    personal_project_count: int


@dataclass(frozen=True, slots=True)
class UserViewPolicyReadModel:
    available_scopes: tuple[str, ...]
    default_scope: str


@dataclass(frozen=True, slots=True)
class UserViewContextReadModel:
    resource: UserViewResourceReadModel
    relations: UserViewRelationsReadModel
    view_policy: UserViewPolicyReadModel
    diagnostics: tuple[str, ...]


class UserViewContextRepositoryPort(Protocol):
    def get_resource_by_external_id(
        self,
        employee_external_id: str,
    ) -> ResourceReadModel | None: ...

    def list_managed_project_ids(
        self,
        local_user_id: str | None,
        employee_external_id: str | None,
    ) -> tuple[str, ...]: ...

    def list_participating_project_ids(
        self,
        resource_id: str,
    ) -> tuple[str, ...]: ...

    def list_coordinated_demand_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]: ...


def resolve_personal_resource(
    principal: AuthPrincipal,
    repository: UserViewContextRepositoryPort,
) -> PersonalResourceResolution:
    employee_external_id = str(principal.employee_external_id or "").strip() or None
    if employee_external_id is None:
        return PersonalResourceResolution(
            employee_external_id=None,
            link_status=LINK_STATUS_UNLINKED,
            resource=None,
        )

    resource = repository.get_resource_by_external_id(employee_external_id)
    if resource is None:
        return PersonalResourceResolution(
            employee_external_id=employee_external_id,
            link_status=LINK_STATUS_RESOURCE_NOT_FOUND,
            resource=None,
        )

    return PersonalResourceResolution(
        employee_external_id=employee_external_id,
        link_status=LINK_STATUS_LINKED,
        resource=resource,
    )


class UserViewContextService:
    """Resolve display context from stable business relationships, never from names."""

    _TRANSVERSE_ROLES = frozenset({ROLE_ADMIN, ROLE_COORDINATOR, ROLE_MANAGER})
    _PERSONAL_ROLES = frozenset({ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN})

    def __init__(self, repository: UserViewContextRepositoryPort) -> None:
        self._repository = repository

    def resolve_relations(self, principal: AuthPrincipal) -> ResolvedUserRelations:
        resource_resolution = resolve_personal_resource(principal, self._repository)
        employee_external_id = resource_resolution.employee_external_id

        managed_project_ids = self._repository.list_managed_project_ids(
            principal.local_user_id,
            employee_external_id,
        )
        participating_project_ids = (
            self._repository.list_participating_project_ids(
                resource_resolution.resource.id
            )
            if resource_resolution.resource is not None
            else ()
        )

        return ResolvedUserRelations(
            employee_external_id=employee_external_id,
            resource_link_status=resource_resolution.link_status,
            resource=resource_resolution.resource,
            managed_project_ids=tuple(managed_project_ids),
            participating_project_ids=tuple(participating_project_ids),
        )

    def available_scopes(self, principal: AuthPrincipal) -> tuple[str, ...]:
        return (
            (SCOPE_MINE, SCOPE_GLOBAL)
            if principal.has_permission(PERMISSION_READ)
            else (SCOPE_MINE,)
        )

    def default_scope(self, principal: AuthPrincipal) -> str:
        roles = frozenset(principal.roles)
        if roles & self._TRANSVERSE_ROLES:
            return SCOPE_GLOBAL
        if roles & self._PERSONAL_ROLES:
            return SCOPE_MINE
        return SCOPE_GLOBAL

    def resolve_project_scope(
        self,
        principal: AuthPrincipal,
        requested_scope: str | None = None,
    ) -> ProjectScopeResolution:
        scope = str(requested_scope or self.default_scope(principal)).strip().casefold()
        available_scopes = self.available_scopes(principal)
        if scope not in available_scopes:
            raise ApplicationValidationError(
                "Le périmètre d'affichage demandé n'est pas disponible pour cet utilisateur.",
                code="view_scope_not_available",
                context={
                    "requested_scope": scope,
                    "available_scopes": list(available_scopes),
                },
            )
        if scope == SCOPE_GLOBAL:
            return ProjectScopeResolution(scope=scope, project_ids=None)

        relations = self.resolve_relations(principal)
        return ProjectScopeResolution(
            scope=scope,
            project_ids=relations.personal_project_ids,
        )

    def resolve_demand_scope(
        self,
        principal: AuthPrincipal,
        requested_scope: str | None = None,
    ) -> DemandScopeResolution:
        project_scope = self.resolve_project_scope(principal, requested_scope)
        if project_scope.project_ids is None:
            return DemandScopeResolution(
                scope=project_scope.scope,
                project_ids=None,
                demand_ids=None,
            )

        local_user_id = str(principal.local_user_id or "").strip()
        demand_ids = (
            tuple(self._repository.list_coordinated_demand_ids(local_user_id))
            if local_user_id
            else ()
        )
        return DemandScopeResolution(
            scope=project_scope.scope,
            project_ids=project_scope.project_ids,
            demand_ids=demand_ids,
        )

    def read(self, principal: AuthPrincipal) -> UserViewContextReadModel:
        relations = self.resolve_relations(principal)
        diagnostics: list[str] = []
        if relations.employee_external_id is None:
            diagnostics.append("employee_external_id_missing")
        elif relations.resource_link_status == LINK_STATUS_RESOURCE_NOT_FOUND:
            diagnostics.append("resource_not_found")

        available_scopes = self.available_scopes(principal)
        resource = relations.resource
        return UserViewContextReadModel(
            resource=UserViewResourceReadModel(
                id=resource.id if resource is not None else None,
                link_status=relations.resource_link_status,
                active=resource.active if resource is not None else None,
            ),
            relations=UserViewRelationsReadModel(
                managed_project_count=len(relations.managed_project_ids),
                participating_project_count=len(relations.participating_project_ids),
                personal_project_count=len(relations.personal_project_ids),
            ),
            view_policy=UserViewPolicyReadModel(
                available_scopes=available_scopes,
                default_scope=self.default_scope(principal),
            ),
            diagnostics=tuple(diagnostics),
        )
