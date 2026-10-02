from __future__ import annotations

from dataclasses import replace
from collections.abc import Sequence
from datetime import date

from ...application.query_models import PlanningSnapshotReadModel, ShiftReadModel
from .medium_term_capacity_query import build_medium_term_capacity_buckets
from .query_repository import SqlPlannerQueryRepository as _BaseSqlPlannerQueryRepository


class SqlPlannerQueryRepository(_BaseSqlPlannerQueryRepository):
    """SQL web read repository enriched with medium-term capacity projections."""

    def list_shifts(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        resource_name: str | None = None,
        resource_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        can_manage_planning: bool = False,
    ) -> tuple[ShiftReadModel, ...]:
        return super().list_shifts(
            start=start,
            end=end,
            resource_name=resource_name,
            resource_id=resource_id,
            project_ids=project_ids,
            can_manage_planning=can_manage_planning,
        )

    def planning_snapshot(
        self,
        *,
        start: date,
        end: date,
        project_ids: Sequence[str] | None = None,
        include_resource_ids: Sequence[str] = (),
        can_manage_planning: bool = False,
    ) -> PlanningSnapshotReadModel:
        snapshot = super().planning_snapshot(
            start=start,
            end=end,
            project_ids=project_ids,
            include_resource_ids=include_resource_ids,
            can_manage_planning=can_manage_planning,
        )
        # Medium-term capacity remains an organization-wide reference. The contextual
        # scope filters projects/work packages/cards, not the company's capacity pool.
        single_bucket = end <= start or (end - start).days <= 6
        global_snapshot_rows = project_ids is None and single_bucket
        return replace(
            snapshot,
            capacity_buckets=build_medium_term_capacity_buckets(
                self,
                self._session,
                start=start,
                end=end,
                preloaded_shifts=(snapshot.shifts if global_snapshot_rows else None),
                preloaded_pending_loads=(
                    snapshot.pending_loads if global_snapshot_rows else None
                ),
            ),
        )
