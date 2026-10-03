from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
import json
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationConflictError,
    ApplicationValidationError,
)
from ...application.query_models import (
    WorkPackageReadModel,
    work_package_resource_class_diagnostic,
)
from ...application.repository_ports import WorkPackageRepositoryPort
from ...application.work_package_load import (
    LOAD_INTERVAL_ORIGIN_MANUAL,
    TERMINAL_STATUSES,
    WorkPackageLoadIntervalValue,
    legacy_weekly_as_intervals,
    terminal_status_from_legacy,
    validate_load_intervals,
    work_package_status,
)
from ...application.work_package_weekly_load import (
    WEEKLY_LOAD_ORIGINS,
    WEEKLY_LOAD_ORIGIN_AUTO,
    WorkPackageWeeklyLoadState,
    WeeklyLoadValue,
    propose_weekly_loads,
    validate_weekly_loads,
)
from .delivery_models import DeliveryPlanRow
from .models import (
    Project,
    RequestLine,
    ResourceRequirement,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
    WorkPackageAudit,
    WorkPackageLoadInterval,
    WorkPackageWeeklyLoad,
    new_id,
)
from .resource_class_models import ResourceClassConfig


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value).replace(",", "."))


class SqlWorkPackageRepository(WorkPackageRepositoryPort):
    """SQLAlchemy WorkPackage mutations inside the caller-owned transaction.

    502A serializes every mutation and every new dependency through the WorkPackage
    row itself. The no-op UPDATE guard is intentionally database-visible: on SQL
    Server it holds an update/exclusive row lock until the transaction completes,
    while the WorkPackage version remains a local aggregate CAS.
    """

    def __init__(self, session: Session, *, actor_user_id: str | None = None) -> None:
        self._session = session
        self._actor_user_id = _optional_text(actor_user_id)

    @staticmethod
    def _read_model(
        work_package: WorkPackage,
        project: Project,
        task: TaskCatalogEntry | None = None,
        resource_class: ResourceClassConfig | None = None,
    ) -> WorkPackageReadModel:
        task_resource_class_code = (
            _optional_text(task.resource_class_code) if task is not None else None
        )
        resource_class_code = _optional_text(work_package.resource_class_code)
        status, status_diagnostic = work_package_status(
            start_date=work_package.start_date,
            terminal_status=work_package.terminal_status,
            legacy_status=work_package.status,
        )
        return WorkPackageReadModel(
            id=work_package.id,
            reference=_optional_text(work_package.legacy_effort_id) or work_package.id,
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
            resource_class_code=resource_class_code,
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
            task_resource_class_code=task_resource_class_code,
            resource_class_diagnostic=work_package_resource_class_diagnostic(
                resource_class_code,
                task_resource_class_code,
            ),
            version=int(work_package.version or 1),
        )

    def _row(
        self,
        reference: str,
    ) -> tuple[
        WorkPackage,
        Project,
        TaskCatalogEntry | None,
        ResourceClassConfig | None,
    ] | None:
        wanted = _text(reference)
        if not wanted:
            return None
        return self._session.execute(
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
            .where(
                (WorkPackage.id == wanted)
                | (WorkPackage.legacy_effort_id == wanted)
            )
        ).one_or_none()

    def get(self, reference: str) -> WorkPackageReadModel | None:
        row = self._row(reference)
        if row is None:
            return None
        work_package, project, task, resource_class = row
        return self._read_model(work_package, project, task, resource_class)

    def _entity(self, reference: str) -> WorkPackage:
        wanted = _text(reference)
        work_package = self._session.scalar(
            select(WorkPackage).where(
                (WorkPackage.id == wanted)
                | (WorkPackage.legacy_effort_id == wanted)
            )
        )
        if work_package is None:
            raise KeyError(f"WorkPackage {wanted} introuvable")
        return work_package

    def _project(self, number: object) -> Project:
        project_number = _text(number)
        project = self._session.scalar(
            select(Project).where(Project.number == project_number)
        )
        if project is None:
            raise KeyError(f"Projet {project_number} introuvable")
        return project

    def _task(self, identifier: object, *, project: Project) -> TaskCatalogEntry:
        task_id = _text(identifier)
        if not task_id:
            raise ApplicationValidationError(
                "La tâche ERP du WorkPackage est requise.",
                code="work_package_task_required",
            )
        task = self._session.get(TaskCatalogEntry, task_id)
        if task is None:
            raise ApplicationValidationError(
                "La tâche ERP sélectionnée est introuvable.",
                code="work_package_task_not_found",
                context={"task_catalog_item_id": task_id},
            )
        if task.project_number != project.number:
            raise ApplicationValidationError(
                "La tâche ERP doit appartenir au même projet que le WorkPackage.",
                code="work_package_task_project_mismatch",
                context={
                    "task_catalog_item_id": task.id,
                    "task_project_number": task.project_number,
                    "work_package_project_number": project.number,
                },
            )
        if not bool(task.active) or task.workforce_eligible is False:
            raise ApplicationValidationError(
                "La tâche ERP sélectionnée n'est pas active ou admissible à la main-d'œuvre.",
                code="work_package_task_ineligible",
                context={"task_catalog_item_id": task.id},
            )
        return task

    def _resource_class_for_assignment(
        self,
        value: object,
    ) -> ResourceClassConfig:
        code = _text(value)
        if not code:
            raise ApplicationValidationError(
                "La classe de ressource du WorkPackage est requise.",
                code="work_package_resource_class_required",
                context={"field": "resource_class_code"},
            )
        resource_class = self._session.get(ResourceClassConfig, code)
        if resource_class is None:
            raise ApplicationValidationError(
                "La classe de ressource sélectionnée est introuvable.",
                code="work_package_resource_class_not_found",
                context={"resource_class_code": code},
            )
        if not bool(resource_class.active):
            raise ApplicationValidationError(
                "Une classe inactive ne peut pas être affectée à un WorkPackage.",
                code="work_package_resource_class_inactive",
                context={"resource_class_code": code},
            )
        return resource_class

    def _initial_resource_class(
        self,
        task: TaskCatalogEntry,
    ) -> ResourceClassConfig | None:
        code = _optional_text(task.resource_class_code)
        if code is None:
            return None
        resource_class = self._session.get(ResourceClassConfig, code)
        if resource_class is None or not bool(resource_class.active):
            return None
        return resource_class

    def _guard(self, work_package: WorkPackage) -> None:
        result = self._session.execute(
            update(WorkPackage)
            .where(WorkPackage.id == work_package.id)
            .values(
                version=WorkPackage.version,
                updated_at=WorkPackage.updated_at,
            )
        )
        if int(result.rowcount or 0) != 1:
            raise KeyError(f"WorkPackage {work_package.id} introuvable")
        self._session.flush()
        self._session.refresh(work_package)

    def _dependency_counts(self, work_package: WorkPackage) -> dict[str, int]:
        references = [work_package.id]
        legacy = _optional_text(work_package.legacy_effort_id)
        if legacy:
            references.append(legacy)
        return {
            "workforce_requests": int(
                self._session.scalar(
                    select(func.count())
                    .select_from(WorkforceRequest)
                    .where(WorkforceRequest.work_package_id == work_package.id)
                )
                or 0
            ),
            "request_lines": int(
                self._session.scalar(
                    select(func.count())
                    .select_from(RequestLine)
                    .where(RequestLine.work_package_id == work_package.id)
                )
                or 0
            ),
            "resource_requirements": int(
                self._session.scalar(
                    select(func.count())
                    .select_from(ResourceRequirement)
                    .where(ResourceRequirement.source_effort_id.in_(tuple(references)))
                )
                or 0
            ),
            "delivery_plans": int(
                self._session.scalar(
                    select(func.count())
                    .select_from(DeliveryPlanRow)
                    .where(DeliveryPlanRow.work_package_id == work_package.id)
                )
                or 0
            ),
        }

    @staticmethod
    def _has_dependencies(counts: Mapping[str, int]) -> bool:
        return any(int(value or 0) > 0 for value in counts.values())

    def _validate_regularization(
        self,
        work_package: WorkPackage,
        target_task: TaskCatalogEntry,
    ) -> None:
        contradictory = int(
            self._session.scalar(
                select(func.count())
                .select_from(RequestLine)
                .where(
                    RequestLine.work_package_id == work_package.id,
                    RequestLine.task_catalog_item_id.is_not(None),
                    RequestLine.task_catalog_item_id != target_task.id,
                )
            )
            or 0
        )
        if contradictory:
            raise ApplicationConflictError(
                "Le WorkPackage possède déjà des lignes liées à une autre tâche ERP.",
                code="work_package_task_regularization_conflict",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    "contradictory_request_lines": contradictory,
                },
            )

    @staticmethod
    def _snapshot(
        work_package: WorkPackage,
        project: Project,
    ) -> dict[str, object]:
        return {
            "project_number": project.number,
            "task_catalog_item_id": work_package.task_catalog_item_id,
            "resource_class_code": _optional_text(work_package.resource_class_code),
            "code": _optional_text(work_package.code),
            "name": work_package.name,
            "description": _optional_text(work_package.description),
            "start_date": (
                work_package.start_date.isoformat()
                if work_package.start_date is not None
                else None
            ),
            "end_date": (
                work_package.end_date.isoformat()
                if work_package.end_date is not None
                else None
            ),
            "planned_hours": (
                float(work_package.planned_hours)
                if work_package.planned_hours is not None
                else None
            ),
            "status": work_package_status(
                start_date=work_package.start_date,
                terminal_status=work_package.terminal_status,
                legacy_status=work_package.status,
            )[0],
            "legacy_status": _text(work_package.status) or "planned",
            "terminal_status": _optional_text(work_package.terminal_status),
            "weekly_load_origin": _optional_text(work_package.weekly_load_origin),
            "version": int(work_package.version or 1),
        }

    def _weekly_loads(self, work_package_id: str) -> tuple[WeeklyLoadValue, ...]:
        rows = self._session.scalars(
            select(WorkPackageWeeklyLoad)
            .where(WorkPackageWeeklyLoad.work_package_id == work_package_id)
            .order_by(WorkPackageWeeklyLoad.week_start)
        ).all()
        return tuple(
            WeeklyLoadValue(
                week_start=row.week_start,
                hours=Decimal(row.hours),
            )
            for row in rows
        )

    def _load_intervals(
        self,
        work_package: WorkPackage,
    ) -> tuple[WorkPackageLoadIntervalValue, ...]:
        rows = self._session.scalars(
            select(WorkPackageLoadInterval)
            .where(WorkPackageLoadInterval.work_package_id == work_package.id)
            .order_by(
                WorkPackageLoadInterval.start_date,
                WorkPackageLoadInterval.end_date,
                WorkPackageLoadInterval.id,
            )
        ).all()
        if rows:
            return tuple(
                WorkPackageLoadIntervalValue(
                    id=row.id,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    hours=Decimal(row.hours),
                    origin=row.origin,
                )
                for row in rows
            )
        legacy = self._weekly_loads(work_package.id)
        return legacy_weekly_as_intervals(
            work_package_start=work_package.start_date,
            work_package_end=work_package.end_date,
            weekly_loads=tuple((row.week_start, row.hours) for row in legacy),
            origin=_optional_text(work_package.weekly_load_origin),
        )

    def _replace_load_intervals(
        self,
        work_package: WorkPackage,
        intervals: Sequence[WorkPackageLoadIntervalValue],
        *,
        start_date: object,
        end_date: object,
        planned_hours: object,
    ) -> tuple[WorkPackageLoadIntervalValue, ...]:
        existing = {row.id: row for row in self._load_intervals(work_package) if row.id}
        prepared = tuple(
            WorkPackageLoadIntervalValue(
                id=item.id,
                start_date=item.start_date,
                end_date=item.end_date,
                hours=item.hours,
                origin=(
                    existing[item.id].origin
                    if item.id is not None and item.id in existing
                    else LOAD_INTERVAL_ORIGIN_MANUAL
                ),
            )
            for item in intervals
        )
        normalized = validate_load_intervals(
            start_date=start_date,  # type: ignore[arg-type]
            end_date=end_date,  # type: ignore[arg-type]
            planned_hours=(
                Decimal(planned_hours) if planned_hours is not None else None
            ),
            intervals=prepared,
        )
        self._session.execute(
            delete(WorkPackageLoadInterval).where(
                WorkPackageLoadInterval.work_package_id == work_package.id
            )
        )
        stored: list[WorkPackageLoadIntervalValue] = []
        for item in normalized:
            identifier = item.id or new_id()
            conflicting = self._session.get(WorkPackageLoadInterval, identifier)
            if conflicting is not None and conflicting.work_package_id != work_package.id:
                raise ApplicationConflictError(
                    "Cet identifiant d'intervalle appartient déjà à un autre WorkPackage.",
                    code="work_package_load_interval_id_conflict",
                    context={"id": identifier},
                )
            self._session.add(
                WorkPackageLoadInterval(
                    id=identifier,
                    work_package_id=work_package.id,
                    start_date=item.start_date,
                    end_date=item.end_date,
                    hours=item.hours,
                    origin=item.origin,
                )
            )
            stored.append(
                WorkPackageLoadIntervalValue(
                    id=identifier,
                    start_date=item.start_date,
                    end_date=item.end_date,
                    hours=item.hours,
                    origin=item.origin,
                )
            )
        return tuple(stored)

    def get_weekly_load_state(
        self,
        reference: str,
    ) -> WorkPackageWeeklyLoadState | None:
        row = self._row(reference)
        if row is None:
            return None
        work_package, _project, _task, _resource_class = row
        return WorkPackageWeeklyLoadState(
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
            loads=self._weekly_loads(work_package.id),
        )

    def _audit(
        self,
        work_package: WorkPackage,
        *,
        action: str,
        old_values: Mapping[str, object],
        new_values: Mapping[str, object],
    ) -> None:
        if not self._actor_user_id:
            raise ApplicationValidationError(
                "Un AppUser authentifié est requis pour auditer une mutation WorkPackage.",
                code="work_package_actor_required",
            )
        self._session.add(
            WorkPackageAudit(
                work_package_id=work_package.id,
                actor_user_id=self._actor_user_id,
                action=action,
                resulting_version=int(work_package.version or 1),
                old_values_json=json.dumps(
                    dict(old_values),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                new_values_json=json.dumps(
                    dict(new_values),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        self._session.flush()

    def create(self, values: Mapping[str, Any]) -> WorkPackageReadModel:
        project = self._project(values.get("project_number"))
        task = self._task(values.get("task_catalog_item_id"), project=project)
        if "resource_class_code" in values:
            supplied_class = values.get("resource_class_code")
            resource_class = (
                None
                if supplied_class is None
                else self._resource_class_for_assignment(supplied_class)
            )
        else:
            resource_class = self._initial_resource_class(task)
        work_package = WorkPackage(
            project_id=project.id,
            task_catalog_item_id=task.id,
            resource_class_code=(
                resource_class.code if resource_class is not None else None
            ),
            version=1,
            code=_optional_text(values.get("code")),
            name=_text(values.get("name")),
            description=_optional_text(values.get("description")),
            start_date=values.get("start_date"),
            end_date=values.get("end_date"),
            planned_hours=_decimal(values.get("planned_hours")),
            status="planned",
            terminal_status=None,
        )
        self._session.add(work_package)
        self._session.flush()
        new_values = self._snapshot(work_package, project)
        self._audit(
            work_package,
            action="CREATE",
            old_values={},
            new_values=new_values,
        )
        return self._read_model(work_package, project, task, resource_class)

    def update(
        self,
        reference: str,
        updates: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> WorkPackageReadModel:
        work_package = self._entity(reference)
        self._guard(work_package)

        expected = int(expected_version)
        current_version = int(work_package.version or 1)
        if current_version != expected:
            raise ApplicationConflictError(
                "Le WorkPackage a été modifié depuis sa lecture.",
                code="work_package_version_conflict",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    "expected_version": expected,
                    "current_version": current_version,
                },
            )

        current_project = self._session.get(Project, work_package.project_id)
        if current_project is None:
            raise KeyError("Projet du WorkPackage introuvable")
        target_project = (
            self._project(updates.get("project_number"))
            if "project_number" in updates
            else current_project
        )
        project_changed = target_project.id != current_project.id

        current_task_id = work_package.task_catalog_item_id
        target_task_id = (
            _optional_text(updates.get("task_catalog_item_id"))
            if "task_catalog_item_id" in updates
            else current_task_id
        )
        if current_task_id is not None and target_task_id is None:
            raise ApplicationValidationError(
                "Une tâche ERP classée ne peut pas être retirée d'un WorkPackage.",
                code="work_package_task_clear_forbidden",
            )
        target_task = (
            self._task(target_task_id, project=target_project)
            if target_task_id is not None
            else None
        )
        task_changed = target_task_id != current_task_id

        dependencies = self._dependency_counts(work_package)
        if project_changed and self._has_dependencies(dependencies):
            raise ApplicationConflictError(
                "Le projet d'un WorkPackage déjà utilisé ne peut pas être changé.",
                code=(
                    "work_package_project_change_linked_demands"
                    if dependencies["workforce_requests"] > 0
                    else "work_package_project_change_in_use"
                ),
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    **dependencies,
                },
            )
        if current_task_id is not None and task_changed and self._has_dependencies(dependencies):
            raise ApplicationConflictError(
                "La tâche ERP d'un WorkPackage déjà utilisé ne peut pas être changée.",
                code="work_package_task_change_in_use",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    **dependencies,
                },
            )
        if current_task_id is None and target_task is not None:
            self._validate_regularization(work_package, target_task)

        old_values = {
            **self._snapshot(work_package, current_project),
            "load_intervals": [
                {
                    "id": item.id,
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                    "hours": str(item.hours),
                    "origin": item.origin,
                }
                for item in self._load_intervals(work_package)
            ],
        }
        values: dict[str, object] = {}
        target_resource_class: ResourceClassConfig | None = (
            self._session.get(ResourceClassConfig, work_package.resource_class_code)
            if work_package.resource_class_code is not None
            else None
        )
        if "resource_class_code" in updates:
            supplied_class = updates.get("resource_class_code")
            if supplied_class is None:
                target_resource_class = None
                values["resource_class_code"] = None
            else:
                target_resource_class = self._resource_class_for_assignment(
                    supplied_class
                )
                values["resource_class_code"] = target_resource_class.code
        if project_changed:
            values["project_id"] = target_project.id
        if task_changed:
            values["task_catalog_item_id"] = target_task_id
        if "code" in updates:
            values["code"] = _optional_text(updates.get("code"))
        if "name" in updates:
            values["name"] = _text(updates.get("name"))
        if "description" in updates:
            values["description"] = _optional_text(updates.get("description"))
        if "start_date" in updates:
            values["start_date"] = updates.get("start_date")
        if "end_date" in updates:
            values["end_date"] = updates.get("end_date")
        if "planned_hours" in updates:
            values["planned_hours"] = _decimal(updates.get("planned_hours"))
        target_start = values.get("start_date", work_package.start_date)
        target_end = values.get("end_date", work_package.end_date)
        target_planned_hours = values.get("planned_hours", work_package.planned_hours)
        existing_intervals = self._load_intervals(work_package)
        if "load_intervals" in updates:
            target_intervals = self._replace_load_intervals(
                work_package,
                updates.get("load_intervals") or (),
                start_date=target_start,
                end_date=target_end,
                planned_hours=target_planned_hours,
            )
            values["weekly_load_origin"] = None
        else:
            target_intervals = existing_intervals
            try:
                validate_load_intervals(
                    start_date=target_start,  # type: ignore[arg-type]
                    end_date=target_end,  # type: ignore[arg-type]
                    planned_hours=(
                        Decimal(target_planned_hours)
                        if target_planned_hours is not None
                        else None
                    ),
                    intervals=existing_intervals,
                )
            except ApplicationValidationError as exc:
                raise ApplicationConflictError(
                    "La modification exclurait ou surallouerait un intervalle explicite; corrige la collection dans la même commande.",
                    code="work_package_load_intervals_replan_required",
                    context={
                        "reference": _optional_text(work_package.legacy_effort_id)
                        or work_package.id,
                        "diagnostic": exc.code,
                    },
                ) from exc

        result = self._session.execute(
            update(WorkPackage)
            .where(
                WorkPackage.id == work_package.id,
                WorkPackage.version == expected,
            )
            .values(**values, version=WorkPackage.version + 1)
        )
        if int(result.rowcount or 0) != 1:
            actual = self._session.scalar(
                select(WorkPackage.version).where(WorkPackage.id == work_package.id)
            )
            raise ApplicationConflictError(
                "Le WorkPackage a été modifié par une autre opération.",
                code="work_package_version_conflict",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    "expected_version": expected,
                    "current_version": int(actual or current_version),
                },
            )

        self._session.flush()
        self._session.refresh(work_package)
        new_values = {
            **self._snapshot(work_package, target_project),
            "load_intervals": [
                {
                    "id": item.id,
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                    "hours": str(item.hours),
                    "origin": item.origin,
                }
                for item in target_intervals
            ],
        }
        action = (
            "REGULARIZE_TASK"
            if current_task_id is None and target_task_id is not None
            else "UPDATE"
        )
        self._audit(
            work_package,
            action=action,
            old_values=old_values,
            new_values=new_values,
        )
        return self._read_model(
            work_package,
            target_project,
            target_task,
            target_resource_class,
        )


    def replace_weekly_loads(
        self,
        reference: str,
        loads: Sequence[WeeklyLoadValue],
        *,
        origin: str,
        expected_version: int,
    ) -> WorkPackageReadModel:
        work_package = self._entity(reference)
        self._guard(work_package)

        expected = int(expected_version)
        current_version = int(work_package.version or 1)
        if current_version != expected:
            raise ApplicationConflictError(
                "Le WorkPackage a été modifié depuis sa lecture.",
                code="work_package_version_conflict",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    "expected_version": expected,
                    "current_version": current_version,
                },
            )
        if origin not in WEEKLY_LOAD_ORIGINS:
            raise ApplicationValidationError(
                "L'origine de répartition doit être AUTO ou MANUAL.",
                code="work_package_weekly_load_origin_invalid",
                context={"origin": origin},
            )

        normalized = validate_weekly_loads(
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            planned_hours=(
                Decimal(work_package.planned_hours)
                if work_package.planned_hours is not None
                else None
            ),
            loads=tuple(loads),
        )
        if origin == WEEKLY_LOAD_ORIGIN_AUTO:
            state = WorkPackageWeeklyLoadState(
                reference=_optional_text(work_package.legacy_effort_id) or work_package.id,
                version=current_version,
                start_date=work_package.start_date,
                end_date=work_package.end_date,
                planned_hours=(
                    Decimal(work_package.planned_hours)
                    if work_package.planned_hours is not None
                    else None
                ),
                origin=_optional_text(work_package.weekly_load_origin),
                loads=self._weekly_loads(work_package.id),
            )
            if normalized != propose_weekly_loads(state):
                raise ApplicationValidationError(
                    "Une répartition AUTO doit correspondre exactement à la proposition backend courante.",
                    code="work_package_weekly_load_auto_proposal_mismatch",
                    context={"reference": state.reference},
                )
        canonical_intervals = legacy_weekly_as_intervals(
            work_package_start=work_package.start_date,
            work_package_end=work_package.end_date,
            weekly_loads=tuple((item.week_start, item.hours) for item in normalized),
            origin=origin,
        )
        self._replace_load_intervals(
            work_package,
            canonical_intervals,
            start_date=work_package.start_date,
            end_date=work_package.end_date,
            planned_hours=work_package.planned_hours,
        )

        project = self._session.get(Project, work_package.project_id)
        if project is None:
            raise KeyError("Projet du WorkPackage introuvable")
        task = (
            self._session.get(TaskCatalogEntry, work_package.task_catalog_item_id)
            if work_package.task_catalog_item_id is not None
            else None
        )
        old_loads = self._weekly_loads(work_package.id)
        old_values = {
            **self._snapshot(work_package, project),
            "weekly_load_count": len(old_loads),
            "weekly_load_hours": str(
                sum((item.hours for item in old_loads), Decimal("0.00"))
            ),
            "weekly_loads": [
                {
                    "week_start": item.week_start.isoformat(),
                    "hours": str(item.hours),
                }
                for item in old_loads
            ],
        }

        self._session.execute(
            delete(WorkPackageWeeklyLoad).where(
                WorkPackageWeeklyLoad.work_package_id == work_package.id
            )
        )
        self._session.add_all(
            [
                WorkPackageWeeklyLoad(
                    work_package_id=work_package.id,
                    week_start=item.week_start,
                    hours=item.hours,
                )
                for item in normalized
            ]
        )
        result = self._session.execute(
            update(WorkPackage)
            .where(
                WorkPackage.id == work_package.id,
                WorkPackage.version == expected,
            )
            .values(
                weekly_load_origin=origin,
                version=WorkPackage.version + 1,
            )
        )
        if int(result.rowcount or 0) != 1:
            actual = self._session.scalar(
                select(WorkPackage.version).where(WorkPackage.id == work_package.id)
            )
            raise ApplicationConflictError(
                "Le WorkPackage a été modifié par une autre opération.",
                code="work_package_version_conflict",
                context={
                    "reference": _optional_text(work_package.legacy_effort_id)
                    or work_package.id,
                    "expected_version": expected,
                    "current_version": int(actual or current_version),
                },
            )

        self._session.flush()
        self._session.refresh(work_package)
        new_values = {
            **self._snapshot(work_package, project),
            "weekly_load_count": len(normalized),
            "weekly_load_hours": str(
                sum((item.hours for item in normalized), Decimal("0.00"))
            ),
            "weekly_loads": [
                {
                    "week_start": item.week_start.isoformat(),
                    "hours": str(item.hours),
                }
                for item in normalized
            ],
        }
        self._audit(
            work_package,
            action="REPLACE_WEEKLY_LOADS",
            old_values=old_values,
            new_values=new_values,
        )
        resource_class = (
            self._session.get(ResourceClassConfig, work_package.resource_class_code)
            if work_package.resource_class_code is not None
            else None
        )
        return self._read_model(work_package, project, task, resource_class)
