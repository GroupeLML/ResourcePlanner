from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
import unicodedata
from typing import Any, Protocol

from .commands.common import UNSET, UnsetType, required_text, validate_date_window
from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    call_application_port,
)
from .query_models import ResourceAvailabilityRuleReadModel, ResourceReadModel


AVAILABILITY_STANDARD = "Horaire standard"
AVAILABILITY_VACATION = "Vacances"
AVAILABILITY_HOLIDAY = "Jour férié"
AVAILABILITY_TYPES = (
    AVAILABILITY_STANDARD,
    AVAILABILITY_VACATION,
    AVAILABILITY_HOLIDAY,
)
AVAILABILITY_WEEKDAYS = ("Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim")
RESOURCE_ORDER_UP = "up"
RESOURCE_ORDER_DOWN = "down"
RESOURCE_ORDER_DIRECTIONS = (RESOURCE_ORDER_UP, RESOURCE_ORDER_DOWN)


def _resource_alpha_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _resource_manual_order_key(resource: ResourceReadModel) -> tuple[object, ...]:
    return (
        int(resource.sort_order),
        _resource_alpha_key(resource.name),
        resource.name.casefold(),
        resource.id,
    )


@dataclass(frozen=True, slots=True)
class ResourceCreateCommand:
    name: str
    email: str | None = None
    resource_class: str | None = None
    competencies: str | None = None
    note: str | None = None
    active: bool = True
    sort_order: int = 0
    external_id: str | None = None

    def __post_init__(self) -> None:
        required_text(self.name, field="resource_name", message="Le nom de la ressource est requis.")
        if self.sort_order < 0:
            raise ApplicationValidationError(
                "L'ordre de la ressource ne peut pas être négatif.",
                code="resource_sort_order_invalid",
                context={"field": "sort_order", "value": self.sort_order},
            )


