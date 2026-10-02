from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    call_application_port,
)


@dataclass(frozen=True, slots=True)
class BusinessContactRecord:
    id: str
    display_name: str
    email: str | None
    phone: str | None
    active: bool
    source: str
    external_system: str | None
    external_entity: str | None
    external_id: str | None
    version: int


@dataclass(frozen=True, slots=True)
class ContactLinkRecord:
    entity_type: str
    entity_id: str
    entity_label: str
    project_manager_contact_id: str | None = None
    operational_responsible_contact_id: str | None = None
    coordinator_contact_id: str | None = None
    operational_responsible_override_contact_id: str | None = None
    aggregate_version: int | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class DemandOverrideMutationResult:
    demand_number: str
    contact_id: str | None
    version: int
    status: str
    reapproval_required: bool
    changed: bool


class BusinessContactAdminRepositoryPort(Protocol):
    def list_contacts(
        self,
        *,
        active_only: bool = False,
        user_backed_only: bool = False,
    ) -> tuple[BusinessContactRecord, ...]: ...
    def get_contact(self, contact_id: str) -> BusinessContactRecord | None: ...
    def create_contact(self, values: Mapping[str, Any]) -> BusinessContactRecord: ...
    def update_contact(
        self,
        contact_id: str,
        values: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BusinessContactRecord: ...
    def project_link(self, project_number: str) -> ContactLinkRecord | None: ...
    def set_project_manager_contact(
        self, project_number: str, contact_id: str | None
    ) -> ContactLinkRecord: ...
    def task_link(self, task_id: str) -> ContactLinkRecord | None: ...
    def set_task_contacts(
        self, task_id: str, values: Mapping[str, str | None]
    ) -> ContactLinkRecord: ...
    def resource_link(self, resource_id: str) -> ContactLinkRecord | None: ...
    def set_resource_coordinator(
        self, resource_id: str, contact_id: str | None
    ) -> ContactLinkRecord: ...
    def demand_link(self, demand_number: str) -> ContactLinkRecord | None: ...
    def set_demand_override(
        self,
        demand_number: str,
        contact_id: str | None,
        *,
        expected_version: int,
        actor_name: str,
    ) -> DemandOverrideMutationResult: ...


def _required_text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApplicationValidationError(
            f"{field} est requis.",
            code="business_contact_field_required",
            context={"field": field},
        )
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class BusinessContactAdminService:
    def __init__(self, repository: BusinessContactAdminRepositoryPort) -> None:
        self._repository = repository

    def list_contacts(
        self,
        *,
        active_only: bool = False,
        user_backed_only: bool = False,
    ) -> tuple[BusinessContactRecord, ...]:
        return call_application_port(
            lambda: self._repository.list_contacts(
                active_only=active_only,
                user_backed_only=user_backed_only,
            ),
            code_prefix="business_contact_list",
        )

    def create_contact(
        self,
        *,
        display_name: str,
        email: str | None = None,
        phone: str | None = None,
        active: bool = True,
        source: str = "LOCAL",
        external_system: str | None = None,
        external_entity: str | None = None,
        external_id: str | None = None,
    ) -> BusinessContactRecord:
        name = _required_text(display_name, field="display_name")
        ext_id = _optional_text(external_id)
        ext_system = _optional_text(external_system)
        ext_entity = _optional_text(external_entity)
        if ext_id and (not ext_system or not ext_entity):
            raise ApplicationValidationError(
                "Une identité externe exige external_system et external_entity.",
                code="business_contact_external_identity_incomplete",
            )
        return call_application_port(
            lambda: self._repository.create_contact(
                {
                    "display_name": name,
                    "email": _optional_text(email),
                    "phone": _optional_text(phone),
                    "active": bool(active),
                    "source": _required_text(source, field="source"),
                    "external_system": ext_system,
                    "external_entity": ext_entity,
                    "external_id": ext_id,
                }
            ),
            code_prefix="business_contact_create",
            context={"display_name": name},
        )

    def update_contact(
        self,
        contact_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BusinessContactRecord:
        identifier = _required_text(contact_id, field="contact_id")
        current = call_application_port(
            lambda: self._repository.get_contact(identifier),
            code_prefix="business_contact_read",
            context={"contact_id": identifier},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Contact {identifier} introuvable.",
                code="business_contact_not_found",
                context={"contact_id": identifier},
            )
        normalized = dict(changes)
        if "display_name" in normalized:
            normalized["display_name"] = _required_text(
                normalized["display_name"], field="display_name"
            )
        for field in ("email", "phone", "external_system", "external_entity", "external_id"):
            if field in normalized:
                normalized[field] = _optional_text(normalized[field])
        if "source" in normalized:
            normalized["source"] = _required_text(normalized["source"], field="source")

        ext_id = normalized.get("external_id", current.external_id)
        ext_system = normalized.get("external_system", current.external_system)
        ext_entity = normalized.get("external_entity", current.external_entity)
        if ext_id and (not ext_system or not ext_entity):
            raise ApplicationValidationError(
                "Une identité externe exige external_system et external_entity.",
                code="business_contact_external_identity_incomplete",
            )
        return call_application_port(
            lambda: self._repository.update_contact(
                identifier,
                normalized,
                expected_version=expected_version,
            ),
            code_prefix="business_contact_update",
            context={"contact_id": identifier},
        )

    def project_link(self, project_number: str) -> ContactLinkRecord:
        number = _required_text(project_number, field="project_number")
        row = call_application_port(
            lambda: self._repository.project_link(number),
            code_prefix="project_contact_link_read",
            context={"project_number": number},
        )
        if row is None:
            raise ApplicationNotFoundError(
                f"Projet {number} introuvable.",
                code="project_not_found",
                context={"project_number": number},
            )
        return row

    def set_project_manager_contact(
        self, project_number: str, contact_id: str | None
    ) -> ContactLinkRecord:
        number = _required_text(project_number, field="project_number")
        raise ApplicationValidationError(
            "Le chargé principal du projet est autoritaire dans l’ERP et ne peut pas être modifié localement.",
            code="project_manager_erp_authoritative",
            context={"project_number": number},
        )

    def task_link(self, task_id: str) -> ContactLinkRecord:
        identifier = _required_text(task_id, field="task_id")
        row = call_application_port(
            lambda: self._repository.task_link(identifier),
            code_prefix="task_contact_link_read",
            context={"task_id": identifier},
        )
        if row is None:
            raise ApplicationNotFoundError(
                f"Tâche {identifier} introuvable.",
                code="task_not_found",
                context={"task_id": identifier},
            )
        return row

    def set_task_contacts(
        self, task_id: str, changes: Mapping[str, str | None]
    ) -> ContactLinkRecord:
        identifier = _required_text(task_id, field="task_id")
        normalized = {
            field: _optional_text(value)
            for field, value in changes.items()
            if field in {"operational_responsible_contact_id", "coordinator_contact_id"}
        }
        if not normalized:
            raise ApplicationValidationError(
                "Au moins un rattachement de tâche doit être fourni.",
                code="task_contact_link_empty",
            )
        return call_application_port(
            lambda: self._repository.set_task_contacts(identifier, normalized),
            code_prefix="task_contact_link_update",
            context={"task_id": identifier},
        )

    def resource_link(self, resource_id: str) -> ContactLinkRecord:
        identifier = _required_text(resource_id, field="resource_id")
        row = call_application_port(
            lambda: self._repository.resource_link(identifier),
            code_prefix="resource_contact_link_read",
            context={"resource_id": identifier},
        )
        if row is None:
            raise ApplicationNotFoundError(
                f"Ressource {identifier} introuvable.",
                code="resource_not_found",
                context={"resource_id": identifier},
            )
        return row

    def set_resource_coordinator(
        self, resource_id: str, contact_id: str | None
    ) -> ContactLinkRecord:
        identifier = _required_text(resource_id, field="resource_id")
        return call_application_port(
            lambda: self._repository.set_resource_coordinator(
                identifier, _optional_text(contact_id)
            ),
            code_prefix="resource_contact_link_update",
            context={"resource_id": identifier},
        )

    def demand_link(self, demand_number: str) -> ContactLinkRecord:
        number = _required_text(demand_number, field="demand_number")
        row = call_application_port(
            lambda: self._repository.demand_link(number),
            code_prefix="demand_contact_link_read",
            context={"demand_number": number},
        )
        if row is None:
            raise ApplicationNotFoundError(
                f"Demande {number} introuvable.",
                code="demand_not_found",
                context={"demand_number": number},
            )
        return row

    def set_demand_override(
        self,
        demand_number: str,
        contact_id: str | None,
        *,
        expected_version: int,
        actor_name: str,
    ) -> DemandOverrideMutationResult:
        number = _required_text(demand_number, field="demand_number")
        if int(expected_version) < 1:
            raise ApplicationValidationError(
                "expected_version doit être supérieur ou égal à 1.",
                code="demand_version_invalid",
            )
        return call_application_port(
            lambda: self._repository.set_demand_override(
                number,
                _optional_text(contact_id),
                expected_version=int(expected_version),
                actor_name=str(actor_name or "").strip(),
            ),
            code_prefix="demand_contact_override_update",
            context={"demand_number": number},
        )
