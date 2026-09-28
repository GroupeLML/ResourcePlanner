from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .erp_user_directory import ErpUserDirectoryRepositoryPort
from .security import (
    AuthPrincipal,
    IdentityService,
    ROLE_TECHNICIAN,
    UserIdentityRecord,
    UserIdentityRepositoryPort,
)


class IdentityProvisioningRepositoryPort(UserIdentityRepositoryPort, Protocol):
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


@dataclass(frozen=True, slots=True)
class AutoProvisioningPolicy:
    """Explicit opt-in policy for first-login provisioning.

    Until the real Acumatica contract is validated, the only permitted automatic
    role is TECHNICIAN (read-only). A disabled policy preserves the existing
    fail-closed behavior for unknown identities.
    """

    enabled: bool = False
    default_role: str = ROLE_TECHNICIAN

    def __post_init__(self) -> None:
        if self.default_role != ROLE_TECHNICIAN:
            raise ValueError(
                "Le rôle d'auto-provisionnement doit rester TECHNICIAN tant que le contrat Acumatica réel n'est pas validé."
            )


class IdentityProvisioningService:
    def __init__(
        self,
        repository: IdentityProvisioningRepositoryPort,
        policy: AutoProvisioningPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or AutoProvisioningPolicy()

    def resolve_or_provision(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str,
        email: str | None,
        auth_mode: str = "oidc",
    ) -> AuthPrincipal | None:
        existing = self._repository.get_by_external_identity(issuer, subject)
        if existing is not None:
            # Never silently reactivate an account that an administrator disabled.
            if not existing.active:
                return None
            return IdentityService(self._repository).resolve(
                issuer=issuer,
                subject=subject,
                auth_mode=auth_mode,
            )

        if not self._policy.enabled:
            return None

        created = self._repository.upsert(
            issuer=issuer,
            subject=subject,
            display_name=display_name,
            email=email,
            roles=(self._policy.default_role,),
            active=True,
            employee_external_id=None,
        )
        return AuthPrincipal.from_roles(
            local_user_id=created.user_id,
            issuer=created.issuer,
            subject=created.subject,
            display_name=created.display_name,
            email=created.email,
            employee_external_id=created.employee_external_id,
            roles=created.roles,
            auth_mode=auth_mode,
        )


class ErpControlledIdentityRepositoryPort(IdentityProvisioningRepositoryPort, Protocol):
    def get_by_employee_external_id(
        self,
        employee_external_id: str,
    ) -> UserIdentityRecord | None: ...


class ErpIdentityProvisioningDenied(RuntimeError):
    """Validated OIDC identity cannot satisfy the configured ERP access policy."""


class ErpIdentityProvisioningConflict(ErpIdentityProvisioningDenied):
    """Validated identity conflicts with an existing stable AppUser/employee link."""


class ErpControlledIdentityProvisioningService:
    """Provision OIDC identities only through an explicitly enabled RP_Users entry."""

    def __init__(
        self,
        identities: ErpControlledIdentityRepositoryPort,
        directory: ErpUserDirectoryRepositoryPort,
    ) -> None:
        self._identities = identities
        self._directory = directory

    def resolve_or_provision(
        self,
        *,
        issuer: str,
        subject: str,
        preferred_username: str | None,
        display_name: str,
        email: str | None,
        auth_mode: str = "oidc",
    ) -> AuthPrincipal | None:
        issuer_value = str(issuer or "").strip()
        subject_value = str(subject or "").strip()
        preferred = str(preferred_username or "").strip()
        existing = self._identities.get_by_external_identity(
            issuer_value,
            subject_value,
        )

        if not preferred:
            if existing is None or not existing.active:
                return None
            return IdentityService(self._identities).resolve(
                issuer=issuer_value,
                subject=subject_value,
                auth_mode=auth_mode,
            )

        erp_user = self._directory.get_by_user_id(preferred)
        if erp_user is None or erp_user.user_id != preferred:
            raise ErpIdentityProvisioningDenied(
                "preferred_username ne correspond à aucun RP_Users.UserID autorisé."
            )
        if not erp_user.source_admissible:
            raise ErpIdentityProvisioningDenied(
                "L'utilisateur ERP n'est pas admissible."
            )
        if not erp_user.local_active:
            raise ErpIdentityProvisioningDenied(
                "L'utilisateur ERP n'est pas activé dans RessourcePlanner."
            )
        if not erp_user.roles:
            raise ErpIdentityProvisioningDenied(
                "Aucun rôle RessourcePlanner n'est configuré pour cet utilisateur ERP."
            )

        employee_external_id = str(erp_user.employee_external_id or "").strip()
        if not employee_external_id:
            raise ErpIdentityProvisioningDenied(
                "L'EmployeID lié à l'utilisateur ERP est absent ou invalide."
            )

        if existing is not None:
            if not existing.active:
                raise ErpIdentityProvisioningDenied(
                    "L'AppUser lié à cette identité OIDC est désactivé."
                )
            existing_employee = str(existing.employee_external_id or "").strip()
            if existing_employee and existing_employee != employee_external_id:
                raise ErpIdentityProvisioningConflict(
                    "L'identité OIDC existante est liée à un autre EmployeID."
                )

        linked = self._identities.get_by_employee_external_id(
            employee_external_id
        )
        if linked is not None and (
            existing is None or linked.user_id != existing.user_id
        ):
            raise ErpIdentityProvisioningConflict(
                "Cet EmployeID est déjà lié à une autre identité OIDC."
            )

        saved = self._identities.upsert(
            issuer=issuer_value,
            subject=subject_value,
            display_name=str(display_name or "").strip() or preferred,
            email=email,
            roles=erp_user.roles,
            active=True,
            employee_external_id=employee_external_id,
        )
        return AuthPrincipal.from_roles(
            local_user_id=saved.user_id,
            issuer=saved.issuer,
            subject=saved.subject,
            display_name=saved.display_name,
            email=saved.email,
            employee_external_id=saved.employee_external_id,
            roles=saved.roles,
            auth_mode=auth_mode,
        )
