from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from .errors import ApplicationAuthorizationError
from .query_models import (
    PlanningCapacityGridReadModel,
    PlanningSnapshotReadModel,
    ResourceReadModel,
    ShiftReadModel,
)
from .security import (
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_READ,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from .user_view_context import (
    SCOPE_GLOBAL,
    SCOPE_MINE,
    UserViewContextRepositoryPort,
    UserViewContextService,
)


@dataclass(frozen=True, slots=True, order=True)
class PlanningProjectDay:
    project_id: str
    work_date: date


@dataclass(frozen=True, slots=True)
class PlanningDemandVisibility:
    demand_id: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanningViewPolicyReadModel:
    """Frontend-safe Planning scope policy projected from backend authorization."""

    available_scopes: tuple[str, ...]
    default_scope: str | None


@dataclass(frozen=True, slots=True)
class PlanningVisibilityResolution:
    """Backend-authoritative publication scope for operational Planning."""

    scope: str
    available_scopes: tuple[str, ...]
    project_ids: tuple[str, ...] | None
    personal_resource_id: str | None
    directly_coordinated_resource_ids: tuple[str, ...]
    visible_demands: tuple[PlanningDemandVisibility, ...]
    technician_project_days: tuple[PlanningProjectDay, ...]

    @property
    def demand_ids(self) -> tuple[str, ...] | None:
        if self.project_ids is None:
            return None
        return tuple(row.demand_id for row in self.visible_demands)

    @property
    def include_resource_ids(self) -> tuple[str, ...]:
        values = list(self.directly_coordinated_resource_ids)
        if self.personal_resource_id:
            values.append(self.personal_resource_id)
        return tuple(dict.fromkeys(values))

    @property
    def shift_resource_ids(self) -> tuple[str, ...]:
        return self.include_resource_ids

    @property
    def segment_resource_ids(self) -> tuple[str, ...]:
        # A technician's Resource identity grants own-Shift visibility, not an
        # unmaterialized Segment. Direct coordinator responsibility is different.
        return self.directly_coordinated_resource_ids

    @property
    def project_day_keys(self) -> tuple[tuple[str, date], ...]:
        return tuple((row.project_id, row.work_date) for row in self.technician_project_days)


class PlanningVisibilityRepositoryPort(UserViewContextRepositoryPort, Protocol):
    def list_directly_coordinated_resource_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]: ...

    def list_current_approval_demand_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]: ...

    def list_shift_project_days(
        self,
        resource_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[tuple[str, date], ...]: ...


class PlanningVisibilityService:
    """Resolve and project ADR-027 Planning visibility without client-side ACLs."""

    _GLOBAL_ROLES = frozenset({ROLE_ADMIN, ROLE_COORDINATOR, ROLE_MANAGER})

    def __init__(self, repository: PlanningVisibilityRepositoryPort) -> None:
        self._repository = repository

    @classmethod
    def available_scopes(cls, principal: AuthPrincipal) -> tuple[str, ...]:
        if not principal.has_permission(PERMISSION_READ):
            return ()
        if cls._GLOBAL_ROLES.intersection(principal.roles):
            return (SCOPE_MINE, SCOPE_GLOBAL)
        return (SCOPE_MINE,)

    @classmethod
    def default_scope(cls, principal: AuthPrincipal) -> str:
        scopes = cls.available_scopes(principal)
        return SCOPE_GLOBAL if SCOPE_GLOBAL in scopes else SCOPE_MINE

    @classmethod
    def view_policy(cls, principal: AuthPrincipal) -> PlanningViewPolicyReadModel:
        scopes = cls.available_scopes(principal)
        return PlanningViewPolicyReadModel(
            available_scopes=scopes,
            default_scope=cls.default_scope(principal) if scopes else None,
        )

    def resolve(
        self,
        principal: AuthPrincipal,
        requested_scope: str | None,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> PlanningVisibilityResolution:
        available = self.available_scopes(principal)
        if not available:
            raise ApplicationAuthorizationError(
                "La lecture du Planning n'est pas autorisée.",
                code="planning_read_not_authorized",
            )
        scope = str(requested_scope or self.default_scope(principal)).strip().lower()
        if scope not in available:
            raise ApplicationAuthorizationError(
                "Ce périmètre Planning n'est pas autorisé pour l'utilisateur courant.",
                code="planning_scope_not_authorized",
                context={
                    "requested_scope": scope,
                    "available_scopes": list(available),
                },
            )
        if scope == SCOPE_GLOBAL:
            return PlanningVisibilityResolution(
                scope=scope,
                available_scopes=available,
                project_ids=None,
                personal_resource_id=None,
                directly_coordinated_resource_ids=(),
                visible_demands=(),
                technician_project_days=(),
            )

        relations = UserViewContextService(self._repository).resolve_relations(principal)
        personal_resource_id = relations.resource.id if relations.resource is not None else None

        directly_coordinated_resource_ids: tuple[str, ...] = ()
        demand_sources: dict[str, set[str]] = {}
        local_user_id = principal.local_user_id
        if ROLE_COORDINATOR in principal.roles and local_user_id:
            directly_coordinated_resource_ids = (
                self._repository.list_directly_coordinated_resource_ids(local_user_id)
            )
            for demand_id in self._repository.list_coordinated_demand_ids(local_user_id):
                demand_sources.setdefault(demand_id, set()).add("COORDINATOR")

        if principal.has_permission(PERMISSION_APPROVE_DEMANDS) and local_user_id:
            for demand_id in self._repository.list_current_approval_demand_ids(
                local_user_id
            ):
                demand_sources.setdefault(demand_id, set()).add("CURRENT_APPROVER")

        technician_project_days: tuple[PlanningProjectDay, ...] = ()
        if ROLE_TECHNICIAN in principal.roles and personal_resource_id:
            technician_project_days = tuple(
                PlanningProjectDay(project_id=project_id, work_date=work_date)
                for project_id, work_date in self._repository.list_shift_project_days(
                    personal_resource_id,
                    start=start,
                    end=end,
                )
            )

        return PlanningVisibilityResolution(
            scope=scope,
            available_scopes=available,
            project_ids=relations.managed_project_ids,
            personal_resource_id=personal_resource_id,
            directly_coordinated_resource_ids=directly_coordinated_resource_ids,
            visible_demands=tuple(
                PlanningDemandVisibility(
                    demand_id=demand_id,
                    sources=tuple(sorted(sources)),
                )
                for demand_id, sources in sorted(demand_sources.items())
            ),
            technician_project_days=technician_project_days,
        )

    @staticmethod
    def _has_full_shift_context(
        row: ShiftReadModel,
        resolution: PlanningVisibilityResolution,
    ) -> bool:
        if resolution.project_ids is None:
            return True
        if row.resource_id == resolution.personal_resource_id:
            return True
        if row.resource_id in set(resolution.directly_coordinated_resource_ids):
            return True
        if row.project_id and row.project_id in set(resolution.project_ids):
            return True
        if row.demand_id and row.demand_id in set(resolution.demand_ids or ()):
            return True
        return False

    def can_read_shift_details(
        self,
        row: ShiftReadModel,
        resolution: PlanningVisibilityResolution,
    ) -> bool:
        return self._has_full_shift_context(row, resolution)

    def project_shift(
        self,
        row: ShiftReadModel,
        resolution: PlanningVisibilityResolution,
    ) -> ShiftReadModel:
        if self._has_full_shift_context(row, resolution):
            return row
        pair = (row.project_id, row.work_date)
        if row.project_id is None or pair not in set(resolution.project_day_keys):
            return row
        # ADR-027 technician-neighbor projection: enough to render who works with
        # the technician on that project/day, without opening the colleague's
        # demand, segment, notes, assets, or mutation/navigation identifiers.
        return replace(
            row,
            allocation_id=(
                "scope-neighbor:"
                f"{row.resource_id}:{row.work_date.isoformat()}:"
                f"{row.project_number or ''}"
            ),
            segment_id="",
            requirement_id=None,
            demand_id=None,
            demand_number=None,
            project_id=None,
            allocation_type=None,
            source="SCOPE_NEIGHBOR",
            locked=False,
            outside_standard_hours=False,
            confirmation_override=None,
            note=None,
            project_manager=None,
            requester=None,
            emergency_override_active=False,
            segment_planned_hours=0.0,
            segment_locked_hours=0.0,
            segment_overallocated_hours=0.0,
            asset_assignment=None,
            related_asset_reservations=(),
            asset_actions=None,
            asset_diagnostics=(),
        )

    @staticmethod
    def _minimal_neighbor_resource(row: ResourceReadModel) -> ResourceReadModel:
        return replace(
            row,
            email=None,
            competencies=None,
            competency_ids=(),
            note=None,
            external_id=None,
            erp_status=None,
            erp_department_description=None,
            erp_department_code=None,
            erp_employee_class=None,
            erp_supervisor_external_id=None,
            erp_phone=None,
            erp_branch_code=None,
            erp_contact_id=None,
        )

    def project_snapshot(
        self,
        snapshot: PlanningSnapshotReadModel,
        resolution: PlanningVisibilityResolution,
    ) -> PlanningSnapshotReadModel:
        if resolution.project_ids is None:
            return snapshot
        projected_shifts = tuple(
            self.project_shift(row, resolution) for row in snapshot.shifts
        )
        full_resource_ids = set(resolution.include_resource_ids)
        full_resource_ids.update(
            row.resource_id
            for row in snapshot.shifts
            if self._has_full_shift_context(row, resolution)
        )
        resources = tuple(
            row
            if row.id in full_resource_ids
            else self._minimal_neighbor_resource(row)
            for row in snapshot.resources
        )
        return replace(snapshot, shifts=projected_shifts, resources=resources)

    def project_capacity(
        self,
        grid: PlanningCapacityGridReadModel,
        *,
        scoped_shifts: tuple[ShiftReadModel, ...],
        resolution: PlanningVisibilityResolution,
    ) -> PlanningCapacityGridReadModel:
        if resolution.project_ids is None or not resolution.technician_project_days:
            return grid
        full_resource_ids = set(resolution.include_resource_ids)
        full_resource_ids.update(
            row.resource_id
            for row in scoped_shifts
            if self._has_full_shift_context(row, resolution)
        )
        return replace(
            grid,
            resources=tuple(
                row for row in grid.resources if row.resource_id in full_resource_ids
            ),
        )
