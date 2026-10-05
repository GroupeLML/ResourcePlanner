from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AvailabilityType = Literal["Horaire standard", "Vacances", "Jour férié"]
OverallocationPolicy = Literal["KEEP_EXCEPTION", "INCREASE_PLANNED"]
LoadProfile = Literal["UNIFORM", "FRONT_LOADED", "BACK_LOADED", "BELL"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompetencyCreateRequest(StrictRequest):
    name: str = Field(min_length=1)
    description: str | None = None
    active: bool = True
    sort_order: int = Field(default=0, ge=0)


class CompetencyUpdateRequest(StrictRequest):
    name: str | None = None
    description: str | None = None
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("name", "active", "sort_order", mode="before")
    @classmethod
    def reject_null_required_competency_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("Ce champ ne peut pas être null; omets-le pour ne pas le modifier.")
        return value


class ResourceCreateRequest(StrictRequest):
    name: str = Field(min_length=1)
    email: str | None = None
    resource_class: str | None = None
    competencies: str | None = None
    competency_ids: list[str] | None = None
    note: str | None = None
    active: bool = True
    sort_order: int = Field(default=0, ge=0)
    external_id: str | None = None


class ResourceUpdateRequest(StrictRequest):
    name: str | None = None
    email: str | None = None
    resource_class: str | None = None
    competencies: str | None = None
    competency_ids: list[str] | None = None
    note: str | None = None
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)
    external_id: str | None = None

    @field_validator("active", "sort_order", mode="before")
    @classmethod
    def reject_null_non_nullable_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("Ce champ ne peut pas être null; omets-le pour ne pas le modifier.")
        return value


class ResourceReorderRequest(StrictRequest):
    direction: Literal["up", "down"]


class AvailabilityRuleCreateRequest(StrictRequest):
    availability_type: AvailabilityType
    resource_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    weekdays: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    note: str | None = None
    active: bool = True


class AvailabilityRuleUpdateRequest(StrictRequest):
    availability_type: AvailabilityType | None = None
    resource_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    weekdays: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    note: str | None = None
    active: bool | None = None

    @field_validator("availability_type", "active", mode="before")
    @classmethod
    def reject_null_required_patch_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("Ce champ ne peut pas être null; omets-le pour ne pas le modifier.")
        return value


class WorkPackageCreateRequest(StrictRequest):
    project_number: str = Field(min_length=1)
    task_catalog_item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    code: str | None = None
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    planned_hours: float | None = Field(default=None, ge=0)
    resource_class_code: str | None = None


class WorkPackageLoadIntervalRequest(StrictRequest):
    id: str | None = None
    start_date: date
    end_date: date
    hours: Decimal = Field(ge=0)


class WorkPackageUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    project_number: str | None = None
    task_catalog_item_id: str | None = None
    resource_class_code: str | None = None
    code: str | None = None
    name: str | None = None
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    planned_hours: float | None = Field(default=None, ge=0)
    load_intervals: list[WorkPackageLoadIntervalRequest] = Field(default_factory=list)


class WorkPackageLifecycleRequest(StrictRequest):
    expected_version: int = Field(ge=1)


class WorkPackageWeeklyLoadRequest(StrictRequest):
    week_start: date
    hours: Decimal = Field(ge=0)


class WorkPackageWeeklyLoadReplaceRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    origin: Literal["AUTO", "MANUAL"]
    loads: list[WorkPackageWeeklyLoadRequest] = Field(min_length=1)


class DemandLineRequest(StrictRequest):
    id: str | None = None
    position: int | None = Field(default=None, ge=0)
    kind: Literal["WORKFORCE", "ASSET"] = "WORKFORCE"
    required_resource_class: str | None = None
    required_competency_ids: list[str] = Field(default_factory=list)
    desired_start: date | None = None
    desired_end: date | None = None
    desired_active_days: int | None = Field(default=None, ge=1)
    estimated_hours: float | None = Field(default=None, gt=0)
    work_package_ref: str | None = None
    task_code: str | None = None
    proposed_resource_id: str | None = None
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None
    confirmation: str = "Confirmée"
    description: str | None = None


_LEGACY_DEMAND_NEED_FIELDS = frozenset(
    {
        "desired_start",
        "desired_end",
        "work_package_ref",
        "task_code",
        "confirmation",
        "resource_count",
        "required_competencies",
        "required_competency_ids",
        "required_resource_class",
        "estimated_hours",
        "estimated_days",
        "proposed_technician",
    }
)


