from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application import (
    DemandHistoryReadModel,
    ResourceAvailabilityRuleReadModel,
    WorkPackageReadModel,
)
from ...application.medium_term_budget import (
    INACTIVE_WORK_PACKAGE_STATUSES,
    MEDIUM_TERM_DIAGNOSTIC_CAPACITY_ZERO,
    MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGE_LOAD,
    MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES,
    MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE,
    ERP_REMAINING_REFERENCE_BASIS,
    ERP_REMAINING_DIAGNOSTIC_FRESHNESS_UNAVAILABLE,
    ERP_REMAINING_DIAGNOSTIC_LOAD_UNAVAILABLE,
    WEEK_DIAGNOSTIC_CAPACITY_ZERO,
    WEEK_DIAGNOSTIC_LOAD_INCOMPLETE,
    REFERENCE_BASIS_ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK,
    MediumTermBudgetReadModel,
    MediumTermBudgetTaskReadModel,
    MediumTermBudgetWorkPackageReadModel,
    MediumTermDemandPeriodReadModel,
    MediumTermClassWeekReadModel,
    MediumTermResourceClassOptionReadModel,
    MediumTermTaskOptionReadModel,
    MediumTermWeekReadModel,
    MediumTermWeeklyLoadReadModel,
    WorkPackageLoadIntervalReadModel,
    actual_hours_diagnostic,
    erp_financial_budget_diagnostic,
    erp_budget_cutoff_date,
    task_budget_diagnostic,
    work_package_hours_on_or_after,
    work_package_is_budget_included,
    work_package_is_current_load_included,
    demand_window_diagnostic,
    requested_workforce_hours,
)
from ...application.project_managers import ProjectManagerResolutionService
from ...application.query_models import (
    PlanningHistoryReadModel,
    work_package_resource_class_diagnostic,
)
from ...application.work_package_load import (
    WorkPackageLoadIntervalValue,
    WorkPackageLoadState,
    legacy_weekly_as_intervals,
    load_diagnostic,
    monday_of,
    projected_weekly_loads,
    work_package_status,
)
from ...application.work_package_weekly_load import (
    WeeklyLoadValue,
)
from .availability_class_scope import availability_class_codes_by_rule
from .models import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    WorkforceRequest,
    WorkforceRequestHistory,
    TaskCatalogEntry,
    TaskCatalogProjectSyncState,
    WorkPackage,
    WorkPackageLoadInterval,
    WorkPackageWeeklyLoad,
)
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodSelection,
)
from .resource_class_models import ResourceClassConfig
from .project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)
from .planning_audit import PlanningChangeHistory
from .capacity_query_repository import SqlPlannerQueryRepository
from .medium_term_capacity_query import (
    UNCLASSIFIED,
    build_workforce_weekly_capacity_by_class,
    medium_term_capacity_state,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def current_business_date() -> date:
    """Return the backend business date through a patchable test seam."""

    return date.today()


class SqlPlannerQueryRepositoryWeb(SqlPlannerQueryRepository):
    """SQL query surface extended with reads required by React V2."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._web_session = session

    def list_work_packages(
        self,
        *,
        project_number: str | None = None,
        active_only: bool = True,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[WorkPackageReadModel, ...]:
        statement = (
            select(WorkPackage, Project, TaskCatalogEntry, ResourceClassConfig)
            .join(Project, WorkPackage.project_id == Project.id)
            .outerjoin(
                TaskCatalogEntry,
                WorkPackage.task_catalog_item_id == TaskCatalogEntry.id,
            )
            .outerjoin(
                ResourceClassConfig,
                WorkPackage.resource_class_code == ResourceClassConfig.code,
            )
            .order_by(Project.number, WorkPackage.start_date, WorkPackage.name, WorkPackage.id)
        )
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return ()
            statement = statement.where(Project.id.in_(identifiers))

        wanted_project = _text(project_number)
        if wanted_project:
            statement = statement.where(Project.number == wanted_project)

        rows = self._web_session.execute(statement).all()
        result: list[WorkPackageReadModel] = []
        for work_package, project, task, resource_class in rows:
            status, status_diagnostic = work_package_status(
                start_date=work_package.start_date,
                terminal_status=work_package.terminal_status,
                legacy_status=work_package.status,
            )
            if active_only and status.casefold() in INACTIVE_WORK_PACKAGE_STATUSES:
                continue
            reference = _optional_text(work_package.legacy_effort_id) or work_package.id
            result.append(
                WorkPackageReadModel(
                    id=work_package.id,
                    reference=reference,
                    project_number=project.number,
                    code=_optional_text(work_package.code),
                    name=work_package.name,
                    description=_optional_text(work_package.description),
                    start_date=work_package.start_date,
                    end_date=work_package.end_date,
                    planned_hours=(
                        float(work_package.planned_hours)
                        if work_package.planned_hours is not None
                        else None
                    ),
                    status=status,
                    status_diagnostic=status_diagnostic,
                    task_catalog_item_id=work_package.task_catalog_item_id,
                    task_code=_optional_text(task.task_code) if task is not None else None,
                    task_label=_optional_text(task.label) if task is not None else None,
                    resource_class_code=_optional_text(work_package.resource_class_code),
                    resource_class_label=(
                        _optional_text(resource_class.label)
                        if resource_class is not None
                        else None
                    ),
                    resource_class_active=(
                        bool(resource_class.active)
                        if resource_class is not None
                        else None
                    ),
                    task_resource_class_code=(
                        _optional_text(task.resource_class_code)
                        if task is not None
                        else None
                    ),
                    resource_class_diagnostic=work_package_resource_class_diagnostic(
                        _optional_text(work_package.resource_class_code),
                        (
                            _optional_text(task.resource_class_code)
                            if task is not None
                            else None
                        ),
                    ),
                    version=int(work_package.version or 1),
                )
            )
        return tuple(result)

    def _medium_term_demand_periods_by_package(
        self,
        *,
        project_ids: tuple[str, ...],
        work_packages: tuple[WorkPackage, ...],
    ) -> dict[str, tuple[MediumTermDemandPeriodReadModel, ...]]:
        """Project current requested windows in batches for the Medium-term Gantt."""
        if not project_ids or not work_packages:
            return {}

        work_package_by_reference = {
            _optional_text(row.legacy_effort_id) or row.id: row
            for row in work_packages
        }
        demands = tuple(self._demands.list(project_ids=project_ids))
        active_demands = tuple(
            demand
            for demand in demands
            if not any(
                token in _text(demand.effective_status or demand.status).casefold()
                for token in ("annul", "cancel")
            )
        )
        if not active_demands:
            return {}

        request_rows = tuple(
            self._web_session.scalars(
                select(WorkforceRequest).where(
                    WorkforceRequest.project_id.in_(project_ids)
                )
            ).all()
        )
        request_by_number = {
            _optional_text(row.legacy_demand_number) or row.id: row
            for row in request_rows
        }
        request_ids = tuple(
            request_by_number[demand.number].id
            for demand in active_demands
            if demand.number in request_by_number
        )
        if not request_ids:
            return {}

        period_rows = tuple(
            self._web_session.scalars(
                select(WorkforceRequestPeriod)
                .where(
                    WorkforceRequestPeriod.workforce_request_id.in_(request_ids),
                    WorkforceRequestPeriod.active == true(),
                )
                .order_by(
                    WorkforceRequestPeriod.workforce_request_id,
                    WorkforceRequestPeriod.request_line_id,
                    WorkforceRequestPeriod.sequence,
                    WorkforceRequestPeriod.created_at,
                    WorkforceRequestPeriod.id,
                )
            ).all()
        )
        selection_rows = tuple(
            self._web_session.scalars(
                select(WorkforceRequestPeriodSelection).where(
                    WorkforceRequestPeriodSelection.workforce_request_id.in_(request_ids)
                )
            ).all()
        )
        selected_period_row_ids = {row.period_id for row in selection_rows}
        periods_by_request: defaultdict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        periods_by_line: defaultdict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        for row in period_rows:
            periods_by_request[row.workforce_request_id].append(row)
            if row.request_line_id:
                periods_by_line[row.request_line_id].append(row)

        result: defaultdict[str, list[MediumTermDemandPeriodReadModel]] = defaultdict(list)

        def append_projection(
            *,
            demand_number: str,
            status: str,
            approved_at: object,
            line_id: str,
            line_kind: str,
            work_package_ref: str | None,
            period_id: str,
            period_kind: str,
            start_date: date | None,
            end_date: date | None,
            hours: Decimal | None,
            alternative_group: str | None = None,
            selected: bool = False,
            confirmation: str | None = None,
        ) -> None:
            if work_package_ref is None:
                return
            package = work_package_by_reference.get(work_package_ref)
            if package is None:
                return
            outside, position, diagnostics = demand_window_diagnostic(
                demand_start=start_date,
                demand_end=end_date,
                work_package_start=package.start_date,
                work_package_end=package.end_date,
            )
            normalized_status = _text(status).casefold()
            provenance = (
                "CANDIDATE"
                if any(
                    token in normalized_status
                    for token in ("brouillon", "soumise", "correction", "corriger")
                )
                else ("APPROVED" if approved_at is not None else "CANDIDATE")
            )
            result[package.id].append(
                MediumTermDemandPeriodReadModel(
                    demand_number=demand_number,
                    line_id=line_id,
                    period_id=period_id,
                    work_package_ref=work_package_ref,
                    start_date=start_date,
                    end_date=end_date,
                    hours=hours,
                    status=status,
                    provenance=provenance,
                    line_kind=line_kind,
                    period_kind=period_kind,
                    alternative_group=alternative_group,
                    selected=selected,
                    confirmation=confirmation,
                    outside_work_package=outside,
                    outside_position=position,
                    diagnostics=diagnostics,
                )
            )

        for demand in active_demands:
            request = request_by_number.get(demand.number)
            if request is None:
                continue
            request_periods = periods_by_request.get(request.id, [])
            active_lines = tuple(line for line in demand.lines if line.active)
            if active_lines:
                for line in active_lines:
                    work_package_ref = line.work_package_ref
                    if work_package_ref is None and not demand.line_mode:
                        work_package_ref = demand.work_package_ref
                    line_periods = list(periods_by_line.get(line.line_id, ()))
                    if not line_periods and len(active_lines) == 1:
                        line_periods = [
                            row
                            for row in request_periods
                            if row.request_line_id in (None, line.line_id)
                        ]
                    if line_periods:
                        for period in line_periods:
                            append_projection(
                                demand_number=demand.number,
                                status=demand.status,
                                approved_at=demand.approved_at,
                                line_id=line.line_id,
                                line_kind=line.kind,
                                work_package_ref=work_package_ref,
                                period_id=period.period_key,
                                period_kind=period.kind,
                                start_date=period.start_date,
                                end_date=period.end_date,
                                hours=(Decimal(period.hours) if period.hours is not None else None),
                                alternative_group=_optional_text(period.alternative_group),
                                selected=period.id in selected_period_row_ids,
                                confirmation=_optional_text(period.confirmation),
                            )
                    else:
                        append_projection(
                            demand_number=demand.number,
                            status=demand.status,
                            approved_at=demand.approved_at,
                            line_id=line.line_id,
                            line_kind=line.kind,
                            work_package_ref=work_package_ref,
                            period_id=f"{line.line_id}:base",
                            period_kind="BASE",
                            start_date=line.desired_start,
                            end_date=line.desired_end or line.desired_start,
                            hours=(Decimal(str(line.estimated_hours)) if line.estimated_hours is not None else None),
                            confirmation=_optional_text(line.confirmation),
                        )
                continue

            work_package_ref = demand.work_package_ref
            if request_periods:
                for period in request_periods:
                    append_projection(
                        demand_number=demand.number,
                        status=demand.status,
                        approved_at=demand.approved_at,
                        line_id=period.request_line_id or request.id,
                        line_kind="WORKFORCE",
                        work_package_ref=work_package_ref,
                        period_id=period.period_key,
                        period_kind=period.kind,
                        start_date=period.start_date,
                        end_date=period.end_date,
                        hours=(Decimal(period.hours) if period.hours is not None else None),
                        alternative_group=_optional_text(period.alternative_group),
                        selected=period.id in selected_period_row_ids,
                        confirmation=_optional_text(period.confirmation),
                    )
            else:
                append_projection(
                    demand_number=demand.number,
                    status=demand.status,
                    approved_at=demand.approved_at,
                    line_id=request.id,
                    line_kind="WORKFORCE",
                    work_package_ref=work_package_ref,
                    period_id=f"{request.id}:base",
                    period_kind="BASE",
                    start_date=demand.desired_start,
                    end_date=demand.desired_end or demand.desired_start,
                    hours=(Decimal(str(demand.estimated_hours)) if demand.estimated_hours is not None else None),
                    confirmation=_optional_text(demand.confirmation),
                )

        return {
            package_id: tuple(
                sorted(
                    rows,
                    key=lambda row: (
                        row.demand_number,
                        row.line_id,
                        row.start_date or date.min,
                        row.end_date or date.min,
                        row.period_id,
                    ),
                )
            )
            for package_id, rows in result.items()
        }

    @staticmethod
    def _medium_term_budget_work_package(
        work_package: WorkPackage,
        legacy_loads: tuple[WeeklyLoadValue, ...],
        intervals: tuple[WorkPackageLoadIntervalValue, ...],
        *,
        project_id: str = "",
        project_number: str = "",
        project_name: str = "",
        resource_class: ResourceClassConfig | None = None,
        task_resource_class_code: str | None = None,
        demand_periods: tuple[MediumTermDemandPeriodReadModel, ...] = (),
    ) -> MediumTermBudgetWorkPackageReadModel:
        if not intervals and legacy_loads:
            intervals = legacy_weekly_as_intervals(
                work_package_start=work_package.start_date,
                work_package_end=work_package.end_date,
                weekly_loads=tuple(
                    (item.week_start, item.hours) for item in legacy_loads
                ),
                origin=_optional_text(work_package.weekly_load_origin),
            )
        status, status_diagnostic = work_package_status(
            start_date=work_package.start_date,
            terminal_status=work_package.terminal_status,
            legacy_status=work_package.status,
        )
        state = WorkPackageLoadState(
            reference=_optional_text(work_package.legacy_effort_id) or work_package.id,
            version=int(work_package.version or 1),
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            planned_hours=(
                Decimal(work_package.planned_hours)
                if work_package.planned_hours is not None
                else None
            ),
            legacy_status=_optional_text(work_package.status),
            terminal_status=_optional_text(work_package.terminal_status),
            intervals=intervals,
        )
        diagnostic = load_diagnostic(state)
        projected_loads = (
            projected_weekly_loads(state)
            if diagnostic is None
            else ()
        )
        explicit_hours = sum(
            (item.hours for item in intervals),
            Decimal("0.00"),
        )
        automatic_hours = (
            state.planned_hours - explicit_hours
            if state.planned_hours is not None
            and explicit_hours <= state.planned_hours
            else None
        )
        requested_hours, requested_hours_diagnostics = requested_workforce_hours(
            demand_periods=demand_periods
        )
        return MediumTermBudgetWorkPackageReadModel(
            id=work_package.id,
            reference=state.reference,
            code=_optional_text(work_package.code),
            name=work_package.name,
            planned_hours=state.planned_hours,
            status=status,
            status_diagnostic=status_diagnostic,
            budget_included=work_package_is_budget_included(status),
            project_id=project_id,
            project_number=project_number,
            project_name=project_name,
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            version=state.version,
            current_load_included=work_package_is_current_load_included(status),
            weekly_load_origin=_optional_text(work_package.weekly_load_origin),
            weekly_loads=tuple(
                MediumTermWeeklyLoadReadModel(
                    week_start=item.week_start,
                    hours=item.hours,
                    explicit_hours=item.explicit_hours,
                    automatic_hours=item.automatic_hours,
                )
                for item in projected_loads
            ),
            weekly_load_diagnostic=diagnostic,
            load_intervals=tuple(
                WorkPackageLoadIntervalReadModel(
                    id=item.id or "",
                    start_date=item.start_date,
                    end_date=item.end_date,
                    hours=item.hours,
                    origin=item.origin,
                )
                for item in intervals
            ),
            explicit_hours=explicit_hours,
            automatic_hours=automatic_hours,
            resource_class_code=_optional_text(work_package.resource_class_code),
            resource_class_label=(
                _optional_text(resource_class.label)
                if resource_class is not None
                else None
            ),
            resource_class_active=(
                bool(resource_class.active)
                if resource_class is not None
                else None
            ),
            task_resource_class_code=_optional_text(task_resource_class_code),
            resource_class_diagnostic=work_package_resource_class_diagnostic(
                _optional_text(work_package.resource_class_code),
                _optional_text(task_resource_class_code),
            ),
            requested_hours=requested_hours,
            requested_hours_diagnostics=requested_hours_diagnostics,
            demand_periods=demand_periods,
        )

    def medium_term_budget_projection(
        self,
        *,
        project_number: str | None = None,
        task_catalog_item_id: str | None = None,
        task_code: str | None = None,
        resource_class_code: str | None = None,
        include_inactive_projects: bool = False,
        project_ids: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> MediumTermBudgetReadModel | None:
        wanted_project = _optional_text(project_number)
        wanted_task = _optional_text(task_catalog_item_id)
        wanted_task_code = _optional_text(task_code)
        wanted_class = _optional_text(resource_class_code)

        visible_projects = tuple(
            self.list_projects(
                active_only=not include_inactive_projects,
                project_ids=project_ids,
            )
        )
        if wanted_project is not None:
            selected_projects = tuple(
                project
                for project in visible_projects
                if project.number == wanted_project
            )
            if not selected_projects:
                return None
        else:
            selected_projects = visible_projects

        project_by_id = {project.id: project for project in selected_projects}
        project_by_number = {project.number: project for project in selected_projects}
        selected_project_ids = tuple(project_by_id)
        selected_project_numbers = tuple(project_by_number)

        project_rows = (
            tuple(
                self._web_session.scalars(
                    select(Project).where(Project.id.in_(selected_project_ids))
                ).all()
            )
            if selected_project_ids
            else ()
        )
        project_records_by_id = {row.id: row for row in project_rows}
        project_manager_projections = ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(self._web_session)
        ).resolve_projects(list(selected_project_ids))
        budget_sync_states = (
            tuple(
                self._web_session.scalars(
                    select(TaskCatalogProjectSyncState).where(
                        TaskCatalogProjectSyncState.project_number.in_(
                            selected_project_numbers
                        )
                    )
                ).all()
            )
            if selected_project_numbers
            else ()
        )
        budget_sync_by_project_number = {
            row.project_number: row for row in budget_sync_states
        }

        configured_classes = tuple(
            self._web_session.scalars(
                select(ResourceClassConfig).order_by(
                    ResourceClassConfig.label,
                    ResourceClassConfig.code,
                )
            ).all()
        )
        resource_classes_by_code = {
            resource_class.code: resource_class
            for resource_class in configured_classes
        }
        resource_class_options = tuple(
            MediumTermResourceClassOptionReadModel(
                code=resource_class.code,
                label=resource_class.label,
                active=bool(resource_class.active),
            )
            for resource_class in configured_classes
        )

        task_rows = (
            tuple(
                self._web_session.scalars(
                    select(TaskCatalogEntry)
                    .where(TaskCatalogEntry.project_number.in_(selected_project_numbers))
                    .order_by(
                        TaskCatalogEntry.project_number,
                        TaskCatalogEntry.task_code,
                        TaskCatalogEntry.id,
                    )
                ).all()
            )
            if selected_project_numbers
            else ()
        )
        depmo_tasks = tuple(
            task
            for task in task_rows
            if _text(task.account_group).upper() == "DEPMO"
        )
        task_options = tuple(
            MediumTermTaskOptionReadModel(
                task_catalog_item_id=task.id,
                project_id=project_by_number[task.project_number].id,
                project_number=task.project_number,
                project_name=project_by_number[task.project_number].name,
                task_code=task.task_code,
                task_label=task.label,
            )
            for task in depmo_tasks
            if task.project_number in project_by_number
        )
        tasks = tuple(
            task
            for task in depmo_tasks
            if (wanted_task is None or task.id == wanted_task)
            and (wanted_task_code is None or task.task_code == wanted_task_code)
        )
        selected_task_ids = {task.id for task in tasks}
        tasks_by_id = {task.id: task for task in depmo_tasks}

        work_packages = (
            tuple(
                self._web_session.scalars(
                    select(WorkPackage)
                    .where(WorkPackage.project_id.in_(selected_project_ids))
                    .order_by(
                        WorkPackage.project_id,
                        WorkPackage.start_date,
                        WorkPackage.name,
                        WorkPackage.id,
                    )
                ).all()
            )
            if selected_project_ids
            else ()
        )
        work_package_ids = tuple(row.id for row in work_packages)
        load_rows = (
            tuple(
                self._web_session.scalars(
                    select(WorkPackageWeeklyLoad)
                    .where(WorkPackageWeeklyLoad.work_package_id.in_(work_package_ids))
                    .order_by(
                        WorkPackageWeeklyLoad.work_package_id,
                        WorkPackageWeeklyLoad.week_start,
                    )
                ).all()
            )
            if work_package_ids
            else ()
        )
        loads_by_package: defaultdict[str, list[WeeklyLoadValue]] = defaultdict(list)
        for row in load_rows:
            loads_by_package[row.work_package_id].append(
                WeeklyLoadValue(
                    week_start=row.week_start,
                    hours=Decimal(row.hours),
                )
            )

        interval_rows = (
            tuple(
                self._web_session.scalars(
                    select(WorkPackageLoadInterval)
                    .where(WorkPackageLoadInterval.work_package_id.in_(work_package_ids))
                    .order_by(
                        WorkPackageLoadInterval.work_package_id,
                        WorkPackageLoadInterval.start_date,
                        WorkPackageLoadInterval.end_date,
                        WorkPackageLoadInterval.id,
                    )
                ).all()
            )
            if work_package_ids
            else ()
        )
        intervals_by_package: defaultdict[str, list[WorkPackageLoadIntervalValue]] = defaultdict(list)
        for row in interval_rows:
            intervals_by_package[row.work_package_id].append(
                WorkPackageLoadIntervalValue(
                    id=row.id,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    hours=Decimal(row.hours),
                    origin=row.origin,
                )
            )

        demand_periods_by_package = self._medium_term_demand_periods_by_package(
            project_ids=selected_project_ids,
            work_packages=work_packages,
        )

        all_by_task: defaultdict[str, list[MediumTermBudgetWorkPackageReadModel]] = defaultdict(list)
        display_by_task: defaultdict[str, list[MediumTermBudgetWorkPackageReadModel]] = defaultdict(list)
        displayed_unclassified: list[MediumTermBudgetWorkPackageReadModel] = []
        displayed_by_id: dict[str, MediumTermBudgetWorkPackageReadModel] = {}

        for work_package in work_packages:
            project = project_by_id.get(work_package.project_id)
            if project is None:
                continue
            task = tasks_by_id.get(work_package.task_catalog_item_id)
            class_code = _optional_text(work_package.resource_class_code)
            projected = self._medium_term_budget_work_package(
                work_package,
                tuple(loads_by_package.get(work_package.id, ())),
                tuple(intervals_by_package.get(work_package.id, ())),
                project_id=project.id,
                project_number=project.number,
                project_name=project.name,
                resource_class=(
                    resource_classes_by_code.get(class_code)
                    if class_code is not None
                    else None
                ),
                task_resource_class_code=(
                    _optional_text(task.resource_class_code)
                    if task is not None
                    else None
                ),
                demand_periods=tuple(demand_periods_by_package.get(work_package.id, ())),
            )
            task_id = work_package.task_catalog_item_id
            if task_id in tasks_by_id:
                all_by_task[task_id].append(projected)

            if (
                (wanted_task is not None or wanted_task_code is not None)
                and task_id not in selected_task_ids
            ):
                continue
            if wanted_class is not None and class_code != wanted_class:
                continue

            if task_id is None:
                displayed_by_id[work_package.id] = projected
                displayed_unclassified.append(projected)
            elif task_id in tasks_by_id:
                displayed_by_id[work_package.id] = projected
                display_by_task[task_id].append(projected)

        reference_week_start = monday_of(current_business_date())
        actual_through_date = reference_week_start - timedelta(days=1)

        task_models: list[MediumTermBudgetTaskReadModel] = []
        for task in tasks:
            project = project_by_number.get(task.project_number)
            if project is None:
                continue
            project_record = project_records_by_id.get(project.id)
            manager_projection = project_manager_projections.get(project.id)
            project_manager = (
                manager_projection.primary
                if manager_projection is not None
                else None
            )
            project_manager_contact_id = (
                project_manager.business_contact_id
                if project_manager is not None
                else None
            )
            manager_group_key = (
                f"erp:{project_manager.employee_external_id}"
                if (
                    project_manager is not None
                    and project_manager.employee_external_id is not None
                )
                else "erp:unassigned"
            )
            associated = tuple(all_by_task.get(task.id, ()))
            displayed = tuple(display_by_task.get(task.id, ()))
            if wanted_class is not None and not displayed:
                continue
            included = tuple(row for row in associated if row.budget_included)
            load_complete = all(row.planned_hours is not None for row in included)
            planned_wp_hours = (
                sum(
                    (row.planned_hours for row in included if row.planned_hours is not None),
                    Decimal("0"),
                )
                if load_complete
                else None
            )
            budget_amount_cad = (
                Decimal(task.budget_amount_cad)
                if task.budget_amount_cad is not None
                else None
            )
            budget_actual_cad = (
                Decimal(task.budget_actual_cad)
                if task.budget_actual_cad is not None
                else None
            )
            remaining_budget_cad = (
                budget_amount_cad - budget_actual_cad
                if budget_amount_cad is not None and budget_actual_cad is not None
                else None
            )
            financial_diagnostic = erp_financial_budget_diagnostic(
                budget_amount_cad=budget_amount_cad,
                budget_actual_cad=budget_actual_cad,
            )
            average_hourly_cost_cad = (
                Decimal(task.average_hourly_cost_cad)
                if task.average_hourly_cost_cad is not None
                else None
            )
            actual_projection_diagnostic = actual_hours_diagnostic(
                financial_diagnostic=financial_diagnostic,
                average_hourly_cost_cad=average_hourly_cost_cad,
            )
            remaining_budget_hours_from_actual = (
                remaining_budget_cad / average_hourly_cost_cad
                if (
                    remaining_budget_cad is not None
                    and average_hourly_cost_cad is not None
                    and actual_projection_diagnostic is None
                )
                else None
            )

            future_packages = tuple(
                row for row in associated if row.current_load_included
            )
            future_load_incomplete = any(
                row.weekly_load_diagnostic is not None
                and (
                    row.end_date is None
                    or row.end_date >= reference_week_start
                )
                for row in future_packages
            )
            future_work_package_hours = (
                None
                if future_load_incomplete
                else sum(
                    (
                        load.hours
                        for row in future_packages
                        for load in row.weekly_loads
                        if load.week_start >= reference_week_start
                    ),
                    Decimal("0"),
                )
            )
            future_work_package_diagnostic = (
                MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE
                if future_load_incomplete
                else None
            )
            remaining_after_work_packages_hours = (
                remaining_budget_hours_from_actual - future_work_package_hours
                if (
                    remaining_budget_hours_from_actual is not None
                    and future_work_package_hours is not None
                )
                else None
            )
            budget_hours = (
                Decimal(task.budget_hours)
                if task.budget_hours is not None
                else None
            )
            remaining_budget_hours = (
                budget_hours - planned_wp_hours
                if budget_hours is not None and planned_wp_hours is not None
                else None
            )

            remaining_reference_date = erp_budget_cutoff_date(
                task.erp_budget_last_success_at
            )
            remaining_mode_diagnostics: list[str] = []
            if financial_diagnostic is not None:
                remaining_mode_diagnostics.append(financial_diagnostic)
            if actual_projection_diagnostic is not None:
                remaining_mode_diagnostics.append(actual_projection_diagnostic)

            remaining_work_package_hours: Decimal | None
            if remaining_reference_date is None:
                remaining_work_package_hours = None
                remaining_mode_diagnostics.append(
                    ERP_REMAINING_DIAGNOSTIC_FRESHNESS_UNAVAILABLE
                )
            else:
                remaining_package_hours = tuple(
                    work_package_hours_on_or_after(
                        row,
                        cutoff=remaining_reference_date,
                    )
                    for row in associated
                    if row.current_load_included
                )
                if any(value is None for value in remaining_package_hours):
                    remaining_work_package_hours = None
                    remaining_mode_diagnostics.append(
                        ERP_REMAINING_DIAGNOSTIC_LOAD_UNAVAILABLE
                    )
                else:
                    remaining_work_package_hours = sum(
                        (
                            value
                            for value in remaining_package_hours
                            if value is not None
                        ),
                        Decimal("0.00"),
                    )

            remaining_structured_balance_hours = (
                remaining_budget_hours_from_actual - remaining_work_package_hours
                if (
                    remaining_budget_hours_from_actual is not None
                    and remaining_work_package_hours is not None
                )
                else None
            )
            task_models.append(
                MediumTermBudgetTaskReadModel(
                    task_catalog_item_id=task.id,
                    task_code=task.task_code,
                    task_label=task.label,
                    erp_task_id=_optional_text(task.erp_task_id),
                    account_group=_text(task.account_group),
                    budget_amount_cad=budget_amount_cad,
                    budget_actual_cad=budget_actual_cad,
                    remaining_budget_cad=remaining_budget_cad,
                    financial_diagnostic=financial_diagnostic,
                    average_hourly_cost_cad=average_hourly_cost_cad,
                    remaining_budget_hours_from_actual=remaining_budget_hours_from_actual,
                    actual_hours_diagnostic=actual_projection_diagnostic,
                    future_work_package_hours=future_work_package_hours,
                    future_work_package_diagnostic=future_work_package_diagnostic,
                    remaining_after_work_packages_hours=remaining_after_work_packages_hours,
                    budget_hours=budget_hours,
                    planned_wp_hours=planned_wp_hours,
                    remaining_budget_hours=remaining_budget_hours,
                    remaining_reference_date=remaining_reference_date,
                    remaining_reference_basis=ERP_REMAINING_REFERENCE_BASIS,
                    remaining_work_package_hours=remaining_work_package_hours,
                    remaining_structured_balance_hours=remaining_structured_balance_hours,
                    remaining_mode_diagnostics=tuple(
                        dict.fromkeys(remaining_mode_diagnostics)
                    ),
                    associated_work_package_count=len(associated),
                    budget_included_work_package_count=len(included),
                    diagnostic_state=task_budget_diagnostic(
                        budget_hours=budget_hours,
                        planned_wp_hours=planned_wp_hours,
                        included_work_package_count=len(included),
                    ),
                    budget_source_diagnostic=_optional_text(task.budget_diagnostic),
                    work_packages=displayed,
                    active=bool(task.active),
                    workforce_eligible=task.workforce_eligible,
                    project_id=project.id,
                    project_number=project.number,
                    project_name=project.name,
                    project_manager_contact_id=project_manager_contact_id,
                    project_manager_display_name=(
                        project_manager.display_name
                        if project_manager is not None
                        else None
                    ),
                    manager_group_key=manager_group_key,
                    manager_display_name=(
                        project_manager.display_name
                        if project_manager is not None
                        else "Sans chargé de projet ERP"
                    ),
                    manager_resolution_status=(
                        project_manager.resolution_status
                        if project_manager is not None
                        else "UNRESOLVED"
                    ),
                    manager_diagnostics=(
                        project_manager.diagnostics
                        if project_manager is not None
                        else (
                            manager_projection.diagnostics
                            if manager_projection is not None
                            else ()
                        )
                    ),
                    erp_budget_last_success_at=task.erp_budget_last_success_at,
                )
            )

        diagnostics: list[str] = []
        weekly_diagnostics: list[str] = []
        if displayed_unclassified:
            diagnostics.append(MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES)

        current_packages = tuple(
            projected
            for projected in displayed_by_id.values()
            if projected.current_load_included
        )
        if any(row.resource_class_code is None for row in current_packages):
            diagnostics.append(
                MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGE_LOAD
            )
        if any(row.weekly_load_diagnostic is not None for row in current_packages):
            weekly_diagnostics.append(MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE)

        effective_start = start
        effective_end = end
        if effective_start is None and effective_end is None:
            dated = tuple(
                row
                for row in current_packages
                if row.start_date is not None and row.end_date is not None
            )
            if dated:
                effective_start = min(
                    row.start_date for row in dated if row.start_date is not None
                )
                effective_end = max(
                    row.end_date for row in dated if row.end_date is not None
                )

        package_class_keys = {
            package.resource_class_code or UNCLASSIFIED
            for package in current_packages
        }

        weeks: list[MediumTermWeekReadModel] = []
        if effective_start is not None and effective_end is not None:
            first_week = effective_start - timedelta(days=effective_start.weekday())
            last_week = effective_end - timedelta(days=effective_end.weekday())
            capacity_by_week = build_workforce_weekly_capacity_by_class(
                self,
                self._web_session,
                start=effective_start,
                end=effective_end,
            )
            cursor = first_week
            any_capacity_zero = False
            while cursor <= last_week:
                week_end = cursor + timedelta(days=6)
                load_by_class: defaultdict[str, Decimal] = defaultdict(
                    lambda: Decimal("0.00")
                )
                incomplete_classes: set[str] = set()
                global_incomplete = False

                for package in current_packages:
                    class_key = package.resource_class_code or UNCLASSIFIED
                    diagnostic = package.weekly_load_diagnostic
                    if diagnostic is not None:
                        overlaps = (
                            package.start_date is None
                            or package.end_date is None
                            or (
                                package.start_date <= week_end
                                and package.end_date >= cursor
                            )
                        )
                        if overlaps:
                            incomplete_classes.add(class_key)
                            global_incomplete = True
                        continue
                    for load in package.weekly_loads:
                        if load.week_start == cursor:
                            load_by_class[class_key] += load.hours

                total_capacity, class_capacity = capacity_by_week.get(
                    cursor,
                    (Decimal("0.00"), {}),
                )
                if wanted_class is not None:
                    legacy_capacity = class_capacity.get(
                        wanted_class,
                        Decimal("0.00"),
                    )
                    class_keys = {wanted_class}
                else:
                    legacy_capacity = total_capacity
                    class_keys = (
                        set(class_capacity)
                        | package_class_keys
                        | set(load_by_class)
                        | incomplete_classes
                    )

                legacy_total = sum(load_by_class.values(), Decimal("0.00"))
                legacy_load: Decimal | None = (
                    None if global_incomplete else legacy_total
                )
                week_diagnostics: list[str] = []
                if legacy_load is None:
                    week_diagnostics.append(WEEK_DIAGNOSTIC_LOAD_INCOMPLETE)
                if legacy_capacity <= 0:
                    any_capacity_zero = True
                    week_diagnostics.append(WEEK_DIAGNOSTIC_CAPACITY_ZERO)
                legacy_utilization = (
                    (
                        legacy_load
                        / legacy_capacity
                        * Decimal("100")
                    ).quantize(Decimal("0.01"))
                    if legacy_load is not None and legacy_capacity > 0
                    else None
                )
                legacy_state = (
                    "unavailable"
                    if legacy_load is None
                    else medium_term_capacity_state(
                        legacy_capacity,
                        legacy_load,
                    )
                )

                class_rows: list[MediumTermClassWeekReadModel] = []
                for class_key in sorted(
                    class_keys,
                    key=lambda value: (
                        value == UNCLASSIFIED,
                        (
                            resource_classes_by_code[value].label
                            if value in resource_classes_by_code
                            else value
                        ).casefold(),
                    ),
                ):
                    class_load = (
                        None
                        if class_key in incomplete_classes
                        else load_by_class.get(class_key, Decimal("0.00"))
                    )
                    capacity = class_capacity.get(
                        class_key,
                        Decimal("0.00"),
                    )
                    class_diagnostics: list[str] = []
                    if class_load is None:
                        class_diagnostics.append(
                            WEEK_DIAGNOSTIC_LOAD_INCOMPLETE
                        )
                    if capacity <= 0:
                        class_diagnostics.append(
                            WEEK_DIAGNOSTIC_CAPACITY_ZERO
                        )
                    utilization = (
                        (
                            class_load
                            / capacity
                            * Decimal("100")
                        ).quantize(Decimal("0.01"))
                        if class_load is not None and capacity > 0
                        else None
                    )
                    state = (
                        "unavailable"
                        if class_load is None
                        else medium_term_capacity_state(
                            capacity,
                            class_load,
                        )
                    )
                    configured = resource_classes_by_code.get(class_key)
                    class_rows.append(
                        MediumTermClassWeekReadModel(
                            resource_class_code=(
                                None
                                if class_key == UNCLASSIFIED
                                else class_key
                            ),
                            resource_class_label=(
                                UNCLASSIFIED
                                if class_key == UNCLASSIFIED
                                else (
                                    configured.label
                                    if configured is not None
                                    else class_key
                                )
                            ),
                            capacity_hours=capacity,
                            work_package_hours=class_load,
                            utilization=utilization,
                            state=state,
                            diagnostics=tuple(class_diagnostics),
                        )
                    )

                weeks.append(
                    MediumTermWeekReadModel(
                        week_start=cursor,
                        work_package_hours=legacy_load,
                        capacity_hours=legacy_capacity,
                        utilization=legacy_utilization,
                        state=legacy_state,
                        diagnostics=tuple(week_diagnostics),
                        classes=tuple(class_rows),
                    )
                )
                cursor += timedelta(days=7)
            if any_capacity_zero:
                weekly_diagnostics.append(MEDIUM_TERM_DIAGNOSTIC_CAPACITY_ZERO)

        selected_project = (
            selected_projects[0]
            if wanted_project is not None and selected_projects
            else None
        )
        budget_sync_state = (
            budget_sync_by_project_number.get(selected_project.number)
            if selected_project is not None
            else None
        )
        return MediumTermBudgetReadModel(
            project_id=selected_project.id if selected_project is not None else None,
            project_number=(
                selected_project.number
                if selected_project is not None
                else None
            ),
            project_name=(
                selected_project.name
                if selected_project is not None
                else None
            ),
            tasks=tuple(task_models),
            reference_week_start=reference_week_start,
            actual_through_date=actual_through_date,
            reference_basis=REFERENCE_BASIS_ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK,
            erp_budget_last_success_at=(
                budget_sync_state.last_success_at
                if budget_sync_state is not None
                else None
            ),
            unclassified_work_packages=tuple(displayed_unclassified),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            weekly_diagnostics=tuple(dict.fromkeys(weekly_diagnostics)),
            window_start=effective_start,
            window_end=effective_end,
            weeks=tuple(weeks),
            project_count=len(selected_projects),
            task_options=task_options,
            resource_classes=resource_class_options,
        )

    def list_availability_rules(
        self,
        *,
        resource_id: str | None = None,
        include_global: bool = True,
        active_only: bool = True,
    ) -> tuple[ResourceAvailabilityRuleReadModel, ...]:
        statement = (
            select(ResourceAvailabilityRule, Resource)
            .outerjoin(Resource, ResourceAvailabilityRule.resource_id == Resource.id)
            .order_by(
                ResourceAvailabilityRule.start_date,
                ResourceAvailabilityRule.availability_type,
                ResourceAvailabilityRule.id,
            )
        )
        if active_only:
            statement = statement.where(ResourceAvailabilityRule.active == true())

        wanted_resource = _text(resource_id)
        if wanted_resource:
            if include_global:
                statement = statement.where(
                    (ResourceAvailabilityRule.resource_id == wanted_resource)
                    | (ResourceAvailabilityRule.resource_id.is_(None))
                )
            else:
                statement = statement.where(
                    ResourceAvailabilityRule.resource_id == wanted_resource
                )
        elif not include_global:
            statement = statement.where(ResourceAvailabilityRule.resource_id.is_not(None))

        rows = self._web_session.execute(statement).all()
        class_codes = availability_class_codes_by_rule(
            self._web_session,
            tuple(rule.id for rule, _resource in rows),
        )
        return tuple(
            ResourceAvailabilityRuleReadModel(
                id=rule.id,
                availability_type=rule.availability_type,
                resource_id=rule.resource_id,
                resource_name=resource.name if resource is not None else None,
                resource_class_codes=class_codes.get(rule.id, ()),
                start_date=rule.start_date,
                end_date=rule.end_date,
                weekdays=_optional_text(rule.weekdays),
                start_time=rule.start_time,
                end_time=rule.end_time,
                note=_optional_text(rule.note),
                active=bool(rule.active),
            )
            for rule, resource in rows
        )

    def list_demand_history(self, number: str) -> tuple[DemandHistoryReadModel, ...]:
        wanted = _text(number)
        if not wanted:
            return ()
        statement = (
            select(WorkforceRequestHistory, WorkforceRequest)
            .join(
                WorkforceRequest,
                WorkforceRequestHistory.workforce_request_id == WorkforceRequest.id,
            )
            .where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
            .order_by(
                WorkforceRequestHistory.occurred_at.desc(),
                WorkforceRequestHistory.id.desc(),
            )
        )
        rows = self._web_session.execute(statement).all()
        return tuple(
            DemandHistoryReadModel(
                demand_number=_optional_text(request.legacy_demand_number) or request.id,
                action=_text(history.action) or "Événement",
                occurred_at=history.occurred_at,
                previous_status=_optional_text(history.previous_status),
                status=_optional_text(history.status),
                comment=_optional_text(history.comment),
                details=_optional_text(history.details),
                actor_user_id=_optional_text(history.actor_user_id),
                actor_name=_optional_text(history.actor_name),
            )
            for history, request in rows
        )

    def list_planning_history(
        self,
        entity_type: str,
        reference: str,
    ) -> tuple[PlanningHistoryReadModel, ...]:
        wanted_type = _text(entity_type).upper()
        wanted_reference = _text(reference)
        if not wanted_type or not wanted_reference:
            return ()
        rows = self._web_session.scalars(
            select(PlanningChangeHistory)
            .where(
                PlanningChangeHistory.entity_type == wanted_type,
                PlanningChangeHistory.entity_reference == wanted_reference,
            )
            .order_by(
                PlanningChangeHistory.occurred_at.desc(),
                PlanningChangeHistory.id.desc(),
            )
        ).all()
        return tuple(
            PlanningHistoryReadModel(
                entity_type=row.entity_type,
                entity_reference=row.entity_reference,
                action=row.action,
                occurred_at=row.occurred_at,
                parent_reference=_optional_text(row.parent_reference),
                details=_optional_text(row.details),
                actor_name=_optional_text(row.actor_name),
            )
            for row in rows
        )
