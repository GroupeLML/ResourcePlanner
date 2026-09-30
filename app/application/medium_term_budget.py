from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


BUDGET_DIAGNOSTIC_UNAVAILABLE = "BUDGET_UNAVAILABLE"
BUDGET_DIAGNOSTIC_WORK_PACKAGE_LOAD_UNAVAILABLE = "WORK_PACKAGE_LOAD_UNAVAILABLE"
BUDGET_DIAGNOSTIC_NO_WORK_PACKAGES = "NO_WORK_PACKAGES"
BUDGET_DIAGNOSTIC_PARTIALLY_COVERED = "PARTIALLY_COVERED"
BUDGET_DIAGNOSTIC_FULLY_COVERED = "FULLY_COVERED"
BUDGET_DIAGNOSTIC_OVERALLOCATED = "OVERALLOCATED"

MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES = "UNCLASSIFIED_WORK_PACKAGES"

CANCELLED_WORK_PACKAGE_STATUSES = frozenset(
    {
        "annulé",
        "annule",
        "annulée",
        "annulee",
        "cancelled",
        "canceled",
    }
)


def normalize_work_package_status(value: object) -> str:
    return str(value or "").strip().casefold()


def work_package_is_budget_included(status: object) -> bool:
    """Return whether a WorkPackage contributes to the ERP budget comparison.

    ADR-013 keeps closed/completed packages in the budget comparison. Only
    cancellation aliases are excluded here; weekly/current-load rules belong
    to 502C and deliberately do not leak into this projection.
    """

    return normalize_work_package_status(status) not in CANCELLED_WORK_PACKAGE_STATUSES


def task_budget_diagnostic(
    *,
    budget_hours: Decimal | None,
    planned_wp_hours: Decimal | None,
    included_work_package_count: int,
) -> str:
    """Resolve the stable backend diagnostic state for one ERP task."""

    if budget_hours is None:
        return BUDGET_DIAGNOSTIC_UNAVAILABLE
    if planned_wp_hours is None:
        return BUDGET_DIAGNOSTIC_WORK_PACKAGE_LOAD_UNAVAILABLE
    if included_work_package_count == 0 and budget_hours > 0:
        return BUDGET_DIAGNOSTIC_NO_WORK_PACKAGES
    if planned_wp_hours > budget_hours:
        return BUDGET_DIAGNOSTIC_OVERALLOCATED
    if planned_wp_hours == budget_hours:
        return BUDGET_DIAGNOSTIC_FULLY_COVERED
    return BUDGET_DIAGNOSTIC_PARTIALLY_COVERED


@dataclass(frozen=True, slots=True)
class MediumTermBudgetWorkPackageReadModel:
    id: str
    reference: str
    code: str | None
    name: str
    planned_hours: Decimal | None
    status: str
    budget_included: bool
    start_date: date | None = None
    end_date: date | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class MediumTermBudgetTaskReadModel:
    task_catalog_item_id: str
    task_code: str
    task_label: str
    erp_task_id: str | None
    account_group: str
    budget_hours: Decimal | None
    planned_wp_hours: Decimal | None
    remaining_budget_hours: Decimal | None
    associated_work_package_count: int
    budget_included_work_package_count: int
    diagnostic_state: str
    budget_source_diagnostic: str | None
    work_packages: tuple[MediumTermBudgetWorkPackageReadModel, ...]
    active: bool = True
    workforce_eligible: bool | None = None


@dataclass(frozen=True, slots=True)
class MediumTermBudgetReadModel:
    project_id: str
    project_number: str
    project_name: str
    tasks: tuple[MediumTermBudgetTaskReadModel, ...]
    unclassified_work_packages: tuple[MediumTermBudgetWorkPackageReadModel, ...] = ()
    diagnostics: tuple[str, ...] = ()