class DemandCreateRequest(StrictRequest):
    project_number: str
    desired_start: date | None = None
    submit: bool = False
    project_name: str = ""
    client: str = ""
    requester_user_id: str | None = None
    work_package_ref: str | None = None
    task_code: str | None = None
    request_type: str = "Projet"
    priority: str = "Normale"
    confirmation: str = "Confirmée"
    desired_end: date | None = None
    description: str = ""
    site_client: str = ""
    location: str = ""
    resource_count: int = Field(default=1, ge=1)
    required_competencies: str | None = None
    required_competency_ids: list[str] | None = None
    required_resource_class: str | None = None
    estimated_hours: float | None = Field(default=None, ge=0)
    estimated_days: int | None = Field(default=None, ge=1)
    proposed_technician: str | None = None
    lines: list[DemandLineRequest] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_need_shape(self) -> "DemandCreateRequest":
        if self.lines is not None:
            mixed = sorted(_LEGACY_DEMAND_NEED_FIELDS.intersection(self.model_fields_set))
            if mixed:
                raise ValueError(
                    "Ne mélange pas les champs de besoin plats avec lines: "
                    + ", ".join(mixed)
                )
        elif self.desired_start is None:
            raise ValueError("desired_start est requis lorsque lines est omis.")
        return self


class DemandUpdateRequest(StrictRequest):
    project_number: str | None = None
    project_name: str | None = None
    client: str | None = None
    requester_user_id: str | None = None
    work_package_ref: str | None = None
    task_code: str | None = None
    request_type: str | None = None
    priority: str | None = None
    confirmation: str | None = None
    desired_start: date | None = None
    desired_end: date | None = None
    description: str | None = None
    site_client: str | None = None
    location: str | None = None
    resource_count: int | None = Field(default=None, ge=1)
    required_competencies: str | None = None
    required_competency_ids: list[str] | None = None
    required_resource_class: str | None = None
    estimated_hours: float | None = Field(default=None, ge=0)
    estimated_days: int | None = Field(default=None, ge=1)
    proposed_technician: str | None = None
    lines: list[DemandLineRequest] | None = Field(default=None, min_length=1)
    expected_version: int | None = Field(default=None, ge=1)
    comment: str = "Demande modifiée via API"

    @model_validator(mode="after")
    def validate_need_shape(self) -> "DemandUpdateRequest":
        if "lines" in self.model_fields_set:
            if self.lines is None:
                raise ValueError("lines ne peut pas être null; omets-le pour ne pas le modifier.")
            mixed = sorted(_LEGACY_DEMAND_NEED_FIELDS.intersection(self.model_fields_set))
            if mixed:
                raise ValueError(
                    "Ne mélange pas les champs de besoin plats avec lines: "
                    + ", ".join(mixed)
                )
            if self.expected_version is None:
                raise ValueError("expected_version est requis lorsque lines est fourni.")
        return self


class DemandPeriodRequest(StrictRequest):
    period_id: str = Field(min_length=1)
    start_date: date
    end_date: date
    hours: float | None = Field(default=None, gt=0)
    kind: Literal["CUMULATIVE", "ALTERNATIVE"] = "CUMULATIVE"
    alternative_group: str | None = None
    confirmation: str = "Tentative"
    proposed_resource: str | None = None
    resource_count: int = Field(default=1, ge=1)
    desired_active_days: int | None = Field(default=None, ge=1)
    note: str = ""


class DemandPeriodsReplaceRequest(StrictRequest):
    periods: list[DemandPeriodRequest]
    expected_request_version: int = Field(ge=1)


class DemandAlternativeSelectionRequest(StrictRequest):
    period_id: str = Field(min_length=1)
    expected_operational_version: int | None = Field(default=None, ge=1)


class DemandOperationalConfirmationRequest(StrictRequest):
    confirmation: Literal["Tentative", "Confirmée"]
    period_id: str | None = Field(default=None, min_length=1)
    expected_operational_version: int | None = Field(default=None, ge=1)


class DemandWorkflowVersionRequest(StrictRequest):
    expected_version: int | None = Field(default=None, ge=1)


class DemandWorkflowOptionalCommentRequest(DemandWorkflowVersionRequest):
    comment: str = ""


class DemandWorkflowRequiredCommentRequest(DemandWorkflowVersionRequest):
    comment: str = Field(min_length=1)


class DemandCancellationRequest(StrictRequest):
    reason: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class DemandCancellationRejectRequest(StrictRequest):
    cancellation_request_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class DemandCancellationAcceptRequest(StrictRequest):
    cancellation_request_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    expected_planning_version: int = Field(ge=1)


class DemandApprovalRequest(DemandWorkflowOptionalCommentRequest):
    expected_planning_version: int | None = Field(default=None, ge=1)


