"""ADR-034 canonical effective-need arithmetic.

Approved history remains nominal. Current operational need is nominal slot budget
minus non-absorbed completion reductions for that stable slot.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Iterable

CENT = Decimal("0.01")


def _hours(value: object) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def allocate_entry_budget(total_hours: object, slot_count: int) -> tuple[Decimal, ...]:
    """Allocate an approved entry budget deterministically without cross-slot rebalance."""
    count = int(slot_count)
    if count < 1:
        raise ValueError("slot_count must be at least 1")
    total = _hours(total_hours)
    if total < 0:
        raise ValueError("approved budget cannot be negative")
    base = (total / Decimal(count)).quantize(CENT, rounding=ROUND_DOWN)
    result = [base for _ in range(count)]
    remainder_cents = int(((total - base * count) / CENT).to_integral_value())
    for index in range(remainder_cents):
        result[index] += CENT
    return tuple(result)


def resolve_effective_slot_need(
    nominal_slot_hours: object,
    reductions: Iterable[object] = (),
) -> Decimal:
    """Resolve H_slot = max(B_slot - D_slot, 0) with exact Decimal arithmetic."""
    nominal = _hours(nominal_slot_hours)
    if nominal < 0:
        raise ValueError("nominal slot budget cannot be negative")
    reduction_total = sum((_hours(value) for value in reductions), Decimal("0.00"))
    if reduction_total < 0:
        raise ValueError("completion reductions cannot be negative")
    return max((nominal - reduction_total).quantize(CENT), Decimal("0.00"))


def resolve_effective_entry_need(
    nominal_entry_hours: object,
    *,
    slot_count: int,
    reductions_by_slot: dict[int, Iterable[object]] | None = None,
) -> tuple[Decimal, ...]:
    """Allocate nominal entry budget first, then subtract reductions per stable slot.

    This intentionally yields 32/40 for nominal 80 split across two slots with an
    8-hour reduction on slot 0; it never redistributes to 36/36.
    """
    nominal_slots = allocate_entry_budget(nominal_entry_hours, slot_count)
    reductions = reductions_by_slot or {}
    return tuple(
        resolve_effective_slot_need(
            nominal,
            reductions.get(index, ()),
        )
        for index, nominal in enumerate(nominal_slots)
    )
