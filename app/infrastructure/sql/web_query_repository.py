from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application import (
    DemandHistoryReadModel,
    ResourceAvailabilityRuleReadModel,
    WorkPackageReadModel,
)
from ...application.query_models import PlanningHistoryReadModel
from .models import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    WorkforceRequest,
    WorkforceRequestHistory,
    TaskCatalogEntry,
    WorkPackage,
)
from .planning_audit import PlanningChangeHistory
from .capacity_query_repository import SqlPlannerQueryRepository


INACTIVE_WORK_PACKAGE_STATUSES = {
    "annulé",
    "annule",
    "fermé",
    "ferme",
    "terminé",
    "termine",
    "closed",
    "cancelled",
}


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
