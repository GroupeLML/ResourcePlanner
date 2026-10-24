from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
import unittest

from app.domain.reservable_assets import (
    AssetOccupation,
    ContinuousAssignmentState,
    continuous_assignment_state,
    overlapping_asset_occupations,
)
from app.infrastructure.sql.asset_shift_projection import (
    ASSOCIATION_INHERITED_RESOURCE_PERIOD,
    asset_shift_association,
)


DAY = date(2026, 10, 23)


class ContinuousVehicleContractTests(unittest.TestCase):
    def test_effective_state_is_backend_domain_rule(self) -> None:
        self.assertEqual(
            continuous_assignment_state(
                start_date=DAY + timedelta(days=1),
                end_date=None,
                as_of=DAY,
            ),
            ContinuousAssignmentState.SCHEDULED,
        )
        self.assertEqual(
            continuous_assignment_state(
                start_date=DAY - timedelta(days=10),
                end_date=DAY + timedelta(days=10),
                as_of=DAY,
            ),
            ContinuousAssignmentState.ACTIVE,
        )
        self.assertEqual(
            continuous_assignment_state(
                start_date=DAY - timedelta(days=10),
                end_date=DAY - timedelta(days=1),
                as_of=DAY,
            ),
            ContinuousAssignmentState.ENDED,
        )

    def test_open_occupancy_overlaps_any_future_candidate_without_expanding_days(self) -> None:
        open_row = AssetOccupation(
            allocation_id="OPEN",
            asset_id="VEHICLE",
            start_date=DAY,
            end_date=None,
        )
        future = AssetOccupation(
            allocation_id="FUTURE",
            asset_id="VEHICLE",
            start_date=DAY + timedelta(days=500),
            end_date=DAY + timedelta(days=501),
        )
        self.assertEqual(
            overlapping_asset_occupations(future, (open_row,)),
            ("OPEN",),
        )
        with self.assertRaises(ValueError):
            open_row.dates()

    def test_open_candidate_is_blocked_by_existing_future_bounded_occupation(self) -> None:
        future = AssetOccupation(
            allocation_id="FUTURE",
            asset_id="VEHICLE",
            start_date=DAY + timedelta(days=30),
            end_date=DAY + timedelta(days=40),
        )
        proposed = AssetOccupation(
            allocation_id="OPEN",
            asset_id="VEHICLE",
            start_date=DAY,
            end_date=None,
        )
        self.assertEqual(
            overlapping_asset_occupations(proposed, (future,)),
            ("FUTURE",),
        )

    def test_open_resource_period_is_inherited_by_future_shift_without_new_allocation(self) -> None:
        requirement = SimpleNamespace(
            status="Planifié",
            origin="RESOURCE_PERIOD",
            context_resource_id="RESOURCE-A",
        )
        allocation = SimpleNamespace(
            start_date=DAY,
            end_date=None,
            operator_resource_id="RESOURCE-A",
        )
        shift = SimpleNamespace(
            id="SHIFT-FUTURE",
            work_date=DAY + timedelta(days=365),
            resource_id="RESOURCE-A",
        )
        human_requirement = SimpleNamespace(
            id="RR-FUTURE",
            workforce_request_id=None,
            project_id="PROJECT-A",
        )
        self.assertEqual(
            asset_shift_association(
                requirement=requirement,
                allocation=allocation,
                shift=shift,
                human_requirement=human_requirement,
            ),
            ASSOCIATION_INHERITED_RESOURCE_PERIOD,
        )

    def test_revoked_continuous_interval_stops_inheritance_after_end(self) -> None:
        requirement = SimpleNamespace(
            status="Planifié",
            origin="RESOURCE_PERIOD",
            context_resource_id="RESOURCE-A",
        )
        allocation = SimpleNamespace(
            start_date=DAY,
            end_date=DAY + timedelta(days=2),
            operator_resource_id="RESOURCE-A",
        )
        shift = SimpleNamespace(
            id="SHIFT-AFTER",
            work_date=DAY + timedelta(days=3),
            resource_id="RESOURCE-A",
        )
        human_requirement = SimpleNamespace(
            id="RR-AFTER",
            workforce_request_id=None,
            project_id="PROJECT-A",
        )
        self.assertIsNone(
            asset_shift_association(
                requirement=requirement,
                allocation=allocation,
                shift=shift,
                human_requirement=human_requirement,
            )
        )


if __name__ == "__main__":
    unittest.main()
