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
    MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES,
    MEDIUM_TERM_DIAGNOSTIC_WEEKLY_LOAD_INCOMPLETE,
    WEEK_DIAGNOSTIC_CAPACITY_ZERO,
    WEEK_DIAGNOSTIC_LOAD_INCOMPLETE,
    MediumTermBudgetReadModel,
    MediumTermBudgetTaskReadModel,
    MediumTermBudgetWorkPackageReadModel,
    MediumTermWeekReadModel,
    MediumTermWeeklyLoadReadModel,
    task_budget_diagnostic,
    work_package_is_budget_included,
    work_package_is_current_load_included,
)
from ...application.query_models import PlanningHistoryReadModel
from ...application.work_package_weekly_load import (
    WorkPackageWeeklyLoadState,
    WeeklyLoadValue,
    weekly_load_diagnostic,
)
from .models import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    WorkforceRequest,
    WorkforceRequestHistory,
    TaskCatalogEntry,
    WorkPackage,
    WorkPackageWeeklyLoad,
)
from .planning_audit import PlanningChangeHistory
from .capacity_query_repository import SqlPlannerQueryRepository
from .medium_term_capacity_query import build_workforce_weekly_capacity


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


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
            select(WorkPackage, Project, TaskCatalogEntry)
            .join(Project, WorkPackage.project_id == Project.id)
            .outerjoin(
                TaskCatalogEntry,
                WorkPackage.task_catalog_item_id == TaskCatalogEntry.id,
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
        for work_package, project, task in rows:
            status = _text(work_package.status) or "planned"
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
                    task_catalog_item_id=work_package.task_catalog_item_id,
                    task_code=_optional_text(task.task_code) if task is not None else None,
                    task_label=_optional_text(task.label) if task is not None else None,
                    version=int(work_package.version or 1),
                )
            )
        return tuple(result)

    @staticmethod
    def _medium_term_budget_work_package(
        work_package: WorkPackage,
        loads: tuple[WeeklyLoadValue, ...],
    ) -> MediumTermBudgetWorkPackageReadModel:
        status = _text(work_package.status) or "planned"
        state = WorkPackageWeeklyLoadState(
            reference=_optional_text(work_package.legacy_effort_id) or work_package.id,
            version=int(work_package.version or 1),
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            planned_hours=(
                Decimal(work_package.planned_hours)
                if work_package.planned_hours is not None
                else None
            ),
            origin=_optional_text(work_package.weekly_load_origin),
            loads=loads,
        )
        return MediumTermBudgetWorkPackageReadModel(
            id=work_package.id,
            reference=state.reference,
            code=_optional_text(work_package.code),
            name=work_package.name,
            planned_hours=state.planned_hours,
            status=status,
            budget_included=work_package_is_budget_included(status),
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            version=state.version,
            current_load_included=work_package_is_current_load_included(status),
            weekly_load_origin=state.origin,
            weekly_loads=tuple(
                MediumTermWeeklyLoadReadModel(
                    week_start=item.week_start,
                    hours=item.hours,
                )
                for item in loads
            ),
            weekly_load_diagnostic=weekly_load_diagnostic(state),
        )

    def medium_term_budget_projection(
        self,
        *,
        project_number: str,
        project_ids: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> MediumTermBudgetReadModel | None:
        wanted_project = _text(project_number)
        if not wanted_project:
            return None

        project_statement = select(Project).where(Project.number == wanted_project)
        if project_ids is not None:
            identifiers = tuple(str(value) for value in project_ids if str(value))
            if not identifiers:
                return None
            project_statement = project_statement.where(Project.id.in_(identifiers))
        project = self._web_session.scalar(project_statement)
        if project is None:
            return None

        task_rows = self._web_session.scalars(
            select(TaskCatalogEntry)
            .where(TaskCatalogEntry.project_number == project.number)
            .order_by(TaskCatalogEntry.task_code, TaskCatalogEntry.id)
        ).all()
        tasks = tuple(
            task
            for task in task_rows
            if _text(task.account_group).upper() == "DEPMO"
        )

        work_packages = tuple(
            self._web_session.scalars(
                select(WorkPackage)
                .where(WorkPackage.project_id == project.id)
                .order_by(WorkPackage.start_date, WorkPackage.name, WorkPackage.id)
            ).all()
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

        by_task: dict[str, list[MediumTermBudgetWorkPackageReadModel]] = {
            task.id: [] for task in tasks
        }
        unclassified: list[MediumTermBudgetWorkPackageReadModel] = []
        projected_by_id: dict[str, MediumTermBudgetWorkPackageReadModel] = {}
        for work_package in work_packages:
            projected = self._medium_term_budget_work_package(
                work_package,
                tuple(loads_by_package.get(work_package.id, ())),
            )
            task_id = work_package.task_catalog_item_id
            if task_id is None:
                projected_by_id[work_package.id] = projected
                unclassified.append(projected)
            elif task_id in by_task:
                projected_by_id[work_package.id] = projected
                by_task[task_id].append(projected)

        task_models: list[MediumTermBudgetTaskReadModel] = []
        for task in tasks:
            associated = tuple(by_task[task.id])
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
            task_models.append(
                MediumTermBudgetTaskReadModel(
                    task_catalog_item_id=task.id,
                    task_code=task.task_code,
                    task_label=task.label,
                    erp_task_id=_optional_text(task.erp_task_id),
                    account_group=_text(task.account_group),
                    budget_hours=budget_hours,
                    planned_wp_hours=planned_wp_hours,
                    remaining_budget_hours=remaining_budget_hours,
                    associated_work_package_count=len(associated),
                    budget_included_work_package_count=len(included),
                    diagnostic_state=task_budget_diagnostic(
                        budget_hours=budget_hours,
                        planned_wp_hours=planned_wp_hours,
                        included_work_package_count=len(included),
                    ),
                    budget_source_diagnostic=_optional_text(task.budget_diagnostic),
                    work_packages=associated,
                    active=bool(task.active),
                    workforce_eligible=task.workforce_eligible,
                )
            )

        diagnostics: list[str] = []
        weekly_diagnostics: list[str] = []
        if unclassified:
            diagnostics.append(MEDIUM_TERM_DIAGNOSTIC_UNCLASSIFIED_WORK_PACKAGES)

        current_packages = tuple(
            projected
            for projected in projected_by_id.values()
            if projected.current_load_included
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
                effective_start = min(row.start_date for row in dated if row.start_date is not None)
                effective_end = max(row.end_date for row in dated if row.end_date is not None)

        weeks: list[MediumTermWeekReadModel] = []
        if effective_start is not None and effective_end is not None:
            first_week = effective_start - timedelta(days=effective_start.weekday())
            last_week = effective_end - timedelta(days=effective_end.weekday())
            capacity_by_week = build_workforce_weekly_capacity(
                self,
                self._web_session,
                start=effective_start,
                end=effective_end,
            )
            cursor = first_week
            any_capacity_zero = False
            while cursor <= last_week:
                week_end = cursor + timedelta(days=6)
                incomplete = False
                total = Decimal("0.00")
                for package in current_packages:
                    diagnostic = package.weekly_load_diagnostic
                    if diagnostic is not None:
                        if package.start_date is None or package.end_date is None:
                            incomplete = True
                        elif package.start_date <= week_end and package.end_date >= cursor:
                            incomplete = True
                        continue
                    for load in package.weekly_loads:
                        if load.week_start == cursor:
                            total += load.hours
                capacity = capacity_by_week.get(cursor, Decimal("0.00"))
                week_diagnostics: list[str] = []
                work_package_hours: Decimal | None = total
                if incomplete:
                    work_package_hours = None
                    week_diagnostics.append(WEEK_DIAGNOSTIC_LOAD_INCOMPLETE)
                if capacity <= 0:
                    any_capacity_zero = True
                    week_diagnostics.append(WEEK_DIAGNOSTIC_CAPACITY_ZERO)
                utilization = (
                    (work_package_hours / capacity * Decimal("100")).quantize(Decimal("0.01"))
                    if work_package_hours is not None and capacity > 0
                    else None
                )
                weeks.append(
                    MediumTermWeekReadModel(
                        week_start=cursor,
                        work_package_hours=work_package_hours,
                        capacity_hours=capacity,
                        utilization=utilization,
                        diagnostics=tuple(week_diagnostics),
                    )
                )
                cursor += timedelta(days=7)
            if any_capacity_zero:
                weekly_diagnostics.append(MEDIUM_TERM_DIAGNOSTIC_CAPACITY_ZERO)

        return MediumTermBudgetReadModel(
            project_id=project.id,
            project_number=project.number,
            project_name=project.name,
            tasks=tuple(task_models),
            unclassified_work_packages=tuple(unclassified),
            diagnostics=tuple(diagnostics),
            weekly_diagnostics=tuple(weekly_diagnostics),
            window_start=effective_start,
            window_end=effective_end,
            weeks=tuple(weeks),
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
        return tuple(
            ResourceAvailabilityRuleReadModel(
                id=rule.id,
                availability_type=rule.availability_type,
                resource_id=rule.resource_id,
                resource_name=resource.name if resource is not None else None,
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
