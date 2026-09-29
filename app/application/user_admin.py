from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from .security import (
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    ROLES,
    UserIdentityRecord,
    normalize_roles,
    permissions_for_roles,
)


ROLE_LABELS = {
    ROLE_ADMIN: "Administrateur",
    ROLE_COORDINATOR: "Coordonnateur",
    ROLE_PROJECT_MANAGER: "Chargé de projet",
    ROLE_MANAGER: "Gestionnaire",
    ROLE_TECHNICIAN: "Technicien",
    ROLE_DELIVERY_CONTRIBUTOR: "Contributeur Delivery",
}


@dataclass(frozen=True, slots=True)
class UserRoleDefinition:
    role: str
    label: str
    permissions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "label": self.label,
            "permissions": list(self.permissions),
        }


class UserAdminRepositoryPort(Protocol):
    def list_users(self) -> tuple[UserIdentityRecord, ...]: ...

    def get_by_id(self, user_id: str) -> UserIdentityRecord | None: ...

    def get_by_external_identity(self, issuer: str, subject: str) -> UserIdentityRecord | None: ...

    def upsert(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        active: bool = True,
        employee_external_id: str | None = None,
    ) -> UserIdentityRecord: ...
    def set_business_phone(
        self,
        user_id: str,
        phone: str | None,
    ) -> UserIdentityRecord: ...


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApplicationValidationError(
            f"{field} est requis.",
            code="user_admin_required_field",
            context={"field": field},
        )
    return text


def _roles(values: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    try:
        roles = normalize_roles(values)
    except ValueError as exc:
        raise ApplicationValidationError(
            str(exc),
            code="user_admin_invalid_roles",
        ) from exc
    if not roles:
        raise ApplicationValidationError(
            "Au moins un rôle RessourcePlanner est requis.",
            code="user_admin_roles_required",
        )
    return roles


class UserAdminService:
    def __init__(self, repository: UserAdminRepositoryPort) -> None:
        self._repository = repository

    def role_catalog(self) -> tuple[UserRoleDefinition, ...]:
        return tuple(
            UserRoleDefinition(
                role=role,
                label=ROLE_LABELS[role],
                permissions=permissions_for_roles((role,)),
            )
            for role in ROLES
        )

    def list_users(self) -> tuple[UserIdentityRecord, ...]:
        return self._repository.list_users()

    def create_user(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        phone: str | None = None,
        active: bool = True,
    ) -> UserIdentityRecord:
        issuer_value = _required(issuer, "issuer")
        subject_value = _required(subject, "subject")
        display_name_value = _required(display_name, "display_name")
        normalized_roles = _roles(roles)
        if self._repository.get_by_external_identity(issuer_value, subject_value) is not None:
            raise ApplicationConflictError(
                "Cette identité externe est déjà provisionnée dans RessourcePlanner.",
                code="user_admin_identity_exists",
                context={"issuer": issuer_value, "subject": subject_value},
            )
        record = self._repository.upsert(
            issuer=issuer_value,
            subject=subject_value,
            display_name=display_name_value,
            email=str(email).strip() if email else None,
            roles=normalized_roles,
            active=bool(active),
            employee_external_id=None,
        )
        return self._repository.set_business_phone(
            record.user_id,
            str(phone).strip() if phone else None,
        )

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        active: bool,
        phone: str | None = None,
        actor_user_id: str | None = None,
    ) -> UserIdentityRecord:
        user_id_value = _required(user_id, "user_id")
        existing = self._repository.get_by_id(user_id_value)
        if existing is None:
            raise ApplicationNotFoundError(
                "Utilisateur RessourcePlanner introuvable.",
                code="user_admin_not_found",
                context={"user_id": user_id_value},
            )

        normalized_roles = _roles(roles)
        active_value = bool(active)
        if actor_user_id and actor_user_id == user_id_value:
            if not active_value:
                raise ApplicationConflictError(
                    "Vous ne pouvez pas désactiver votre propre compte administrateur.",
                    code="user_admin_self_deactivation",
                )
            if ROLE_ADMIN not in normalized_roles:
                raise ApplicationConflictError(
                    "Vous ne pouvez pas retirer votre propre rôle Administrateur.",
                    code="user_admin_self_admin_removal",
                )

        record = self._repository.upsert(
            issuer=existing.issuer,
            subject=existing.subject,
            display_name=_required(display_name, "display_name"),
            email=str(email).strip() if email else None,
            roles=normalized_roles,
            active=active_value,
            employee_external_id=existing.employee_external_id,
        )
        return self._repository.set_business_phone(
            record.user_id,
            str(phone).strip() if phone else None,
        )
