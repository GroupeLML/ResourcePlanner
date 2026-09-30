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
MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE = "WEEKLY_LOAD_INCOMPLETE"
MEDIUM_TERM_DIAGNOSTIC_CAPACITY_ZERO = "WORKFORCE_CAPACITY_ZERO"
WEEK_DIAGNOSTIC_LOAD_INCOMPLETE = "WORK_PACKAGE_LOAD_INCOMPLETE"
WEEK_DIAGNOSTIC_CAPACITY_ZERO = "WORKFORCE_CAPACITY_ZERO"

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
CLOSED_WORK_PACKAGE_STATUSES = frozenset(
    {
        "fermé",
        "ferme",
        "terminé",
        "termine",
        "closed",
        "completed",
    }
)
INACTIVE_WORK_PACKAGE_STATUSES = CANCELLED_WORK_PACKAGE_STATUSES | CLOSED_WORK_PACKAGE_STATUSES


def normalize_work_package_status(value: object) -> str:
    return str(value or "").strip().casefold()


def work_package_is_budget_included(status: object) -> bool:
    """Closed history remains budget-relevant; cancelled packages do not."""
    return normalize_work_package_status(status) not in CANCELLED_WORK_PACKAGE_STATUSES


def work_package_is_current_load_included(status: object) -> bool:
    """Current medium-term load excludes closed/completed and cancelled packages."""
    return normalize_work_package_status(status) not in INACTIVE_WORK_PACKAGE_STATUSES


def task_budget_diagnostic(
    *,
    budget_hours: Decimal | None,
    planned_wp_hours: Decimal | None,
    included_work_package_count: int,
) -> str:
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
class MediumTermWeeklyLoadReadModel:
    week_start: date
    hours: Decimal


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
    current_load_included: bool = True
    weekly_load_origin: str | None = None
    weekly_loads: tuple[MediumTermWeeklyLoadReadModel, ...] = ()
    weekly_load_diagnostic: str | None = None


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
class MediumTermWeekReadModel:
    week_start: date
    work_package_hours: Decimal | None
    capacity_hours: Decimal
    utilization: Decimal | None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MediumTermBudgetReadModel:
    project_id: str
    project_number: str
    project_name: str
    tasks: tuple[MediumTermBudgetTaskReadModel, ...]
    unclassified_work_packages: tuple[MediumTermBudgetWorkPackageReadModel, ...] = ()
    diagnostics: tuple[str, ...] = ()
    window_start: date | None = None
    window_end: date | None = None
    weeks: tuple[MediumTermWeekReadModel, ...] = ()
