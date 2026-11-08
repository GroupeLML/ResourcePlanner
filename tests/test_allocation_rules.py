from __future__ import annotations

import unittest
from datetime import date

from app.domain.allocation_rules import (
    fixed_segment_spread,
    flexible_segment_spread,
    remaining_segment_hours,
    residual_capacity,
    spread_hours,
    total_allocated,
)


D1 = date(2026, 8, 17)
D2 = date(2026, 8, 18)
D3 = date(2026, 8, 19)


class AllocationRulesTests(unittest.TestCase):
    def test_remaining_hours_subtracts_locked_allocations(self) -> None:
        self.assertEqual(remaining_segment_hours(24, 8), 16)

    def test_remaining_hours_never_becomes_negative(self) -> None:
        self.assertEqual(remaining_segment_hours(8, 12), 0)

    def test_residual_capacity_consumes_locked_fixed_and_flexible_hours(self) -> None:
        self.assertEqual(
            residual_capacity(8, locked_hours=1, fixed_hours=3, flexible_hours=2),
            2,
        )

    def test_residual_capacity_never_becomes_negative(self) -> None:
        self.assertEqual(residual_capacity(8, fixed_hours=10), 0)

    def test_residual_capacity_preserves_each_legitimate_cent_after_decimal_subtraction(self) -> None:
        self.assertEqual(residual_capacity(8.00, flexible_hours=7.90), 0.10)
        self.assertEqual(residual_capacity(8.00, flexible_hours=7.95), 0.05)
        self.assertEqual(residual_capacity(8.00, flexible_hours=7.99), 0.01)

    def test_residual_capacity_does_not_round_subcent_room_up(self) -> None:
        self.assertEqual(residual_capacity(8.004, flexible_hours=8.00), 0.0)
        self.assertEqual(residual_capacity(8.009, flexible_hours=8.00), 0.0)

    def test_spread_hours_is_proportional(self) -> None:
        result = spread_hours(8, [(D1, 8), (D2, 8)])
        self.assertEqual(result, {D1: 4.0, D2: 4.0})

    def test_spread_hours_respects_unequal_capacity(self) -> None:
        result = spread_hours(6, [(D1, 8), (D2, 4)])
        self.assertEqual(result, {D1: 4.0, D2: 2.0})

    def test_spread_hours_caps_at_total_capacity(self) -> None:
        result = spread_hours(20, [(D1, 8), (D2, 4)])
        self.assertEqual(total_allocated(result), 12)
        self.assertLessEqual(result[D1], 8)
        self.assertLessEqual(result[D2], 4)

    def test_spread_hours_ignores_negative_capacity(self) -> None:
        result = spread_hours(4, [(D1, -8), (D2, 8)])
        self.assertEqual(result, {D2: 4.0})

    def test_spread_hours_empty_for_no_capacity(self) -> None:
        self.assertEqual(spread_hours(8, []), {})
        self.assertEqual(spread_hours(8, [(D1, 0), (D2, -1)]), {})

    def test_spread_hours_empty_for_non_positive_request(self) -> None:
        self.assertEqual(spread_hours(0, [(D1, 8)]), {})
        self.assertEqual(spread_hours(-2, [(D1, 8)]), {})

    def test_spread_rounding_preserves_requested_total_when_capacity_allows(self) -> None:
        result = spread_hours(7, [(D1, 8), (D2, 8), (D3, 8)])
        self.assertAlmostEqual(total_allocated(result), 7.0, places=4)
        for amount in result.values():
            self.assertGreater(amount, 0)

    def test_fixed_segment_subtracts_locked_hours_from_segment_total(self) -> None:
        result = fixed_segment_spread(
            12,
            [(D1, 8)],
            locked_hours=4,
            locked_by_day={D1: 4},
        )
        self.assertEqual(total_allocated(result), 8)

    def test_fixed_segment_can_create_overload_when_capacity_is_short(self) -> None:
        result = fixed_segment_spread(12, [(D1, 8)])
        self.assertEqual(result, {D1: 12.0})

    def test_fixed_segment_returns_empty_when_locked_hours_cover_requirement(self) -> None:
        self.assertEqual(
            fixed_segment_spread(8, [(D1, 8)], locked_hours=8, locked_by_day={D1: 8}),
            {},
        )

    def test_flexible_segment_never_auto_overloads(self) -> None:
        result = flexible_segment_spread(12, [(D1, 8)])
        self.assertEqual(result, {D1: 8.0})
        self.assertEqual(total_allocated(result), 8)

    def test_flexible_segment_consumes_only_true_residual_capacity(self) -> None:
        result = flexible_segment_spread(
            8,
            [(D1, 8)],
            locked_by_day={D1: 1},
            fixed_by_day={D1: 3},
            flexible_by_day={D1: 2},
        )
        self.assertEqual(result, {D1: 2.0})

    def test_flexible_segment_respects_locked_total_and_daily_capacity(self) -> None:
        result = flexible_segment_spread(
            8,
            [(D1, 8)],
            locked_hours=4,
            locked_by_day={D1: 4},
        )
        self.assertEqual(result, {D1: 4.0})


if __name__ == "__main__":
    unittest.main()
