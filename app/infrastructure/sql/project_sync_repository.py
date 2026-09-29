from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.errors import ApplicationConflictError, ApplicationValidationError
from ...application.project_sync import ExternalProjectRecord, ProjectSyncRepositoryPort
from .identity_models import AppUser
from .models import Project


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    text = _text(value)
    return text or None


class SqlProjectSyncRepository(ProjectSyncRepositoryPort):
    """Idempotent ERP-project upsert for the local operational project table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _resolve_project_manager_contact_id(
        self,
        employee_external_id: str,
    ) -> str | None:
        """Resolve an ERP EmployeID through the canonical AppUser employee link only."""
        employee_id = _text(employee_external_id)
        if not employee_id:
            return None
        matches = self._session.scalars(
            select(AppUser).where(AppUser.employee_external_id == employee_id)
        ).all()
        if len(matches) > 1:
            # The SQL model already forbids this state. Keep the sync fail-closed
            # rather than selecting an arbitrary identity if legacy/corrupt data
            # bypassed the uniqueness constraint.
            raise ApplicationConflictError(
                "Plusieurs utilisateurs locaux partagent le même identifiant employé ERP.",
                code="project_sync_manager_identity_conflict",
                context={"employee_external_id": employee_id},
            )
        if not matches:
            return None
        return _optional_text(matches[0].business_contact_id)

    def upsert_external_project(self, project: ExternalProjectRecord) -> str:
        external_id = _text(project.external_id)
        number = _text(project.number)
        name = _text(project.name)
        if not number:
            raise ApplicationValidationError(
                "Un numéro de projet est requis pour synchroniser un projet.",
                code="project_sync_number_required",
                context={"erp_external_id": external_id or None},
            )
        if not name:
            raise ApplicationValidationError(
                "Un nom de projet est requis pour synchroniser un projet.",
                code="project_sync_name_required",
                context={"erp_external_id": external_id or None, "project_number": number},
            )

        by_external = (
            self._session.scalar(select(Project).where(Project.erp_external_id == external_id))
            if external_id
            else None
        )
        by_number = self._session.scalar(select(Project).where(Project.number == number))

        if by_external is not None and by_number is not None and by_external.id != by_number.id:
            raise ApplicationConflictError(
                "Le numéro de projet et l'identifiant ERP pointent vers deux projets locaux différents.",
                code="project_sync_identity_conflict",
                context={"erp_external_id": external_id, "project_number": number},
            )

        row = by_external or by_number
        if row is None:
            manager_external_id = _optional_text(project.project_manager_external_id)
            self._session.add(
                Project(
                    erp_external_id=external_id or None,
                    number=number,
                    name=name,
                    client=_optional_text(project.client),
                    project_manager_external_id=manager_external_id,
                    project_manager_name=_optional_text(project.project_manager_name),
                    project_manager_contact_id=(
                        self._resolve_project_manager_contact_id(manager_external_id)
                        if manager_external_id is not None
                        else None
                    ),
                    status=_text(project.status) or "active",
                )
            )
            self._session.flush()
            return "created"

        if external_id and row.erp_external_id and row.erp_external_id != external_id:
            raise ApplicationConflictError(
                "Le projet local est déjà lié à un autre identifiant ERP.",
                code="project_sync_external_id_conflict",
                context={
                    "project_number": row.number,
                    "existing_erp_external_id": row.erp_external_id,
                    "incoming_erp_external_id": external_id,
                },
            )

        values = {
            "number": number,
            "name": name,
            "client": _optional_text(project.client),
            "status": _text(project.status) or "active",
        }
        incoming_manager_external_id = _optional_text(
            project.project_manager_external_id
        )
        incoming_manager_name = _optional_text(project.project_manager_name)
        # Current export/source contracts cannot distinguish omitted from explicit
        # clear. Fail safe: an omitted manager field must not erase an existing
        # stable mapping. A future Acumatica contract may add explicit clear semantics.
        if incoming_manager_external_id is not None:
            values["project_manager_external_id"] = incoming_manager_external_id
            values["project_manager_contact_id"] = (
                self._resolve_project_manager_contact_id(
                    incoming_manager_external_id
                )
            )
        if incoming_manager_name is not None:
            values["project_manager_name"] = incoming_manager_name
        if external_id:
            # Manual XLSX exports do not expose the Acumatica REST row id. In that
            # case preserve any existing binding; a later live ERP sync can attach it.
            values["erp_external_id"] = external_id

        changed = False
        for field, value in values.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True

        if changed:
            self._session.flush()
            return "updated"
        return "unchanged"
