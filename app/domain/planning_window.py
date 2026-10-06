from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class PlanningWindow:
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("La fin de fenêtre Planning doit être postérieure ou égale au début.")


def resolve_effective_planning_window(
    *,
    approved_start: date,
    approved_end: date,
    override_start: date | None = None,
    override_end: date | None = None,
) -> PlanningWindow:
    """Resolve one effective window without mutating the approved authorization."""

    approved = PlanningWindow(approved_start, approved_end)
    if override_start is None and override_end is None:
        return approved
    if override_start is None or override_end is None:
        raise ValueError("Une dérogation de fenêtre doit fournir ses deux bornes.")

    override = PlanningWindow(override_start, override_end)
    if override.start_date > approved.start_date or override.end_date < approved.end_date:
        raise ValueError("Une dérogation de fenêtre Planning ne peut que l'élargir.")
    if override == approved:
        raise ValueError("Une dérogation de fenêtre Planning doit élargir au moins une borne.")
    return override


def planning_window_covers(
    *,
    outer_start: date,
    outer_end: date,
    inner_start: date,
    inner_end: date,
) -> bool:
    outer = PlanningWindow(outer_start, outer_end)
    inner = PlanningWindow(inner_start, inner_end)
    return outer.start_date <= inner.start_date and outer.end_date >= inner.end_date
