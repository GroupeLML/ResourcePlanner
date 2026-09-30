from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from .medium_term_budget import MediumTermBudgetReadModel
from .plan_delta import DemandApprovalStateReadModel, DemandPlanDeltaReadModel
from .query_models import (
    AssetPlanningWindowReadModel,
    AssetRequirementReadModel,
    DemandCancellationMaterializationReadModel,
    DemandHistoryReadModel,
    DemandMaterializedRequirementReadModel,
    MediumTermUnlinkedSegmentReadModel,
    PendingDemandLoadReadModel,
    PlanningActionReadModel,
    PlanningCapacityGridReadModel,
    PlanningHistoryReadModel,
    PlanningSnapshotReadModel,
    ProjectReadModel,
    ResourceAvailabilityRuleReadModel,
    ResourceReadModel,
    ResourceRecommendationReadModel,
    ShiftReadModel,
    WorkPackageReadModel,
)
from .read_models import DemandPeriodReadModel, DemandReadModel, SegmentReadModel


class PlannerQueryPort(Protocol):
    """Read-only canonical query surface required by web clients."""

    def list_projects(
        self,
        *,
        active_only: bool = False,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[ProjectReadModel]: ...

    def list_work_packages(
        self,
        *,
        project_number: str | None = None,
        active_only: bool = True,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[WorkPackageReadModel]: ...

    def medium_term_budget_projection(
        self,
        *,
        project_number: str,
        project_ids: Sequence[str] | None = None,
    ) -> MediumTermBudgetReadModel | None: ...

    def list_resources(self, *, active_only: bool = True) -> Sequence[ResourceReadModel]: ...

    def list_availability_rules(
        self,
        *,
        resource_id: str | None = None,
        include_global: bool = True,
        active_only: bool = True,
    ) -> Sequence[ResourceAvailabilityRuleReadModel]: ...

    def list_schedulable_resources(
        self,
        *,
        start: date,
        end: date,
    ) -> Sequence[ResourceReadModel]: ...

    def list_demands(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> Sequence[DemandReadModel]: ...

    def list_demands_with_cancellation_materialization(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> Sequence[
        tuple[DemandReadModel, DemandCancellationMaterializationReadModel]
    ]: ...

    def get_demand(self, number: str) -> DemandReadModel | None: ...

    def get_demand_with_cancellation_materialization(
        self,
        number: str,
    ) -> tuple[DemandReadModel, DemandCancellationMaterializationReadModel] | None: ...

    def demand_cancellation_materialization(
        self,
        number: str,
    ) -> DemandCancellationMaterializationReadModel: ...

    def list_demand_cancellation_materializations(
        self,
        numbers: Sequence[str],
    ) -> Sequence[DemandCancellationMaterializationReadModel]: ...

    def list_demand_history(self, number: str) -> Sequence[DemandHistoryReadModel]: ...

    def list_demand_periods(
        self,
        number: str,
        *,
        request_line_id: str | None = None,
    ) -> Sequence[DemandPeriodReadModel]: ...

    def demand_approval_state(
        self,
        number: str,
    ) -> DemandApprovalStateReadModel | None: ...

    def list_demand_requirements(
        self,
        number: str,
    ) -> Sequence[DemandMaterializedRequirementReadModel]: ...

    def list_demand_asset_requirements(
        self,
        number: str,
    ) -> Sequence[AssetRequirementReadModel]: ...

    def asset_planning_window(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> AssetPlanningWindowReadModel: ...

    def demand_plan_delta(self, number: str) -> DemandPlanDeltaReadModel | None: ...

    def list_pending_loads(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[PendingDemandLoadReadModel]: ...

    def list_medium_term_unlinked_segments(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[MediumTermUnlinkedSegmentReadModel]: ...

    def planning_capacity_grid(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        include_resource_ids: Sequence[str] = (),
    ) -> PlanningCapacityGridReadModel: ...

    def list_planning_actions(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[PlanningActionReadModel]: ...

    def recommend_resources(
        self,
        segment_id: str,
    ) -> Sequence[ResourceRecommendationReadModel]: ...

    def list_segments(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        include_cancelled: bool = False,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[SegmentReadModel]: ...

    def get_segment(self, segment_id: str) -> SegmentReadModel | None: ...

    def list_planning_history(
        self,
        entity_type: str,
        reference: str,
    ) -> Sequence[PlanningHistoryReadModel]: ...

    def list_shifts(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        resource_name: str | None = None,
        resource_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> Sequence[ShiftReadModel]: ...

    def planning_snapshot(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        include_resource_ids: Sequence[str] = (),
    ) -> PlanningSnapshotReadModel: ...
