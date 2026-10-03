from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


BUDGET_DIAGNOSTIC_UNAVAILABLE = "BUDGET_UNAVAILABLE"
BUDGET_DIAGNOSTIC_WORK_PACKAGE_LOAD_UNAVAILABLE = "WORK_PACKAGE_LOAD_UNAVAILABLE"
BUDGET_DIAGNOSTIC_NO_WORK_PACKAGES = "NO_WORK_PACKAGES"
BUDGET_DIAGNOSTIC_PARTIALLY_COVERED = "PARTIALLY_COVERED"
BUDGET_DIAGNOSTIC_FULLY_COVERED = "FULLY_COVERED"
BUDGET_DIAGNOSTIC_OVERALLOCATED = "OVERALLOCATED"

MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES = "UNCLASSIFIED_WORK_PACKAGES"
MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGE_LOAD = "UNCLASSIFIED_WORK_PACKAGE_LOAD"
MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE = "WEEKLY_LOAD_INCOMPLETE"
MEDIUM_TERM_DIAGNOSTIC_CAPACITY_ZERO = "WORKFORCE_CAPACITY_ZERO"
WEEK_DIAGNOSTIC_LOAD_INCOMPLETE = "WORK_PACKAGE_LOAD_INCOMPLETE"
WEEK_DIAGNOSTIC_CAPACITY_ZERO = "WORKFORCE_CAPACITY_ZERO"
REFERENCE_BASIS_ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK = (
    "ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK"
)
ERP_FINANCIAL_BUDGET_UNAVAILABLE = "ERP_FINANCIAL_BUDGET_UNAVAILABLE"
ERP_FINANCIAL_BUDGET_INCOMPLETE = "ERP_FINANCIAL_BUDGET_INCOMPLETE"
ACTUAL_HOURS_DIAGNOSTIC_COST_MISSING = "resource_class_cost_missing"
ACTUAL_HOURS_DIAGNOSTIC_COST_ZERO = "resource_class_cost_zero"
ACTUAL_HOURS_DIAGNOSTIC_COST_NEGATIVE = "resource_class_cost_negative"

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


def erp_financial_budget_diagnostic(
    *,
    budget_amount_cad: Decimal | None,
    budget_actual_cad: Decimal | None,
) -> str | None:
    if budget_amount_cad is None and budget_actual_cad is None:
        return ERP_FINANCIAL_BUDGET_UNAVAILABLE
    if budget_amount_cad is None or budget_actual_cad is None:
        return ERP_FINANCIAL_BUDGET_INCOMPLETE
    return None


def actual_hours_diagnostic(
    *,
    financial_diagnostic: str | None,
    average_hourly_cost_cad: Decimal | None,
) -> str | None:
    if financial_diagnostic is not None:
        return None
    if average_hourly_cost_cad is None:
        return ACTUAL_HOURS_DIAGNOSTIC_COST_MISSING
    if average_hourly_cost_cad == 0:
        return ACTUAL_HOURS_DIAGNOSTIC_COST_ZERO
    if average_hourly_cost_cad < 0:
        return ACTUAL_HOURS_DIAGNOSTIC_COST_NEGATIVE
    return None


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
    explicit_hours: Decimal = Decimal("0.00")
    automatic_hours: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class WorkPackageLoadIntervalReadModel:
    id: str
    start_date: date
    end_date: date
    hours: Decimal
    origin: str


@dataclass(frozen=True, slots=True)
class MediumTermBudgetWorkPackageReadModel:
    id: str
    reference: str
    code: str | None
    name: str
    planned_hours: Decimal | None
    status: str
    budget_included: bool
    status_diagnostic: str | None = None
    project_id: str = ""
    project_number: str = ""
    project_name: str = ""
    start_date: date | None = None
    end_date: date | None = None
    version: int = 1
    current_load_included: bool = True
    weekly_load_origin: str | None = None
    weekly_loads: tuple[MediumTermWeeklyLoadReadModel, ...] = ()
    weekly_load_diagnostic: str | None = None
    load_intervals: tuple[WorkPackageLoadIntervalReadModel, ...] = ()
    explicit_hours: Decimal | None = None
    automatic_hours: Decimal | None = None
    resource_class_code: str | None = None
    resource_class_label: str | None = None
    resource_class_active: bool | None = None
    task_resource_class_code: str | None = None
    resource_class_diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class MediumTermBudgetTaskReadModel:
    task_catalog_item_id: str
    task_code: str
    task_label: str
    erp_task_id: str | None
    account_group: str
    budget_amount_cad: Decimal | None
    budget_actual_cad: Decimal | None
    remaining_budget_cad: Decimal | None
    financial_diagnostic: str | None
    average_hourly_cost_cad: Decimal | None
    remaining_budget_hours_from_actual: Decimal | None
    actual_hours_diagnostic: str | None
    future_work_package_hours: Decimal | None
    future_work_package_diagnostic: str | None
    remaining_after_work_packages_hours: Decimal | None
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
    project_id: str = ""
    project_number: str = ""
    project_name: str = ""
    project_manager_contact_id: str | None = None
    project_manager_display_name: str | None = None
    manager_group_key: str = "erp:unassigned"
    manager_display_name: str | None = None
    manager_resolution_status: str = "UNRESOLVED"
    manager_diagnostics: tuple[str, ...] = ()
    erp_budget_last_success_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MediumTermTaskOptionReadModel:
    task_catalog_item_id: str
    project_id: str
    project_number: str
    project_name: str
    task_code: str
    task_label: str


@dataclass(frozen=True, slots=True)
class MediumTermResourceClassOptionReadModel:
    code: str
    label: str
    active: bool


@dataclass(frozen=True, slots=True)
class MediumTermClassWeekReadModel:
    resource_class_code: str | None
    resource_class_label: str
    capacity_hours: Decimal
    work_package_hours: Decimal | None
    utilization: Decimal | None
    state: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MediumTermWeekReadModel:
    week_start: date
    work_package_hours: Decimal | None
    capacity_hours: Decimal
    utilization: Decimal | None
    state: str = "unavailable"
    diagnostics: tuple[str, ...] = ()
    classes: tuple[MediumTermClassWeekReadModel, ...] = ()


@dataclass(frozen=True, slots=True)
class MediumTermBudgetReadModel:
    project_id: str | None
    project_number: str | None
    project_name: str | None
    tasks: tuple[MediumTermBudgetTaskReadModel, ...]
    reference_week_start: date | None = None
    actual_through_date: date | None = None
    reference_basis: str = REFERENCE_BASIS_ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK
    erp_budget_last_success_at: datetime | None = None
    unclassified_work_packages: tuple[MediumTermBudgetWorkPackageReadModel, ...] = ()
    diagnostics: tuple[str, ...] = ()
    weekly_diagnostics: tuple[str, ...] = ()
    window_start: date | None = None
    window_end: date | None = None
    weeks: tuple[MediumTermWeekReadModel, ...] = ()
    project_count: int = 0
    task_options: tuple[MediumTermTaskOptionReadModel, ...] = ()
    resource_classes: tuple[MediumTermResourceClassOptionReadModel, ...] = ()
