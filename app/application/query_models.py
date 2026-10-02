from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from .read_models import DemandPeriodReadModel, DemandReadModel, SegmentReadModel


WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE = (
    "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE"
)


def work_package_resource_class_diagnostic(
    work_package_resource_class_code: str | None,
    task_resource_class_code: str | None,
) -> str | None:
    work_package_class = str(work_package_resource_class_code or "").strip() or None
    task_class = str(task_resource_class_code or "").strip() or None
    if work_package_class == task_class:
        return None
    return WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE


@dataclass(frozen=True, slots=True)
class ProjectReadModel:
    id: str
    number: str
    name: str
    client: str | None = None
    project_manager: str | None = None
    status: str = "active"
    active: bool = True
    erp_external_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkPackageReadModel:
    id: str
    reference: str
    project_number: str
    code: str | None
    name: str
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    planned_hours: float | None = None
    status: str = "planned"
    task_catalog_item_id: str | None = None
    task_code: str | None = None
    task_label: str | None = None
    resource_class_code: str | None = None
    resource_class_label: str | None = None
    resource_class_active: bool | None = None
    task_resource_class_code: str | None = None
    resource_class_diagnostic: str | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class ResourceReadModel:
    id: str
    name: str
    email: str | None = None
    resource_class: str | None = None
    competencies: str | None = None
    competency_ids: tuple[str, ...] = ()
    note: str | None = None
    active: bool = True
    sort_order: int = 0
    external_id: str | None = None
    erp_status: str | None = None
    erp_active: bool = True
    erp_department_description: str | None = None
    erp_department_code: str | None = None
    erp_employee_class: str | None = None
    erp_supervisor_external_id: str | None = None
    erp_phone: str | None = None
    erp_branch_code: str | None = None
    erp_contact_id: int | None = None

    @property
    def effective_active(self) -> bool:
        return self.active and self.erp_active


@dataclass(frozen=True, slots=True)
class DemandCancellationMaterializationReadModel:
    demand_number: str
    human_shift_count: int = 0
    locked_human_shift_count: int = 0
    asset_allocation_count: int = 0
    locked_asset_allocation_count: int = 0

    @property
    def has_operational_decisions(self) -> bool:
        return self.human_shift_count > 0 or self.asset_allocation_count > 0


@dataclass(frozen=True, slots=True)
class DemandMaterializedResourceReadModel:
    resource_id: str
    resource_name: str
    allocated_hours: float
    locked_hours: float = 0.0


@dataclass(frozen=True, slots=True)
class DemandMaterializedRequirementReadModel:
    requirement_id: str
    segment_id: str
    source_request_line_id: str | None
    status: str
    start_date: date
    end_date: date
    planned_hours: float
    covered_hours: float
    locked_hours: float
    remaining_hours: float
    excess_hours: float
    automatic_target_resource_id: str | None = None
    automatic_target_resource_name: str | None = None
    mobilized_resources: tuple[DemandMaterializedResourceReadModel, ...] = ()
    approval_revision_id: str | None = None
    approved_entry_key: str | None = None
    approval_reference_status: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceAvailabilityRuleReadModel:
    id: str
    availability_type: str
    resource_id: str | None = None
    resource_name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    weekdays: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    note: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class DemandHistoryReadModel:
    demand_number: str
    action: str
    occurred_at: datetime
    previous_status: str | None = None
    status: str | None = None
    comment: str | None = None
    details: str | None = None
    actor_user_id: str | None = None
    actor_name: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningHistoryReadModel:
    entity_type: str
    entity_reference: str
    action: str
    occurred_at: datetime
    parent_reference: str | None = None
    details: str | None = None
    actor_name: str | None = None


@dataclass(frozen=True, slots=True)
class ShiftReadModel:
    allocation_id: str
    segment_id: str
    resource_id: str
    resource_name: str
    work_date: date
    hours: float
    allocation_type: str | None = None
    source: str = "AUTO"
    locked: bool = False
    outside_standard_hours: bool = False
    confirmation: str | None = None
    confirmation_override: str | None = None
    load_kind: str = "FIRM"
    note: str | None = None
    demand_number: str | None = None
    project_number: str | None = None
    project_name: str | None = None
    project_manager: str | None = None
    requester: str | None = None
    emergency_override_active: bool = False
    segment_planned_hours: float = 0.0
    segment_locked_hours: float = 0.0
    segment_overallocated_hours: float = 0.0


