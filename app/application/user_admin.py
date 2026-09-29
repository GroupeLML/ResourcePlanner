from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .erp_user_directory import ErpUserDirectoryRecord, ErpUserDirectoryRepositoryPort
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

AUDIT_PREPROVISIONED = "APP_USER_PREPROVISIONED"
AUDIT_ACTIVATED = "APP_USER_ACTIVATED"
AUDIT_DEACTIVATED = "APP_USER_DEACTIVATED"
AUDIT_ROLES_CHANGED = "APP_USER_ROLES_CHANGED"


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


class IdentityAdminAuditPort(Protocol):
    def record_event(
        self,
        *,
        actor_user_id: str,
        target_user_id: str,
        erp_user_id: str | None,
        action: str,
        old_state: Mapping[str, object],
        new_state: Mapping[str, object],
    ) -> None: ...


class UserAdminRepositoryPort(Protocol):
    def list_users(self) -> tuple[UserIdentityRecord, ...]: ...

    def get_by_id(self, user_id: str) -> UserIdentityRecord | None: ...

    def get_by_external_identity(self, issuer: str, subject: str) -> UserIdentityRecord | None: ...

    def get_by_employee_external_id(
        self,
        employee_external_id: str,
    ) -> UserIdentityRecord | None: ...

    def get_by_erp_user_id(self, erp_user_id: str) -> UserIdentityRecord | None: ...

    def create_account(
        self,
        *,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        active: bool = True,
        employee_external_id: str | None = None,
        erp_user_id: str | None = None,
    ) -> UserIdentityRecord: ...

    def update_account(
        self,
        app_user_id: str,
        *,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        active: bool,
        employee_external_id: str | None,
        erp_user_id: str | None,
    ) -> UserIdentityRecord: ...

    def bind_external_identity(
        self,
        app_user_id: str,
        issuer: str,
        subject: str,
    ) -> UserIdentityRecord: ...

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


def _normalize_roles(
    values: tuple[str, ...] | list[str] | set[str],
    *,
    required: bool,
) -> tuple[str, ...]:
    try:
        roles = normalize_roles(values)
    except ValueError as exc:
        raise ApplicationValidationError(
            str(exc),
            code="user_admin_invalid_roles",
        ) from exc
    if required and not roles:
        raise ApplicationValidationError(
            "Au moins un rôle RessourcePlanner est requis.",
            code="user_admin_roles_required",
        )
    return roles


