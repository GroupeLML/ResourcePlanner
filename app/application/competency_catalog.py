from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .commands.common import UNSET, UnsetType, required_text
from .errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    call_application_port,
)


@dataclass(frozen=True, slots=True)
class CompetencyReadModel:
    id: str
    name: str
    description: str | None = None
    active: bool = True
    sort_order: int = 0
    resource_class_code: str | None = None
    resource_class_label: str | None = None
    resource_class_active: bool | None = None
    resource_class_version: int = 1


@dataclass(frozen=True, slots=True)
class CompetencyResourceClassReadModel:
    code: str
    label: str
    active: bool


@dataclass(frozen=True, slots=True)
class CompetencyResourceClassMutationResult:
    competency_id: str
    resource_class_code: str | None
    version: int
    action: str

    def to_dict(self) -> dict[str, object]:
        return {
            "competency_id": self.competency_id,
            "resource_class_code": self.resource_class_code,
            "version": self.version,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class CompetencyCreateCommand:
    name: str
    description: str | None = None
    active: bool = True
    sort_order: int = 0

    def __post_init__(self) -> None:
        required_text(
            self.name,
            field="competency_name",
            message="Le nom de la compétence est requis.",
        )
        if self.sort_order < 0:
            raise ApplicationValidationError(
                "L'ordre de la compétence ne peut pas être négatif.",
                code="competency_sort_order_invalid",
                context={"field": "sort_order", "value": self.sort_order},
            )


@dataclass(frozen=True, slots=True)
class CompetencyUpdateCommand:
    competency_id: str
    name: str | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
    active: bool | UnsetType = UNSET
    sort_order: int | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.competency_id,
            field="competency_id",
            message="L'identifiant de la compétence est requis.",
        )
        if self.name is not UNSET:
            required_text(
                self.name,
                field="competency_name",
                message="Le nom de la compétence est requis.",
            )
        if self.sort_order is not UNSET and int(self.sort_order) < 0:
            raise ApplicationValidationError(
                "L'ordre de la compétence ne peut pas être négatif.",
                code="competency_sort_order_invalid",
                context={"field": "sort_order", "value": self.sort_order},
            )

    def changes(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in ("name", "description", "active", "sort_order"):
            value = getattr(self, field)
            if value is not UNSET:
                result[field] = value
        return result


@dataclass(frozen=True, slots=True)
class CompetencyMutationResult:
    competency_id: str
    action: str

    def to_dict(self) -> dict[str, object]:
        return {"competency_id": self.competency_id, "action": self.action}


class CompetencyCatalogRepositoryPort(Protocol):
    def list_competencies(
        self,
        *,
        query: str | None = None,
        active_only: bool = True,
        limit: int = 200,
    ) -> Sequence[CompetencyReadModel]: ...

    def get_competency(self, competency_id: str) -> CompetencyReadModel | None: ...

    def find_by_name(self, name: str) -> CompetencyReadModel | None: ...

    def create_competency(self, values: Mapping[str, Any]) -> str: ...

    def update_competency(self, competency_id: str, values: Mapping[str, Any]) -> str: ...

    def get_resource_class(
        self,
        code: str,
    ) -> CompetencyResourceClassReadModel | None: ...

    def set_competency_resource_class(
        self,
        competency_id: str,
        resource_class_code: str | None,
        *,
        actor_user_id: str,
        expected_version: int,
    ) -> CompetencyResourceClassMutationResult: ...

    def set_resource_competencies(
        self,
        resource_id: str,
        competency_ids: Sequence[str],
    ) -> None: ...

    def set_demand_competencies(
        self,
        demand_number: str,
        competency_ids: Sequence[str],
    ) -> None: ...

    def set_segment_competency(
        self,
        segment_id: str,
        competency_id: str | None,
    ) -> None: ...


class CompetencyCatalogService:
    """Canonical local competency catalog and stable selections.

    Existing text fields are kept as compatibility snapshots by the SQL repository.
    Planning therefore keeps consuming its current vocabulary while React and new API
    clients use stable competency identifiers.
    """

    def __init__(self, repository: CompetencyCatalogRepositoryPort) -> None:
        self._repository = repository

    def list(
        self,
        *,
        query: str | None = None,
        active_only: bool = True,
        limit: int = 200,
    ) -> tuple[CompetencyReadModel, ...]:
        return tuple(
            call_application_port(
                lambda: self._repository.list_competencies(
                    query=query,
                    active_only=active_only,
                    limit=limit,
                ),
                code_prefix="competency_read",
            )
        )

    def create(self, command: CompetencyCreateCommand) -> CompetencyMutationResult:
        existing = call_application_port(
            lambda: self._repository.find_by_name(command.name),
            code_prefix="competency_read",
            context={"name": command.name},
        )
        if existing is not None:
            raise ApplicationConflictError(
                f"Une compétence nommée {command.name} existe déjà.",
                code="competency_name_conflict",
                context={"name": command.name, "competency_id": existing.id},
            )
        identifier = call_application_port(
            lambda: self._repository.create_competency(
                {
                    "name": command.name,
                    "description": command.description,
                    "active": command.active,
                    "sort_order": command.sort_order,
                }
            ),
            code_prefix="competency_create",
            context={"name": command.name},
        )
        return CompetencyMutationResult(str(identifier), "created")

    def update(self, command: CompetencyUpdateCommand) -> CompetencyMutationResult:
        current = call_application_port(
            lambda: self._repository.get_competency(command.competency_id),
            code_prefix="competency_read",
            context={"competency_id": command.competency_id},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Compétence {command.competency_id} introuvable.",
                code="competency_not_found",
                context={"competency_id": command.competency_id},
            )
        changes = command.changes()
        if "name" in changes:
            existing = call_application_port(
                lambda: self._repository.find_by_name(str(changes["name"])),
                code_prefix="competency_read",
                context={"name": changes["name"]},
            )
            if existing is not None and existing.id != command.competency_id:
                raise ApplicationConflictError(
                    f"Une compétence nommée {changes['name']} existe déjà.",
                    code="competency_name_conflict",
                    context={
                        "name": changes["name"],
                        "competency_id": existing.id,
                    },
                )
        identifier = call_application_port(
            lambda: self._repository.update_competency(
                command.competency_id,
                changes,
            ),
            code_prefix="competency_update",
            context={"competency_id": command.competency_id},
        )
        return CompetencyMutationResult(str(identifier), "updated")

    def deactivate(self, competency_id: str) -> CompetencyMutationResult:
        return self.update(
            CompetencyUpdateCommand(competency_id=competency_id, active=False)
        )

    def set_resource_class(
        self,
        competency_id: str,
        resource_class_code: str | None,
        *,
        actor_user_id: str | None,
        expected_version: int,
    ) -> CompetencyResourceClassMutationResult:
        current = call_application_port(
            lambda: self._repository.get_competency(competency_id),
            code_prefix="competency_read",
            context={"competency_id": competency_id},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"Compétence {competency_id} introuvable.",
                code="competency_not_found",
                context={"competency_id": competency_id},
            )
        actor = str(actor_user_id or "").strip()
        if not actor:
            raise ApplicationAuthorizationError(
                "Une identité locale authentifiée est requise pour administrer la classe de regroupement.",
                code="competency_resource_class_actor_required",
                context={"competency_id": competency_id},
            )
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "La version attendue du rattachement de classe est invalide.",
                code="competency_resource_class_version_invalid",
                context={"expected_version": expected_version},
            ) from exc
        if expected < 1:
            raise ApplicationValidationError(
                "La version attendue du rattachement de classe doit être positive.",
                code="competency_resource_class_version_invalid",
                context={"expected_version": expected},
            )
        class_code = str(resource_class_code or "").strip() or None
        if class_code is not None:
            resource_class = call_application_port(
                lambda: self._repository.get_resource_class(class_code),
                code_prefix="resource_class_read",
                context={"code": class_code},
            )
            if resource_class is None:
                raise ApplicationNotFoundError(
                    f"Classe {class_code} introuvable.",
                    code="competency_resource_class_not_found",
                    context={"code": class_code},
                )
            if not resource_class.active:
                raise ApplicationValidationError(
                    f"La classe {resource_class.code} est inactive.",
                    code="competency_resource_class_inactive",
                    context={"code": resource_class.code},
                )
            class_code = resource_class.code
        return call_application_port(
            lambda: self._repository.set_competency_resource_class(
                competency_id,
                class_code,
                actor_user_id=actor,
                expected_version=expected,
            ),
            code_prefix="competency_resource_class_update",
            context={"competency_id": competency_id},
        )

    def resolve_selection(
        self,
        competency_ids: Sequence[str] | None,
        *,
        allow_inactive: bool = False,
    ) -> tuple[CompetencyReadModel, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in competency_ids or ():
            identifier = str(value or "").strip()
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            normalized.append(identifier)

        result: list[CompetencyReadModel] = []
        for identifier in normalized:
            row = call_application_port(
                lambda identifier=identifier: self._repository.get_competency(identifier),
                code_prefix="competency_read",
                context={"competency_id": identifier},
            )
            if row is None:
                raise ApplicationNotFoundError(
                    f"Compétence {identifier} introuvable.",
                    code="competency_not_found",
                    context={"competency_id": identifier},
                )
            if not row.active and not allow_inactive:
                raise ApplicationValidationError(
                    f"La compétence {row.name} est inactive.",
                    code="competency_inactive",
                    context={"competency_id": row.id, "name": row.name},
                )
            result.append(row)
        return tuple(result)

    def snapshot_text(self, competency_ids: Sequence[str] | None) -> str | None:
        rows = self.resolve_selection(competency_ids)
        text = "; ".join(row.name for row in rows)
        return text or None

    def assign_resource(
        self,
        resource_id: str,
        competency_ids: Sequence[str] | None,
    ) -> None:
        rows = self.resolve_selection(competency_ids)
        call_application_port(
            lambda: self._repository.set_resource_competencies(
                resource_id,
                tuple(row.id for row in rows),
            ),
            code_prefix="resource_competency_update",
            context={"resource_id": resource_id},
        )

    def assign_demand(
        self,
        demand_number: str,
        competency_ids: Sequence[str] | None,
    ) -> None:
        rows = self.resolve_selection(competency_ids)
        call_application_port(
            lambda: self._repository.set_demand_competencies(
                demand_number,
                tuple(row.id for row in rows),
            ),
            code_prefix="demand_competency_update",
            context={"demand_number": demand_number},
        )

    def assign_segment(
        self,
        segment_id: str,
        competency_id: str | None,
    ) -> None:
        normalized = str(competency_id or "").strip() or None
        if normalized is not None:
            rows = self.resolve_selection((normalized,))
            normalized = rows[0].id
        call_application_port(
            lambda: self._repository.set_segment_competency(segment_id, normalized),
            code_prefix="segment_competency_update",
            context={"segment_id": segment_id},
        )
