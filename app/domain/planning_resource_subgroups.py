from __future__ import annotations

from dataclasses import dataclass


INSTALL_ELEC_CLASS = "INSTALL_ELEC"

COMPANION = "COMPANION"
APPRENTICE = "APPRENTICE"
OTHER = "OTHER"

_COMPANION_CODES = frozenset({"ELCOMP"})
_APPRENTICE_CODES = frozenset({"ELAPP1", "ELAPP2", "ELAPP3", "ELAPP4"})

_LABELS = {
    COMPANION: "Compagnons électriciens",
    APPRENTICE: "Apprentis électriciens",
    OTHER: "Autres / Non classé",
}


@dataclass(frozen=True, slots=True)
class PlanningResourceSubgroup:
    code: str
    label: str


def classify_planning_resource_subgroup(
    *,
    resource_class: str | None,
    erp_employee_class: str | None,
) -> PlanningResourceSubgroup | None:
    """Classify only INSTALL_ELEC resources using the authoritative ERP EmployeeClass."""

    if (resource_class or "").strip().upper() != INSTALL_ELEC_CLASS:
        return None

    employee_class = (erp_employee_class or "").strip().upper()
    if employee_class in _COMPANION_CODES:
        code = COMPANION
    elif employee_class in _APPRENTICE_CODES:
        code = APPRENTICE
    else:
        code = OTHER

    return PlanningResourceSubgroup(code=code, label=_LABELS[code])
