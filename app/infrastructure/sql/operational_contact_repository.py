from __future__ import annotations

from collections.abc import Sequence
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.operational_contacts import (
    MaterializedContactContext,
    OperationalContactRepositoryPort,
    RequestLineContactContext,
)
from ...application.project_managers import (
    EffectiveProjectManager,
    ProjectManagerResolutionService,
)
from ...domain.operational_contacts import (
    BusinessContactSnapshot,
    ContactCandidate,
    DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
    DIAGNOSTIC_APPROVED_TASK_PROJECT_MISMATCH,
    DIAGNOSTIC_APPROVED_TASK_REFERENCE_INVALID,
    DIAGNOSTIC_RESOURCE_INACTIVE,
    DIAGNOSTIC_RESOURCE_REFERENCE_INVALID,
    DIAGNOSTIC_TASK_INACTIVE,
    DIAGNOSTIC_TASK_PROJECT_MISMATCH,
    DIAGNOSTIC_TASK_REFERENCE_INVALID,
    DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE,
    DIAGNOSTIC_TASK_REFERENCE_UNRESOLVED,
    SOURCE_NONE,
    SOURCE_PROJECT_MANAGER,
    SOURCE_PROJECT_OVERRIDE,
    SOURCE_REQUEST_OVERRIDE,
    SOURCE_REQUIREMENT_OVERRIDE,
    SOURCE_RESOURCE_COORDINATOR,
    SOURCE_SHIFT_OVERRIDE,
    SOURCE_TASK_COORDINATOR,
    SOURCE_TASK_RESPONSIBLE,
    STATUS_UNRESOLVED,
)
from .business_contact_models import BusinessContact
from .project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)
from .models import (
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _snapshot(row: BusinessContact | None) -> BusinessContactSnapshot | None:
    if row is None:
        return None
    return BusinessContactSnapshot(
        contact_id=row.id,
        display_name=row.display_name,
        email=row.email,
        phone=row.phone,
        active=bool(row.active),
    )


class SqlOperationalContactRepository(OperationalContactRepositoryPort):
    """Load current RequestLine contact inputs without embedding fallback rules."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._contact_cache: dict[str, BusinessContact] = {}
        self._missing_contact_ids: set[str] = set()

    def _contacts(self, contact_ids: tuple[str | None, ...]) -> dict[str, BusinessContact]:
        wanted = tuple(
            dict.fromkeys(
                contact_id
                for contact_id in contact_ids
                if contact_id is not None and str(contact_id).strip()
            )
        )
        if not wanted:
            return {}
        missing = tuple(
            contact_id
            for contact_id in wanted
            if contact_id not in self._contact_cache
            and contact_id not in self._missing_contact_ids
        )
        if missing:
            rows = self._session.scalars(
                select(BusinessContact).where(BusinessContact.id.in_(missing))
            ).all()
            found = {row.id: row for row in rows}
            self._contact_cache.update(found)
            self._missing_contact_ids.update(set(missing) - set(found))
        return {
            contact_id: self._contact_cache[contact_id]
            for contact_id in wanted
            if contact_id in self._contact_cache
        }

    @staticmethod
    def _candidate(
        *,
        source_type: str,
        source_entity_id: str | None,
        source_label: str | None,
        contact_id: str | None,
        contacts: dict[str, BusinessContact],
        diagnostics: tuple[str, ...] = (),
        external_id: str | None = None,
        display_name_hint: str | None = None,
        captured_status: str | None = None,
    ) -> ContactCandidate:
        return ContactCandidate(
            source_type=source_type,
            source_entity_id=source_entity_id,
            source_label=source_label,
            contact_id=contact_id,
            contact=_snapshot(contacts.get(contact_id)) if contact_id else None,
            diagnostics=diagnostics,
            external_id=external_id,
            display_name_hint=display_name_hint,
            captured_status=captured_status,
        )

    def _project_manager_primaries(
        self,
        project_ids: Sequence[str],
    ) -> dict[str, EffectiveProjectManager | None]:
        wanted = tuple(
            dict.fromkeys(
                str(project_id or "").strip()
                for project_id in project_ids
                if str(project_id or "").strip()
            )
        )
        if not wanted:
            return {}
        projections = ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(self._session)
        ).resolve_projects(list(wanted))
        return {
            project_id: projection.primary
            for project_id, projection in projections.items()
        }

    @staticmethod
    def _project_manager_candidate(
        *,
        project: Project,
        manager: EffectiveProjectManager | None,
        contacts: dict[str, BusinessContact],
    ) -> ContactCandidate:
        if manager is None:
            return ContactCandidate(
                source_type=SOURCE_PROJECT_MANAGER,
                source_entity_id=project.id,
                source_label=f"Chargé de projet ERP · {project.number}",
            )

        contact = (
            contacts.get(manager.business_contact_id)
            if manager.business_contact_id
            else None
        )
        snapshot = _snapshot(contact)
        if snapshot is not None and manager.user_active is False:
            snapshot = BusinessContactSnapshot(
                contact_id=snapshot.contact_id,
                display_name=snapshot.display_name,
                email=snapshot.email,
                phone=snapshot.phone,
                active=False,
            )
        return ContactCandidate(
            source_type=SOURCE_PROJECT_MANAGER,
            source_entity_id=project.id,
            source_label=f"Chargé de projet ERP · {project.number}",
            contact_id=manager.business_contact_id,
            contact=snapshot,
            diagnostics=manager.diagnostics,
            external_id=manager.employee_external_id,
            display_name_hint=manager.display_name,
        )

    def _task_for_line(
        self,
        *,
        line: RequestLine,
        project: Project,
        diagnostics: list[str],
    ) -> TaskCatalogEntry | None:
        task: TaskCatalogEntry | None = None
        if line.task_catalog_item_id:
            task = self._session.get(TaskCatalogEntry, line.task_catalog_item_id)
            if task is None:
                diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_INVALID)
                return None
            if task.project_number != project.number:
                diagnostics.append(DIAGNOSTIC_TASK_PROJECT_MISMATCH)
                return None
        elif line.erp_task_code:
            task = self._session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project.number,
                    TaskCatalogEntry.task_code == line.erp_task_code,
                )
            )
            if task is None:
                diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_UNRESOLVED)
                return None
            diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE)

        if task is not None and not bool(task.active):
            diagnostics.append(DIAGNOSTIC_TASK_INACTIVE)
        return task

    def _resource_for_line(
        self,
        *,
        line: RequestLine,
        diagnostics: list[str],
    ) -> Resource | None:
        if not line.proposed_resource_id:
            return None
        resource = self._session.get(Resource, line.proposed_resource_id)
        if resource is None:
            diagnostics.append(DIAGNOSTIC_RESOURCE_REFERENCE_INVALID)
            return None
        if not bool(resource.active):
            diagnostics.append(DIAGNOSTIC_RESOURCE_INACTIVE)
        return resource

    def _approved_task_for_requirement(
        self,
        *,
        requirement: ResourceRequirement,
        project: Project,
        diagnostics: list[str],
    ) -> tuple[TaskCatalogEntry | None, bool, tuple[str, ...]]:
        if requirement.approved_contact_context_status == "LEGACY_UNKNOWN":
            context_diagnostics = (
                DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
            )
            diagnostics.extend(context_diagnostics)
            return None, False, context_diagnostics

        if not requirement.approved_task_catalog_item_id:
            return None, True, ()

        task = self._session.get(
            TaskCatalogEntry,
            requirement.approved_task_catalog_item_id,
        )
        if task is None:
            context_diagnostics = (DIAGNOSTIC_APPROVED_TASK_REFERENCE_INVALID,)
            diagnostics.extend(context_diagnostics)
            return None, False, context_diagnostics
        if task.project_number != project.number:
            context_diagnostics = (DIAGNOSTIC_APPROVED_TASK_PROJECT_MISMATCH,)
            diagnostics.extend(context_diagnostics)
            return None, False, context_diagnostics

        if not bool(task.active):
            diagnostics.append(DIAGNOSTIC_TASK_INACTIVE)
        return task, True, ()

    def _materialized_resource(
        self,
        *,
        resource_id: str | None,
        diagnostics: list[str],
    ) -> tuple[Resource | None, bool, tuple[str, ...]]:
        if not resource_id:
            return None, True, ()

        resource = self._session.get(Resource, resource_id)
        if resource is None:
            context_diagnostics = (DIAGNOSTIC_RESOURCE_REFERENCE_INVALID,)
            diagnostics.extend(context_diagnostics)
            return None, False, context_diagnostics
        if not bool(resource.active):
            diagnostics.append(DIAGNOSTIC_RESOURCE_INACTIVE)
        return resource, True, ()

    def get_project_operational_candidates(
        self,
        project_id: str,
        *,
        task_id: str | None = None,
    ) -> tuple[ContactCandidate, ContactCandidate, ContactCandidate]:
        project = self._session.get(Project, str(project_id or "").strip())
        if project is None:
            raise KeyError(f"Projet {project_id} introuvable")

        task = (
            self._session.get(TaskCatalogEntry, task_id)
            if task_id
            else None
        )
        diagnostics: tuple[str, ...] = ()
        if task_id and task is None:
            diagnostics = (DIAGNOSTIC_TASK_REFERENCE_INVALID,)
        elif task is not None and task.project_number != project.number:
            diagnostics = (DIAGNOSTIC_TASK_PROJECT_MISMATCH,)
            task = None
        elif task is not None and not bool(task.active):
            diagnostics = (DIAGNOSTIC_TASK_INACTIVE,)

        manager = self._project_manager_primaries((project.id,)).get(project.id)
        contact_ids = (
            task.operational_responsible_contact_id if task is not None else None,
            project.operational_responsible_override_contact_id,
            manager.business_contact_id if manager is not None else None,
        )
        contacts = self._contacts(contact_ids)
        return (
            self._candidate(
                source_type=SOURCE_TASK_RESPONSIBLE,
                source_entity_id=task.id if task is not None else task_id,
                source_label=(
                    f"Tâche {task.task_code}" if task is not None else None
                ),
                contact_id=(
                    task.operational_responsible_contact_id
                    if task is not None
                    else None
                ),
                contacts=contacts,
                diagnostics=diagnostics,
            ),
            self._candidate(
                source_type=SOURCE_PROJECT_OVERRIDE,
                source_entity_id=project.id,
                source_label=f"Override projet · {project.number}",
                contact_id=project.operational_responsible_override_contact_id,
                contacts=contacts,
            ),
            self._project_manager_candidate(
                project=project,
                manager=manager,
                contacts=contacts,
            ),
        )

    @staticmethod
    def _captured_diagnostics(value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return (str(value),)
        if not isinstance(payload, list):
            return (str(value),)
        return _unique([str(item) for item in payload if str(item)])

    def _materialized_context(
        self,
        *,
        requirement: ResourceRequirement,
        shift: Shift | None = None,
    ) -> MaterializedContactContext | None:
        project = self._session.get(Project, requirement.project_id)
        if project is None:
            return None

        request = (
            self._session.get(WorkforceRequest, requirement.workforce_request_id)
            if requirement.workforce_request_id
            else None
        )
        diagnostics: list[str] = []
        task, task_context_known, task_context_diagnostics = (
            self._approved_task_for_requirement(
                requirement=requirement,
                project=project,
                diagnostics=diagnostics,
            )
        )

        resource_id = (
            shift.resource_id
            if shift is not None
            else requirement.assigned_resource_id
        )
        resource, resource_context_known, resource_context_diagnostics = (
            self._materialized_resource(
                resource_id=resource_id,
                diagnostics=diagnostics,
            )
        )

        provenance = str(
            requirement.operational_responsibility_context_provenance
            or "LEGACY_UNKNOWN"
        )
        captured_diagnostics = self._captured_diagnostics(
            requirement.captured_operational_responsible_diagnostics
        )
        captured_source = (
            requirement.captured_operational_responsible_source_type
            or SOURCE_NONE
        )
        captured_status = requirement.captured_operational_responsible_status
        if provenance == "LEGACY_UNKNOWN":
            captured_source = SOURCE_NONE
            captured_status = STATUS_UNRESOLVED
            captured_diagnostics = _unique(
                list(captured_diagnostics)
                + [DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN]
            )
        elif (
            captured_status is None
            and requirement.captured_operational_responsible_contact_id is None
        ):
            captured_status = STATUS_UNRESOLVED

        contact_ids = (
            shift.operational_responsible_override_contact_id
            if shift is not None
            else None,
            requirement.operational_responsible_override_contact_id,
            requirement.captured_operational_responsible_contact_id,
            resource.coordinator_contact_id if resource is not None else None,
            task.coordinator_contact_id if task is not None else None,
        )
        contacts = self._contacts(contact_ids)
        task_label = f"Tâche {task.task_code}" if task is not None else None
        demand_number = (
            request.legacy_demand_number
            if request is not None
            else None
        )

        return MaterializedContactContext(
            requirement_id=requirement.id,
            shift_id=shift.id if shift is not None else None,
            request_line_id=requirement.source_request_line_id,
            demand_number=demand_number,
            project_id=project.id,
            project_number=project.number,
            approved_request_version=requirement.approved_request_version,
            approved_contact_context_status=(
                requirement.approved_contact_context_status
            ),
            task_id=task.id if task is not None else None,
            task_code=task.task_code if task is not None else None,
            task_label=task.label if task is not None else None,
            resource_id=resource.id if resource is not None else resource_id,
            resource_name=resource.name if resource is not None else None,
            shift_override=self._candidate(
                source_type=SOURCE_SHIFT_OVERRIDE,
                source_entity_id=shift.id if shift is not None else None,
                source_label="Override du quart" if shift is not None else None,
                contact_id=(
                    shift.operational_responsible_override_contact_id
                    if shift is not None
                    else None
                ),
                contacts=contacts,
            ),
            requirement_override=self._candidate(
                source_type=SOURCE_REQUIREMENT_OVERRIDE,
                source_entity_id=requirement.id,
                source_label="Override du besoin",
                contact_id=requirement.operational_responsible_override_contact_id,
                contacts=contacts,
            ),
            captured_inherited=self._candidate(
                source_type=captured_source,
                source_entity_id=(
                    requirement.captured_operational_responsible_source_entity_id
                    or requirement.id
                ),
                source_label="Contexte de responsabilité capturé",
                contact_id=requirement.captured_operational_responsible_contact_id,
                contacts=contacts,
                diagnostics=captured_diagnostics,
                captured_status=captured_status,
            ),
            operational_responsibility_context_provenance=provenance,
            operational_responsibility_context_version=(
                requirement.operational_responsibility_context_version
            ),
            resource_coordinator=self._candidate(
                source_type=SOURCE_RESOURCE_COORDINATOR,
                source_entity_id=resource.id if resource is not None else resource_id,
                source_label=(
                    f"Ressource {resource.name}"
                    if resource is not None
                    else None
                ),
                contact_id=(
                    resource.coordinator_contact_id
                    if resource is not None
                    else None
                ),
                contacts=contacts,
            ),
            task_coordinator=self._candidate(
                source_type=SOURCE_TASK_COORDINATOR,
                source_entity_id=task.id if task is not None else None,
                source_label=task_label,
                contact_id=task.coordinator_contact_id if task is not None else None,
                contacts=contacts,
            ),
            task_context_known=task_context_known,
            task_context_diagnostics=task_context_diagnostics,
            resource_context_known=resource_context_known,
            resource_context_diagnostics=resource_context_diagnostics,
            diagnostics=_unique(diagnostics),
        )

    def _prime_materialized_context_rows(
        self,
        requirements: Sequence[ResourceRequirement],
        *,
        shifts: Sequence[Shift] = (),
    ) -> tuple[
        tuple[Project, ...],
        tuple[WorkforceRequest, ...],
        tuple[TaskCatalogEntry, ...],
        tuple[Resource, ...],
    ]:
        """Prime the SQLAlchemy identity map for fixed-cost materialized resolution."""

        project_ids = {
            row.project_id for row in requirements if row.project_id
        }
        request_ids = {
            row.workforce_request_id
            for row in requirements
            if row.workforce_request_id
        }
        task_ids = {
            row.approved_task_catalog_item_id
            for row in requirements
            if row.approved_task_catalog_item_id
        }
        resource_ids = {
            row.assigned_resource_id
            for row in requirements
            if row.assigned_resource_id
        }
        resource_ids.update(
            row.resource_id for row in shifts if row.resource_id
        )

        projects = (
            tuple(
                self._session.scalars(
                    select(Project).where(Project.id.in_(tuple(project_ids)))
                ).all()
            )
            if project_ids
            else ()
        )
        requests = (
            tuple(
                self._session.scalars(
                    select(WorkforceRequest).where(
                        WorkforceRequest.id.in_(tuple(request_ids))
                    )
                ).all()
            )
            if request_ids
            else ()
        )
        tasks = (
            tuple(
                self._session.scalars(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.id.in_(tuple(task_ids))
                    )
                ).all()
            )
            if task_ids
            else ()
        )
        resources = (
            tuple(
                self._session.scalars(
                    select(Resource).where(Resource.id.in_(tuple(resource_ids)))
                ).all()
            )
            if resource_ids
            else ()
        )

        task_by_id = {row.id: row for row in tasks}
        resource_by_id = {row.id: row for row in resources}
        contact_ids: list[str | None] = []
        for requirement in requirements:
            project = project_by_id.get(requirement.project_id)
            task = (
                task_by_id.get(requirement.approved_task_catalog_item_id)
                if requirement.approved_task_catalog_item_id
                else None
            )
            contact_ids.extend(
                (
                    requirement.operational_responsible_override_contact_id,
                    requirement.captured_operational_responsible_contact_id,
                    (
                        task.coordinator_contact_id
                        if task is not None
                        else None
                    ),
                )
            )
        for resource in resources:
            contact_ids.append(resource.coordinator_contact_id)
        self._contacts(tuple(contact_ids))
        return projects, requests, tasks, resources

    def get_resource_requirement_contact_contexts(
        self,
        requirement_ids: Sequence[str],
    ) -> tuple[MaterializedContactContext, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(requirement_id or "").strip()
                for requirement_id in requirement_ids
                if str(requirement_id or "").strip()
            )
        )
        if not wanted:
            return ()
        requirements = tuple(
            self._session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.id.in_(wanted)
                )
            ).all()
        )
        by_id = {row.id: row for row in requirements}
        primed = self._prime_materialized_context_rows(requirements)
        result = tuple(
            context
            for requirement_id in wanted
            if (requirement := by_id.get(requirement_id)) is not None
            and (
                context := self._materialized_context(
                    requirement=requirement,
                )
            )
            is not None
        )
        _ = primed
        return result

    def get_shift_contact_contexts(
        self,
        shift_ids: Sequence[str],
    ) -> tuple[MaterializedContactContext, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(shift_id or "").strip()
                for shift_id in shift_ids
                if str(shift_id or "").strip()
            )
        )
        if not wanted:
            return ()
        shifts = tuple(
            self._session.scalars(
                select(Shift).where(Shift.id.in_(wanted))
            ).all()
        )
        shift_by_id = {row.id: row for row in shifts}
        requirement_ids = {
            row.resource_requirement_id
            for row in shifts
            if row.resource_requirement_id
        }
        requirements = (
            tuple(
                self._session.scalars(
                    select(ResourceRequirement).where(
                        ResourceRequirement.id.in_(tuple(requirement_ids))
                    )
                ).all()
            )
            if requirement_ids
            else ()
        )
        requirement_by_id = {row.id: row for row in requirements}
        primed = self._prime_materialized_context_rows(
            requirements,
            shifts=shifts,
        )
        result: list[MaterializedContactContext] = []
        for shift_id in wanted:
            shift = shift_by_id.get(shift_id)
            if shift is None:
                continue
            requirement = requirement_by_id.get(shift.resource_requirement_id)
            if requirement is None:
                continue
            context = self._materialized_context(
                requirement=requirement,
                shift=shift,
            )
            if context is not None:
                result.append(context)
        _ = primed
        return tuple(result)

    def get_resource_requirement_contact_context(
        self,
        requirement_id: str,
    ) -> MaterializedContactContext | None:
        requirement = self._session.get(
            ResourceRequirement,
            str(requirement_id or "").strip(),
        )
        if requirement is None:
            return None
        return self._materialized_context(requirement=requirement)

    def get_shift_contact_context(
        self,
        shift_id: str,
    ) -> MaterializedContactContext | None:
        shift = self._session.get(Shift, str(shift_id or "").strip())
        if shift is None:
            return None
        requirement = self._session.get(
            ResourceRequirement,
            shift.resource_requirement_id,
        )
        if requirement is None:
            return None
        return self._materialized_context(
            requirement=requirement,
            shift=shift,
        )

    def get_workforce_request_contact_context(
        self,
        request_id: str,
    ) -> RequestLineContactContext | None:
        request = self._session.get(
            WorkforceRequest,
            str(request_id or "").strip(),
        )
        if request is None:
            return None
        project = self._session.get(Project, request.project_id)
        if project is None:
            return None

        diagnostics: list[str] = []
        task: TaskCatalogEntry | None = None
        if request.work_package_id:
            work_package = self._session.get(WorkPackage, request.work_package_id)
            task_id = (
                work_package.task_catalog_item_id
                if work_package is not None
                else None
            )
            if task_id:
                task = self._session.get(TaskCatalogEntry, task_id)
                if task is None:
                    diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_INVALID)
                elif task.project_number != project.number:
                    diagnostics.append(DIAGNOSTIC_TASK_PROJECT_MISMATCH)
                    task = None

        if task is None and request.erp_task_code:
            task = self._session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == project.number,
                    TaskCatalogEntry.task_code == request.erp_task_code,
                )
            )
            if task is None:
                diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_UNRESOLVED)
            else:
                diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE)

        if task is not None and not bool(task.active):
            diagnostics.append(DIAGNOSTIC_TASK_INACTIVE)

        resource = None
        if request.proposed_resource_id:
            resource = self._session.get(Resource, request.proposed_resource_id)
            if resource is None:
                diagnostics.append(DIAGNOSTIC_RESOURCE_REFERENCE_INVALID)
            elif not bool(resource.active):
                diagnostics.append(DIAGNOSTIC_RESOURCE_INACTIVE)

        manager = self._project_manager_primaries((project.id,)).get(project.id)
        contact_ids = (
            request.operational_responsible_override_contact_id,
            task.operational_responsible_contact_id if task is not None else None,
            project.operational_responsible_override_contact_id,
            manager.business_contact_id if manager is not None else None,
            resource.coordinator_contact_id if resource is not None else None,
            task.coordinator_contact_id if task is not None else None,
        )
        contacts = self._contacts(contact_ids)
        task_label = f"Tâche {task.task_code}" if task is not None else None

        return RequestLineContactContext(
            line_id=request.id,
            demand_number=request.legacy_demand_number,
            project_id=project.id,
            project_number=project.number,
            task_id=task.id if task is not None else None,
            task_code=(
                task.task_code
                if task is not None
                else request.erp_task_code
            ),
            task_label=(
                task.label
                if task is not None
                else request.erp_task_label
            ),
            proposed_resource_id=(
                resource.id if resource is not None else request.proposed_resource_id
            ),
            proposed_resource_name=resource.name if resource is not None else None,
            request_override=self._candidate(
                source_type=SOURCE_REQUEST_OVERRIDE,
                source_entity_id=request.id,
                source_label=(
                    f"Demande {request.legacy_demand_number}"
                    if request.legacy_demand_number
                    else "Demande"
                ),
                contact_id=request.operational_responsible_override_contact_id,
                contacts=contacts,
            ),
            task_responsible=self._candidate(
                source_type=SOURCE_TASK_RESPONSIBLE,
                source_entity_id=task.id if task is not None else None,
                source_label=task_label,
                contact_id=(
                    task.operational_responsible_contact_id
                    if task is not None
                    else None
                ),
                contacts=contacts,
            ),
            project_override=self._candidate(
                source_type=SOURCE_PROJECT_OVERRIDE,
                source_entity_id=project.id,
                source_label=f"Override projet · {project.number}",
                contact_id=project.operational_responsible_override_contact_id,
                contacts=contacts,
            ),
            project_manager=self._project_manager_candidate(
                project=project,
                manager=manager,
                contacts=contacts,
            ),
            resource_coordinator=self._candidate(
                source_type=SOURCE_RESOURCE_COORDINATOR,
                source_entity_id=(
                    resource.id
                    if resource is not None
                    else request.proposed_resource_id
                ),
                source_label=(
                    f"Ressource {resource.name}"
                    if resource is not None
                    else None
                ),
                contact_id=(
                    resource.coordinator_contact_id
                    if resource is not None
                    else None
                ),
                contacts=contacts,
            ),
            task_coordinator=self._candidate(
                source_type=SOURCE_TASK_COORDINATOR,
                source_entity_id=task.id if task is not None else None,
                source_label=task_label,
                contact_id=task.coordinator_contact_id if task is not None else None,
                contacts=contacts,
            ),
            diagnostics=_unique(diagnostics),
        )

    def get_request_line_contact_contexts(
        self,
        line_ids: Sequence[str],
    ) -> tuple[RequestLineContactContext, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(line_id or "").strip()
                for line_id in line_ids
                if str(line_id or "").strip()
            )
        )
        if not wanted:
            return ()

        lines = tuple(
            self._session.scalars(
                select(RequestLine).where(RequestLine.id.in_(wanted))
            ).all()
        )
        if not lines:
            return ()
        line_by_id = {line.id: line for line in lines}

        request_ids = {
            line.workforce_request_id
            for line in lines
            if line.workforce_request_id
        }
        requests = {
            row.id: row
            for row in self._session.scalars(
                select(WorkforceRequest).where(
                    WorkforceRequest.id.in_(tuple(request_ids))
                )
            ).all()
        }
        project_ids = {
            request.project_id
            for request in requests.values()
            if request.project_id
        }
        projects = {
            row.id: row
            for row in self._session.scalars(
                select(Project).where(Project.id.in_(tuple(project_ids)))
            ).all()
        }
        project_managers = self._project_manager_primaries(tuple(projects))

        direct_task_ids = {
            line.task_catalog_item_id
            for line in lines
            if line.task_catalog_item_id
        }
        tasks = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.id.in_(tuple(direct_task_ids))
                    )
                ).all()
            }
            if direct_task_ids
            else {}
        )
        legacy_project_numbers = {
            projects[request.project_id].number
            for request in requests.values()
            if request.project_id in projects
        }
        legacy_codes = {
            line.erp_task_code for line in lines if line.erp_task_code
        }
        legacy_tasks = (
            self._session.scalars(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number.in_(
                        tuple(legacy_project_numbers)
                    ),
                    TaskCatalogEntry.task_code.in_(tuple(legacy_codes)),
                )
            ).all()
            if legacy_project_numbers and legacy_codes
            else []
        )
        legacy_task_by_key = {
            (row.project_number, row.task_code): row for row in legacy_tasks
        }

        resource_ids = {
            line.proposed_resource_id
            for line in lines
            if line.proposed_resource_id
        }
        resources = (
            {
                row.id: row
                for row in self._session.scalars(
                    select(Resource).where(Resource.id.in_(tuple(resource_ids)))
                ).all()
            }
            if resource_ids
            else {}
        )

        resolved: dict[
            str,
            tuple[
                RequestLine,
                WorkforceRequest,
                Project,
                TaskCatalogEntry | None,
                Resource | None,
                tuple[str, ...],
            ],
        ] = {}
        all_contact_ids: list[str | None] = []

        for line_id in wanted:
            line = line_by_id.get(line_id)
            if line is None:
                continue
            request = requests.get(line.workforce_request_id)
            if request is None:
                continue
            project = projects.get(request.project_id)
            if project is None:
                continue

            diagnostics: list[str] = []
            task: TaskCatalogEntry | None = None
            if line.task_catalog_item_id:
                task = tasks.get(line.task_catalog_item_id)
                if task is None:
                    diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_INVALID)
                elif task.project_number != project.number:
                    diagnostics.append(DIAGNOSTIC_TASK_PROJECT_MISMATCH)
                    task = None
            elif line.erp_task_code:
                task = legacy_task_by_key.get(
                    (project.number, line.erp_task_code)
                )
                if task is None:
                    diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_UNRESOLVED)
                else:
                    diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE)

            if task is not None and not bool(task.active):
                diagnostics.append(DIAGNOSTIC_TASK_INACTIVE)

            resource: Resource | None = None
            if line.proposed_resource_id:
                resource = resources.get(line.proposed_resource_id)
                if resource is None:
                    diagnostics.append(DIAGNOSTIC_RESOURCE_REFERENCE_INVALID)
                elif not bool(resource.active):
                    diagnostics.append(DIAGNOSTIC_RESOURCE_INACTIVE)

            manager = project_managers.get(project.id)
            all_contact_ids.extend(
                (
                    request.operational_responsible_override_contact_id,
                    (
                        task.operational_responsible_contact_id
                        if task is not None
                        else None
                    ),
                    project.operational_responsible_override_contact_id,
                    manager.business_contact_id if manager is not None else None,
                    (
                        resource.coordinator_contact_id
                        if resource is not None
                        else None
                    ),
                    (
                        task.coordinator_contact_id
                        if task is not None
                        else None
                    ),
                )
            )
            resolved[line.id] = (
                line,
                request,
                project,
                task,
                resource,
                _unique(diagnostics),
            )

        contacts = self._contacts(tuple(all_contact_ids))
        result: list[RequestLineContactContext] = []
        for line_id in wanted:
            values = resolved.get(line_id)
            if values is None:
                continue
            line, request, project, task, resource, diagnostics = values
            task_label = None
            if task is not None:
                task_label = f"Tâche {task.task_code}"
            elif line.erp_task_code:
                task_label = f"Tâche {line.erp_task_code}"

            result.append(
                RequestLineContactContext(
                    line_id=line.id,
                    demand_number=request.legacy_demand_number,
                    project_id=project.id,
                    project_number=project.number,
                    task_id=task.id if task is not None else None,
                    task_code=(
                        task.task_code
                        if task is not None
                        else line.erp_task_code
                    ),
                    task_label=(
                        task.label
                        if task is not None
                        else line.erp_task_label
                    ),
                    proposed_resource_id=(
                        resource.id
                        if resource is not None
                        else line.proposed_resource_id
                    ),
                    proposed_resource_name=(
                        resource.name if resource is not None else None
                    ),
                    request_override=self._candidate(
                        source_type=SOURCE_REQUEST_OVERRIDE,
                        source_entity_id=request.id,
                        source_label=(
                            f"Demande {request.legacy_demand_number}"
                            if request.legacy_demand_number
                            else "Demande"
                        ),
                        contact_id=(
                            request.operational_responsible_override_contact_id
                        ),
                        contacts=contacts,
                    ),
                    task_responsible=self._candidate(
                        source_type=SOURCE_TASK_RESPONSIBLE,
                        source_entity_id=(
                            task.id if task is not None else None
                        ),
                        source_label=task_label,
                        contact_id=(
                            task.operational_responsible_contact_id
                            if task is not None
                            else None
                        ),
                        contacts=contacts,
                    ),
                    project_override=self._candidate(
                        source_type=SOURCE_PROJECT_OVERRIDE,
                        source_entity_id=project.id,
                        source_label=f"Override projet · {project.number}",
                        contact_id=project.operational_responsible_override_contact_id,
                        contacts=contacts,
                    ),
                    project_manager=self._project_manager_candidate(
                        project=project,
                        manager=project_managers.get(project.id),
                        contacts=contacts,
                    ),
                    resource_coordinator=self._candidate(
                        source_type=SOURCE_RESOURCE_COORDINATOR,
                        source_entity_id=(
                            resource.id
                            if resource is not None
                            else line.proposed_resource_id
                        ),
                        source_label=(
                            f"Ressource {resource.name}"
                            if resource is not None
                            else None
                        ),
                        contact_id=(
                            resource.coordinator_contact_id
                            if resource is not None
                            else None
                        ),
                        contacts=contacts,
                    ),
                    task_coordinator=self._candidate(
                        source_type=SOURCE_TASK_COORDINATOR,
                        source_entity_id=(
                            task.id if task is not None else None
                        ),
                        source_label=task_label,
                        contact_id=(
                            task.coordinator_contact_id
                            if task is not None
                            else None
                        ),
                        contacts=contacts,
                    ),
                    diagnostics=diagnostics,
                )
            )
        return tuple(result)

    def get_request_line_contact_context(
        self,
        line_id: str,
    ) -> RequestLineContactContext | None:
        wanted = str(line_id or "").strip()
        rows = self.get_request_line_contact_contexts((wanted,))
        return rows[0] if rows else None

