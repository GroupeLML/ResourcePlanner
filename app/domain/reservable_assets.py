"""Typed, day-granular occupancy rules for physical assets.

Usage hours are deliberately absent: one occupied day is one exclusive day,
regardless of the number of hours an asset is used on that day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Iterable


class LineKind(StrEnum):
    WORKFORCE = "WORKFORCE"
    ASSET = "ASSET"


class OccupancyPolicy(StrEnum):
    EXCLUSIVE_DAY = "EXCLUSIVE_DAY"


class ResourcePeriodMode(StrEnum):
    BOUNDED = "BOUNDED"
    CONTINUOUS = "CONTINUOUS"


class ContinuousAssignmentState(StrEnum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


def continuous_assignment_state(
    *,
    start_date: date,
    end_date: date | None,
    as_of: date,
) -> ContinuousAssignmentState:
    if start_date > as_of:
        return ContinuousAssignmentState.SCHEDULED
    if end_date is None or as_of <= end_date:
        return ContinuousAssignmentState.ACTIVE
    return ContinuousAssignmentState.ENDED


class AssetRequirementOrigin(StrEnum):
    REQUEST = "REQUEST"
    SHIFT_AD_HOC = "SHIFT_AD_HOC"
    PROJECT_DIRECT = "PROJECT_DIRECT"
    SEGMENT = "SEGMENT"
    RESOURCE_PERIOD = "RESOURCE_PERIOD"


@dataclass(frozen=True, slots=True)
class CapacityOwner:
    kind: LineKind
    identifier: str

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("A capacity owner needs a stable identifier")


@dataclass(frozen=True, slots=True)
class AssetOccupation:
    allocation_id: str
    asset_id: str
    start_date: date
    end_date: date | None
    locked: bool = False

    def __post_init__(self) -> None:
        if (
            not self.allocation_id
            or not self.asset_id
            or (self.end_date is not None and self.end_date < self.start_date)
        ):
            raise ValueError("Invalid asset occupation")

    def dates(self) -> frozenset[date]:
        if self.end_date is None:
            raise ValueError("Open asset occupations cannot be expanded without a bounded window")
        return frozenset(
            self.start_date + timedelta(days=offset)
            for offset in range((self.end_date - self.start_date).days + 1)
        )


def overlapping_asset_occupations(
    proposed: AssetOccupation,
    existing: Iterable[AssetOccupation],
) -> tuple[str, ...]:
    """Return conflicting allocation IDs, excluding an update of the same row."""
    return tuple(
        occupation.allocation_id
        for occupation in existing
        if occupation.asset_id == proposed.asset_id
        and occupation.allocation_id != proposed.allocation_id
        and (proposed.end_date is None or occupation.start_date <= proposed.end_date)
        and (occupation.end_date is None or proposed.start_date <= occupation.end_date)
    )
