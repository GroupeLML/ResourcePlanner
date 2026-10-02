from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...application.operational_contacts import OperationalContactService
from ...application.project_communications import ProjectCommunicationRepositoryPort
from ...application.project_managers import (
    EffectiveProjectManager,
    ProjectManagerResolutionService,
)
from ...domain.confirmation import effective_confirmation
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from ...domain.reservable_assets import AssetRequirementOrigin
from ...domain.project_communication import (
    ProjectCommunicationAssignment,
    ProjectCommunicationParticipant,
)
from .asset_models import AssetAllocation, AssetRequirement
from .asset_qualification import (
    QUALIFICATION_SATISFIED,
    evaluate_asset_qualification,
)
from .business_contact_models import BusinessContact
from .identity_models import AppUser
from .models import Project, Resource, ResourceRequirement, Shift, WorkforceRequest
from .project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)


DIAGNOSTIC_PROJECT_MANAGER_CONTACT_MISSING = "PROJECT_MANAGER_CONTACT_MISSING"
DIAGNOSTIC_PROJECT_MANAGER_CONTACT_INVALID = "PROJECT_MANAGER_CONTACT_INVALID"
DIAGNOSTIC_PROJECT_MANAGER_USER_MISSING = "PROJECT_MANAGER_USER_MISSING"
DIAGNOSTIC_PROJECT_MANAGER_INACTIVE = "PROJECT_MANAGER_INACTIVE"
DIAGNOSTIC_PROJECT_MANAGER_EMAIL_MISSING = "PROJECT_MANAGER_EMAIL_MISSING"
DIAGNOSTIC_RESOURCE_USER_LINK_MISSING = "RESOURCE_USER_LINK_MISSING"
DIAGNOSTIC_RESOURCE_CONTACT_MISSING = "RESOURCE_CONTACT_MISSING"
DIAGNOSTIC_RESOURCE_CONTACT_INACTIVE = "RESOURCE_CONTACT_INACTIVE"
DIAGNOSTIC_RESOURCE_EMAIL_MISSING = "RESOURCE_EMAIL_MISSING"
DIAGNOSTIC_OPERATIONAL_RESPONSIBLE_USER_MISSING = (
    "OPERATIONAL_RESPONSIBLE_USER_MISSING"
)
DIAGNOSTIC_TASK_DESCRIPTION_FALLBACK = "TASK_DESCRIPTION_FALLBACK"


def _text(value: object) -> str:
    return str(value or "").strip()


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