def _roles(values: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    return _normalize_roles(values, required=True)


def _state(record: UserIdentityRecord) -> dict[str, object]:
    return {
        "active": bool(record.active),
        "roles": list(record.roles),
        "employee_external_id": record.employee_external_id,
        "erp_user_id": record.erp_user_id,
    }


class UserAdminService:
    def __init__(
        self,
        repository: UserAdminRepositoryPort,
        *,
        erp_directory: ErpUserDirectoryRepositoryPort | None = None,
        audit: IdentityAdminAuditPort | None = None,
    ) -> None:
        self._repository = repository
        self._erp_directory = erp_directory
        self._audit = audit

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

    def list_erp_users(self) -> tuple[ErpUserDirectoryRecord, ...]:
        if self._erp_directory is None:
            raise RuntimeError("Annuaire ERP non configuré.")
        return self._erp_directory.list_users()

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

    def _assert_self_protection(
        self,
        existing: UserIdentityRecord,
        *,
        active: bool,
        roles: tuple[str, ...],
        actor_user_id: str | None,
    ) -> None:
        if not actor_user_id or actor_user_id != existing.user_id:
            return
        if not active:
            raise ApplicationConflictError(
                "Vous ne pouvez pas désactiver votre propre compte administrateur.",
                code="user_admin_self_deactivation",
            )
        if ROLE_ADMIN not in roles:
            raise ApplicationConflictError(
                "Vous ne pouvez pas retirer votre propre rôle Administrateur.",
                code="user_admin_self_admin_removal",
            )

    def _audit_changes(
        self,
        *,
        actor_user_id: str | None,
        old: UserIdentityRecord | None,
        new: UserIdentityRecord,
        preprovisioned: bool = False,
    ) -> None:
        if self._audit is None or not actor_user_id:
            return
        old_state = _state(old) if old is not None else {}
        new_state = _state(new)
        if preprovisioned:
            self._audit.record_event(
                actor_user_id=actor_user_id,
                target_user_id=new.user_id,
                erp_user_id=new.erp_user_id,
                action=AUDIT_PREPROVISIONED,
                old_state=old_state,
                new_state=new_state,
            )
        if old is None:
            if new.active:
                self._audit.record_event(
                    actor_user_id=actor_user_id,
                    target_user_id=new.user_id,
                    erp_user_id=new.erp_user_id,
                    action=AUDIT_ACTIVATED,
                    old_state={},
                    new_state=new_state,
                )
            if new.roles:
                self._audit.record_event(
                    actor_user_id=actor_user_id,
                    target_user_id=new.user_id,
                    erp_user_id=new.erp_user_id,
                    action=AUDIT_ROLES_CHANGED,
                    old_state={"roles": []},
                    new_state={"roles": list(new.roles)},
                )
            return

        if old.active != new.active:
            self._audit.record_event(
                actor_user_id=actor_user_id,
                target_user_id=new.user_id,
                erp_user_id=new.erp_user_id,
                action=AUDIT_ACTIVATED if new.active else AUDIT_DEACTIVATED,
                old_state=old_state,
                new_state=new_state,
            )
        if old.roles != new.roles:
            self._audit.record_event(
                actor_user_id=actor_user_id,
                target_user_id=new.user_id,
                erp_user_id=new.erp_user_id,
                action=AUDIT_ROLES_CHANGED,
                old_state={"roles": list(old.roles)},
                new_state={"roles": list(new.roles)},
            )

    def _assert_erp_reactivation_allowed(self, existing: UserIdentityRecord) -> None:
        if self._erp_directory is None or not existing.erp_user_id:
            return
        source = self._erp_directory.get_by_user_id(existing.erp_user_id)
        if source is None:
            raise ApplicationConflictError(
                "Le compte ERP lié est introuvable.",
                code="user_admin_erp_user_missing",
                context={"erp_user_id": existing.erp_user_id},
            )
        if not source.source_admissible:
            raise ApplicationConflictError(
                "Le compte ERP lié n'est pas admissible à l'activation RessourcePlanner.",
                code="user_admin_erp_source_ineligible",
                context={"erp_user_id": existing.erp_user_id},
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
        self._assert_self_protection(
            existing,
            active=active_value,
            roles=normalized_roles,
            actor_user_id=actor_user_id,
        )
        if active_value and not existing.active:
            self._assert_erp_reactivation_allowed(existing)

        try:
            record = self._repository.update_account(
                existing.user_id,
                display_name=_required(display_name, "display_name"),
                email=str(email).strip() if email else None,
                roles=normalized_roles,
                active=active_value,
                employee_external_id=existing.employee_external_id,
                erp_user_id=existing.erp_user_id,
            )
            record = self._repository.set_business_phone(
                record.user_id,
                str(phone).strip() if phone else None,
            )
            if self._erp_directory is not None and record.erp_user_id:
                self._erp_directory.update_local_access(
                    record.erp_user_id,
                    active=record.active,
                    roles=record.roles,
                )
        except ValueError as exc:
            raise ApplicationConflictError(
                str(exc),
                code="user_admin_identity_conflict",
                context={"user_id": existing.user_id},
            ) from exc

        self._audit_changes(
            actor_user_id=actor_user_id,
            old=existing,
            new=record,
        )
        return record

    def update_erp_user_access(
        self,
        user_id: str,
        *,
        active: bool,
        roles: tuple[str, ...] | list[str] | set[str],
        actor_user_id: str,
    ) -> ErpUserDirectoryRecord:
        if self._erp_directory is None:
            raise RuntimeError("Annuaire ERP non configuré.")

        user_id_value = _required(user_id, "user_id")
        source = self._erp_directory.get_by_user_id(user_id_value)
        if source is None:
            raise ApplicationNotFoundError(
                "Utilisateur ERP introuvable.",
                code="erp_user_not_found",
                context={"user_id": user_id_value},
            )

        requested_roles = _normalize_roles(roles, required=bool(active))
        employee_external_id = str(source.employee_external_id or "").strip()
        existing_by_erp = self._repository.get_by_erp_user_id(user_id_value)
        existing_by_employee = (
            self._repository.get_by_employee_external_id(employee_external_id)
            if employee_external_id
            else None
        )

        if existing_by_erp is not None:
            if (
                not employee_external_id
                or existing_by_erp.employee_external_id != employee_external_id
            ):
                raise ApplicationConflictError(
                    "Le UserID ERP est déjà lié à un AppUser avec un EmployeID différent.",
                    code="erp_user_employee_identity_conflict",
                    context={"user_id": user_id_value},
                )
            if (
                existing_by_employee is not None
                and existing_by_employee.user_id != existing_by_erp.user_id
            ):
                raise ApplicationConflictError(
                    "L'EmployeID est déjà lié à un autre AppUser.",
                    code="erp_user_employee_identity_conflict",
                    context={"user_id": user_id_value},
                )
        elif existing_by_employee is not None:
            raise ApplicationConflictError(
                "L'EmployeID est déjà lié à un autre compte ERP; aucun transfert automatique n'est permis.",
                code="erp_user_employee_identity_conflict",
                context={
                    "user_id": user_id_value,
                    "existing_erp_user_id": existing_by_employee.erp_user_id,
                },
            )

        if not active and existing_by_erp is None:
            return self._erp_directory.update_local_access(
                user_id_value,
                active=False,
                roles=requested_roles,
            )

        if active:
            if not source.source_admissible:
                raise ApplicationConflictError(
                    "Cet utilisateur ERP n'est pas admissible à l'activation RessourcePlanner.",
                    code="erp_user_source_ineligible",
                    context={"user_id": user_id_value},
                )
            if not employee_external_id:
                raise ApplicationConflictError(
                    "EmployeID est requis pour activer un utilisateur ERP.",
                    code="erp_user_employee_id_required",
                    context={"user_id": user_id_value},
                )

        if existing_by_erp is None:
            try:
                record = self._repository.create_account(
                    display_name=source.display_name,
                    email=source.email,
                    roles=requested_roles,
                    active=True,
                    employee_external_id=employee_external_id,
                    erp_user_id=user_id_value,
                )
            except ValueError as exc:
                raise ApplicationConflictError(
                    str(exc),
                    code="erp_user_identity_conflict",
                    context={"user_id": user_id_value},
                ) from exc
            self._erp_directory.update_local_access(
                user_id_value,
                active=True,
                roles=record.roles,
            )
            self._audit_changes(
                actor_user_id=actor_user_id,
                old=None,
                new=record,
                preprovisioned=True,
            )
            refreshed = self._erp_directory.get_by_user_id(user_id_value)
            assert refreshed is not None
            return refreshed

        effective_roles = requested_roles or existing_by_erp.roles
        self._assert_self_protection(
            existing_by_erp,
            active=bool(active),
            roles=effective_roles,
            actor_user_id=actor_user_id,
        )
        if active and not existing_by_erp.active and not source.source_admissible:
            raise ApplicationConflictError(
                "Cet utilisateur ERP n'est pas admissible à la réactivation RessourcePlanner.",
                code="erp_user_source_ineligible",
                context={"user_id": user_id_value},
            )
        try:
            record = self._repository.update_account(
                existing_by_erp.user_id,
                display_name=existing_by_erp.display_name,
                email=existing_by_erp.email,
                roles=effective_roles,
                active=bool(active),
                employee_external_id=existing_by_erp.employee_external_id,
                erp_user_id=existing_by_erp.erp_user_id,
            )
            self._erp_directory.update_local_access(
                user_id_value,
                active=record.active,
                roles=record.roles,
            )
        except ValueError as exc:
            raise ApplicationConflictError(
                str(exc),
                code="erp_user_identity_conflict",
                context={"user_id": user_id_value},
            ) from exc

        self._audit_changes(
            actor_user_id=actor_user_id,
            old=existing_by_erp,
            new=record,
        )
        refreshed = self._erp_directory.get_by_user_id(user_id_value)
        assert refreshed is not None
        return refreshed
