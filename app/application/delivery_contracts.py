"""Application contracts for Delivery that intentionally do not mutate Planning."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from app.domain.delivery import DeliveryItem, DeliveryItemType, DeliveryPlan

class PlanningCapacityProvenance(StrEnum):
    ACTIVE_APPROVED_PLAN = 'ACTIVE_APPROVED_PLAN'

@dataclass(frozen=True, slots=True)
class ApprovedPlanningCapacitySourceReadModel:
    demand_reference: str
    approval_revision_id: str
    approved_request_version: int | None = None
    approved_entry_keys: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class AssetReservedCapacityReadModel:
    asset_type_id: str
    reserved_days: int

@dataclass(frozen=True, slots=True)
class WorkPackagePlanningCapacityReadModel:
    # 362D must populate this from active/approved Planning only. Candidate demand is
    # not active capacity; Shift-backed capacity is a reservation, never actual hours.
    # observed_planning_version is informational freshness only and never acquires CAS.
    work_package_id: str
    work_package_reference: str
    window_start: date | None
    window_end: date | None
    human_reserved_hours: float
    total_reserved_hours: float
    observed_planning_version: int
    provenance: PlanningCapacityProvenance = PlanningCapacityProvenance.ACTIVE_APPROVED_PLAN
    approved_sources: tuple[ApprovedPlanningCapacitySourceReadModel, ...] = ()
    asset_reserved_capacity: tuple[AssetReservedCapacityReadModel, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.work_package_id or '').strip():
            raise ValueError('work_package_id requires a stable identifier')
        if not str(self.work_package_reference or '').strip():
            raise ValueError('work_package_reference is required')
        if self.window_start and self.window_end and (self.window_end < self.window_start):
            raise ValueError('Planning capacity window is invalid')
        if self.human_reserved_hours < 0 or self.total_reserved_hours < 0:
            raise ValueError('Reserved capacity cannot be negative')
        if abs(self.human_reserved_hours - self.total_reserved_hours) > 0.001:
            raise ValueError('total_reserved_hours is human capacity only; asset days remain separate')
        if self.observed_planning_version < 1:
            raise ValueError('observed_planning_version must be at least 1')

class DeliveryPlanningReadPort(Protocol):
    def get_work_package_planning_capacity(self, work_package_id: str) -> WorkPackagePlanningCapacityReadModel | None:
        ...

class DeliveryAction(StrEnum):
    MANAGE_PLAN_LIFECYCLE = 'MANAGE_PLAN_LIFECYCLE'
    ASSIGN_TEAM_LEAD = 'ASSIGN_TEAM_LEAD'
    SET_PLAN_PRIORITY_DUE_DATE = 'SET_PLAN_PRIORITY_DUE_DATE'
    MANAGE_STRUCTURE = 'MANAGE_STRUCTURE'
    ORDER_ITEMS = 'ORDER_ITEMS'
    ASSIGN_STORIES = 'ASSIGN_STORIES'
    ESTIMATE_STORIES = 'ESTIMATE_STORIES'
    MANAGE_STORY_STATUS_BLOCKING = 'MANAGE_STORY_STATUS_BLOCKING'
    MANAGE_SPRINT = 'MANAGE_SPRINT'
    UPDATE_OWN_STORY_STATUS = 'UPDATE_OWN_STORY_STATUS'
    UPDATE_OWN_REMAINING_HOURS = 'UPDATE_OWN_REMAINING_HOURS'
    DOCUMENT_OWN_BLOCKAGE = 'DOCUMENT_OWN_BLOCKAGE'

@dataclass(frozen=True, slots=True)
class DeliveryActorContext:
    user_id: str
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.user_id or '').strip():
            raise ValueError('Delivery actor requires a stable AppUser id')
        object.__setattr__(self, 'roles', tuple(dict.fromkeys((str(role).strip().upper() for role in self.roles if str(role).strip()))))

def delivery_actions_for(actor: DeliveryActorContext, plan: DeliveryPlan, item: DeliveryItem | None=None) -> frozenset[DeliveryAction]:
    # Conceptual actions only: 362B will bind these to persisted RBAC. No Planning
    # permission is introduced here, and lead_user_id grants plan-local authority only.
    actions: set[DeliveryAction] = set()
    roles = set(actor.roles)
    if roles.intersection({'ADMIN', 'PROJECT_MANAGER'}):
        actions.update({DeliveryAction.MANAGE_PLAN_LIFECYCLE, DeliveryAction.ASSIGN_TEAM_LEAD, DeliveryAction.SET_PLAN_PRIORITY_DUE_DATE})
    if plan.lead_user_id == actor.user_id:
        actions.update({DeliveryAction.MANAGE_STRUCTURE, DeliveryAction.ORDER_ITEMS, DeliveryAction.ASSIGN_STORIES, DeliveryAction.ESTIMATE_STORIES, DeliveryAction.MANAGE_STORY_STATUS_BLOCKING, DeliveryAction.MANAGE_SPRINT})
    if 'TECHNICIAN' in roles and item is not None and (item.item_type is DeliveryItemType.STORY) and (item.assignee_user_id == actor.user_id):
        actions.update({DeliveryAction.UPDATE_OWN_STORY_STATUS, DeliveryAction.UPDATE_OWN_REMAINING_HOURS, DeliveryAction.DOCUMENT_OWN_BLOCKAGE})
    return frozenset(actions)


@dataclass(frozen=True, slots=True)
class WorkPackageDeliveryReferenceReadModel:
    work_package_id: str
    reference: str
    name: str
    status: str
    reference_hours: float | None
    resource_class_code: str | None = None
    resource_class_label: str | None = None
    resource_class_active: bool | None = None
    task_resource_class_code: str | None = None
    resource_class_diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not str(self.work_package_id or '').strip():
            raise ValueError('work_package_id requires a stable identifier')
        if not str(self.reference or '').strip():
            raise ValueError('WorkPackage reference is required')
        if not str(self.name or '').strip():
            raise ValueError('WorkPackage name is required')
        if self.reference_hours is not None and self.reference_hours < 0:
            raise ValueError('WorkPackage reference hours cannot be negative')


class DeliveryWorkPackageReadPort(Protocol):
    def get_work_package_delivery_reference(
        self,
        work_package_id: str,
    ) -> WorkPackageDeliveryReferenceReadModel | None:
        ...
