from __future__ import annotations

from decimal import Decimal
import unittest

from app.domain.effective_need import (
    allocate_entry_budget,
    resolve_effective_entry_need,
    resolve_effective_slot_need,
)


class EffectiveNeedTests(unittest.TestCase):
    def test_entry_budget_is_allocated_before_slot_reduction(self) -> None:
        self.assertEqual(
            resolve_effective_entry_need(
                Decimal("80"),
                slot_count=2,
                reductions_by_slot={0: (Decimal("8"),)},
            ),
            (Decimal("32.00"), Decimal("40.00")),
        )

    def test_reduction_never_redistributes_to_sibling_slot(self) -> None:
        nominal = allocate_entry_budget(Decimal("80"), 2)
        self.assertEqual(nominal, (Decimal("40.00"), Decimal("40.00")))
        self.assertNotEqual(
            resolve_effective_entry_need(
                Decimal("80"),
                slot_count=2,
                reductions_by_slot={0: (Decimal("8"),)},
            ),
            (Decimal("36.00"), Decimal("36.00")),
        )

    def test_effective_need_is_clamped_at_zero(self) -> None:
        self.assertEqual(
            resolve_effective_slot_need(Decimal("8"), (Decimal("8"),)),
            Decimal("0.00"),
        )
        self.assertEqual(
            resolve_effective_slot_need(Decimal("8"), (Decimal("10"),)),
            Decimal("0.00"),
        )

    def test_cent_allocation_is_deterministic(self) -> None:
        self.assertEqual(
            allocate_entry_budget(Decimal("10"), 3),
            (Decimal("3.34"), Decimal("3.33"), Decimal("3.33")),
        )


if __name__ == "__main__":
    unittest.main()
