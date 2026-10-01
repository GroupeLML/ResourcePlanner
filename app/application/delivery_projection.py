from __future__ import annotations

from typing import Protocol, Sequence

from app.domain.delivery import (
    DeliveryItem,
    DeliveryPlan,
    delivery_forecast,
    delivery_progress,
)

from .delivery_contracts import (
    DeliveryPlanningReadPort,
    DeliveryWorkPackageReadPort,
    WorkPackagePlanningCapacityReadModel,
)
from .errors import ApplicationNotFoundError


class DeliveryProjectionRepositoryPort(Protocol):
    def get_non_archived_plan_for_work_package(
        self,
        work_package_id: str,
    ) -> DeliveryPlan | None: ...

    def list_items(self, plan_id: str) -> Sequence[DeliveryItem]: ...


class DeliveryProjectionService:
    def __init__(
        self,
        repository: DeliveryProjectionRepositoryPort,
        work_packages: DeliveryWorkPackageReadPort,
        planning: DeliveryPlanningReadPort,
    ) -> None:
        self._repository = repository
        self._work_packages = work_packages
        self._planning = planning

    @staticmethod
    def _planning_payload(
        capacity: WorkPackagePlanningCapacityReadModel,
    ) -> dict[str, object]:
        return {
            "provenance": capacity.provenance.value,
            "window_start": capacity.window_start,
            "window_end": capacity.window_end,
            "human_reserved_hours": capacity.human_reserved_hours,
            "total_reserved_hours": capacity.total_reserved_hours,
            "observed_planning_version": capacity.observed_planning_version,
            "approved_sources": [
                {
                    "demand_reference": source.demand_reference,
                    "approval_revision_id": source.approval_revision_id,
                    "approved_request_version": source.approved_request_version,
                    "approved_entry_keys": list(source.approved_entry_keys),
                }
                for source in capacity.approved_sources
            ],
            "asset_reserved_capacity": [
                {
                    "asset_type_id": item.asset_type_id,
                    "reserved_days": item.reserved_days,
                }
                for item in capacity.asset_reserved_capacity
            ],
        }

    def work_package_rollup(self, work_package_id: str) -> dict[str, object]:
        work_package = self._work_packages.get_work_package_delivery_reference(
            work_package_id
        )
        if work_package is None:
            raise ApplicationNotFoundError(
                "WorkPackage introuvable.",
                code="delivery_work_package_not_found",
                context={"work_package_id": work_package_id},
            )

        plan = self._repository.get_non_archived_plan_for_work_package(
            work_package.work_package_id
        )
        items = (
            tuple(self._repository.list_items(plan.id))
            if plan is not None
            else ()
        )
        progress = delivery_progress(items)
        forecast = delivery_forecast(items)
        capacity = self._planning.get_work_package_planning_capacity(
            work_package.work_package_id
        )
        if capacity is None:
            raise ApplicationNotFoundError(
                "Projection Planning du WorkPackage introuvable.",
                code="delivery_planning_capacity_not_found",
                context={"work_package_id": work_package.work_package_id},
            )

        diagnostics: list[dict[str, object]] = []
        if plan is None:
            diagnostics.append({"code": "DELIVERY_PLAN_MISSING"})
        if progress.unestimated_story_ids:
            diagnostics.append(
                {
                    "code": "DELIVERY_REFERENCE_ESTIMATE_INCOMPLETE",
                    "story_ids": list(progress.unestimated_story_ids),
                }
            )
        if forecast.missing_remaining_story_ids:
            diagnostics.append(
                {
                    "code": "DELIVERY_REMAINING_HOURS_INCOMPLETE",
                    "story_ids": list(forecast.missing_remaining_story_ids),
                }
            )
        if not capacity.approved_sources:
            diagnostics.append({"code": "APPROVED_PLANNING_CAPACITY_ABSENT"})

        balance = (
            None
            if forecast.total_remaining_hours is None
            else round(
                capacity.human_reserved_hours
                - forecast.total_remaining_hours,
                2,
            )
        )

        return {
            "work_package": {
                "id": work_package.work_package_id,
                "reference": work_package.reference,
                "name": work_package.name,
                "status": work_package.status,
                "reference_hours": work_package.reference_hours,
                "resource_class_code": work_package.resource_class_code,
                "resource_class_label": work_package.resource_class_label,
                "resource_class_active": work_package.resource_class_active,
                "task_resource_class_code": work_package.task_resource_class_code,
                "resource_class_diagnostic": work_package.resource_class_diagnostic,
            },
            "delivery_plan": (
                {
                    "id": plan.id,
                    "status": plan.status.value,
                    "delivery_version": plan.delivery_version,
                    "lead_user_id": plan.lead_user_id,
                }
                if plan is not None
                else None
            ),
            "progress": {
                "state": progress.state.value,
                "progress_ratio": progress.progress_ratio,
                "coverage_ratio": progress.coverage_ratio,
                "completed_reference_hours": progress.completed_reference_hours,
                "total_reference_hours": progress.total_reference_hours,
                "included_story_count": progress.included_story_count,
                "estimated_story_count": progress.estimated_story_count,
                "unestimated_story_ids": list(progress.unestimated_story_ids),
                "cancelled_story_count": progress.cancelled_story_count,
            },
            "forecast": {
                "state": forecast.state.value,
                "total_remaining_hours": forecast.total_remaining_hours,
                "coverage_ratio": forecast.coverage_ratio,
                "open_story_count": forecast.open_story_count,
                "covered_story_count": forecast.covered_story_count,
                "missing_remaining_story_ids": list(
                    forecast.missing_remaining_story_ids
                ),
                "cancelled_story_count": forecast.cancelled_story_count,
            },
            "planning_capacity": self._planning_payload(capacity),
            "forecast_capacity_balance_hours": balance,
            "diagnostics": diagnostics,
        }