class SqlProjectCommunicationRepository(ProjectCommunicationRepositoryPort):
    """Read assigned shifts without using the legacy communication contact directory."""

    def __init__(
        self,
        session: Session,
        *,
        operational_contacts: OperationalContactService,
    ) -> None:
        self._session = session
        self._operational_contacts = operational_contacts

    def _project_manager(
        self,
        *,
        manager: EffectiveProjectManager | None,
        contacts: dict[str, BusinessContact],
    ) -> ProjectCommunicationParticipant:
        diagnostics: list[str] = []
        if manager is None:
            diagnostics.append(DIAGNOSTIC_PROJECT_MANAGER_CONTACT_MISSING)
            return ProjectCommunicationParticipant(
                contact_id=None,
                user_id=None,
                display_name="Chargé de projet non défini",
                email=None,
                phone=None,
                active=False,
                diagnostics=_unique(diagnostics),
            )

        diagnostics.extend(manager.diagnostics)
        contact_id = _text(manager.business_contact_id) or None
        contact = contacts.get(contact_id) if contact_id else None

        if contact_id is None:
            diagnostics.append(DIAGNOSTIC_PROJECT_MANAGER_CONTACT_MISSING)
        elif contact is None:
            diagnostics.append(DIAGNOSTIC_PROJECT_MANAGER_CONTACT_INVALID)
        if (
            manager.user_active is False
            or manager.contact_active is False
            or (contact is not None and not bool(contact.active))
        ):
            diagnostics.append(DIAGNOSTIC_PROJECT_MANAGER_INACTIVE)

        email = _text(contact.email) if contact is not None else ""
        if not email:
            diagnostics.append(DIAGNOSTIC_PROJECT_MANAGER_EMAIL_MISSING)

        return ProjectCommunicationParticipant(
            contact_id=contact_id,
            user_id=manager.app_user_id,
            display_name=manager.display_name,
            email=email or None,
            phone=(_text(contact.phone) or None) if contact is not None else None,
            active=bool(
                contact is not None
                and contact.active
                and manager.user_active is not False
                and manager.contact_active is not False
            ),
            diagnostics=_unique(diagnostics),
        )

    def _resource_contact(
        self,
        *,
        resource: Resource,
        users_by_employee: dict[str, AppUser],
        contacts: dict[str, BusinessContact],
    ) -> ProjectCommunicationParticipant:
        diagnostics: list[str] = []
        external_id = _text(resource.external_id)
        user = users_by_employee.get(external_id) if external_id else None
        if user is None:
            diagnostics.append(DIAGNOSTIC_RESOURCE_USER_LINK_MISSING)

        contact_id = _text(user.business_contact_id) if user is not None else ""
        contact = contacts.get(contact_id) if contact_id else None
        if user is not None and contact is None:
            diagnostics.append(DIAGNOSTIC_RESOURCE_CONTACT_MISSING)
        if (
            (contact is not None and not bool(contact.active))
            or (user is not None and not bool(user.active))
        ):
            diagnostics.append(DIAGNOSTIC_RESOURCE_CONTACT_INACTIVE)

        email = _text(contact.email) if contact is not None else ""
        if not email:
            diagnostics.append(DIAGNOSTIC_RESOURCE_EMAIL_MISSING)

        return ProjectCommunicationParticipant(
            contact_id=contact_id or None,
            user_id=user.id if user is not None else None,
            display_name=contact.display_name if contact is not None else resource.name,
            email=email or None,
            phone=(_text(contact.phone) or None) if contact is not None else None,
            active=bool(
                contact is not None
                and contact.active
                and user is not None
                and user.active
            ),
            diagnostics=_unique(diagnostics),
        )

    @staticmethod
    def _task_description(
        *,
        requirement: ResourceRequirement,
        request: WorkforceRequest | None,
        task_label: str | None,
    ) -> tuple[str, tuple[str, ...]]:
        snapshot = _text(requirement.description)
        generic_snapshots = {
            "Besoin approuvé",
            "Période approuvée",
            "Ressource additionnelle",
        }
        if snapshot and snapshot not in generic_snapshots:
            return snapshot, ()
        if _text(task_label):
            return _text(task_label), ()
        if (
            request is not None
            and requirement.approved_request_version is not None
            and int(requirement.approved_request_version)
            == int(request.aggregate_version or 1)
            and _text(request.description)
        ):
            return _text(request.description), ()
        return (
            snapshot or "Travaux planifiés",
            (DIAGNOSTIC_TASK_DESCRIPTION_FALLBACK,),
        )

    def list_assignments(
        self,
        *,
        week_start: date,
        week_end: date,
    ) -> tuple[ProjectCommunicationAssignment, ...]:
        rows = self._session.execute(
            select(Shift, ResourceRequirement, Resource, Project)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .join(Resource, Shift.resource_id == Resource.id)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .where(
                Shift.work_date >= week_start,
                Shift.work_date <= week_end,
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
            )
            .order_by(
                Project.number,
                Shift.work_date,
                Resource.name,
                Shift.id,
            )
        ).all()
        if not rows:
            return ()

        request_ids = {
            requirement.workforce_request_id
            for _shift, requirement, _resource, _project in rows
            if requirement.workforce_request_id
        }
        requests = (
            self._session.scalars(
                select(WorkforceRequest).where(WorkforceRequest.id.in_(request_ids))
            ).all()
            if request_ids
            else []
        )
        requests_by_id = {row.id: row for row in requests}

        asset_qualification_diagnostics: dict[str, list[str]] = {}
        asset_qualification_diagnostics_by_shift: dict[str, list[str]] = {}
        shift_ids = tuple(
            shift.id for shift, _requirement, _resource, _project in rows
        )
        asset_scope = []
        if request_ids:
            asset_scope.append(
                AssetRequirement.workforce_request_id.in_(request_ids)
            )
        if shift_ids:
            asset_scope.append(AssetRequirement.shift_id.in_(shift_ids))
        asset_rows = (
            self._session.execute(
                select(AssetAllocation, AssetRequirement)
                .join(
                    AssetRequirement,
                    AssetAllocation.asset_requirement_id == AssetRequirement.id,
                )
                .where(
                    or_(*asset_scope),
                    AssetAllocation.start_date <= week_end,
                    AssetAllocation.end_date >= week_start,
                )
            ).all()
            if asset_scope
            else []
        )
        for allocation, asset_requirement in asset_rows:
            qualification = evaluate_asset_qualification(
                self._session,
                requirement=asset_requirement,
                allocation=allocation,
            )
            if qualification.state == QUALIFICATION_SATISFIED:
                continue
            diagnostic = f"ASSET_QUALIFICATION_{qualification.state}"
            if (
                asset_requirement.origin
                == AssetRequirementOrigin.SHIFT_AD_HOC.value
                and asset_requirement.shift_id
            ):
                asset_qualification_diagnostics_by_shift.setdefault(
                    asset_requirement.shift_id,
                    [],
                ).append(diagnostic)
            elif asset_requirement.workforce_request_id:
                asset_qualification_diagnostics.setdefault(
                    asset_requirement.workforce_request_id,
                    [],
                ).append(diagnostic)

        resource_external_ids = {
            _text(resource.external_id)
            for _shift, _requirement, resource, _project in rows
            if _text(resource.external_id)
        }
        resource_users = (
            self._session.scalars(
                select(AppUser).where(
                    AppUser.employee_external_id.in_(resource_external_ids)
                )
            ).all()
            if resource_external_ids
            else []
        )
        users_by_employee = {
            _text(row.employee_external_id): row
            for row in resource_users
            if _text(row.employee_external_id)
        }

        resolutions = {
            resolution.shift_id: resolution
            for resolution in self._operational_contacts.resolve_shifts(shift_ids)
            if resolution.shift_id is not None
        }
        project_ids = tuple(
            dict.fromkeys(
                project.id for _shift, _requirement, _resource, project in rows
            )
        )
        project_manager_projections = ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(self._session)
        ).resolve_projects(list(project_ids))
        project_managers = {
            project_id: projection.primary
            for project_id, projection in project_manager_projections.items()
        }
        relevant_contact_ids = {
            _text(manager.business_contact_id)
            for manager in project_managers.values()
            if manager is not None and _text(manager.business_contact_id)
        }
        relevant_contact_ids.update(
            _text(user.business_contact_id)
            for user in resource_users
            if _text(user.business_contact_id)
        )
        relevant_contact_ids.update(
            _text(resolution.operational_responsible.contact_id)
            for resolution in resolutions.values()
            if _text(resolution.operational_responsible.contact_id)
        )

        contacts = (
            self._session.scalars(
                select(BusinessContact).where(
                    BusinessContact.id.in_(relevant_contact_ids)
                )
            ).all()
            if relevant_contact_ids
            else []
        )
        contacts_by_id = {row.id: row for row in contacts}

        users_by_contact_rows = (
            self._session.scalars(
                select(AppUser).where(
                    AppUser.business_contact_id.in_(relevant_contact_ids)
                )
            ).all()
            if relevant_contact_ids
            else []
        )
        users_by_contact = {
            _text(row.business_contact_id): row
            for row in users_by_contact_rows
            if _text(row.business_contact_id)
        }

        result: list[ProjectCommunicationAssignment] = []
        for shift, requirement, resource, project in rows:
            resolution = resolutions[shift.id]
            request = (
                requests_by_id.get(requirement.workforce_request_id)
                if requirement.workforce_request_id
                else None
            )
            description, description_diagnostics = self._task_description(
                requirement=requirement,
                request=request,
                task_label=resolution.task_label,
            )
            manager = self._project_manager(
                manager=project_managers.get(project.id),
                contacts=contacts_by_id,
            )
            resource_contact = self._resource_contact(
                resource=resource,
                users_by_employee=users_by_employee,
                contacts=contacts_by_id,
            )
            operational_diagnostics: list[str] = []
            responsible_contact_id = _text(
                resolution.operational_responsible.contact_id
            )
            if (
                responsible_contact_id
                and responsible_contact_id not in users_by_contact
            ):
                operational_diagnostics.append(
                    DIAGNOSTIC_OPERATIONAL_RESPONSIBLE_USER_MISSING
                )
            diagnostics = _unique(
                list(resolution.diagnostics)
                + list(description_diagnostics)
                + list(manager.diagnostics)
                + list(resource_contact.diagnostics)
                + operational_diagnostics
                + list(
                    asset_qualification_diagnostics.get(
                        requirement.workforce_request_id or "",
                        (),
                    )
                )
                + list(
                    asset_qualification_diagnostics_by_shift.get(
                        shift.id,
                        (),
                    )
                )
            )
            result.append(
                ProjectCommunicationAssignment(
                    shift_id=shift.id,
                    requirement_id=requirement.id,
                    project_id=project.id,
                    project_number=_text(project.number),
                    project_name=_text(project.name),
                    day=shift.work_date,
                    hours=float(shift.hours),
                    allocation_type=(
                        _text(shift.allocation_type)
                        or _text(requirement.planning_type)
                        or "Flexible"
                    ),
                    outside_schedule=bool(shift.outside_standard_hours),
                    confirmation=effective_confirmation(
                        shift.confirmation,
                        requirement.confirmation,
                    ),
                    task_id=resolution.task_id,
                    task_code=resolution.task_code,
                    task_description=description,
                    resource_id=resource.id,
                    resource_name=resource.name,
                    resource_contact=resource_contact,
                    project_manager=manager,
                    operational_responsible=resolution.operational_responsible,
                    diagnostics=diagnostics,
                )
            )
        return tuple(result)
