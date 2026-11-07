from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


CapacityWindow = Iterable[tuple[date, float]]
HoursByDay = Mapping[date, float]
CENT = Decimal("0.01")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def canonical_hours(value: object) -> float:
    """Return one persisted Planning quantity at canonical cent precision."""
    return float(max(_decimal(value), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP))


def _capacity_cents(value: object) -> int:
    """Canonical capacity never rounds upward beyond the supplied physical room."""
    amount = max(_decimal(value), Decimal("0"))
    return int((amount / CENT).to_integral_value(rounding=ROUND_FLOOR))


def apportion_canonical_hours(
    target_hours: object,
    raw_allocations: Sequence[float],
    capacities: Sequence[float],
    *,
    tie_order: Sequence[int] | None = None,
) -> tuple[float, ...]:
    """Convert an allocation vector to exact cent rows without losing the total.

    Raw planning math may use fractional values, but physical Shift quantities do not.
    Each row is floored to whole cents first. Remaining cents are assigned by largest
    fractional remainder; exact ties follow the supplied stable tie order.
    """
    if len(raw_allocations) != len(capacities):
        raise ValueError("raw_allocations and capacities must have the same length")
    size = len(raw_allocations)
    if size == 0:
        return ()

    capacity_cents = [_capacity_cents(value) for value in capacities]
    requested = max(_decimal(target_hours), Decimal("0")).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )
    wanted_cents = min(int(requested / CENT), sum(capacity_cents))
    if wanted_cents <= 0:
        return tuple(0.0 for _ in range(size))

    raw_decimals = [
        min(
            max(_decimal(value), Decimal("0")),
            max(_decimal(capacities[index]), Decimal("0")),
        )
        for index, value in enumerate(raw_allocations)
    ]
    allocated_cents = [
        min(
            int((value / CENT).to_integral_value(rounding=ROUND_FLOOR)),
            capacity_cents[index],
        )
        for index, value in enumerate(raw_decimals)
    ]

    excess = sum(allocated_cents) - wanted_cents
    if excess > 0:
        removal_order = sorted(
            range(size),
            key=lambda index: (
                (raw_decimals[index] / CENT)
                - (raw_decimals[index] / CENT).to_integral_value(rounding=ROUND_FLOOR),
                index,
            ),
        )
        for index in removal_order:
            if excess <= 0:
                break
            removable = min(allocated_cents[index], excess)
            allocated_cents[index] -= removable
            excess -= removable

    remaining = wanted_cents - sum(allocated_cents)
    if remaining <= 0:
        return tuple(float(Decimal(value) * CENT) for value in allocated_cents)

    preferred = list(tie_order or range(size))
    rank = {index: position for position, index in enumerate(preferred)}
    fractional = []
    for index, value in enumerate(raw_decimals):
        raw_cents = value / CENT
        floor_cents = raw_cents.to_integral_value(rounding=ROUND_FLOOR)
        fractional.append(raw_cents - floor_cents)

    order = sorted(
        range(size),
        key=lambda index: (
            -fractional[index],
            rank.get(index, size + index),
            index,
        ),
    )

    while remaining > 0:
        progressed = False
        for index in order:
            if remaining <= 0:
                break
            if allocated_cents[index] >= capacity_cents[index]:
                continue
            allocated_cents[index] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break

    return tuple(float(Decimal(value) * CENT) for value in allocated_cents)


def fill_canonical_hours(
    hours: object,
    capacities: Sequence[tuple[date, float]],
) -> dict[date, float]:
    """Fill ordered capacity slots sequentially using only canonical cent rows."""
    if not capacities:
        return {}
    capacity_cents = [_capacity_cents(capacity) for _day, capacity in capacities]
    requested = max(_decimal(hours), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)
    remaining = min(int(requested / CENT), sum(capacity_cents))
    result: dict[date, float] = {}
    for (day, _capacity), room_cents in zip(capacities, capacity_cents):
        if remaining <= 0:
            break
        amount_cents = min(room_cents, remaining)
        if amount_cents <= 0:
            continue
        result[day] = float(Decimal(amount_cents) * CENT)
        remaining -= amount_cents
    return result


