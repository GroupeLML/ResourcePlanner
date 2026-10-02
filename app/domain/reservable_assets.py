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


class AssetRequirementOrigin(StrEnum):
    REQUEST = "REQUEST"
    SHIFT_AD_HOC = "SHIFT_AD_HOC"


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
    end_date: date
    locked: bool = False

    def __post_init__(self) -> None:
        if not self.allocation_id or not self.asset_id or self.end_date < self.start_date:
            raise ValueError("Invalid asset occupation")

    def dates(self) -> frozenset[date]:
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
        and occupation.start_date <= proposed.end_date
        and proposed.start_date <= occupation.end_date
    )
