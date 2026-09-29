"""Pure Delivery contracts and rules from ADR-008 / issue #362A.

Delivery intentionally owns technical work state only.  It has no dependency on
Planning entities, persistence, HTTP, or RBAC infrastructure.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable, Sequence

class DeliveryPlanStatus(StrEnum):
    DRAFT = 'DRAFT'
    ACTIVE = 'ACTIVE'
    ARCHIVED = 'ARCHIVED'

class DeliveryItemType(StrEnum):
    EPIC = 'EPIC'
    STORY = 'STORY'

class DeliveryItemStatus(StrEnum):
    BACKLOG = 'BACKLOG'
    TODO = 'TODO'
    IN_PROGRESS = 'IN_PROGRESS'
    BLOCKED = 'BLOCKED'
    DONE = 'DONE'
    CANCELLED = 'CANCELLED'

class DeliveryMetricState(StrEnum):
    AVAILABLE = 'AVAILABLE'
    PARTIAL_COVERAGE = 'PARTIAL_COVERAGE'
    NO_REFERENCE_ESTIMATE = 'NO_REFERENCE_ESTIMATE'
    NO_REMAINING_ESTIMATE = 'NO_REMAINING_ESTIMATE'
    NO_INCLUDED_STORIES = 'NO_INCLUDED_STORIES'
_REFERENCE_CAPTURE_STATUSES = frozenset({DeliveryItemStatus.TODO, DeliveryItemStatus.IN_PROGRESS, DeliveryItemStatus.BLOCKED, DeliveryItemStatus.DONE})
_TERMINAL_STORY_STATUSES = frozenset({DeliveryItemStatus.DONE, DeliveryItemStatus.CANCELLED})

def _required_id(value: str, field: str) -> str:
    normalized = str(value or '').strip()
    if not normalized:
        raise ValueError(f'{field} requires a stable identifier')
    return normalized

def _optional_id(value: str | None) -> str | None:
    normalized = str(value or '').strip()
    return normalized or None

def _positive_optional_hours(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f'{field} must be positive when provided')
    return round(normalized, 2)

def _non_negative_optional_hours(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if normalized < 0:
        raise ValueError(f'{field} cannot be negative')
    return round(normalized, 2)

@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    id: str
    work_package_id: str
    status: DeliveryPlanStatus = DeliveryPlanStatus.DRAFT
    lead_user_id: str | None = None
    delivery_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    activated_at: datetime | None = None
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'id', _required_id(self.id, 'DeliveryPlan.id'))
        object.__setattr__(self, 'work_package_id', _required_id(self.work_package_id, 'DeliveryPlan.work_package_id'))
        object.__setattr__(self, 'lead_user_id', _optional_id(self.lead_user_id))
        object.__setattr__(self, 'status', DeliveryPlanStatus(self.status))
        if self.delivery_version < 1:
            raise ValueError('delivery_version must be at least 1')

@dataclass(frozen=True, slots=True)
class DeliveryItem:
    id: str
    delivery_plan_id: str
    item_type: DeliveryItemType
    title: str
    parent_id: str | None = None
    description: str | None = None
    priority: str | None = None
    status: DeliveryItemStatus = DeliveryItemStatus.BACKLOG
    assignee_user_id: str | None = None
    current_estimate_hours: float | None = None
    reference_estimate_hours: float | None = None
    remaining_hours: float | None = None
    due_date: date | None = None
    position: int = 0
    sprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'id', _required_id(self.id, 'DeliveryItem.id'))
        object.__setattr__(self, 'delivery_plan_id', _required_id(self.delivery_plan_id, 'DeliveryItem.delivery_plan_id'))
        object.__setattr__(self, 'parent_id', _optional_id(self.parent_id))
        object.__setattr__(self, 'assignee_user_id', _optional_id(self.assignee_user_id))
        object.__setattr__(self, 'item_type', DeliveryItemType(self.item_type))
        object.__setattr__(self, 'status', DeliveryItemStatus(self.status))
        title = str(self.title or '').strip()
        if not title:
            raise ValueError('DeliveryItem.title is required')
        object.__setattr__(self, 'title', title)
        object.__setattr__(self, 'current_estimate_hours', _positive_optional_hours(self.current_estimate_hours, 'current_estimate_hours'))
        object.__setattr__(self, 'reference_estimate_hours', _positive_optional_hours(self.reference_estimate_hours, 'reference_estimate_hours'))
        object.__setattr__(self, 'remaining_hours', _non_negative_optional_hours(self.remaining_hours, 'remaining_hours'))
        if self.position < 0:
            raise ValueError('DeliveryItem.position cannot be negative')

@dataclass(frozen=True, slots=True)
class DeliveryProgress:
    state: DeliveryMetricState
    progress_ratio: float | None
    completed_reference_hours: float
    total_reference_hours: float
    included_story_count: int
    estimated_story_count: int
    unestimated_story_ids: tuple[str, ...]
    cancelled_story_count: int

    @property
    def coverage_ratio(self) -> float | None:
        if self.included_story_count == 0:
            return None
        return round(self.estimated_story_count / self.included_story_count, 4)

@dataclass(frozen=True, slots=True)
class DeliveryForecast:
    state: DeliveryMetricState
    total_remaining_hours: float | None
    open_story_count: int
    covered_story_count: int
    missing_remaining_story_ids: tuple[str, ...]
    cancelled_story_count: int

    @property
    def coverage_ratio(self) -> float | None:
        if self.open_story_count == 0:
            return None
        return round(self.covered_story_count / self.open_story_count, 4)

def validate_non_archived_plan_uniqueness(plans: Sequence[DeliveryPlan]) -> None:
    active_by_work_package: dict[str, str] = {}
    for plan in plans:
        if plan.status is DeliveryPlanStatus.ARCHIVED:
            continue
        existing_id = active_by_work_package.get(plan.work_package_id)
        if existing_id is not None:
            raise ValueError(f'Only one non-archived DeliveryPlan is allowed per WorkPackage: {plan.work_package_id} ({existing_id}, {plan.id})')
        active_by_work_package[plan.work_package_id] = plan.id

def validate_delivery_hierarchy(plan: DeliveryPlan, items: Sequence[DeliveryItem]) -> None:
    by_id: dict[str, DeliveryItem] = {}
    for item in items:
        if item.delivery_plan_id != plan.id:
            raise ValueError(f'DeliveryItem {item.id} belongs to another DeliveryPlan')
        if item.id in by_id:
            raise ValueError(f'Duplicate DeliveryItem id: {item.id}')
        by_id[item.id] = item
    for item in items:
        if item.item_type is DeliveryItemType.EPIC:
            if item.parent_id is not None:
                raise ValueError('EPIC cannot have a parent')
            continue
        if item.parent_id is None:
            continue
        parent = by_id.get(item.parent_id)
        if parent is None:
            raise ValueError(f'Parent DeliveryItem not found: {item.parent_id}')
        if parent.item_type is not DeliveryItemType.EPIC:
            raise ValueError('STORY can only have an EPIC parent')

def transition_delivery_plan(plan: DeliveryPlan, target_status: DeliveryPlanStatus, *, occurred_at: datetime | None=None) -> DeliveryPlan:
    target = DeliveryPlanStatus(target_status)
    allowed = {DeliveryPlanStatus.DRAFT: DeliveryPlanStatus.ACTIVE, DeliveryPlanStatus.ACTIVE: DeliveryPlanStatus.ARCHIVED}
    if plan.status == target:
        return plan
    if allowed.get(plan.status) != target:
        raise ValueError(f'Invalid DeliveryPlan transition: {plan.status} -> {target}')
    values: dict[str, object] = {'status': target, 'updated_at': occurred_at or plan.updated_at}
    if target is DeliveryPlanStatus.ACTIVE:
        values['activated_at'] = occurred_at
    elif target is DeliveryPlanStatus.ARCHIVED:
        values['archived_at'] = occurred_at
    return replace(plan, **values)

def transition_story_status(story: DeliveryItem, target_status: DeliveryItemStatus) -> DeliveryItem:
    if story.item_type is not DeliveryItemType.STORY:
        raise ValueError('Only STORY status is governed by the Story transition rule')
    target = DeliveryItemStatus(target_status)
    reference = story.reference_estimate_hours
    if reference is None and story.status is DeliveryItemStatus.BACKLOG and (target in _REFERENCE_CAPTURE_STATUSES) and (story.current_estimate_hours is not None):
        reference = story.current_estimate_hours
    return replace(story, status=target, reference_estimate_hours=reference)

def reestimate_story(story: DeliveryItem, hours: float | None) -> DeliveryItem:
    if story.item_type is not DeliveryItemType.STORY:
        raise ValueError('Only STORY has a technical estimate')
    normalized = _positive_optional_hours(hours, 'current_estimate_hours')
    reference = story.reference_estimate_hours
    if reference is None and story.status in _REFERENCE_CAPTURE_STATUSES and (normalized is not None):
        reference = normalized
    return replace(story, current_estimate_hours=normalized, reference_estimate_hours=reference)

def update_story_remaining_hours(story: DeliveryItem, hours: float | None) -> DeliveryItem:
    if story.item_type is not DeliveryItemType.STORY:
        raise ValueError('Only STORY has remaining_hours')
    normalized = _non_negative_optional_hours(hours, 'remaining_hours')
    return replace(story, remaining_hours=normalized)

def delivery_progress(items: Iterable[DeliveryItem]) -> DeliveryProgress:
    stories = tuple((item for item in items if item.item_type is DeliveryItemType.STORY))
    cancelled = tuple((story for story in stories if story.status is DeliveryItemStatus.CANCELLED))
    included = tuple((story for story in stories if story.status is not DeliveryItemStatus.CANCELLED))
    estimated = tuple((story for story in included if story.reference_estimate_hours is not None))
    unestimated_ids = tuple((story.id for story in included if story.reference_estimate_hours is None))
    total_reference = round(sum((float(story.reference_estimate_hours or 0.0) for story in estimated)), 2)
    completed_reference = round(sum((float(story.reference_estimate_hours or 0.0) for story in estimated if story.status is DeliveryItemStatus.DONE)), 2)
    if not included:
        state = DeliveryMetricState.NO_INCLUDED_STORIES
        ratio = None
    elif total_reference <= 0:
        state = DeliveryMetricState.NO_REFERENCE_ESTIMATE
        ratio = None
    else:
        state = DeliveryMetricState.PARTIAL_COVERAGE if unestimated_ids else DeliveryMetricState.AVAILABLE
        ratio = round(completed_reference / total_reference, 4)
    return DeliveryProgress(state=state, progress_ratio=ratio, completed_reference_hours=completed_reference, total_reference_hours=total_reference, included_story_count=len(included), estimated_story_count=len(estimated), unestimated_story_ids=unestimated_ids, cancelled_story_count=len(cancelled))

def delivery_forecast(items: Iterable[DeliveryItem]) -> DeliveryForecast:
    stories = tuple((item for item in items if item.item_type is DeliveryItemType.STORY))
    cancelled = tuple((story for story in stories if story.status is DeliveryItemStatus.CANCELLED))
    open_stories = tuple((story for story in stories if story.status not in _TERMINAL_STORY_STATUSES))
    covered = tuple((story for story in open_stories if story.remaining_hours is not None))
    missing_ids = tuple((story.id for story in open_stories if story.remaining_hours is None))
    if not open_stories:
        state = DeliveryMetricState.AVAILABLE
        remaining: float | None = 0.0
    elif not covered:
        state = DeliveryMetricState.NO_REMAINING_ESTIMATE
        remaining = None
    else:
        state = DeliveryMetricState.PARTIAL_COVERAGE if missing_ids else DeliveryMetricState.AVAILABLE
        remaining = round(sum((float(story.remaining_hours or 0.0) for story in covered)), 2)
    return DeliveryForecast(state=state, total_remaining_hours=remaining, open_story_count=len(open_stories), covered_story_count=len(covered), missing_remaining_story_ids=missing_ids, cancelled_story_count=len(cancelled))