def remaining_segment_hours(planned_hours: float, locked_hours: float = 0.0) -> float:
    """Return canonical automatic hours still to place after locked/manual work."""
    return canonical_hours(
        max(_decimal(planned_hours) - _decimal(locked_hours), Decimal("0"))
    )


def residual_capacity(
    raw_capacity: float,
    *,
    locked_hours: float = 0.0,
    fixed_hours: float = 0.0,
    flexible_hours: float = 0.0,
) -> float:
    """Return standard capacity still available to automatic planning."""
    return max(
        float(raw_capacity)
        - float(locked_hours)
        - float(fixed_hours)
        - float(flexible_hours),
        0.0,
    )


def spread_hours(hours: float, capacities: CapacityWindow) -> dict[date, float]:
    """Spread hours proportionally at canonical persisted cent precision.

    The target is capped by canonical available capacity. Proportional raw shares are
    converted to cents with largest-remainder apportionment. On equal remainders the
    later stable input day wins first, matching historical uniform remainder behavior.
    """
    normalized = [(day, max(float(capacity), 0.0)) for day, capacity in capacities]
    if hours <= 0 or not normalized:
        return {}

    total_capacity = sum(capacity for _, capacity in normalized)
    if total_capacity <= 0:
        return {}

    target = min(canonical_hours(hours), canonical_hours(total_capacity))
    if target <= 0:
        return {}

    raw = [
        target * capacity / total_capacity
        for _day, capacity in normalized
    ]
    apportioned = apportion_canonical_hours(
        target,
        raw,
        [capacity for _day, capacity in normalized],
        tie_order=tuple(reversed(range(len(normalized)))),
    )
    return {
        day: amount
        for (day, _capacity), amount in zip(normalized, apportioned)
        if amount >= 0.01
    }


def fixed_segment_spread(
    planned_hours: float,
    capacities: CapacityWindow,
    *,
    locked_hours: float = 0.0,
    locked_by_day: HoursByDay | None = None,
) -> dict[date, float]:
    """Plan automatic hours for a historical V1.5 fixed segment."""
    remaining = remaining_segment_hours(planned_hours, locked_hours)
    if remaining <= 0:
        return {}

    locked_by_day = locked_by_day or {}
    raw_days = [
        (day, max(float(raw), 0.0))
        for day, raw in capacities
        if float(raw) > 0
    ]
    residual_days = [
        (
            day,
            residual_capacity(
                raw,
                locked_hours=float(locked_by_day.get(day, 0.0)),
            ),
        )
        for day, raw in raw_days
    ]
    residual_days = [(day, room) for day, room in residual_days if room >= 0.01]

    result = spread_hours(remaining, residual_days)
    missing = canonical_hours(max(remaining - sum(result.values()), 0.0))
    if missing >= 0.01 and raw_days:
        overflow = spread_hours(missing, raw_days)
        for day, amount in overflow.items():
            result[day] = canonical_hours(result.get(day, 0.0) + amount)
    return result


def flexible_segment_spread(
    planned_hours: float,
    capacities: CapacityWindow,
    *,
    locked_hours: float = 0.0,
    locked_by_day: HoursByDay | None = None,
    fixed_by_day: HoursByDay | None = None,
    flexible_by_day: HoursByDay | None = None,
) -> dict[date, float]:
    """Plan automatic hours using canonical residual standard capacity only."""
    remaining = remaining_segment_hours(planned_hours, locked_hours)
    if remaining <= 0:
        return {}

    locked_by_day = locked_by_day or {}
    fixed_by_day = fixed_by_day or {}
    flexible_by_day = flexible_by_day or {}
    residual_days: list[tuple[date, float]] = []
    for day, raw in capacities:
        room = residual_capacity(
            raw,
            locked_hours=float(locked_by_day.get(day, 0.0)),
            fixed_hours=float(fixed_by_day.get(day, 0.0)),
            flexible_hours=float(flexible_by_day.get(day, 0.0)),
        )
        if room >= 0.01:
            residual_days.append((day, room))

    return spread_hours(remaining, residual_days)


def total_allocated(spread: dict[date, float]) -> float:
    """Stable helper for allocation summaries and tests."""
    return canonical_hours(sum(float(value) for value in spread.values()))