@dataclass(frozen=True, slots=True)
class ResourceUpdateCommand:
    resource_id: str
    name: str | UnsetType = UNSET
    email: str | None | UnsetType = UNSET
    resource_class: str | None | UnsetType = UNSET
    competencies: str | None | UnsetType = UNSET
    note: str | None | UnsetType = UNSET
    active: bool | UnsetType = UNSET
    sort_order: int | UnsetType = UNSET
    external_id: str | None | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.resource_id,
            field="resource_id",
            message="L'identifiant de la ressource est requis.",
        )
        if self.name is not UNSET:
            required_text(self.name, field="resource_name", message="Le nom de la ressource est requis.")
        if self.sort_order is not UNSET and int(self.sort_order) < 0:
            raise ApplicationValidationError(
                "L'ordre de la ressource ne peut pas être négatif.",
                code="resource_sort_order_invalid",
                context={"field": "sort_order", "value": self.sort_order},
            )

    def changes(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in (
            "name",
            "email",
            "resource_class",
            "competencies",
            "note",
            "active",
            "sort_order",
            "external_id",
        ):
            value = getattr(self, field)
            if value is not UNSET:
                result[field] = value
        return result


@dataclass(frozen=True, slots=True)
class ResourceReorderCommand:
    resource_id: str
    direction: str

    def __post_init__(self) -> None:
        required_text(
            self.resource_id,
            field="resource_id",
            message="L'identifiant de la ressource est requis.",
        )
        if self.direction not in RESOURCE_ORDER_DIRECTIONS:
            raise ApplicationValidationError(
                "La direction de réordonnancement doit être up ou down.",
                code="resource_reorder_direction_invalid",
                context={"direction": self.direction},
            )


@dataclass(frozen=True, slots=True)
class AvailabilityRuleCreateCommand:
    availability_type: str
    resource_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    weekdays: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    note: str | None = None
    active: bool = True

    def __post_init__(self) -> None:
        _validate_availability_values(
            availability_type=self.availability_type,
            resource_id=self.resource_id,
            start_date=self.start_date,
            end_date=self.end_date,
            weekdays=self.weekdays,
            start_time=self.start_time,
            end_time=self.end_time,
        )


@dataclass(frozen=True, slots=True)
class AvailabilityRuleUpdateCommand:
    rule_id: str
    availability_type: str | UnsetType = UNSET
    resource_id: str | None | UnsetType = UNSET
    start_date: date | None | UnsetType = UNSET
    end_date: date | None | UnsetType = UNSET
    weekdays: str | None | UnsetType = UNSET
    start_time: time | None | UnsetType = UNSET
    end_time: time | None | UnsetType = UNSET
    note: str | None | UnsetType = UNSET
    active: bool | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.rule_id,
            field="availability_rule_id",
            message="L'identifiant de la règle de disponibilité est requis.",
        )

    def changes(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in (
            "availability_type",
            "resource_id",
            "start_date",
            "end_date",
            "weekdays",
            "start_time",
            "end_time",
            "note",
            "active",
        ):
            value = getattr(self, field)
            if value is not UNSET:
                result[field] = value
        return result


@dataclass(frozen=True, slots=True)
class ResourceMutationResult:
    resource_id: str
    action: str

    def to_dict(self) -> dict[str, object]:
        return {"resource_id": self.resource_id, "action": self.action}


@dataclass(frozen=True, slots=True)
class AvailabilityRuleMutationResult:
    rule_id: str
    action: str

    def to_dict(self) -> dict[str, object]:
        return {"rule_id": self.rule_id, "action": self.action}


class ResourceAdminRepositoryPort(Protocol):
    def list_resources(self, *, active_only: bool = False) -> tuple[ResourceReadModel, ...]: ...

    def get_resource(self, resource_id: str) -> ResourceReadModel | None: ...

    def find_resource_by_name(self, name: str) -> ResourceReadModel | None: ...

    def create_resource(self, values: Mapping[str, Any]) -> str: ...

    def update_resource(self, resource_id: str, values: Mapping[str, Any]) -> str: ...

    def replace_resource_sort_order(self, ordered_resource_ids: Sequence[str]) -> None: ...

    def get_availability_rule(self, rule_id: str) -> ResourceAvailabilityRuleReadModel | None: ...

    def create_availability_rule(self, values: Mapping[str, Any]) -> str: ...

    def update_availability_rule(self, rule_id: str, values: Mapping[str, Any]) -> str: ...


def _validate_weekdays(value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    tokens = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    invalid = [token for token in tokens if token not in AVAILABILITY_WEEKDAYS]
    if invalid:
        raise ApplicationValidationError(
            "Un ou plusieurs jours de semaine ne sont pas reconnus.",
            code="availability_weekdays_invalid",
            context={"invalid": invalid, "allowed": list(AVAILABILITY_WEEKDAYS)},
        )


def _validate_availability_values(
    *,
    availability_type: object,
    resource_id: object,
    start_date: date | None,
    end_date: date | None,
    weekdays: object,
    start_time: time | None,
    end_time: time | None,
) -> None:
    kind = required_text(
        availability_type,
        field="availability_type",
        message="Le type de disponibilité est requis.",
    )
    if kind not in AVAILABILITY_TYPES:
        raise ApplicationValidationError(
            "Le type de disponibilité n'est pas supporté.",
            code="availability_type_invalid",
            context={"availability_type": kind, "allowed": list(AVAILABILITY_TYPES)},
        )
    resource = str(resource_id or "").strip()
    if kind != AVAILABILITY_HOLIDAY and not resource:
        raise ApplicationValidationError(
            "Une ressource est requise pour cette règle de disponibilité.",
            code="availability_resource_required",
            context={"availability_type": kind},
        )
    validate_date_window(start_date, end_date, prefix="availability")
    _validate_weekdays(weekdays)
    if kind == AVAILABILITY_STANDARD and (start_time is None or end_time is None):
        raise ApplicationValidationError(
            "L'heure de début et l'heure de fin sont requises pour un horaire standard.",
            code="availability_standard_time_required",
            context={"availability_type": kind},
        )
    if kind in {AVAILABILITY_VACATION, AVAILABILITY_HOLIDAY} and start_date is None:
        raise ApplicationValidationError(
            "Une date de début est requise pour cette règle de disponibilité.",
            code="availability_start_date_required",
            context={"availability_type": kind},
        )


class ResourceAdminService:
    """Application rules for resource profiles and availability administration."""

    def __init__(self, repository: ResourceAdminRepositoryPort) -> None:
        self._repository = repository

    def create_resource(self, command: ResourceCreateCommand) -> ResourceMutationResult:
        existing = call_application_port(
            lambda: self._repository.find_resource_by_name(command.name),
            code_prefix="resource_read",
            context={"name": command.name},
        )
        if existing is not None:
            raise ApplicationConflictError(
                f"Une ressource nommée {command.name} existe déjà.",
                code="resource_name_conflict",
                context={"name": command.name, "resource_id": existing.id},
            )
        identifier = call_application_port(
            lambda: self._repository.create_resource(
                {
                    "name": command.name,
                    "email": command.email,
                    "resource_class": command.resource_class,
                    "competencies": command.competencies,
                    "note": command.note,
                    "active": command.active,
                    "sort_order": command.sort_order,
                    "external_id": command.external_id,
                }
            ),
            code_prefix="resource_create",
            context={"name": command.name},
        )
        return ResourceMutationResult(str(identifier), "created")

    def update_resource(self, command: ResourceUpdateCommand) -> ResourceMutationResult:
        current = call_application_port(
            lambda: self._repository.get_resource(command.resource_id),
            code_prefix="resource_read",
            context={"resource_id": command.resource_id},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Ressource {command.resource_id} introuvable.",
                code="resource_not_found",
                context={"resource_id": command.resource_id},
            )
        changes = command.changes()
        if "name" in changes:
            existing = call_application_port(
                lambda: self._repository.find_resource_by_name(str(changes["name"])),
                code_prefix="resource_read",
                context={"name": str(changes["name"])},
            )
            if existing is not None and existing.id != command.resource_id:
                raise ApplicationConflictError(
                    f"Une ressource nommée {changes['name']} existe déjà.",
                    code="resource_name_conflict",
                    context={"name": changes["name"], "resource_id": existing.id},
                )
        identifier = call_application_port(
            lambda: self._repository.update_resource(command.resource_id, changes),
            code_prefix="resource_update",
            context={"resource_id": command.resource_id},
        )
        return ResourceMutationResult(str(identifier), "updated")

    def reorder_resource(self, command: ResourceReorderCommand) -> ResourceMutationResult:
        current = call_application_port(
            lambda: self._repository.get_resource(command.resource_id),
            code_prefix="resource_read",
            context={"resource_id": command.resource_id},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Ressource {command.resource_id} introuvable.",
                code="resource_not_found",
                context={"resource_id": command.resource_id},
            )
        if not current.active or not current.erp_active:
            raise ApplicationValidationError(
                "Une ressource inactive ne peut pas être réordonnée depuis le planning.",
                code="resource_reorder_inactive",
                context={"resource_id": command.resource_id},
            )

        resources = call_application_port(
            lambda: self._repository.list_resources(active_only=True),
            code_prefix="resource_read",
            context={"resource_id": command.resource_id},
        )
        class_key = current.resource_class or ""
        group = sorted(
            (
                resource
                for resource in resources
                if (resource.resource_class or "") == class_key
            ),
            key=_resource_manual_order_key,
        )
        current_index = next(
            (index for index, resource in enumerate(group) if resource.id == current.id),
            None,
        )
        if current_index is None:
            raise ApplicationValidationError(
                "La ressource n'est pas disponible dans le planning courant.",
                code="resource_reorder_unavailable",
                context={"resource_id": command.resource_id},
            )

        offset = -1 if command.direction == RESOURCE_ORDER_UP else 1
        target_index = current_index + offset
        if target_index < 0 or target_index >= len(group):
            return ResourceMutationResult(current.id, "unchanged")

        group[current_index], group[target_index] = group[target_index], group[current_index]
        ordered_ids = tuple(resource.id for resource in group)
        call_application_port(
            lambda: self._repository.replace_resource_sort_order(ordered_ids),
            code_prefix="resource_reorder",
            context={
                "resource_id": command.resource_id,
                "direction": command.direction,
                "resource_class": current.resource_class,
            },
        )
        return ResourceMutationResult(current.id, "reordered")

    def create_availability_rule(
        self,
        command: AvailabilityRuleCreateCommand,
    ) -> AvailabilityRuleMutationResult:
        if command.resource_id:
            self._require_resource(command.resource_id)
        identifier = call_application_port(
            lambda: self._repository.create_availability_rule(
                {
                    "availability_type": command.availability_type,
                    "resource_id": command.resource_id,
                    "start_date": command.start_date,
                    "end_date": command.end_date,
                    "weekdays": command.weekdays,
                    "start_time": command.start_time,
                    "end_time": command.end_time,
                    "note": command.note,
                    "active": command.active,
                }
            ),
            code_prefix="availability_create",
            context={"resource_id": command.resource_id, "availability_type": command.availability_type},
        )
        return AvailabilityRuleMutationResult(str(identifier), "created")

    def update_availability_rule(
        self,
        command: AvailabilityRuleUpdateCommand,
    ) -> AvailabilityRuleMutationResult:
        current = call_application_port(
            lambda: self._repository.get_availability_rule(command.rule_id),
            code_prefix="availability_read",
            context={"rule_id": command.rule_id},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Règle de disponibilité {command.rule_id} introuvable.",
                code="availability_rule_not_found",
                context={"rule_id": command.rule_id},
            )
        changes = command.changes()
        availability_type = changes.get("availability_type", current.availability_type)
        resource_id = changes.get("resource_id", current.resource_id)
        start_date = changes.get("start_date", current.start_date)
        end_date = changes.get("end_date", current.end_date)
        weekdays = changes.get("weekdays", current.weekdays)
        start_time = changes.get("start_time", current.start_time)
        end_time = changes.get("end_time", current.end_time)
        _validate_availability_values(
            availability_type=availability_type,
            resource_id=resource_id,
            start_date=start_date if isinstance(start_date, date) else None,
            end_date=end_date if isinstance(end_date, date) else None,
            weekdays=weekdays,
            start_time=start_time if isinstance(start_time, time) else None,
            end_time=end_time if isinstance(end_time, time) else None,
        )
        if resource_id:
            self._require_resource(str(resource_id))
        identifier = call_application_port(
            lambda: self._repository.update_availability_rule(command.rule_id, changes),
            code_prefix="availability_update",
            context={"rule_id": command.rule_id},
        )
        return AvailabilityRuleMutationResult(str(identifier), "updated")

    def deactivate_availability_rule(self, rule_id: str) -> AvailabilityRuleMutationResult:
        return self.update_availability_rule(
            AvailabilityRuleUpdateCommand(rule_id=rule_id, active=False)
        )

    def _require_resource(self, resource_id: str) -> ResourceReadModel:
        row = call_application_port(
            lambda: self._repository.get_resource(resource_id),
            code_prefix="resource_read",
            context={"resource_id": resource_id},
        )
        if row is None:
            raise ApplicationNotFoundError(
                f"Ressource {resource_id} introuvable.",
                code="resource_not_found",
                context={"resource_id": resource_id},
            )
        return row
