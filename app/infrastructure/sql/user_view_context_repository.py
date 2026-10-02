from __future__ import annotations

from sqlalchemy import exists, or_, select, true
from sqlalchemy.orm import Session

from ...application.operational_contacts import OperationalContactService
from ...application.query_models import ResourceReadModel
from ...application.user_view_context import UserViewContextRepositoryPort
from .identity_models import AppUser
from .project_manager_resolution_repository import project_managed_by_user_predicate
from .models import (
    Project,
    RequestLine,
    Resource,
    ResourceCompetency,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
)
from .operational_contact_repository import SqlOperationalContactRepository


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class SqlUserViewContextRepository(UserViewContextRepositoryPort):
    """Resolve stable user/resource/project relationships without display-name joins."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_resource_by_external_id(
        self,
        employee_external_id: str,
    ) -> ResourceReadModel | None:
        external_id = str(employee_external_id or "").strip()
        if not external_id:
            return None
        resource = self._session.scalar(
            select(Resource).where(Resource.external_id == external_id)
        )
        if resource is None:
            return None
        competency_ids = tuple(
            self._session.scalars(
                select(ResourceCompetency.competency_id)
                .where(ResourceCompetency.resource_id == resource.id)
                .order_by(ResourceCompetency.competency_id)
            ).all()
        )
        return ResourceReadModel(
            id=resource.id,
            name=resource.name,
            email=_optional_text(resource.email),
            resource_class=_optional_text(resource.resource_class),
            competencies=_optional_text(resource.competencies),
            competency_ids=competency_ids,
            note=_optional_text(resource.note),
            active=bool(resource.active),
            sort_order=int(resource.sort_order or 0),
            external_id=_optional_text(resource.external_id),
        )

    def list_managed_project_ids(
        self,
        local_user_id: str | None,
        employee_external_id: str | None,
    ) -> tuple[str, ...]:
        user_id = str(local_user_id or "").strip()
        external_id = str(employee_external_id or "").strip() or None
        business_contact_id: str | None = None
        if user_id:
            user = self._session.get(AppUser, user_id)
            if user is not None:
                business_contact_id = (
                    str(user.business_contact_id or "").strip() or None
                )
                external_id = (
                    str(user.employee_external_id or "").strip()
                    or external_id
                )
        if external_id is None and business_contact_id is None:
            return ()
        return tuple(
            self._session.scalars(
                select(Project.id)
                .where(
                    project_managed_by_user_predicate(
                        employee_external_id=external_id,
                        business_contact_id=business_contact_id,
                    )
                )
                .order_by(Project.number, Project.id)
            ).all()
        )


    def list_coordinated_demand_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]:
        """Return exact request IDs whose canonical coordinator is the current user.

        Candidate lines are authoritative only before materialization. Once a request
        has an active materialized requirement, #289C/#289F approved context remains
        authoritative so an unapproved candidate edit cannot move display scope.
        """

        user_id = str(local_user_id or "").strip()
        if not user_id:
            return ()
        user = self._session.get(AppUser, user_id)
        contact_id = (
            str(user.business_contact_id or "").strip()
            if user is not None
            else ""
        )
        if not contact_id:
            return ()

        active_requirement_rows = tuple(
            self._session.execute(
                select(
                    ResourceRequirement.id,
                    ResourceRequirement.workforce_request_id,
                ).where(
                    ResourceRequirement.workforce_request_id.is_not(None),
                    ResourceRequirement.origin == "REQUEST",
                    ResourceRequirement.status != "Annulé",
                )
            ).all()
        )
        requirement_ids = tuple(row.id for row in active_requirement_rows)
        request_id_by_requirement_id = {
            row.id: row.workforce_request_id
            for row in active_requirement_rows
            if row.workforce_request_id
        }
        materialized_request_ids = set(
            request_id_by_requirement_id.values()
        )

        shift_rows = (
            tuple(
                self._session.execute(
                    select(
                        Shift.id,
                        Shift.resource_requirement_id,
                    ).where(
                        Shift.resource_requirement_id.in_(requirement_ids)
                    )
                ).all()
            )
            if requirement_ids
            else ()
        )
        request_id_by_shift_id = {
            row.id: request_id_by_requirement_id.get(
                row.resource_requirement_id
            )
            for row in shift_rows
        }

        contacts = OperationalContactService(
            SqlOperationalContactRepository(self._session)
        )
        coordinated_request_ids: set[str] = set()

        for resolution in contacts.resolve_resource_requirements(
            requirement_ids
        ):
            if (
                resolution.coordinator.resolved
                and resolution.coordinator.contact_id == contact_id
            ):
                request_id = request_id_by_requirement_id.get(
                    resolution.requirement_id
                )
                if request_id:
                    coordinated_request_ids.add(request_id)

        for resolution in contacts.resolve_shifts(
            tuple(row.id for row in shift_rows)
        ):
            if (
                resolution.coordinator.resolved
                and resolution.coordinator.contact_id == contact_id
            ):
                request_id = request_id_by_shift_id.get(
                    resolution.subject_id
                )
                if request_id:
                    coordinated_request_ids.add(request_id)

        candidate_statement = (
            select(RequestLine.id, RequestLine.workforce_request_id)
            .join(
                WorkforceRequest,
                RequestLine.workforce_request_id == WorkforceRequest.id,
            )
            .where(
                RequestLine.active == true(),
                WorkforceRequest.status == "Soumise",
            )
        )
        if materialized_request_ids:
            candidate_statement = candidate_statement.where(
                RequestLine.workforce_request_id.notin_(
                    tuple(materialized_request_ids)
                )
            )
        candidate_rows = tuple(
            self._session.execute(candidate_statement).all()
        )
        request_id_by_line_id = {
            row.id: row.workforce_request_id for row in candidate_rows
        }
        for resolution in contacts.resolve_request_lines(
            tuple(request_id_by_line_id)
        ):
            if (
                resolution.coordinator.resolved
                and resolution.coordinator.contact_id == contact_id
            ):
                request_id = request_id_by_line_id.get(resolution.line_id)
                if request_id:
                    coordinated_request_ids.add(request_id)

        return tuple(sorted(coordinated_request_ids))

    def list_participating_project_ids(
        self,
        resource_id: str,
    ) -> tuple[str, ...]:
        wanted_resource_id = str(resource_id or "").strip()
        if not wanted_resource_id:
            return ()

        assigned = exists(
            select(1)
            .select_from(ResourceRequirement)
            .where(
                ResourceRequirement.project_id == Project.id,
                ResourceRequirement.assigned_resource_id == wanted_resource_id,
            )
        )
        shifted = exists(
            select(1)
            .select_from(Shift)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .where(
                ResourceRequirement.project_id == Project.id,
                Shift.resource_id == wanted_resource_id,
            )
        )
        return tuple(
            self._session.scalars(
                select(Project.id)
                .where(or_(assigned, shifted))
                .order_by(Project.number, Project.id)
            ).all()
        )