class DemandApprovalVoteRequest(StrictRequest):
    approval_cycle_id: str = Field(min_length=1)
    expected_request_version: int = Field(ge=1)
    requirement_ids: list[str] = Field(default_factory=list)
    request_line_ids: list[str] = Field(default_factory=list)
    decision: Literal["APPROVE"] = "APPROVE"
    comment: str = ""
    expected_planning_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_vote_targets(self) -> "DemandApprovalVoteRequest":
        self.requirement_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in self.requirement_ids
                if str(value).strip()
            )
        )
        self.request_line_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in self.request_line_ids
                if str(value).strip()
            )
        )
        if not self.requirement_ids and not self.request_line_ids:
            raise ValueError("Au moins une exigence ou une ligne doit être ciblée.")
        return self


class OptionalCommentRequest(StrictRequest):
    comment: str = ""


class RequiredCommentRequest(StrictRequest):
    comment: str = Field(min_length=1)


class SegmentCreateRequest(StrictRequest):
    demand_number: str
    start_date: date
    end_date: date
    planned_hours: float = Field(gt=0)
    project_number: str | None = None
    project_name: str | None = None
    technician: str | None = None
    status: str = "À assigner"
    description: str = ""
    source_effort_id: str | None = None
    required_competency: str | None = None
    required_competency_id: str | None = None
    planning_type: str = "Flexible"
    priority: str = "Normale"
    outside_standard_hours: bool = False
    confirmation: str | None = None
    load_profile: LoadProfile = "UNIFORM"


class SegmentUpdateRequest(StrictRequest):
    demand_number: str | None = None
    project_number: str | None = None
    project_name: str | None = None
    technician: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    planned_hours: float | None = Field(default=None, gt=0)
    status: str | None = None
    description: str | None = None
    source_effort_id: str | None = None
    required_competency: str | None = None
    required_competency_id: str | None = None
    planning_type: str | None = None
    priority: str | None = None
    outside_standard_hours: bool | None = None
    confirmation: str | None = None
    load_profile: LoadProfile | None = None
    allow_locked_overallocation: bool = False

    @field_validator("outside_standard_hours", "load_profile", mode="before")
    @classmethod
    def reject_null_non_nullable_patch_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError(
                "Ce champ ne peut pas être null; omets-le pour ne pas le modifier."
            )
        return value


class ResourceReferenceRequest(StrictRequest):
    resource_id: str | None = Field(default=None, min_length=1)
    technician: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_resource_reference(self) -> "ResourceReferenceRequest":
        if not str(self.resource_id or "").strip() and not str(self.technician or "").strip():
            raise ValueError("Une ressource est requise.")
        return self


class SegmentAssignRequest(ResourceReferenceRequest):
    pass


class AllocationMoveRequest(ResourceReferenceRequest):
    day: date
    outside_standard_hours: bool = False
    expected_planning_version: int | None = Field(default=None, ge=1)


class ManualAllocationRequest(ResourceReferenceRequest):
    day: date
    hours: float = Field(gt=0)
    outside_standard_hours: bool = False
    note: str = ""
    confirmation: str | None = None
    overallocation_policy: OverallocationPolicy | None = None


class AllocationDropEvaluateRequest(StrictRequest):
    resource_id: str = Field(min_length=1)
    day: date
    outside_standard_hours: bool = False


class AtomicAllocationBaseRequest(StrictRequest):
    resource_id: str = Field(min_length=1)
    day: date
    expected_planning_version: int = Field(ge=1)
    outside_standard_hours: bool | None = None
    overallocation_policy: OverallocationPolicy | None = None
    expected_approval_revision_id: str | None = Field(default=None, min_length=1)
    expected_operational_version: int | None = Field(default=None, ge=1)


class AllocationWindowExtensionProposalRequest(AllocationDropEvaluateRequest):
    expected_request_version: int = Field(ge=1)
    expected_approval_revision_id: str = Field(min_length=1)


class AllocationExtendMoveRequest(AtomicAllocationBaseRequest):
    confirm_window_extension: bool
    outside_standard_hours: bool = False


class AllocationSplitRequest(AtomicAllocationBaseRequest):
    transfer_hours: float = Field(gt=0)


class AllocationDuplicateRequest(AtomicAllocationBaseRequest):
    pass


class QuickShiftRequest(StrictRequest):
    project_number: str
    technician: str
    day: date
    hours: float = Field(gt=0)
    project_name: str = ""
    outside_standard_hours: bool = False
    note: str = ""
    description: str = ""
    confirmation: str = "Confirmée"
