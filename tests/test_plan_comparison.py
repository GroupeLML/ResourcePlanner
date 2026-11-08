from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.domain.plan_comparison import (
    AllocationProjection,
    compare_allocation_plans,
    summarize_differences,
)


DAY = date(2026, 8, 18)


class PlanComparisonTests(unittest.TestCase):
    def allocation(
        self,
        *,
        hours: float,
        segment: str = "SEG-1",
        resource: str = "RES-1",
        allocation_type: str = "Flexible",
        locked: bool = False,
        outside_schedule: bool = False,
        day: date = DAY,
    ) -> AllocationProjection:
        return AllocationProjection(
            segment_id=segment,
            resource_id=resource,
            day=day,
            hours=hours,
            allocation_type=allocation_type,
            locked=locked,
            outside_schedule=outside_schedule,
        )

    def test_identical_plans_match(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=4)],
            [self.allocation(hours=4)],
        )
        self.assertTrue(comparison.matches)
        self.assertEqual(comparison.differences, ())

    def test_generated_row_splits_are_aggregated(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=2), self.allocation(hours=2)],
            [self.allocation(hours=4)],
        )
        self.assertTrue(comparison.matches)
        self.assertEqual(comparison.compared_keys, 1)

    def test_comparison_uses_excel_two_decimal_precision(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=2.3333)],
            [self.allocation(hours=2.3349)],
        )
        self.assertTrue(comparison.matches)

    def test_hour_difference_is_reported(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=4)],
            [self.allocation(hours=3.5)],
        )
        self.assertFalse(comparison.matches)
        self.assertEqual(len(comparison.differences), 1)
        self.assertEqual(comparison.differences[0].legacy_hours, 4)
        self.assertEqual(comparison.differences[0].shadow_hours, 3.5)

    def test_locked_type_is_normalized(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=2, allocation_type="Fixe", locked=True)],
            [self.allocation(hours=2, allocation_type="Locked", locked=True)],
        )
        self.assertTrue(comparison.matches)

    def test_real_business_allocation_type_change_remains_semantic(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=8, allocation_type="Flexible")],
            [self.allocation(hours=8, allocation_type="Fixe")],
        )
        self.assertFalse(comparison.matches)
        self.assertEqual(
            {(row.allocation_type, row.legacy_hours, row.shadow_hours) for row in comparison.differences},
            {
                ("Flexible", 8.0, 0.0),
                ("Fixe", 0.0, 8.0),
            },
        )

    def test_resource_and_outside_schedule_are_semantic_keys(self) -> None:
        resource_diff = compare_allocation_plans(
            [self.allocation(hours=2, resource="RES-1")],
            [self.allocation(hours=2, resource="RES-2")],
        )
        outside_diff = compare_allocation_plans(
            [self.allocation(hours=2, locked=True, outside_schedule=False)],
            [self.allocation(hours=2, locked=True, outside_schedule=True)],
        )
        self.assertFalse(resource_diff.matches)
        self.assertFalse(outside_diff.matches)

    def test_summary_detects_pure_redistribution_without_net_hour_change(self) -> None:
        tomorrow = DAY + timedelta(days=1)
        comparison = compare_allocation_plans(
            [self.allocation(hours=4, day=DAY)],
            [self.allocation(hours=4, day=tomorrow)],
        )
        summary = summarize_differences(comparison)
        self.assertEqual(summary.difference_count, 2)
        self.assertEqual(summary.affected_segment_count, 1)
        self.assertEqual(summary.redistributed_segment_count, 1)
        self.assertEqual(summary.segment_total_delta_count, 0)
        self.assertEqual(summary.legacy_only_key_count, 1)
        self.assertEqual(summary.shadow_only_key_count, 1)
        self.assertEqual(summary.net_shadow_minus_legacy_hours, 0)
        self.assertEqual(summary.absolute_difference_hours, 8)

    def test_summary_detects_real_total_hour_delta_and_type(self) -> None:
        comparison = compare_allocation_plans(
            [self.allocation(hours=8, allocation_type="Flexible")],
            [self.allocation(hours=5, allocation_type="Flexible")],
        )
        summary = summarize_differences(comparison)
        self.assertEqual(summary.affected_segment_count, 1)
        self.assertEqual(summary.redistributed_segment_count, 0)
        self.assertEqual(summary.segment_total_delta_count, 1)
        self.assertEqual(summary.changed_key_count, 1)
        self.assertEqual(summary.net_shadow_minus_legacy_hours, -3)
        self.assertEqual(summary.difference_count_by_type, (("Flexible", 1),))
        self.assertEqual(summary.net_hours_by_type, (("Flexible", -3.0),))


if __name__ == "__main__":
    unittest.main()
