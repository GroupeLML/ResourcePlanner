from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from .allocation_rules import apportion_canonical_hours


LOAD_PROFILE_UNIFORM = "UNIFORM"
LOAD_PROFILE_FRONT_LOADED = "FRONT_LOADED"
LOAD_PROFILE_BACK_LOADED = "BACK_LOADED"
LOAD_PROFILE_BELL = "BELL"
LOAD_PROFILES = (
    LOAD_PROFILE_UNIFORM,
    LOAD_PROFILE_FRONT_LOADED,
    LOAD_PROFILE_BACK_LOADED,
    LOAD_PROFILE_BELL,
)

_ALIASES = {
    "": LOAD_PROFILE_UNIFORM,
    "UNIFORM": LOAD_PROFILE_UNIFORM,
    "UNIFORME": LOAD_PROFILE_UNIFORM,
    "FRONT_LOADED": LOAD_PROFILE_FRONT_LOADED,
    "FRONT-LOADED": LOAD_PROFILE_FRONT_LOADED,
    "DEBUT": LOAD_PROFILE_FRONT_LOADED,
    "DÉBUT": LOAD_PROFILE_FRONT_LOADED,
    "PROFIL EN DEBUT": LOAD_PROFILE_FRONT_LOADED,
    "PROFIL EN DÉBUT": LOAD_PROFILE_FRONT_LOADED,
    "BACK_LOADED": LOAD_PROFILE_BACK_LOADED,
    "BACK-LOADED": LOAD_PROFILE_BACK_LOADED,
    "FIN": LOAD_PROFILE_BACK_LOADED,
    "PROFIL EN FIN": LOAD_PROFILE_BACK_LOADED,
    "BELL": LOAD_PROFILE_BELL,
    "CLOCHE": LOAD_PROFILE_BELL,
    "EN CLOCHE": LOAD_PROFILE_BELL,
}


def normalize_load_profile(value: object) -> str:
    """Return one canonical load-profile identifier.

    Storage and transport should use the canonical English identifiers. A small set of
    French aliases is accepted at compatibility boundaries so historical/UI values can
    be normalized once before entering the pure planning engine.
    """

    text = str(value or "").strip().upper()
    normalized = _ALIASES.get(text)
    if normalized is None:
        raise ValueError(
            "Le profil de charge doit être UNIFORM, FRONT_LOADED, BACK_LOADED ou BELL."
        )
    return normalized


def load_profile_weights(profile: object, count: int) -> tuple[float, ...]:
    """Return deterministic relative weights for chronological active days."""

    size = int(count)
    if size <= 0:
        return ()
    normalized = normalize_load_profile(profile)
    if normalized == LOAD_PROFILE_UNIFORM:
        return tuple(1.0 for _ in range(size))
    if normalized == LOAD_PROFILE_FRONT_LOADED:
        return tuple(float(size - index) for index in range(size))
    if normalized == LOAD_PROFILE_BACK_LOADED:
        return tuple(float(index + 1) for index in range(size))

    # Symmetric triangular bell. Even-sized windows intentionally have two equal peaks.
    return tuple(
        float(min(index + 1, size - index))
        for index in range(size)
    )


def _round_preserving_total(
    target: float,
    raw: Sequence[float],
    capacities: Sequence[float],
    weights: Sequence[float],
) -> tuple[float, ...]:
    """Canonicalize weighted planning output to exact persisted cents.

    Largest fractional remainders win first. Exact ties retain the existing profile
    priority: higher profile weight, then earlier chronological index.
    """
    tie_order = tuple(
        sorted(range(len(raw)), key=lambda index: (-weights[index], index))
    )
    return apportion_canonical_hours(
        target,
        raw,
        capacities,
        tie_order=tie_order,
    )


def spread_profile_hours(
    requested_hours: float,
    capacity_by_day: Sequence[tuple[date, float]],
    profile: object,
) -> dict[date, float]:
    """Distribute hours by profile without exceeding any day's residual capacity.

    The algorithm is weighted water-filling: days keep their relative profile weights
    until one reaches capacity; that day is then capped and the remaining hours are
    redistributed among the others. This preserves total requested work whenever the
    supplied capacity can hold it.
    """

    requested = max(float(requested_hours), 0.0)
    if requested <= 0 or not capacity_by_day:
        return {}

    ordered = sorted(
        ((day, max(float(capacity), 0.0)) for day, capacity in capacity_by_day),
        key=lambda item: item[0],
    )
    capacities = [capacity for _day, capacity in ordered]
    total_capacity = sum(capacities)
    target = min(requested, total_capacity)
    if target <= 0:
        return {}

    weights = list(load_profile_weights(profile, len(ordered)))
    allocated = [0.0 for _ in ordered]
    active = {index for index, capacity in enumerate(capacities) if capacity > 0}
    remaining = target

    while remaining > 0.0000001 and active:
        total_weight = sum(weights[index] for index in active)
        if total_weight <= 0:
            break
        shares = {
            index: remaining * weights[index] / total_weight
            for index in active
        }
        saturated = [
            index
            for index in active
            if shares[index] >= capacities[index] - allocated[index] - 0.0000001
        ]
        if not saturated:
            for index in active:
                allocated[index] += shares[index]
            remaining = 0.0
            break
        for index in saturated:
            room = max(capacities[index] - allocated[index], 0.0)
            allocated[index] += room
            remaining -= room
            active.remove(index)

    rounded = _round_preserving_total(target, allocated, capacities, weights)
    return {
        day: hours
        for (day, _capacity), hours in zip(ordered, rounded)
        if hours >= 0.01
    }
