from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


ROLE_ADMIN = "ADMIN"
ROLE_COORDINATOR = "COORDINATOR"
ROLE_PROJECT_MANAGER = "PROJECT_MANAGER"
ROLE_MANAGER = "MANAGER"
ROLE_TECHNICIAN = "TECHNICIAN"
ROLE_DELIVERY_CONTRIBUTOR = "DELIVERY_CONTRIBUTOR"

OIDC_STATE_PENDING = "pending"
OIDC_STATE_LINKED = "linked"
OIDC_STATE_CONFLICT = "conflict"
OidcIdentityState = Literal["pending", "linked", "conflict"]

ROLES = (
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_PROJECT_MANAGER,
    ROLE_MANAGER,
    ROLE_TECHNICIAN,
    ROLE_DELIVERY_CONTRIBUTOR,
)

PERMISSION_READ = "read"
PERMISSION_MANAGE_DEMANDS = "manage_demands"
PERMISSION_APPROVE_DEMANDS = "approve_demands"
PERMISSION_MANAGE_PLANNING = "manage_planning"
PERMISSION_OVERRIDE_PLANNING_WINDOW = "override_planning_window"
PERMISSION_MANAGE_WORK_PACKAGES = "manage_work_packages"
PERMISSION_MANAGE_DELIVERY = "manage_delivery"
PERMISSION_CONTRIBUTE_DELIVERY = "contribute_delivery"
PERMISSION_MANAGE_VERIFICATION = "manage_verification"
PERMISSION_EXECUTE_VERIFICATION = "execute_verification"
PERMISSION_MANAGE_RESOURCES = "manage_resources"
PERMISSION_MANAGE_COMMUNICATIONS = "manage_communications"
PERMISSION_SYNC_PROJECTS = "sync_projects"
PERMISSION_ADMIN_USERS = "admin_users"
PERMISSION_ADMIN_SETTINGS = "admin_settings"

PERMISSIONS = (
    PERMISSION_READ,
    PERMISSION_MANAGE_DEMANDS,
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_OVERRIDE_PLANNING_WINDOW,
    PERMISSION_MANAGE_WORK_PACKAGES,
    PERMISSION_MANAGE_DELIVERY,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_MANAGE_VERIFICATION,
    PERMISSION_EXECUTE_VERIFICATION,
    PERMISSION_MANAGE_RESOURCES,
    PERMISSION_MANAGE_COMMUNICATIONS,
    PERMISSION_SYNC_PROJECTS,
    PERMISSION_ADMIN_USERS,
    PERMISSION_ADMIN_SETTINGS,
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(PERMISSIONS),
    ROLE_COORDINATOR: frozenset(
        {
            PERMISSION_READ,
            PERMISSION_MANAGE_DEMANDS,
            PERMISSION_APPROVE_DEMANDS,
            PERMISSION_MANAGE_PLANNING,
            PERMISSION_OVERRIDE_PLANNING_WINDOW,
            PERMISSION_MANAGE_WORK_PACKAGES,
            PERMISSION_MANAGE_RESOURCES,
            PERMISSION_MANAGE_COMMUNICATIONS,
        }
    ),
    ROLE_PROJECT_MANAGER: frozenset(
        {
            PERMISSION_READ,
            PERMISSION_MANAGE_DEMANDS,
            PERMISSION_MANAGE_WORK_PACKAGES,
            PERMISSION_MANAGE_DELIVERY,
            PERMISSION_CONTRIBUTE_DELIVERY,
            PERMISSION_MANAGE_VERIFICATION,
            PERMISSION_EXECUTE_VERIFICATION,
        }
    ),
    ROLE_MANAGER: frozenset({PERMISSION_READ, PERMISSION_APPROVE_DEMANDS}),
    ROLE_TECHNICIAN: frozenset({PERMISSION_READ}),
    ROLE_DELIVERY_CONTRIBUTOR: frozenset(
        {
            PERMISSION_READ,
            PERMISSION_CONTRIBUTE_DELIVERY,
            PERMISSION_MANAGE_VERIFICATION,
            PERMISSION_EXECUTE_VERIFICATION,
        }
    ),
}