@dataclass(frozen=True, slots=True)
class PendingDemandLoadReadModel:
    """Read-only workload proposed by a submitted, not-yet-approved request.

    ``mode=ADDITIVE`` is concurrent potential load and may be added to the current
    capacity exposure. ``mode=REPLACEMENT`` is a scenario that would replace the
    currently approved plan and is intentionally kept out of additive totals.
    """

    demand_number: str
    project_number: str | None
    project_name: str | None
    start_date: date
    end_date: date
    projected_hours: float | None
    window_hours: float
    mode: str
    load_kind: str = "POTENTIAL"
    current_plan_hours: float = 0.0
    delta_hours: float | None = None
    resource_count: int = 1
    required_competencies: str | None = None
    proposed_resource: str | None = None
    work_package_ref: str | None = None
    confirmation: str | None = None
    periods: tuple[DemandPeriodReadModel, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningActionReadModel:
    """One coordinator action shown above the operational planning board."""

    kind: str
    reference: str
    demand_number: str | None
    segment_id: str | None
    project_number: str | None
    project_name: str | None
    task_code: str | None
    task_label: str | None
    start_date: date
    end_date: date
    planned_hours: float
    required_competency: str | None = None
    required_competency_id: str | None = None
    priority: str | None = None
    status: str | None = None
    confirmation: str | None = None
    project_manager: str | None = None
    requester: str | None = None
    emergency_override_active: bool = False


@dataclass(frozen=True, slots=True)
class ResourceRecommendationReadModel:
    """Backend-ranked candidate for explicitly assigning one operational segment."""

    resource_id: str
    resource_name: str
    resource_class: str | None
    required_competency: str | None
    required_class: str | None
    competency_match: bool
    class_match: bool
    capacity_hours: float
    confirmed_hours: float
    tentative_hours: float
    outside_standard_hours: float
    free_after_confirmed: float
    prudent_free: float
    overtime_needed: float
    enough_after_confirmed: bool
    enough_prudent: bool
    score: float
    rank: int
    recommended: bool


@dataclass(frozen=True, slots=True)
class MediumTermUnlinkedSegmentReadModel:
    """Segment visible in medium-term planning without a valid WorkPackage link."""

    segment_id: str
    demand_number: str | None
    project_number: str
    project_name: str
    task_code: str | None
    task_label: str | None
    start_date: date
    end_date: date
    planned_hours: float
    resource_name: str | None
    status: str
    origin: str
    classification: str
    anomaly: bool
    link_target: str
    current_work_package_ref: str | None = None
    reapproval_on_link: bool = False
    description: str | None = None


@dataclass(frozen=True, slots=True)
class MediumTermCapacityBucketReadModel:
    """One backend-authoritative weekly capacity bucket."""

    week_start: date
    week_end: date
    resource_class: str | None
    resource_count: int
    capacity_hours: float
    firm_hours: float
    current_potential_hours: float
    submitted_hours: float
    replacement_proposal_hours: float
    replacement_delta_hours: float
    exposure_hours: float
    firm_residual_hours: float
    residual_hours: float
    utilization_pct: float | None
    state: str


@dataclass(frozen=True, slots=True)
class PlanningDayCapacityReadModel:
    day: date
    capacity_hours: float
    confirmed_hours: float
    tentative_hours: float
    outside_standard_hours: float
    total_hours: float
    prudent_free: float
    available: bool
    overloaded: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningResourceCapacityReadModel:
    resource_id: str
    resource_name: str
    resource_class: str | None
    capacity_hours: float
    confirmed_hours: float
    tentative_hours: float
    outside_standard_hours: float
    prudent_free: float
    overloaded: bool
    days: tuple[PlanningDayCapacityReadModel, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningSegmentCapacityDiagnosticReadModel:
    segment_id: str
    resource_id: str | None
    resource_name: str | None
    planned_hours: float
    allocated_hours: float
    outside_standard_hours: float
    unplaced_hours: float
    requires_outside_standard_hours: bool
    automatic_target_resource_id: str | None = None
    automatic_target_resource_name: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningCapacityGridReadModel:
    start: date
    end: date
    resources: tuple[PlanningResourceCapacityReadModel, ...]
    segment_diagnostics: tuple[PlanningSegmentCapacityDiagnosticReadModel, ...]


@dataclass(frozen=True, slots=True)
class AssetTypeReadModel:
    id: str
    code: str
    label: str
    category: str
    occupancy_policy: str
    active: bool
    qualification_policy: str = "ANY_ASSIGNED_WORKFORCE"
    required_competency_ids: tuple[str, ...] = ()
    required_competency_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssetReadModel:
    id: str
    code: str
    label: str
    asset_type_id: str
    active: bool


@dataclass(frozen=True, slots=True)
class AssetAllocationReadModel:
    allocation_id: str
    requirement_id: str
    asset_id: str
    asset_code: str
    asset_label: str
    start_date: date
    end_date: date
    locked: bool
    source: str
    operator_resource_id: str | None = None
    operator_resource_name: str | None = None
    qualification_state: str = "SATISFIED"
    required_competency_ids: tuple[str, ...] = ()
    required_competency_names: tuple[str, ...] = ()
    project_number: str | None = None


@dataclass(frozen=True, slots=True)
class AssetUnavailabilityReadModel:
    id: str
    asset_id: str
    start_date: date
    end_date: date
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AssetRequirementReadModel:
    requirement_id: str
    demand_number: str
    project_id: str
    project_number: str
    source_request_line_id: str
    source_period_id: str | None
    approval_revision_id: str | None
    approved_entry_key: str
    slot_index: int
    asset_type_id: str
    asset_type_code: str
    asset_type_label: str
    start_date: date
    end_date: date
    usage_hours: float | None
    status: str
    allocation_id: str | None = None
    asset_id: str | None = None
    asset_code: str | None = None
    asset_label: str | None = None
    allocation_start_date: date | None = None
    allocation_end_date: date | None = None
    allocation_locked: bool = False
    operator_resource_id: str | None = None
    operator_resource_name: str | None = None
    qualification_state: str = "SATISFIED"
    required_competency_ids: tuple[str, ...] = ()
    required_competency_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssetDayCapacityReadModel:
    asset_id: str
    day: date
    capacity_units: int
    occupied_units: int
    remaining_units: int
    unavailable: bool
    available: bool
    visible_occupations: tuple[AssetAllocationReadModel, ...] = ()
    has_hidden_occupancy: bool = False


@dataclass(frozen=True, slots=True)
class AssetPlanningDiagnosticReadModel:
    code: str
    message: str
    requirement_id: str | None = None
    allocation_id: str | None = None
    asset_id: str | None = None
    day: date | None = None


@dataclass(frozen=True, slots=True)
class AssetPlanningWindowReadModel:
    asset_types: tuple[AssetTypeReadModel, ...] = ()
    assets: tuple[AssetReadModel, ...] = ()
    requirements: tuple[AssetRequirementReadModel, ...] = ()
    allocations: tuple[AssetAllocationReadModel, ...] = ()
    unavailability: tuple[AssetUnavailabilityReadModel, ...] = ()
    capacity: tuple[AssetDayCapacityReadModel, ...] = ()
    diagnostics: tuple[AssetPlanningDiagnosticReadModel, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningSnapshotReadModel:
    """Canonical, transaction-coherent planning window exposed to web clients."""

    start: date
    end: date
    resources: tuple[ResourceReadModel, ...]
    demands: tuple[DemandReadModel, ...]
    segments: tuple[SegmentReadModel, ...]
    shifts: tuple[ShiftReadModel, ...]
    planning_version: int = 1
    pending_loads: tuple[PendingDemandLoadReadModel, ...] = ()
    capacity_buckets: tuple[MediumTermCapacityBucketReadModel, ...] = ()
    asset_types: tuple[AssetTypeReadModel, ...] = ()
    assets: tuple[AssetReadModel, ...] = ()
    asset_requirements: tuple[AssetRequirementReadModel, ...] = ()
    asset_allocations: tuple[AssetAllocationReadModel, ...] = ()
    asset_unavailability: tuple[AssetUnavailabilityReadModel, ...] = ()
    asset_capacity: tuple[AssetDayCapacityReadModel, ...] = ()
    asset_diagnostics: tuple[AssetPlanningDiagnosticReadModel, ...] = ()
    firm_hours: float = 0.0
    potential_hours: float = 0.0
    replacement_proposal_hours: float = 0.0
