from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.business_contact_admin import (
    BusinessContactAdminRepositoryPort,
    BusinessContactRecord,
    ContactLinkRecord,
    DemandOverrideMutationResult,
)
from ...application.errors import ApplicationConflictError, ApplicationValidationError
from ...application.project_managers import ProjectManagerResolutionService
from .base import new_id, utc_now
from .business_contact_models import BusinessContact
from .identity_models import AppUser
from .project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)
from .models import (
    Project,
    Resource,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    text = _text(value)
    return text or None


def _record(row: BusinessContact) -> BusinessContactRecord:
    return BusinessContactRecord(
        id=row.id,
        display_name=row.display_name,
        email=_optional_text(row.email),
        phone=_optional_text(row.phone),
        active=bool(row.active),
        source=row.source,
        external_system=_optional_text(row.external_system),
        external_entity=_optional_text(row.external_entity),
        external_id=_optional_text(row.external_id),
        version=int(row.version or 1),
    )


class SqlBusinessContactAdminRepository(BusinessContactAdminRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def _contact(self, contact_id: str | None) -> BusinessContact | None:
        if not contact_id:
            return None
        return self._session.get(BusinessContact, contact_id)

    def _require_contact(self, contact_id: str | None) -> str | None:
        wanted = _optional_text(contact_id)
        if wanted is None:
            return None
        if self._contact(wanted) is None:
            raise KeyError(f"Contact {wanted} introuvable")
        return wanted

    def _ensure_external_identity_available(
        self,
        *,
        external_system: str | None,
        external_entity: str | None,
        external_id: str | None,
        exclude_contact_id: str | None = None,
    ) -> None:
        if not external_id:
            return
        statement = select(BusinessContact).where(
            BusinessContact.external_system == external_system,
            BusinessContact.external_entity == external_entity,
            BusinessContact.external_id == external_id,
        )
        if exclude_contact_id:
            statement = statement.where(BusinessContact.id != exclude_contact_id)
        existing = self._session.scalar(statement)
        if existing is not None:
            raise ApplicationConflictError(
                "Cette identité externe est déjà liée à un autre contact.",
                code="business_contact_external_identity_conflict",
                context={
                    "contact_id": existing.id,
                    "external_system": external_system,
                    "external_entity": external_entity,
                    "external_id": external_id,
                },
            )

    def list_contacts(
        self,
        *,
        active_only: bool = False,
        user_backed_only: bool = False,
    ) -> tuple[BusinessContactRecord, ...]:
        statement = select(BusinessContact)
        if user_backed_only:
            statement = statement.join(
                AppUser,
                AppUser.business_contact_id == BusinessContact.id,
            )
        if active_only:
            statement = statement.where(BusinessContact.active == true())
        rows = self._session.scalars(
            statement.order_by(BusinessContact.display_name, BusinessContact.id)
        ).all()
        return tuple(_record(row) for row in rows)

    def get_contact(self, contact_id: str) -> BusinessContactRecord | None:
        row = self._session.get(BusinessContact, _text(contact_id))
        return _record(row) if row is not None else None

    def create_contact(self, values: Mapping[str, Any]) -> BusinessContactRecord:
        self._ensure_external_identity_available(
            external_system=_optional_text(values.get("external_system")),
            external_entity=_optional_text(values.get("external_entity")),
            external_id=_optional_text(values.get("external_id")),
        )
        row = BusinessContact(
            id=new_id(),
            display_name=_text(values.get("display_name")),
            email=_optional_text(values.get("email")),
            phone=_optional_text(values.get("phone")),
            active=bool(values.get("active", True)),
            source=_text(values.get("source")) or "LOCAL",
            external_system=_optional_text(values.get("external_system")),
            external_entity=_optional_text(values.get("external_entity")),
            external_id=_optional_text(values.get("external_id")),
            version=1,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def update_contact(
        self,
        contact_id: str,
        values: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BusinessContactRecord:
        row = self._session.get(BusinessContact, _text(contact_id))
        if row is None:
            raise KeyError(f"Contact {contact_id} introuvable")
        if expected_version is not None and int(expected_version) != int(row.version or 1):
            raise ApplicationConflictError(
                "Le contact a été modifié depuis sa lecture.",
                code="business_contact_version_conflict",
                context={
                    "contact_id": row.id,
                    "expected_version": int(expected_version),
                    "current_version": int(row.version or 1),
                },
            )
        candidate_external_system = (
            _optional_text(values.get("external_system"))
            if "external_system" in values
            else row.external_system
        )
        candidate_external_entity = (
            _optional_text(values.get("external_entity"))
            if "external_entity" in values
            else row.external_entity
        )
        candidate_external_id = (
            _optional_text(values.get("external_id"))
            if "external_id" in values
            else row.external_id
        )
        self._ensure_external_identity_available(
            external_system=candidate_external_system,
            external_entity=candidate_external_entity,
            external_id=candidate_external_id,
            exclude_contact_id=row.id,
        )

        changed = False
        for field in (
            "display_name",
            "email",
            "phone",
            "active",
            "source",
            "external_system",
            "external_entity",
            "external_id",
        ):
            if field not in values:
                continue
            value = values[field]
            if field in {"display_name", "source"}:
                value = _text(value)
            elif field in {"email", "phone", "external_system", "external_entity", "external_id"}:
                value = _optional_text(value)
            elif field == "active":
                value = bool(value)
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
        if changed:
            row.version = int(row.version or 1) + 1
            self._session.flush()
        return _record(row)

    def project_link(self, project_number: str) -> ContactLinkRecord | None:
        row = self._session.scalar(
            select(Project).where(Project.number == _text(project_number))
        )
        if row is None:
            return None
        managers = ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(self._session)
        ).resolve_project(row.id)
        return ContactLinkRecord(
            entity_type="PROJECT",
            entity_id=row.id,
            entity_label=row.number,
            project_manager_contact_id=(
                managers.primary.business_contact_id
                if managers.primary is not None
                else None
            ),
        )

    def set_project_manager_contact(
        self, project_number: str, contact_id: str | None
    ) -> ContactLinkRecord:
        raise ApplicationValidationError(
            "Le chargé principal du projet est autoritaire dans l’ERP et ne peut pas être modifié localement.",
            code="project_manager_erp_authoritative",
            context={"project_number": _text(project_number)},
        )

    def task_link(self, task_id: str) -> ContactLinkRecord | None:
        row = self._session.get(TaskCatalogEntry, _text(task_id))
        if row is None:
            return None
        return ContactLinkRecord(
            entity_type="TASK",
            entity_id=row.id,
            entity_label=f"{row.project_number}:{row.task_code}",
            operational_responsible_contact_id=row.operational_responsible_contact_id,
            coordinator_contact_id=row.coordinator_contact_id,
        )

    def set_task_contacts(
        self, task_id: str, values: Mapping[str, str | None]
    ) -> ContactLinkRecord:
        row = self._session.get(TaskCatalogEntry, _text(task_id))
        if row is None:
            raise KeyError(f"Tâche {task_id} introuvable")
        if "operational_responsible_contact_id" in values:
            row.operational_responsible_contact_id = self._require_contact(
                values.get("operational_responsible_contact_id")
            )
        if "coordinator_contact_id" in values:
            row.coordinator_contact_id = self._require_contact(
                values.get("coordinator_contact_id")
            )
        self._session.flush()
        return self.task_link(task_id)  # type: ignore[return-value]

    def resource_link(self, resource_id: str) -> ContactLinkRecord | None:
        row = self._session.get(Resource, _text(resource_id))
        if row is None:
            return None
        return ContactLinkRecord(
            entity_type="RESOURCE",
            entity_id=row.id,
            entity_label=row.name,
            coordinator_contact_id=row.coordinator_contact_id,
        )

    def set_resource_coordinator(
        self, resource_id: str, contact_id: str | None
    ) -> ContactLinkRecord:
        row = self._session.get(Resource, _text(resource_id))
        if row is None:
            raise KeyError(f"Ressource {resource_id} introuvable")
        row.coordinator_contact_id = self._require_contact(contact_id)
        self._session.flush()
        return self.resource_link(resource_id)  # type: ignore[return-value]

    def _demand(self, demand_number: str) -> WorkforceRequest | None:
        wanted = _text(demand_number)
        return self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )

    def demand_link(self, demand_number: str) -> ContactLinkRecord | None:
        row = self._demand(demand_number)
        if row is None:
            return None
        return ContactLinkRecord(
            entity_type="DEMAND",
            entity_id=row.id,
            entity_label=_text(row.legacy_demand_number) or row.id,
            operational_responsible_override_contact_id=(
                row.operational_responsible_override_contact_id
            ),
            aggregate_version=int(row.aggregate_version or 1),
            status=row.status,
        )

    def set_demand_override(
        self,
        demand_number: str,
        contact_id: str | None,
        *,
        expected_version: int,
        actor_name: str,
    ) -> DemandOverrideMutationResult:
        row = self._demand(demand_number)
        if row is None:
            raise KeyError(f"Demande {demand_number} introuvable")
        current_version = int(row.aggregate_version or 1)
        if int(expected_version) != current_version:
            raise ApplicationConflictError(
                "La demande a été modifiée depuis sa lecture.",
                code="demand_version_conflict",
                context={
                    "demand_number": _text(row.legacy_demand_number) or row.id,
                    "expected_version": int(expected_version),
                    "current_version": current_version,
                },
            )
        wanted = self._require_contact(contact_id)
        previous = row.operational_responsible_override_contact_id
        if previous == wanted:
            return DemandOverrideMutationResult(
                demand_number=_text(row.legacy_demand_number) or row.id,
                contact_id=wanted,
                version=current_version,
                status=row.status,
                reapproval_required=False,
                changed=False,
            )

        previous_status = row.status
        reapproval_required = row.status == "En planification"
        row.operational_responsible_override_contact_id = wanted
        if reapproval_required:
            row.status = "Soumise"
            row.approved_by_external_id = None
            row.approved_by_name = None
            row.approved_at = None
            row.approval_comment = (
                "Responsable opérationnel modifié après approbation — "
                "nouvelle approbation requise"
            )
        row.aggregate_version = current_version + 1
        details = json.dumps(
            {
                "aggregate_version": int(row.aggregate_version),
                "line_mode": bool(row.line_mode),
                "changed_fields": [
                    "operational_responsible_override_contact_id"
                ],
                "previous_contact_id": previous,
                "contact_id": wanted,
                "reapproval_required": reapproval_required,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=row.id,
                action="Modification responsable opérationnel",
                previous_status=previous_status,
                status=row.status,
                comment=(
                    "Nouvelle approbation requise; la planification existante est conservée"
                    if reapproval_required
                    else "Override du responsable opérationnel modifié"
                ),
                details=details,
                actor_name=_optional_text(actor_name),
                occurred_at=utc_now(),
            )
        )
        self._session.flush()
        return DemandOverrideMutationResult(
            demand_number=_text(row.legacy_demand_number) or row.id,
            contact_id=wanted,
            version=int(row.aggregate_version),
            status=row.status,
            reapproval_required=reapproval_required,
            changed=True,
        )