def normalize_roles(roles: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(role).strip().upper() for role in roles if str(role).strip()))
    invalid = sorted(set(normalized) - set(ROLES))
    if invalid:
        raise ValueError(f"Rôle(s) RessourcePlanner invalide(s): {', '.join(invalid)}")
    return normalized


def permissions_for_roles(roles: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    normalized = normalize_roles(roles)
    permissions: set[str] = set()
    for role in normalized:
        permissions.update(ROLE_PERMISSIONS[role])
    return tuple(permission for permission in PERMISSIONS if permission in permissions)


def oidc_identity_state(
    issuer: str | None,
    subject: str | None,
) -> OidcIdentityState:
    """Project the persisted OIDC coordinates into the canonical admin state."""

    issuer_value = str(issuer or "").strip() or None
    subject_value = str(subject or "").strip() or None
    if issuer_value is None and subject_value is None:
        return OIDC_STATE_PENDING
    if issuer_value is not None and subject_value is not None:
        return OIDC_STATE_LINKED
    return OIDC_STATE_CONFLICT


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    local_user_id: str | None
    issuer: str
    subject: str
    display_name: str
    email: str | None
    employee_external_id: str | None
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    auth_mode: str

    @classmethod
    def from_roles(
        cls,
        *,
        local_user_id: str | None,
        issuer: str,
        subject: str,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        auth_mode: str,
        employee_external_id: str | None = None,
    ) -> "AuthPrincipal":
        normalized_roles = normalize_roles(roles)
        return cls(
            local_user_id=local_user_id,
            issuer=str(issuer).strip(),
            subject=str(subject).strip(),
            display_name=str(display_name).strip(),
            email=str(email).strip() if email else None,
            employee_external_id=(
                str(employee_external_id).strip() if employee_external_id else None
            ),
            roles=normalized_roles,
            permissions=permissions_for_roles(normalized_roles),
            auth_mode=str(auth_mode).strip(),
        )

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def to_dict(self) -> dict[str, object]:
        return {
            "local_user_id": self.local_user_id,
            "issuer": self.issuer,
            "subject": self.subject,
            "display_name": self.display_name,
            "email": self.email,
            "employee_external_id": self.employee_external_id,
            "roles": list(self.roles),
            "permissions": list(self.permissions),
            "auth_mode": self.auth_mode,
        }


@dataclass(frozen=True, slots=True)
class UserIdentityRecord:
    user_id: str
    issuer: str | None
    subject: str | None
    display_name: str
    email: str | None
    roles: tuple[str, ...]
    active: bool
    employee_external_id: str | None = None
    erp_user_id: str | None = None
    business_contact_id: str | None = None
    phone: str | None = None

    @property
    def oidc_state(self) -> OidcIdentityState:
        return oidc_identity_state(self.issuer, self.subject)


class UserIdentityRepositoryPort(Protocol):
    def get_by_external_identity(self, issuer: str, subject: str) -> UserIdentityRecord | None: ...


class IdentityService:
    def __init__(self, repository: UserIdentityRepositoryPort) -> None:
        self._repository = repository

    def resolve(self, *, issuer: str, subject: str, auth_mode: str = "oidc") -> AuthPrincipal | None:
        record = self._repository.get_by_external_identity(issuer, subject)
        if (
            record is None
            or not record.active
            or not record.issuer
            or not record.subject
        ):
            return None
        return AuthPrincipal.from_roles(
            local_user_id=record.user_id,
            issuer=record.issuer,
            subject=record.subject,
            display_name=record.display_name,
            email=record.email,
            employee_external_id=record.employee_external_id,
            roles=record.roles,
            auth_mode=auth_mode,
        )
