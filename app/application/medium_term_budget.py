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

DEMAND_WINDOW_DIAGNOSTIC_BEFORE_WORK_PACKAGE = "DEMAND_BEFORE_WORK_PACKAGE"
DEMAND_WINDOW_DIAGNOSTIC_AFTER_WORK_PACKAGE = "DEMAND_AFTER_WORK_PACKAGE"
DEMAND_WINDOW_DIAGNOSTIC_WORK_PACKAGE_UNAVAILABLE = "WORK_PACKAGE_WINDOW_UNAVAILABLE"
DEMAND_WINDOW_DIAGNOSTIC_DEMAND_UNAVAILABLE = "DEMAND_WINDOW_UNAVAILABLE"
DEMAND_OUTSIDE_NONE = "NONE"
DEMAND_OUTSIDE_BEFORE = "BEFORE"
DEMAND_OUTSIDE_AFTER = "AFTER"
DEMAND_OUTSIDE_BOTH = "BOTH"
DEMAND_OUTSIDE_UNAVAILABLE = "UNAVAILABLE"
DEMAND_HOURS_DIAGNOSTIC_UNAVAILABLE = "DEMAND_HOURS_UNAVAILABLE"
DEMAND_HOURS_DIAGNOSTIC_ALTERNATIVE_UNRESOLVED = "DEMAND_ALTERNATIVE_UNRESOLVED"

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


def demand_window_diagnostic(
    *,
    demand_start: date | None,
    demand_end: date | None,
    work_package_start: date | None,
    work_package_end: date | None,
) -> tuple[bool, str, tuple[str, ...]]:
    """Compare full requested dates to the WorkPackage before any horizon clipping."""
    if demand_start is None:
        return (
            True,
            DEMAND_OUTSIDE_UNAVAILABLE,
            (DEMAND_WINDOW_DIAGNOSTIC_DEMAND_UNAVAILABLE,),
        )
    effective_demand_end = demand_end or demand_start
    if work_package_start is None or work_package_end is None:
        return (
            True,
            DEMAND_OUTSIDE_UNAVAILABLE,
            (DEMAND_WINDOW_DIAGNOSTIC_WORK_PACKAGE_UNAVAILABLE,),
        )

    before = demand_start < work_package_start
    after = effective_demand_end > work_package_end
    diagnostics: list[str] = []
    if before:
        diagnostics.append(DEMAND_WINDOW_DIAGNOSTIC_BEFORE_WORK_PACKAGE)
    if after:
        diagnostics.append(DEMAND_WINDOW_DIAGNOSTIC_AFTER_WORK_PACKAGE)
    if before and after:
        position = DEMAND_OUTSIDE_BOTH
    elif before:
        position = DEMAND_OUTSIDE_BEFORE
    elif after:
        position = DEMAND_OUTSIDE_AFTER
    else:
        position = DEMAND_OUTSIDE_NONE
    return bool(diagnostics), position, tuple(diagnostics)


def requested_workforce_hours(
    *,
    demand_periods: tuple[MediumTermDemandPeriodReadModel, ...],
) -> tuple[Decimal | None, tuple[str, ...]]:
    """Aggregate current workforce demand hours without double-counting alternatives."""
    workforce_periods = tuple(
        row
        for row in demand_periods
        if str(row.line_kind or "").strip().upper() == "WORKFORCE"
    )
    if not workforce_periods:
        return Decimal("0.00"), ()

    periods_by_line: dict[tuple[str, str], list[MediumTermDemandPeriodReadModel]] = {}
    for row in workforce_periods:
        periods_by_line.setdefault((row.demand_number, row.line_id), []).append(row)

    total = Decimal("0.00")
    diagnostics: list[str] = []
    unavailable = False

    def add_diagnostic(code: str) -> None:
        if code not in diagnostics:
            diagnostics.append(code)

    for line_periods in periods_by_line.values():
        detailed_periods = [
            row
            for row in line_periods
            if str(row.period_kind or "").strip().upper() != "BASE"
        ]
        effective_periods = detailed_periods or line_periods
        alternative_groups: dict[str, list[MediumTermDemandPeriodReadModel]] = {}

        for row in effective_periods:
            period_kind = str(row.period_kind or "").strip().upper()
            if period_kind == "ALTERNATIVE":
                group_key = row.alternative_group or row.period_id
                alternative_groups.setdefault(group_key, []).append(row)
                continue
            if row.hours is None:
                unavailable = True
                add_diagnostic(DEMAND_HOURS_DIAGNOSTIC_UNAVAILABLE)
                continue
            total += row.hours

        for options in alternative_groups.values():
            selected = [row for row in options if row.selected]
            candidates = selected or options
            if not selected:
                add_diagnostic(DEMAND_HOURS_DIAGNOSTIC_ALTERNATIVE_UNRESOLVED)
            if any(row.hours is None for row in candidates):
                unavailable = True
                add_diagnostic(DEMAND_HOURS_DIAGNOSTIC_UNAVAILABLE)
                continue
            if candidates:
                total += max(
                    row.hours
                    for row in candidates
                    if row.hours is not None
                )

    if unavailable:
        return None, tuple(diagnostics)
    return total, tuple(diagnostics)


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
class MediumTermDemandPeriodReadModel:
    demand_number: str
    line_id: str
    period_id: str
    work_package_ref: str
    start_date: date | None
    end_date: date | None
    hours: Decimal | None
    status: str
    provenance: str
    line_kind: str
    period_kind: str
    alternative_group: str | None = None
    selected: bool = False
    confirmation: str | None = None
    outside_work_package: bool = False
    outside_position: str = DEMAND_OUTSIDE_NONE
    diagnostics: tuple[str, ...] = ()


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
    requested_hours: Decimal | None = Decimal("0.00")
    requested_hours_diagnostics: tuple[str, ...] = ()
    demand_periods: tuple[MediumTermDemandPeriodReadModel, ...] = ()


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
