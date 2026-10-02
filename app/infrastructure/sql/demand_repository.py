from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
import json
import re
from typing import Any

from sqlalchemy import delete, func, or_, select, true, update
from sqlalchemy.orm import Session, aliased

from ...application.demand_completion import (
    DemandCompletionFacts,
    project_effective_demand_state,
)
from ...application.errors import ApplicationConflictError, ApplicationValidationError
from ...application.query_models import DemandCancellationMaterializationReadModel
from ...application.read_models import DemandLineReadModel, DemandReadModel
from ...application.repository_ports import DemandRepositoryPort
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import AssetAllocation, AssetRequirement
from .base import new_id, utc_now
from .request_version import acquire_request_aggregate_version
from .resource_class_models import ResourceClassConfig
from .models import (
    Competency,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestCompetency,
    WorkforceRequestHistory,
    WorkPackage,
)


_DEMAND_NUMBER_RE = re.compile(r"^DMO-(\d{4})-(\d+)$", re.IGNORECASE)
_UNSET = object()


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value_text = _text(value)
    return value_text or None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value).replace(",", "."))


class SqlDemandRepository(DemandRepositoryPort):
    """SQLAlchemy implementation of the workforce-demand persistence port.

    The repository participates in the caller's transaction. It flushes mutations so
    constraints fail inside the use case, but it never commits or rolls back itself.
    """

    def __init__(
        self,
        session: Session,
        *,
        actor_name: str = "",
        actor_user_id: str | None = None,
    ) -> None:
        self._session = session
        self._actor_name = _text(actor_name)
        self._actor_user_id = _optional_text(actor_user_id)

    def _acquire_request_version(
        self,
        request: WorkforceRequest,
        expected_version: int,
    ) -> int:
        return acquire_request_aggregate_version(
            self._session,
            request,
            expected_version,
        )

    def _read_model(
        self,
        request: WorkforceRequest,
        project: Project,
        work_package: WorkPackage | None,
        proposed_resource: Resource | None,
        competency_ids: tuple[str, ...] = (),
        lines: tuple[DemandLineReadModel, ...] = (),
        completion_facts: DemandCompletionFacts | None = None,
    ) -> DemandReadModel:
        model = DemandReadModel(
            # During the first SQL cutover the existing NoDemande is preserved in
            # legacy_demand_number. A dedicated business-number column can replace
            # this compatibility name later without changing the application port.
            number=_text(request.legacy_demand_number) or request.id,
            status=_text(request.status),
            approved_by_name=_optional_text(request.approved_by_name),
            approved_at=request.approved_at,
            approval_comment=_optional_text(request.approval_comment),
            created_at=request.created_at,
            cancellation_request_id=_optional_text(request.cancellation_request_id),
            cancellation_state=_optional_text(request.cancellation_state),
            cancellation_requested_by_user_id=_optional_text(
                request.cancellation_requested_by_user_id
            ),
            cancellation_requested_at=request.cancellation_requested_at,
            cancellation_reason=_optional_text(request.cancellation_reason),
            cancellation_resolved_by_user_id=_optional_text(
                request.cancellation_resolved_by_user_id
            ),
            cancellation_resolved_at=request.cancellation_resolved_at,
            cancellation_resolution_comment=_optional_text(
                request.cancellation_resolution_comment
            ),
            project_number=_optional_text(project.number),
            project_name=_optional_text(project.name),
            client=_optional_text(project.client),
            project_manager=_optional_text(project.project_manager_name),
            requester_user_id=_optional_text(request.requester_user_id),
            requester=_optional_text(request.requester_name),
            priority=_optional_text(request.priority),
            confirmation=_optional_text(request.confirmation),
            desired_start=request.desired_start,
            desired_end=request.desired_end,
            description=_optional_text(request.description),
            work_package_ref=(
                _optional_text(work_package.legacy_effort_id) or work_package.id
                if work_package is not None
                else None
            ),
            work_package_name=(
                _optional_text(work_package.name) if work_package is not None else None
            ),
            task_code=_optional_text(request.erp_task_code),
            task_label=_optional_text(request.erp_task_label),
            resource_count=max(int(request.resource_count or 1), 1),
            required_competencies=_optional_text(request.required_competencies),
            required_competency_ids=competency_ids,
            estimated_hours=(
                float(request.estimated_hours)
                if request.estimated_hours is not None
                else None
            ),
            estimated_days=(
                float(request.estimated_days)
                if request.estimated_days is not None
                else None
            ),
            proposed_resource=(
                _optional_text(proposed_resource.name)
                if proposed_resource is not None
                else None
            ),
            version=max(int(request.aggregate_version or 1), 1),
            line_mode=bool(request.line_mode),
            lines=lines,
        )
        return project_effective_demand_state(
            model,
            completion_facts or DemandCompletionFacts(),
        )

    def _aggregate_children(
        self,
        request_ids: Sequence[str],
    ) -> tuple[
        dict[str, tuple[str, ...]],
        dict[str, tuple[DemandLineReadModel, ...]],
        dict[str, tuple[int, int, int, int]],
        dict[str, DemandCompletionFacts],
    ]:
        """Load child projections plus lifecycle facts in one batch query."""

        identifiers = tuple(str(value) for value in request_ids if str(value))
        if not identifiers:
            return {}, {}, {}, {}

        request_competency = aliased(WorkforceRequestCompetency)
        line_competency = aliased(RequestLineCompetency)
        proposed_resource = aliased(Resource)

        human_shift_count = (
            select(func.count(Shift.id))
            .select_from(Shift)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .where(
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
                ResourceRequirement.origin == "REQUEST",
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        locked_human_shift_count = (
            select(func.count(Shift.id))
            .select_from(Shift)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .where(
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
                ResourceRequirement.origin == "REQUEST",
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                Shift.locked == true(),
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        asset_allocation_count = (
            select(func.count(AssetAllocation.id))
            .select_from(AssetAllocation)
            .join(
                AssetRequirement,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .where(
                AssetRequirement.workforce_request_id == WorkforceRequest.id,
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        locked_asset_allocation_count = (
            select(func.count(AssetAllocation.id))
            .select_from(AssetAllocation)
            .join(
                AssetRequirement,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .where(
                AssetRequirement.workforce_request_id == WorkforceRequest.id,
                AssetAllocation.locked == true(),
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )

        counted_shift_hours = (
            select(func.coalesce(func.sum(Shift.hours), 0))
            .where(
                Shift.resource_requirement_id == ResourceRequirement.id,
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
            )
            .correlate(ResourceRequirement)
            .scalar_subquery()
        )
        human_requirement_count = (
            select(func.count(ResourceRequirement.id))
            .where(
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
                ResourceRequirement.origin == "REQUEST",
                ResourceRequirement.status != "Annulé",
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        human_remaining_requirement_count = (
            select(func.count(ResourceRequirement.id))
            .where(
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
                ResourceRequirement.origin == "REQUEST",
                ResourceRequirement.status != "Annulé",
                ResourceRequirement.planned_hours > counted_shift_hours,
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        current_or_future_shift_count = (
            select(func.count(Shift.id))
            .select_from(Shift)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .where(
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
                ResourceRequirement.origin == "REQUEST",
                ResourceRequirement.status != "Annulé",
                Shift.work_date >= date.today(),
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        active_asset_requirement_count = (
            select(func.count(AssetRequirement.id))
            .where(
                AssetRequirement.workforce_request_id == WorkforceRequest.id,
                AssetRequirement.status != "Annulé",
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        covering_asset_allocation_count = (
            select(func.count(AssetAllocation.id))
            .where(
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
                AssetAllocation.start_date <= AssetRequirement.start_date,
                AssetAllocation.end_date >= AssetRequirement.end_date,
            )
            .correlate(AssetRequirement)
            .scalar_subquery()
        )
        uncovered_asset_requirement_count = (
            select(func.count(AssetRequirement.id))
            .where(
                AssetRequirement.workforce_request_id == WorkforceRequest.id,
                AssetRequirement.status != "Annulé",
                covering_asset_allocation_count == 0,
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )
        current_or_future_asset_allocation_count = (
            select(func.count(AssetAllocation.id))
            .select_from(AssetAllocation)
            .join(
                AssetRequirement,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .where(
                AssetRequirement.workforce_request_id == WorkforceRequest.id,
                AssetRequirement.status != "Annulé",
                AssetAllocation.end_date >= date.today(),
            )
            .correlate(WorkforceRequest)
            .scalar_subquery()
        )

        rows = self._session.execute(
            select(
                WorkforceRequest.id,
                RequestLine,
                WorkPackage,
                proposed_resource,
                request_competency.competency_id,
                line_competency.competency_id,
                human_shift_count,
                locked_human_shift_count,
                asset_allocation_count,
                locked_asset_allocation_count,
                human_requirement_count,
                human_remaining_requirement_count,
                current_or_future_shift_count,
                active_asset_requirement_count,
                uncovered_asset_requirement_count,
                current_or_future_asset_allocation_count,
            )
            .select_from(WorkforceRequest)
            .outerjoin(
                RequestLine,
                RequestLine.workforce_request_id == WorkforceRequest.id,
            )
            .outerjoin(
                WorkPackage,
                RequestLine.work_package_id == WorkPackage.id,
            )
            .outerjoin(
                proposed_resource,
                RequestLine.proposed_resource_id == proposed_resource.id,
            )
            .outerjoin(
                request_competency,
                request_competency.workforce_request_id == WorkforceRequest.id,
            )
            .outerjoin(
                line_competency,
                line_competency.request_line_id == RequestLine.id,
            )
            .where(WorkforceRequest.id.in_(identifiers))
            .order_by(
                WorkforceRequest.id,
                RequestLine.position,
                RequestLine.id,
                request_competency.competency_id,
                line_competency.competency_id,
            )
        ).all()

        request_competencies: dict[str, set[str]] = {
            identifier: set() for identifier in identifiers
        }
        line_competencies: dict[str, set[str]] = {}
        line_rows: dict[
            str,
            tuple[str, RequestLine, WorkPackage | None, Resource | None],
        ] = {}
        cancellation_counts: dict[str, tuple[int, int, int, int]] = {}
        completion_facts: dict[str, DemandCompletionFacts] = {}

        for (
            request_id,
            line,
            work_package,
            resource,
            request_competency_id,
            line_competency_id,
            human_count,
            locked_human_count,
            asset_count,
            locked_asset_count,
            human_requirement_total,
            human_remaining_total,
            current_or_future_shift_total,
            asset_requirement_total,
            uncovered_asset_requirement_total,
            current_or_future_asset_allocation_total,
        ) in rows:
            cancellation_counts.setdefault(
                request_id,
                (
                    int(human_count or 0),
                    int(locked_human_count or 0),
                    int(asset_count or 0),
                    int(locked_asset_count or 0),
                ),
            )
            completion_facts.setdefault(
                request_id,
                DemandCompletionFacts(
                    human_requirement_count=int(human_requirement_total or 0),
                    human_remaining_requirement_count=int(human_remaining_total or 0),
                    current_or_future_shift_count=int(
                        current_or_future_shift_total or 0
                    ),
                    asset_requirement_count=int(asset_requirement_total or 0),
                    asset_uncovered_requirement_count=int(
                        uncovered_asset_requirement_total or 0
                    ),
                    current_or_future_asset_allocation_count=int(
                        current_or_future_asset_allocation_total or 0
                    ),
                ),
            )
            if request_competency_id is not None:
                request_competencies.setdefault(request_id, set()).add(
                    request_competency_id
                )
            if line is None:
                continue
            line_rows.setdefault(
                line.id,
                (request_id, line, work_package, resource),
            )
            if line_competency_id is not None:
                line_competencies.setdefault(line.id, set()).add(line_competency_id)

        grouped_lines: dict[str, list[DemandLineReadModel]] = {
            identifier: [] for identifier in identifiers
        }
        ordered_line_rows = sorted(
            line_rows.values(),
            key=lambda row: (
                row[0],
                int(row[1].position or 0),
                row[1].id,
            ),
        )
        for request_id, line, work_package, resource in ordered_line_rows:
            grouped_lines.setdefault(request_id, []).append(
                DemandLineReadModel(
                    line_id=line.id,
                    position=int(line.position or 0),
                    kind=_text(line.kind) or "WORKFORCE",
                    slot_count=max(int(line.slot_count or 1), 1),
                    required_resource_class=_optional_text(line.required_resource_class),
                    required_competencies=_optional_text(
                        line.required_competencies_snapshot
                    ),
                    required_competency_ids=tuple(
                        sorted(line_competencies.get(line.id, set()))
                    ),
                    desired_start=line.desired_start,
                    desired_end=line.desired_end,
                    desired_active_days=(
                        int(line.desired_active_days)
                        if line.desired_active_days is not None
                        else None
                    ),
                    estimated_hours=(
                        float(line.estimated_hours)
                        if line.estimated_hours is not None
                        else None
                    ),
                    estimated_hours_source=_optional_text(line.estimated_hours_source),
                    default_hours_per_day=(
                        float(line.default_hours_per_day)
                        if line.default_hours_per_day is not None
                        else None
                    ),
                    confirmation=_text(line.confirmation) or "Confirmée",
                    work_package_ref=(
                        _optional_text(work_package.legacy_effort_id) or work_package.id
                        if work_package is not None
                        else None
                    ),
                    work_package_name=(
                        _optional_text(work_package.name)
                        if work_package is not None
                        else None
                    ),
                    task_code=_optional_text(line.erp_task_code),
                    task_label=_optional_text(line.erp_task_label),
                    proposed_resource_id=_optional_text(line.proposed_resource_id),
                    proposed_resource=(
                        _optional_text(resource.name) if resource is not None else None
                    ),
                    description=_optional_text(line.description),
                    active=bool(line.active),
                    asset_type_id=_optional_text(line.asset_type_id),
                    proposed_asset_id=_optional_text(line.proposed_asset_id),
                )
            )

        return (
            {
                request_id: tuple(sorted(values))
                for request_id, values in request_competencies.items()
            },
            {
                request_id: tuple(values)
                for request_id, values in grouped_lines.items()
            },
            cancellation_counts,
            completion_facts,
        )

    def _row_query(self):
        proposed_resource = aliased(Resource)
        return (
            select(WorkforceRequest, Project, WorkPackage, proposed_resource)
            .join(Project, WorkforceRequest.project_id == Project.id)
            .outerjoin(WorkPackage, WorkforceRequest.work_package_id == WorkPackage.id)
            .outerjoin(
                proposed_resource,
                WorkforceRequest.proposed_resource_id == proposed_resource.id,
            )
        )

    @staticmethod
    def _cancellation_materialization(
        request: WorkforceRequest,
        counts: tuple[int, int, int, int] | None,
    ) -> DemandCancellationMaterializationReadModel:
        human_count, locked_human_count, asset_count, locked_asset_count = (
            counts or (0, 0, 0, 0)
        )
        return DemandCancellationMaterializationReadModel(
            demand_number=_text(request.legacy_demand_number) or request.id,
            human_shift_count=human_count,
            locked_human_shift_count=locked_human_count,
            asset_allocation_count=asset_count,
            locked_asset_allocation_count=locked_asset_count,
        )

    def list_with_cancellation_materialization(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> Sequence[
        tuple[DemandReadModel, DemandCancellationMaterializationReadModel]
    ]:
        statement = self._row_query()
        scope_filters = []
        if project_ids is not None:
            project_identifiers = tuple(
                str(value) for value in project_ids if str(value)
            )
            if project_identifiers:
                scope_filters.append(
                    WorkforceRequest.project_id.in_(project_identifiers)
                )
        if demand_ids is not None:
            demand_identifiers = tuple(
                str(value) for value in demand_ids if str(value)
            )
            if demand_identifiers:
                scope_filters.append(
                    WorkforceRequest.id.in_(demand_identifiers)
                )
        if project_ids is not None or demand_ids is not None:
            if not scope_filters:
                return ()
            statement = statement.where(or_(*scope_filters))
        rows = self._session.execute(
            statement.order_by(
                WorkforceRequest.desired_start,
                WorkforceRequest.legacy_demand_number,
                WorkforceRequest.id,
            )
        ).all()
        request_ids = tuple(
            request.id for request, _project, _work_package, _resource in rows
        )
        (
            competency_ids,
            lines_by_request,
            cancellation_counts,
            completion_facts,
        ) = self._aggregate_children(request_ids)
        return tuple(
            (
                self._read_model(
                    request,
                    project,
                    work_package,
                    proposed_resource,
                    competency_ids.get(request.id, ()),
                    lines_by_request.get(request.id, ()),
                    completion_facts.get(request.id),
                ),
                self._cancellation_materialization(
                    request,
                    cancellation_counts.get(request.id),
                ),
            )
            for request, project, work_package, proposed_resource in rows
        )

    def list(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        demand_ids: Sequence[str] | None = None,
    ) -> Sequence[DemandReadModel]:
        return tuple(
            demand
            for demand, _materialization in self.list_with_cancellation_materialization(
                project_ids=project_ids,
                demand_ids=demand_ids,
            )
        )

    def get_with_cancellation_materialization(
        self,
        number: str,
    ) -> tuple[DemandReadModel, DemandCancellationMaterializationReadModel] | None:
        wanted = _text(number)
        if not wanted:
            return None
        row = self._session.execute(
            self._row_query().where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        ).one_or_none()
        if row is None:
            return None
        request, project, work_package, proposed_resource = row
        (
            competency_ids,
            lines_by_request,
            cancellation_counts,
            completion_facts,
        ) = self._aggregate_children((request.id,))
        return (
            self._read_model(
                request,
                project,
                work_package,
                proposed_resource,
                competency_ids.get(request.id, ()),
                lines_by_request.get(request.id, ()),
                completion_facts.get(request.id),
            ),
            self._cancellation_materialization(
                request,
                cancellation_counts.get(request.id),
            ),
        )

    def get(self, number: str) -> DemandReadModel | None:
        row = self.get_with_cancellation_materialization(number)
        return row[0] if row is not None else None

    def _request(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        return request

    def _request_fresh(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest)
            .where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
            .execution_options(populate_existing=True)
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        return request

    def _project(self, number: object) -> Project:
        project_number = _text(number)
        project = self._session.scalar(
            select(Project).where(Project.number == project_number)
        )
        if project is None:
            raise KeyError(f"Projet {project_number} introuvable")
        return project

    def _work_package(
        self,
        reference: object,
        *,
        project_id: str,
    ) -> WorkPackage | None:
        work_package_ref = _text(reference)
        if not work_package_ref:
            return None
        work_package = self._session.scalar(
            select(WorkPackage).where(
                (WorkPackage.id == work_package_ref)
                | (WorkPackage.legacy_effort_id == work_package_ref)
            )
        )
        if work_package is None:
            raise KeyError(f"Plage moyen terme {work_package_ref} introuvable")
        if work_package.project_id != project_id:
            raise ValueError(
                f"La plage moyen terme {work_package_ref} n'appartient pas au projet sélectionné."
            )
        return work_package

    def _guard_work_packages(
        self,
        work_packages: Sequence[WorkPackage | None],
        *,
        project_id: str | None = None,
    ) -> None:
        unique = {
            work_package.id: work_package
            for work_package in work_packages
            if work_package is not None
        }
        for identifier in sorted(unique):
            work_package = unique[identifier]
            result = self._session.execute(
                update(WorkPackage)
                .where(WorkPackage.id == identifier)
                .values(
                    version=WorkPackage.version,
                    updated_at=WorkPackage.updated_at,
                )
            )
            if int(result.rowcount or 0) != 1:
                raise KeyError(f"WorkPackage {identifier} introuvable")
            self._session.flush()
            self._session.refresh(work_package)
            if project_id is not None and work_package.project_id != project_id:
                raise ApplicationConflictError(
                    "Le WorkPackage a changé de projet pendant le rattachement.",
                    code="work_package_project_changed_during_link",
                    context={
                        "work_package_id": work_package.id,
                        "expected_project_id": project_id,
                        "current_project_id": work_package.project_id,
                    },
                )

    def _effective_task(
        self,
        work_package: WorkPackage | None,
        explicit_task: TaskCatalogEntry | None,
        *,
        project_number: str,
    ) -> TaskCatalogEntry | None:
        if work_package is None or not work_package.task_catalog_item_id:
            return explicit_task
        package_task = self._session.get(
            TaskCatalogEntry,
            work_package.task_catalog_item_id,
        )
        if package_task is None:
            raise ApplicationConflictError(
                "La tâche ERP classée du WorkPackage est introuvable.",
                code="work_package_task_reference_broken",
                context={"work_package_id": work_package.id},
            )
        if package_task.project_number != _text(project_number):
            raise ApplicationConflictError(
                "Le WorkPackage et sa tâche ERP ne correspondent plus au projet de la demande.",
                code="work_package_task_project_mismatch",
                context={
                    "work_package_id": work_package.id,
                    "task_catalog_item_id": package_task.id,
                    "task_project_number": package_task.project_number,
                    "request_project_number": _text(project_number),
                },
            )
        if explicit_task is not None and explicit_task.id != package_task.id:
            raise ApplicationValidationError(
                "La tâche ERP de la ligne contredit la tâche classée du WorkPackage.",
                code="request_line_work_package_task_conflict",
                context={
                    "work_package_id": work_package.id,
                    "work_package_task_catalog_item_id": package_task.id,
                    "request_task_catalog_item_id": explicit_task.id,
                },
            )
        return package_task

    def _task(self, code: object, *, project_number: str) -> TaskCatalogEntry | None:
        task_code = _text(code)
        if not task_code:
            return None
        task = self._session.scalar(
            select(TaskCatalogEntry).where(
                TaskCatalogEntry.project_number == _text(project_number),
                TaskCatalogEntry.task_code == task_code,
            )
        )
        if task is None:
            raise KeyError(
                f"Tâche ERP {_text(project_number)}/{task_code} introuvable"
            )
        if not task.active:
            raise ValueError(
                f"La tâche ERP {_text(project_number)}/{task_code} est inactive."
            )
        return task

    def _resource(self, name: object) -> Resource | None:
        resource_name = _text(name)
        if not resource_name:
            return None
        resource = self._session.scalar(
            select(Resource).where(Resource.name == resource_name)
        )
        if resource is None:
            raise KeyError(f"Ressource {resource_name} introuvable")
        return resource

    def _resource_by_id(self, identifier: object) -> Resource | None:
        resource_id = _text(identifier)
        if not resource_id:
            return None
        resource = self._session.get(Resource, resource_id)
        if resource is None:
            raise KeyError(f"Ressource {resource_id} introuvable")
        return resource

    def _competencies(self, identifiers: Sequence[str]) -> tuple[Competency, ...]:
        wanted = tuple(dict.fromkeys(_text(value) for value in identifiers if _text(value)))
        if not wanted:
            return ()
        rows = self._session.scalars(
            select(Competency).where(Competency.id.in_(wanted))
        ).all()
        by_id = {row.id: row for row in rows}
        missing = [identifier for identifier in wanted if identifier not in by_id]
        if missing:
            raise KeyError(f"Compétence {missing[0]} introuvable")
        inactive = [identifier for identifier in wanted if not by_id[identifier].active]
        if inactive:
            raise ValueError(f"Compétence {inactive[0]} inactive")
        return tuple(by_id[identifier] for identifier in wanted)

    def _replace_request_lines(
        self,
        request: WorkforceRequest,
        project: Project,
        values: Sequence[Mapping[str, Any]],
    ) -> tuple[RequestLine, ...]:
        existing = {
            line.id: line
            for line in self._session.scalars(
                select(RequestLine).where(
                    RequestLine.workforce_request_id == request.id
                )
            ).all()
        }
        seen: set[str] = set()
        active_lines: list[RequestLine] = []
        resolved_work_packages = [
            self._work_package(
                raw.get("work_package_ref"),
                project_id=project.id,
            )
            for raw in values
        ]
        self._guard_work_packages(
            resolved_work_packages,
            project_id=project.id,
        )

        for index, raw in enumerate(values):
            supplied_id = _optional_text(raw.get("id"))
            line_id = supplied_id or new_id()
            if line_id in seen:
                raise ValueError(f"Identifiant de ligne dupliqué: {line_id}")
            seen.add(line_id)

            line = existing.get(line_id)
            if line is None:
                foreign = self._session.get(RequestLine, line_id)
                if foreign is not None:
                    raise ValueError(
                        f"La ligne {line_id} appartient à une autre demande."
                    )
                line = RequestLine(
                    id=line_id,
                    workforce_request_id=request.id,
                    position=index,
                    kind="WORKFORCE",
                )
                self._session.add(line)
            elif line.workforce_request_id != request.id:
                raise ValueError(
                    f"La ligne {line_id} appartient à une autre demande."
                )

            kind = _text(raw.get("kind")) or "WORKFORCE"
            if kind not in {"WORKFORCE", "ASSET"}:
                raise ValueError("Type de ligne non supporté.")
            work_package = resolved_work_packages[index]
            explicit_task = self._task(
                raw.get("task_code"),
                project_number=project.number,
            )
            task = self._effective_task(
                work_package,
                explicit_task,
                project_number=project.number,
            )
            proposed = self._resource_by_id(raw.get("proposed_resource_id"))
            asset_type_id = _optional_text(raw.get("asset_type_id"))
            proposed_asset_id = _optional_text(raw.get("proposed_asset_id"))
            if kind == "ASSET":
                from .asset_models import Asset, AssetType
                asset_type = self._session.get(AssetType, asset_type_id) if asset_type_id else None
                if asset_type is None or not asset_type.active:
                    raise ValueError("Le type d'actif est introuvable ou inactif.")
                asset = self._session.get(Asset, proposed_asset_id) if proposed_asset_id else None
                if proposed_asset_id and (asset is None or not asset.active or asset.asset_type_id != asset_type.id):
                    raise ValueError("L'actif proposé est incompatible ou inactif.")
            competencies = self._competencies(
                tuple(raw.get("required_competency_ids") or ())
            )

            line.position = int(raw.get("position") if raw.get("position") is not None else index)
            line.kind = kind
            line.slot_count = 1
            line.required_resource_class = _optional_text(
                raw.get("required_resource_class")
            )
            line.required_competencies_snapshot = _optional_text(
                raw.get("required_competencies")
            )
            line.desired_start = raw.get("desired_start")
            line.desired_end = raw.get("desired_end")
            line.desired_active_days = _decimal(raw.get("desired_active_days"))
            line.estimated_hours = _decimal(raw.get("estimated_hours"))
            line.estimated_hours_source = _optional_text(
                raw.get("estimated_hours_source")
            )
            line.default_hours_per_day = _decimal(raw.get("default_hours_per_day"))
            line.confirmation = _text(raw.get("confirmation")) or "Confirmée"
            line.work_package_id = work_package.id if work_package is not None else None
            line.task_catalog_item_id = task.id if task is not None else None
            line.erp_task_code = task.task_code if task is not None else None
            line.erp_task_label = task.label if task is not None else None
            line.proposed_resource_id = proposed.id if proposed is not None else None
            line.asset_type_id = asset_type_id if kind == "ASSET" else None
            line.proposed_asset_id = proposed_asset_id if kind == "ASSET" else None
            line.description = _optional_text(raw.get("description"))
            line.active = True

            self._session.execute(
                delete(RequestLineCompetency).where(
                    RequestLineCompetency.request_line_id == line.id
                )
            )
            self._session.add_all(
                [
                    RequestLineCompetency(
                        request_line_id=line.id,
                        competency_id=competency.id,
                    )
                    for competency in competencies
                ]
            )
            active_lines.append(line)

        for line_id, line in existing.items():
            if line_id not in seen:
                line.active = False

        self._session.execute(
            delete(WorkforceRequestCompetency).where(
                WorkforceRequestCompetency.workforce_request_id == request.id
            )
        )
        self._session.flush()
        self._sync_flat_summary_from_lines(request, active_lines)
        return tuple(active_lines)

    @staticmethod
    def _same_or_none(values: Sequence[object]) -> object | None:
        unique = {value for value in values}
        return next(iter(unique)) if len(unique) == 1 else None

    def _sync_flat_summary_from_lines(
        self,
        request: WorkforceRequest,
        lines: Sequence[RequestLine],
    ) -> None:
        if not lines:
            return
        starts = [line.desired_start for line in lines if line.desired_start is not None]
        ends = [
            line.desired_end or line.desired_start
            for line in lines
            if line.desired_start is not None
        ]
        request.desired_start = min(starts) if starts else None
        request.desired_end = max(ends) if ends else None
        workforce_lines = [line for line in lines if line.kind == "WORKFORCE"]
        request.resource_count = max(
            sum(max(int(line.slot_count or 1), 1) for line in workforce_lines), 1
        )
        request.estimated_hours = (
            sum((line.estimated_hours or Decimal("0")) for line in workforce_lines)
            if workforce_lines and all(line.estimated_hours is not None for line in workforce_lines)
            else None
        )
        request.estimated_days = (
            sum((line.desired_active_days or Decimal("0")) for line in workforce_lines)
            if workforce_lines and all(line.desired_active_days is not None for line in workforce_lines)
            else None
        )
        snapshots = tuple(
            dict.fromkeys(
                _text(line.required_competencies_snapshot)
                for line in lines
                if _text(line.required_competencies_snapshot)
            )
        )
        request.required_competencies = "; ".join(snapshots) or None
        confirmations = [line.confirmation for line in lines]
        request.confirmation = (
            str(self._same_or_none(confirmations) or "Tentative")
        )
        request.work_package_id = self._same_or_none(
            [line.work_package_id for line in lines]
        )
        task_code = self._same_or_none([line.erp_task_code for line in lines])
        request.erp_task_code = str(task_code) if task_code is not None else None
        task_label = self._same_or_none([line.erp_task_label for line in lines])
        request.erp_task_label = str(task_label) if task_label is not None else None
        request.proposed_resource_id = (
            lines[0].proposed_resource_id if len(lines) == 1 else None
        )

    def _sync_legacy_request_line(
        self,
        request: WorkforceRequest,
        *,
        hours_source: str | None = None,
        required_resource_class: object = _UNSET,
    ) -> RequestLine:
        """Mirror the current flat request into its transitional single line.

        288B does not expose multi-line writes yet. This keeps requests created or
        edited between the schema rollout and 288C coherent without making the line
        authoritative prematurely.
        """

        line = self._session.get(RequestLine, request.id)
        if line is None:
            line = RequestLine(
                id=request.id,
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
            )
            self._session.add(line)

        project = self._session.get(Project, request.project_id)
        work_package = (
            self._session.get(WorkPackage, request.work_package_id)
            if request.work_package_id
            else None
        )
        self._guard_work_packages(
            (work_package,),
            project_id=request.project_id,
        )
        explicit_task = (
            self._task(request.erp_task_code, project_number=project.number)
            if project is not None and request.erp_task_code
            else None
        )
        task = (
            self._effective_task(
                work_package,
                explicit_task,
                project_number=project.number,
            )
            if project is not None
            else explicit_task
        )

        line.slot_count = max(int(request.resource_count or 1), 1)
        if required_resource_class is not _UNSET:
            class_code = _optional_text(required_resource_class)
            if class_code is None:
                line.required_resource_class = None
            else:
                resource_class = self._session.get(ResourceClassConfig, class_code)
                if resource_class is None:
                    raise ApplicationValidationError(
                        "La classe de ressource sélectionnée n'existe pas.",
                        code="demand_resource_class_not_found",
                        context={"required_resource_class": class_code},
                    )
                if not resource_class.active:
                    raise ApplicationValidationError(
                        "La classe de ressource sélectionnée est inactive.",
                        code="demand_resource_class_inactive",
                        context={"required_resource_class": class_code},
                    )
                line.required_resource_class = resource_class.code
        line.required_competencies_snapshot = request.required_competencies
        line.desired_start = request.desired_start
        line.desired_end = request.desired_end
        line.desired_active_days = request.estimated_days
        line.estimated_hours = request.estimated_hours
        if hours_source:
            line.estimated_hours_source = _text(hours_source)
            line.default_hours_per_day = (
                Decimal("8.00")
                if _text(hours_source) == "DEFAULT_8H"
                else None
            )
        elif line.estimated_hours_source is None and request.estimated_hours is not None:
            line.estimated_hours_source = "LEGACY"
        line.confirmation = request.confirmation
        line.work_package_id = request.work_package_id
        line.task_catalog_item_id = task.id if task is not None else None
        line.erp_task_code = task.task_code if task is not None else request.erp_task_code
        line.erp_task_label = task.label if task is not None else request.erp_task_label
        line.proposed_resource_id = request.proposed_resource_id
        line.description = request.description
        line.active = True
        return line

    def _next_request_number(self) -> str:
        year = date.today().year
        prefix = f"DMO-{year}-"
        numbers = self._session.scalars(
            select(WorkforceRequest.legacy_demand_number).where(
                WorkforceRequest.legacy_demand_number.like(f"{prefix}%")
            )
        ).all()
        max_sequence = 0
        for number in numbers:
            match = _DEMAND_NUMBER_RE.match(_text(number))
            if match and int(match.group(1)) == year:
                max_sequence = max(max_sequence, int(match.group(2)))
        return f"DMO-{year}-{max_sequence + 1:04d}"

    def create(self, values: Mapping[str, Any], *, submit: bool = False) -> str:
        project = self._project(values.get("NumeroProjet"))
        work_package = self._work_package(
            values.get("SourceEffortID"),
            project_id=project.id,
        )
        self._guard_work_packages(
            (work_package,),
            project_id=project.id,
        )
        explicit_task = self._task(
            values.get("TaskCode"),
            project_number=project.number,
        )
        task = self._effective_task(
            work_package,
            explicit_task,
            project_number=project.number,
        )
        proposed_resource = self._resource(values.get("TechnicienPropose"))
        number = self._next_request_number()
        status = "Soumise" if submit else "Brouillon"

        request = WorkforceRequest(
            legacy_demand_number=number,
            project_id=project.id,
            work_package_id=work_package.id if work_package is not None else None,
            erp_task_code=task.task_code if task is not None else None,
            erp_task_label=task.label if task is not None else None,
            requester_user_id=_optional_text(values.get("RequesterUserId")),
            # Canonical Web callers provide RequesterUserId and a server-derived
            # display snapshot. Legacy callers may still provide only Demandeur.
            # preserve the historical behavior and default to the authenticated actor.
            requester_name=_optional_text(values.get("Demandeur")) or self._actor_name or None,
            request_type=_text(values.get("TypeDemande")) or "Projet",
            priority=_text(values.get("Priorite")) or "Normale",
            confirmation=_text(values.get("Confirmation")) or "Confirmée",
            desired_start=values.get("DateDebutSouhaitee"),
            desired_end=values.get("DateFinSouhaitee"),
            description=_optional_text(values.get("Description")),
            site_client=_optional_text(values.get("SiteClient")),
            location=_optional_text(values.get("Lieu")),
            resource_count=int(values.get("NombreRessources") or 1),
            required_competencies=_optional_text(values.get("CompetencesRequises")),
            estimated_hours=_decimal(values.get("TempsEstimeHeures")),
            estimated_days=_decimal(values.get("TempsEstimeJours")),
            proposed_resource_id=proposed_resource.id if proposed_resource else None,
            status=status,
        )
        self._session.add(request)
        self._session.flush()
        request_lines = values.get("RequestLines")
        if request_lines is not None:
            request.line_mode = True
            self._replace_request_lines(
                request,
                project,
                tuple(request_lines),
            )
        else:
            self._sync_legacy_request_line(
                request,
                hours_source=_optional_text(values.get("RequestLineHoursSource")),
                required_resource_class=values.get("RequiredResourceClass"),
            )
        self._session.flush()
        self._append_history(
            request,
            action="Création",
            comment="Demande créée et soumise" if submit else "Demande créée",
            changed_fields=("create",),
        )
        self._session.flush()
        return number

    def request_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        reason: str,
        expected_version: int,
    ) -> None:
        request = self._request(number)
        previous_status = request.status
        self._acquire_request_version(request, expected_version)
        occurred_at = utc_now()
        request.cancellation_request_id = _text(cancellation_request_id)
        request.cancellation_state = "PENDING"
        request.cancellation_requested_by_user_id = self._actor_user_id
        request.cancellation_requested_at = occurred_at
        request.cancellation_reason = _text(reason)
        request.cancellation_resolved_by_user_id = None
        request.cancellation_resolved_at = None
        request.cancellation_resolution_comment = None
        self._session.flush()
        self._append_history(
            request,
            action="Demande d'annulation",
            comment=_text(reason),
            previous_status=previous_status,
            changed_fields=(
                "cancellation_request_id",
                "cancellation_state",
                "cancellation_requested_by_user_id",
                "cancellation_requested_at",
                "cancellation_reason",
            ),
            extra_details={
                "cancellation_request_id": request.cancellation_request_id,
                "cancellation_state": request.cancellation_state,
                "requested_by_user_id": request.cancellation_requested_by_user_id,
                "requested_at": occurred_at.isoformat(),
                "reason": request.cancellation_reason,
            },
        )
        self._session.flush()

    def reject_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        comment: str,
        expected_version: int,
    ) -> None:
        request = self._request(number)
        previous_status = request.status
        wanted_cycle = _text(cancellation_request_id)
        if request.cancellation_state != "PENDING" or request.cancellation_request_id != wanted_cycle:
            raise ApplicationConflictError(
                "La demande d'annulation à résoudre n'est plus active.",
                code="cancellation_cycle_conflict",
                context={
                    "demand_number": _text(request.legacy_demand_number) or request.id,
                    "expected_cancellation_request_id": wanted_cycle,
                    "current_cancellation_request_id": request.cancellation_request_id,
                    "cancellation_state": request.cancellation_state,
                },
            )
        self._acquire_request_version(request, expected_version)
        occurred_at = utc_now()
        request.cancellation_state = "REJECTED"
        request.cancellation_resolved_by_user_id = self._actor_user_id
        request.cancellation_resolved_at = occurred_at
        request.cancellation_resolution_comment = _text(comment)
        self._session.flush()
        self._append_history(
            request,
            action="Refus d'annulation",
            comment=_text(comment),
            previous_status=previous_status,
            changed_fields=(
                "cancellation_state",
                "cancellation_resolved_by_user_id",
                "cancellation_resolved_at",
                "cancellation_resolution_comment",
            ),
            extra_details={
                "cancellation_request_id": request.cancellation_request_id,
                "cancellation_state": request.cancellation_state,
                "requested_by_user_id": request.cancellation_requested_by_user_id,
                "requested_at": (
                    request.cancellation_requested_at.isoformat()
                    if request.cancellation_requested_at is not None
                    else None
                ),
                "reason": request.cancellation_reason,
                "resolved_by_user_id": request.cancellation_resolved_by_user_id,
                "resolved_at": occurred_at.isoformat(),
                "resolution_comment": request.cancellation_resolution_comment,
                "resolution": "REJECTED",
            },
        )
        self._session.flush()

    def accept_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        comment: str,
        expected_version: int,
        planning_version: int,
        correlation_id: str,
    ) -> Mapping[str, Any]:
        # Called only after ADR-006 planning_version acquisition. Re-read the request
        # from SQL so the request CAS and cleanup decisions are based on guarded state.
        request = self._request_fresh(number)
        previous_status = request.status
        wanted_cycle = _text(cancellation_request_id)
        if (
            request.cancellation_state != "PENDING"
            or request.cancellation_request_id != wanted_cycle
        ):
            raise ApplicationConflictError(
                "La demande d'annulation à résoudre n'est plus active.",
                code="cancellation_cycle_conflict",
                context={
                    "demand_number": _text(request.legacy_demand_number) or request.id,
                    "expected_cancellation_request_id": wanted_cycle,
                    "current_cancellation_request_id": request.cancellation_request_id,
                    "cancellation_state": request.cancellation_state,
                },
            )

        request_version = self._acquire_request_version(request, expected_version)

        workforce_requirements = list(
            self._session.scalars(
                select(ResourceRequirement)
                .where(ResourceRequirement.workforce_request_id == request.id)
                .order_by(ResourceRequirement.id)
            ).all()
        )
        workforce_requirement_ids = tuple(row.id for row in workforce_requirements)
        human_shifts = (
            list(
                self._session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id.in_(workforce_requirement_ids))
                    .order_by(Shift.id)
                ).all()
            )
            if workforce_requirement_ids
            else []
        )

        request_asset_requirements = list(
            self._session.scalars(
                select(AssetRequirement)
                .where(
                    AssetRequirement.origin
                    == AssetRequirementOrigin.REQUEST.value,
                    AssetRequirement.workforce_request_id == request.id,
                )
                .order_by(AssetRequirement.id)
            ).all()
        )
        human_shift_ids = tuple(row.id for row in human_shifts)
        ad_hoc_asset_requirements = (
            list(
                self._session.scalars(
                    select(AssetRequirement)
                    .where(
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        AssetRequirement.shift_id.in_(human_shift_ids),
                    )
                    .order_by(AssetRequirement.id)
                ).all()
            )
            if human_shift_ids
            else []
        )
        asset_requirements = [
            *request_asset_requirements,
            *ad_hoc_asset_requirements,
        ]
        asset_requirement_ids = tuple(dict.fromkeys(row.id for row in asset_requirements))
        asset_allocations = (
            list(
                self._session.scalars(
                    select(AssetAllocation)
                    .where(AssetAllocation.asset_requirement_id.in_(asset_requirement_ids))
                    .order_by(AssetAllocation.id)
                ).all()
            )
            if asset_requirement_ids
            else []
        )

        asset_allocation_ids = tuple(dict.fromkeys(row.id for row in asset_allocations))
        ad_hoc_asset_requirement_ids = tuple(
            row.id for row in ad_hoc_asset_requirements
        )
        locked_human = sum(1 for row in human_shifts if bool(row.locked))
        locked_assets = sum(1 for row in asset_allocations if bool(row.locked))

        # ADR-009 explicitly authorizes deletion of locked decisions inside this
        # request-owned cancellation scope.  ADR-016 adds a second ownership path:
        # allocations first, then SHIFT_AD_HOC requirements, then their owning Shift.
        # No cascade/rebuild is used as cleanup authority.
        for row in asset_allocations:
            self._session.delete(row)
        if asset_allocations:
            self._session.flush()
        for row in ad_hoc_asset_requirements:
            self._session.delete(row)
        if ad_hoc_asset_requirements:
            self._session.flush()
        for row in human_shifts:
            self._session.delete(row)
        if human_shifts:
            self._session.flush()

        cancelled_workforce = 0
        for row in workforce_requirements:
            if row.status != "Terminé":
                row.status = "Annulé"
                cancelled_workforce += 1

        cancelled_assets = 0
        for row in request_asset_requirements:
            if row.status != "Terminé":
                row.status = "Annulé"
                cancelled_assets += 1

        occurred_at = utc_now()
        request.status = "Annulée"
        request.cancellation_state = "ACCEPTED"
        request.cancellation_resolved_by_user_id = self._actor_user_id
        request.cancellation_resolved_at = occurred_at
        request.cancellation_resolution_comment = _text(comment)
        self._session.flush()

        self._append_history(
            request,
            action="Acceptation d'annulation",
            comment=_text(comment),
            previous_status=previous_status,
            changed_fields=(
                "status",
                "cancellation_state",
                "cancellation_resolved_by_user_id",
                "cancellation_resolved_at",
                "cancellation_resolution_comment",
            ),
            extra_details={
                "cancellation_request_id": request.cancellation_request_id,
                "cancellation_state": request.cancellation_state,
                "resolution": "ACCEPTED",
                "correlation_id": _text(correlation_id),
                "planning_version": int(planning_version),
                "request_version": int(request_version),
                "requested_by_user_id": request.cancellation_requested_by_user_id,
                "requested_at": (
                    request.cancellation_requested_at.isoformat()
                    if request.cancellation_requested_at is not None
                    else None
                ),
                "reason": request.cancellation_reason,
                "resolved_by_user_id": request.cancellation_resolved_by_user_id,
                "resolved_at": occurred_at.isoformat(),
                "resolution_comment": request.cancellation_resolution_comment,
                "deleted_human_shift_ids": list(human_shift_ids),
                "deleted_asset_allocation_ids": list(asset_allocation_ids),
                "deleted_ad_hoc_asset_requirement_ids": list(
                    ad_hoc_asset_requirement_ids
                ),
                "workforce_requirement_ids": list(workforce_requirement_ids),
                "asset_requirement_ids": list(asset_requirement_ids),
                "released_locked_human_shifts": locked_human,
                "released_locked_asset_allocations": locked_assets,
            },
        )
        self._session.flush()
        return {
            "request_version": int(request_version),
            "deleted_human_shifts": len(human_shifts),
            "deleted_asset_allocations": len(asset_allocations),
            "cancelled_workforce_requirements": cancelled_workforce,
            "cancelled_asset_requirements": cancelled_assets,
            "deleted_ad_hoc_asset_requirements": len(
                ad_hoc_asset_requirements
            ),
            "released_locked_human_shifts": locked_human,
            "released_locked_asset_allocations": locked_assets,
        }

    def extend_candidate_window(
        self,
        number: str,
        target_day: date,
        *,
        request_line_id: str | None = None,
        expected_version: int,
        action: str,
        comment: str = "",
    ) -> bool:
        request = self._request(number)
        current_version = int(request.aggregate_version or 1)
        if int(expected_version) != current_version:
            raise ApplicationConflictError(
                "La demande a été modifiée depuis sa lecture.",
                code="demand_version_conflict",
                context={
                    "demand_number": _text(request.legacy_demand_number) or request.id,
                    "expected_version": int(expected_version),
                    "current_version": current_version,
                },
            )

        previous_status = request.status
        changed_fields: tuple[str, ...]
        if bool(request.line_mode):
            wanted = _text(request_line_id)
            if not wanted:
                raise KeyError("La ligne candidate est requise.")
            line = self._session.scalar(
                select(RequestLine).where(
                    RequestLine.id == wanted,
                    RequestLine.workforce_request_id == request.id,
                    RequestLine.active == true(),
                )
            )
            if line is None or line.desired_start is None:
                raise KeyError(f"Ligne {wanted} introuvable pour la demande.")
            proposed_start = min(line.desired_start, target_day)
            proposed_end = max(line.desired_end or line.desired_start, target_day)
            if proposed_start == line.desired_start and proposed_end == line.desired_end:
                return False
            self._acquire_request_version(request, expected_version)
            line.desired_start = proposed_start
            line.desired_end = proposed_end
            active_lines = tuple(
                self._session.scalars(
                    select(RequestLine)
                    .where(
                        RequestLine.workforce_request_id == request.id,
                        RequestLine.active == true(),
                    )
                    .order_by(RequestLine.position, RequestLine.id)
                ).all()
            )
            self._sync_flat_summary_from_lines(request, active_lines)
            changed_fields = (f"RequestLine[{wanted}].DateWindow",)
        else:
            if request.desired_start is None:
                raise KeyError("La fenêtre candidate de la demande est incomplète.")
            proposed_start = min(request.desired_start, target_day)
            proposed_end = max(request.desired_end or request.desired_start, target_day)
            if proposed_start == request.desired_start and proposed_end == request.desired_end:
                return False
            self._acquire_request_version(request, expected_version)
            request.desired_start = proposed_start
            request.desired_end = proposed_end
            self._sync_legacy_request_line(request)
            changed_fields = ("DateDebutSouhaitee", "DateFinSouhaitee")

        self._session.flush()
        self._append_history(
            request,
            action=_text(action) or "Modification",
            comment=_text(comment),
            previous_status=previous_status,
            changed_fields=changed_fields,
        )
        self._session.flush()
        return True

    def update(
        self,
        number: str,
        updates: Mapping[str, Any],
        *,
        action: str,
        comment: str = "",
    ) -> None:
        request = self._request(number)
        previous_status = request.status
        request_lines = updates.get("RequestLines")
        expected_version = updates.get("ExpectedVersion")
        guarded_version = expected_version is not None
        if expected_version is not None:
            self._acquire_request_version(request, int(expected_version))

        line_owned_flat_fields = {
            "SourceEffortID",
            "TaskCode",
            "Confirmation",
            "DateDebutSouhaitee",
            "DateFinSouhaitee",
            "NombreRessources",
            "CompetencesRequises",
            "RequiredResourceClass",
            "TempsEstimeHeures",
            "TempsEstimeJours",
            "TechnicienPropose",
        }
        if (
            request_lines is None
            and bool(request.line_mode)
            and line_owned_flat_fields.intersection(updates)
        ):
            raise ValueError(
                "Les champs de besoin plats ne peuvent pas modifier une demande gérée par lignes."
            )

        project_changed = "NumeroProjet" in updates
        if project_changed and request_lines is None and bool(request.line_mode):
            raise ValueError(
                "Le changement de projet d'une demande gérée par lignes doit fournir lines."
            )
        if project_changed:
            request.project_id = self._project(updates.get("NumeroProjet")).id

        project = self._session.get(Project, request.project_id)
        if project is None:
            raise KeyError("Projet de la demande introuvable")

        work_package = None
        if "SourceEffortID" in updates:
            work_package = self._work_package(
                updates.get("SourceEffortID"),
                project_id=request.project_id,
            )
            request.work_package_id = (
                work_package.id if work_package is not None else None
            )
        elif request.work_package_id:
            candidate = self._session.get(WorkPackage, request.work_package_id)
            if candidate is not None and candidate.project_id == request.project_id:
                work_package = candidate
            elif project_changed:
                request.work_package_id = None

        self._guard_work_packages(
            (work_package,),
            project_id=request.project_id,
        )
        explicit_task = None
        if "TaskCode" in updates:
            explicit_task = self._task(
                updates.get("TaskCode"),
                project_number=project.number,
            )
        elif not project_changed and request.erp_task_code:
            explicit_task = self._task(
                request.erp_task_code,
                project_number=project.number,
            )
        task = self._effective_task(
            work_package,
            explicit_task,
            project_number=project.number,
        )
        if task is not None:
            request.erp_task_code = task.task_code
            request.erp_task_label = task.label
        elif "TaskCode" in updates or project_changed:
            request.erp_task_code = None
            request.erp_task_label = None

        if "RequesterUserId" in updates:
            request.requester_user_id = _optional_text(updates.get("RequesterUserId"))
        if "Demandeur" in updates:
            request.requester_name = _optional_text(updates.get("Demandeur"))
        if "TypeDemande" in updates:
            request.request_type = _text(updates.get("TypeDemande")) or "Projet"
        if "Priorite" in updates:
            request.priority = _text(updates.get("Priorite")) or "Normale"
        if "Confirmation" in updates:
            request.confirmation = _text(updates.get("Confirmation")) or "Confirmée"
        if "DateDebutSouhaitee" in updates:
            request.desired_start = updates.get("DateDebutSouhaitee")
        if "DateFinSouhaitee" in updates:
            request.desired_end = updates.get("DateFinSouhaitee")
        if "Description" in updates:
            request.description = _optional_text(updates.get("Description"))
        if "SiteClient" in updates:
            request.site_client = _optional_text(updates.get("SiteClient"))
        if "Lieu" in updates:
            request.location = _optional_text(updates.get("Lieu"))
        if "NombreRessources" in updates:
            request.resource_count = int(updates.get("NombreRessources") or 1)
        if "CompetencesRequises" in updates:
            request.required_competencies = _optional_text(
                updates.get("CompetencesRequises")
            )
        if "TempsEstimeHeures" in updates:
            request.estimated_hours = _decimal(updates.get("TempsEstimeHeures"))
        if "TempsEstimeJours" in updates:
            request.estimated_days = _decimal(updates.get("TempsEstimeJours"))
        if "TechnicienPropose" in updates:
            proposed = self._resource(updates.get("TechnicienPropose"))
            request.proposed_resource_id = proposed.id if proposed else None
        if "Statut" in updates:
            request.status = _text(updates.get("Statut"))
        if "ApprouvePar" in updates:
            request.approved_by_name = _optional_text(updates.get("ApprouvePar"))
        if "DateApprobation" in updates:
            approved_at = updates.get("DateApprobation")
            request.approved_at = approved_at if isinstance(approved_at, datetime) else None
        if "CommentaireApprobation" in updates:
            request.approval_comment = _optional_text(
                updates.get("CommentaireApprobation")
            )

        # NomProjet/Client/ChargeProjet intentionally remain project-owned. They are
        # projected from projects and will ultimately be mastered by Acumatica.
        if request_lines is not None:
            request.line_mode = True
            project = self._session.get(Project, request.project_id)
            if project is None:
                raise KeyError("Projet de la demande introuvable")
            self._replace_request_lines(request, project, tuple(request_lines))
        elif not bool(request.line_mode):
            self._sync_legacy_request_line(
                request,
                hours_source=_optional_text(updates.get("RequestLineHoursSource")),
                required_resource_class=(
                    updates["RequiredResourceClass"]
                    if "RequiredResourceClass" in updates
                    else _UNSET
                ),
            )
        if not guarded_version:
            request.aggregate_version = int(request.aggregate_version or 1) + 1
        self._session.flush()
        self._append_history(
            request,
            action=_text(action) or "Modification",
            comment=_text(comment),
            previous_status=previous_status,
            changed_fields=tuple(
                sorted(
                    str(field)
                    for field in updates
                    if str(field) != "ExpectedVersion"
                )
            ),
        )
        self._session.flush()

    def _append_history(
        self,
        request: WorkforceRequest,
        *,
        action: str,
        comment: str,
        previous_status: str | None = None,
        changed_fields: tuple[str, ...] = (),
        extra_details: Mapping[str, Any] | None = None,
    ) -> None:
        detail_payload: dict[str, Any] = {
            "aggregate_version": int(request.aggregate_version or 1),
            "line_mode": bool(request.line_mode),
            "changed_fields": list(dict.fromkeys(changed_fields)),
        }
        if extra_details:
            detail_payload.update(dict(extra_details))
        details = json.dumps(
            detail_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=action,
                previous_status=previous_status,
                status=request.status,
                comment=comment or None,
                details=details,
                actor_user_id=self._actor_user_id,
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )
