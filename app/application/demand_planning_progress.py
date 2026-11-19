from __future__ import annotations

from collections.abc import Iterable
from typing import Literal


PlanningComponentCoverage = Literal["uncovered", "partial", "full"]
PlanningProgress = Literal["À planifier", "En planification", "Planifié"]


def project_demand_planning_progress(
    components: Iterable[PlanningComponentCoverage],
) -> PlanningProgress | None:
    """Aggregate independent planifiable components into one demand progression."""

    states = tuple(components)
    if not states:
        return None
    if all(state == "full" for state in states):
        return "Planifié"
    if all(state == "uncovered" for state in states):
        return "À planifier"
    return "En planification"
