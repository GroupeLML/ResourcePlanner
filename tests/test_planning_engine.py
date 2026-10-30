from __future__ import annotations

import unittest
from datetime import date

from app.domain.planning_engine import (
    MISSING_ALLOCATION_TYPE,
    LockedAllocationInput,
    SegmentInput,
    build_allocation_plan,
)


D1 = date(2026, 8, 17)
D2 = date(2026, 8, 18)
D3 = date(2026, 8, 22)


class PlanningEngineTests(unittest.TestCase):
    def test_locked_allocation_is_preserved_and_subtracted_from_segment(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 8, "Fixe")],
            [LockedAllocationInput("S1", "R1", D1, 3)],
            {("R1", D1): 8},
        )
        self.assertEqual(result.locked_allocation_count, 1)
        self.assertEqual(result.allocated_hours, 8)
        locked = [row for row in result.allocations if row.locked]
        automatic = [row for row in result.allocations if not row.locked]
        self.assertEqual(locked[0].hours, 3)
        self.assertEqual(automatic[0].hours, 5)

    def test_fixed_shortage_becomes_non_counting_outside_schedule_proposal(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 12, "Fixe")],
            [],
            {("R1", D1): 8},
        )
        actual = [row for row in result.allocations if row.counts_as_allocated]
        missing = [row for row in result.allocations if not row.counts_as_allocated]
        self.assertEqual(result.allocated_hours, 8)
        self.assertEqual(result.unallocated_hours, 4)
        self.assertEqual(actual[0].hours, 8)
        self.assertEqual(missing[0].hours, 4)
        self.assertEqual(missing[0].allocation_type, MISSING_ALLOCATION_TYPE)
        self.assertEqual(result.missing_allocation_count, 1)

    def test_fixed_shortage_is_real_overtime_when_segment_allows_it(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 12, "Fixe", overtime_allowed=True)],
            [],
            {("R1", D1): 8},
        )
        self.assertEqual(result.allocated_hours, 12)
        self.assertEqual(result.unallocated_hours, 0)
        overtime = [row for row in result.allocations if row.outside_schedule]
        self.assertEqual(len(overtime), 1)
        self.assertEqual(overtime[0].hours, 4)
        self.assertEqual(overtime[0].allocation_type, "Fixe")
        self.assertEqual(result.overtime_hours, 4)

    def test_flexible_segment_leaves_shortage_unallocated_without_overtime_permission(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 12, "Flexible")],
            [],
            {("R1", D1): 8},
        )
        self.assertEqual(result.allocated_hours, 8)
        self.assertEqual(result.unallocated_hours, 4)
        self.assertEqual(result.missing_allocation_count, 1)

    def test_flexible_shortage_uses_authorized_outside_schedule_slot(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 12, "Flexible", overtime_allowed=True)],
            [],
            {("R1", D1): 8},
        )
        self.assertEqual(result.allocated_hours, 12)
        self.assertEqual(result.unallocated_hours, 0)
        overtime = [row for row in result.allocations if row.outside_schedule]
        self.assertEqual([(row.day, row.hours, row.allocation_type) for row in overtime], [(D1, 4, "Flexible")])

    def test_placement_cutoff_constrains_standard_and_overtime_generation(self) -> None:
        cutoff = date(2026, 8, 18)
        segment = SegmentInput(
            "S-CUTOFF",
            "R1",
            D1,
            cutoff,
            12,
            "Flexible",
            overtime_allowed=True,
            placement_not_before_date=cutoff,
        )
        result = build_allocation_plan(
            [segment],
            [],
            {("R1", D1): 8, ("R1", cutoff): 8},
            outside_schedule_eligible_by_resource_day={
                ("R1", D1): True,
                ("R1", cutoff): True,
            },
        )
        counted = [row for row in result.allocations if row.counts_as_allocated]
        self.assertTrue(counted)
        self.assertTrue(all(row.day >= cutoff for row in counted))
        self.assertEqual(sum(row.hours for row in counted), 12)

    def test_cutoff_with_no_eligible_interval_leaves_work_unallocated(self) -> None:
        cutoff = date(2026, 8, 19)
        result = build_allocation_plan(
            [
                SegmentInput(
                    "S-CUTOFF-NONE",
                    "R1",
                    D1,
                    D2,
                    8,
                    "Flexible",
                    placement_not_before_date=cutoff,
                )
            ],
            [],
            {("R1", D1): 8, ("R1", D2): 8},
        )
        self.assertEqual(result.allocated_hours, 0)
        self.assertEqual(result.unallocated_hours, 8)

    def test_fixed_work_consumes_capacity_before_flexible_work(self) -> None:
        result = build_allocation_plan(
            [
                SegmentInput("FIX", "R1", D1, D1, 6, "Fixe"),
                SegmentInput("FLEX", "R1", D1, D1, 6, "Flexible"),
            ],
            [],
            {("R1", D1): 8},
        )
        actual_hours = {
            row.segment_id: row.hours
            for row in result.allocations
            if row.counts_as_allocated
        }
        self.assertEqual(actual_hours["FIX"], 6)
        self.assertEqual(actual_hours["FLEX"], 2)
        self.assertEqual(result.unallocated_hours, 4)

    def test_priority_orders_competing_flexible_segments(self) -> None:
        result = build_allocation_plan(
            [
                SegmentInput("NORMAL", "R1", D1, D1, 6, "Flexible", priority_rank=2),
                SegmentInput("URGENT", "R1", D1, D1, 6, "Flexible", priority_rank=0),
            ],
            [],
            {("R1", D1): 8},
        )
        actual_hours = {
            row.segment_id: row.hours
            for row in result.allocations
            if row.counts_as_allocated
        }
        self.assertEqual(actual_hours["URGENT"], 6)
        self.assertEqual(actual_hours["NORMAL"], 2)

    def test_resources_have_independent_capacity(self) -> None:
        result = build_allocation_plan(
            [
                SegmentInput("S1", "LABOR-A", D1, D1, 8, "Flexible"),
                SegmentInput("S2", "EQUIPMENT-A", D1, D1, 8, "Flexible"),
            ],
            [],
            {("LABOR-A", D1): 8, ("EQUIPMENT-A", D1): 8},
        )
        self.assertEqual(result.allocated_hours, 16)
        self.assertEqual(result.unallocated_hours, 0)

    def test_locked_allocation_for_unknown_segment_is_ignored(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D1, 8, "Flexible")],
            [LockedAllocationInput("OLD", "R1", D1, 4)],
            {("R1", D1): 8},
        )
        self.assertEqual(result.locked_allocation_count, 0)
        self.assertEqual(result.allocated_hours, 8)

    def test_multi_day_flexible_work_spreads_across_window(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D2, 8, "Flexible")],
            [],
            {("R1", D1): 8, ("R1", D2): 8},
        )
        by_day = {
            row.day: row.hours
            for row in result.allocations
            if row.counts_as_allocated
        }
        self.assertEqual(by_day, {D1: 4, D2: 4})

    def test_outside_schedule_prefers_zero_standard_capacity_day(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D3, 12, "Flexible", overtime_allowed=True)],
            [],
            {("R1", D1): 8, ("R1", D2): 0, ("R1", D3): 0},
            outside_schedule_eligible_by_resource_day={
                ("R1", D1): True,
                ("R1", D2): False,
                ("R1", D3): True,
            },
        )
        overtime = [row for row in result.allocations if row.outside_schedule]
        self.assertEqual(len(overtime), 1)
        self.assertEqual(overtime[0].day, D3)
        self.assertEqual(overtime[0].hours, 4)

    def test_outside_schedule_daily_limit_matches_refined_engine(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D1, D2, 20, "Flexible", overtime_allowed=True)],
            [],
            {("R1", D1): 0, ("R1", D2): 0},
        )
        overtime = [row.hours for row in result.allocations if row.outside_schedule]
        self.assertEqual(overtime, [8, 8])
        self.assertEqual(result.allocated_hours, 16)
        self.assertEqual(result.unallocated_hours, 4)

    def test_locked_allocation_without_generable_segment_still_consumes_capacity(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S-TARGET", "R1", D1, D1, 8, "Flexible")],
            [LockedAllocationInput("S-LOCKED-ONLY", "R1", D1, 8)],
            {("R1", D1): 8},
            preserve_locked_segment_ids={"S-LOCKED-ONLY", "S-TARGET"},
        )

        locked = [row for row in result.allocations if row.locked]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0].segment_id, "S-LOCKED-ONLY")
        self.assertEqual(locked[0].resource_id, "R1")
        self.assertEqual(locked[0].hours, 8)

        counted_target = [
            row
            for row in result.allocations
            if row.segment_id == "S-TARGET" and row.counts_as_allocated
        ]
        self.assertEqual(counted_target, [])
        self.assertEqual(result.locked_allocation_count, 1)
        self.assertEqual(result.allocated_hours, 0.0)
        self.assertEqual(result.unallocated_hours, 8.0)

    def test_invalid_window_remains_unallocated(self) -> None:
        result = build_allocation_plan(
            [SegmentInput("S1", "R1", D2, D1, 8, "Flexible")],
            [],
            {("R1", D1): 8, ("R1", D2): 8},
        )
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.unallocated_hours, 8)


if __name__ == "__main__":
    unittest.main()
