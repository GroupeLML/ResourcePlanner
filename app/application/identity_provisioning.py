from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

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
    """Legacy configuration retained while the runtime contract is retired.

    IDENTITY-D removes this policy from the OIDC callback. Keeping the value
    parseable avoids an unrelated configuration-breaking change, but enabling it
    no longer authorizes OIDC to create an AppUser.
    """

    enabled: bool = False
    default_role: str = ROLE_TECHNICIAN

    def __post_init__(self) -> None:
        if self.default_role != ROLE_TECHNICIAN:
            raise ValueError(
                "Le rôle d'auto-provisionnement legacy doit rester TECHNICIAN."
            )


class IdentityProvisioningService:
    """Legacy generic provisioner.

    Kept temporarily for compatibility with non-callback callers/tests. The OIDC
    callback must not invoke this service after IDENTITY-D.
    """

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
            issuer=created.issuer or "",
            subject=created.subject or "",
            display_name=created.display_name,
            email=created.email,
            employee_external_id=created.employee_external_id,
            roles=created.roles,
            auth_mode=auth_mode,
        )


class ErpIdentityLinkRepositoryPort(UserIdentityRepositoryPort, Protocol):
    def get_by_employee_external_id(
        self,
        employee_external_id: str,
    ) -> UserIdentityRecord | None: ...

    def get_by_erp_user_id(self, erp_user_id: str) -> UserIdentityRecord | None: ...

    def bind_external_identity(
        self,
        app_user_id: str,
        issuer: str,
        subject: str,
    ) -> UserIdentityRecord: ...


class IdentityLinkAuditPort(Protocol):
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


class ErpIdentityLinkDenied(RuntimeError):
    """Validated OIDC identity cannot authenticate the pre-provisioned account."""


class ErpIdentityLinkConflict(ErpIdentityLinkDenied):
    """Validated OIDC identity conflicts with an existing stable identity link."""


AUDIT_OIDC_IDENTITY_LINKED = "OIDC_IDENTITY_LINKED"


def _principal(record: UserIdentityRecord, *, auth_mode: str) -> AuthPrincipal:
    issuer = str(record.issuer or "").strip()
    subject = str(record.subject or "").strip()
    if not issuer or not subject:
        raise ErpIdentityLinkConflict(
            "L'AppUser ne possède pas une identité OIDC complète après la liaison."
        )
    return AuthPrincipal.from_roles(
        local_user_id=record.user_id,
        issuer=issuer,
        subject=subject,
        display_name=record.display_name,
        email=record.email,
        employee_external_id=record.employee_external_id,
        roles=record.roles,
        auth_mode=auth_mode,
    )


class ErpPreprovisionedIdentityLinkService:
    """Resolve or bind OIDC only to an AppUser explicitly pre-provisioned by ADMIN."""

    def __init__(
        self,
        identities: ErpIdentityLinkRepositoryPort,
        directory: ErpUserDirectoryRepositoryPort,
        audit: IdentityLinkAuditPort | None = None,
    ) -> None:
        self._identities = identities
        self._directory = directory
        self._audit = audit

    def _validate_linked_account(
        self,
        record: UserIdentityRecord,
        *,
        preferred_username: str,
    ) -> None:
        if not record.active:
            raise ErpIdentityLinkDenied(
                "L'AppUser lié à cette identité OIDC est désactivé."
            )
        if not record.roles:
            raise ErpIdentityLinkDenied(
                "L'AppUser lié ne possède aucun rôle RessourcePlanner."
            )

        erp_user_id = str(record.erp_user_id or "").strip()
        if not erp_user_id:
            raise ErpIdentityLinkConflict(
                "L'AppUser lié ne possède pas de UserID ERP explicite."
            )
        if preferred_username and preferred_username != erp_user_id:
            raise ErpIdentityLinkConflict(
                "preferred_username ne correspond pas au UserID ERP déjà lié à cette identité OIDC."
            )

        erp_user = self._directory.get_by_user_id(erp_user_id)
        if erp_user is None or erp_user.user_id != erp_user_id:
            raise ErpIdentityLinkDenied(
                "Le compte ERP lié à l'AppUser est introuvable."
            )
        if not erp_user.source_admissible:
            raise ErpIdentityLinkDenied(
                "L'utilisateur ERP lié n'est pas admissible."
            )

        expected_employee = str(erp_user.employee_external_id or "").strip()
        actual_employee = str(record.employee_external_id or "").strip()
        if not expected_employee or not actual_employee:
            raise ErpIdentityLinkConflict(
                "L'EmployeID lié au compte est absent ou invalide."
            )
        if expected_employee != actual_employee:
            raise ErpIdentityLinkConflict(
                "Le UserID ERP lié référence maintenant un EmployeID différent."
            )

    def resolve_or_link(
        self,
        *,
        issuer: str,
        subject: str,
        preferred_username: str | None,
        auth_mode: str = "oidc",
    ) -> AuthPrincipal | None:
        issuer_value = str(issuer or "").strip()
        subject_value = str(subject or "").strip()
        preferred = str(preferred_username or "").strip()
        if not issuer_value or not subject_value:
            raise ErpIdentityLinkDenied(
                "L'identité OIDC validée est incomplète."
            )

        existing = self._identities.get_by_external_identity(
            issuer_value,
            subject_value,
        )
        if existing is not None:
            self._validate_linked_account(
                existing,
                preferred_username=preferred,
            )
            return _principal(existing, auth_mode=auth_mode)

        if not preferred:
            return None

        erp_user = self._directory.get_by_user_id(preferred)
        if erp_user is None or erp_user.user_id != preferred:
            raise ErpIdentityLinkDenied(
                "preferred_username ne correspond à aucun RP_Users.UserID."
            )
        if not erp_user.source_admissible:
            raise ErpIdentityLinkDenied(
                "L'utilisateur ERP n'est pas admissible."
            )

        employee_external_id = str(erp_user.employee_external_id or "").strip()
        if not employee_external_id:
            raise ErpIdentityLinkDenied(
                "L'EmployeID lié à l'utilisateur ERP est absent ou invalide."
            )

        target = self._identities.get_by_erp_user_id(preferred)
        if target is None:
            raise ErpIdentityLinkDenied(
                "Aucun AppUser pré-provisionné ne correspond à ce UserID ERP."
            )
        if not target.active:
            raise ErpIdentityLinkDenied(
                "L'AppUser pré-provisionné est désactivé."
            )
        if not target.roles:
            raise ErpIdentityLinkDenied(
                "L'AppUser pré-provisionné ne possède aucun rôle RessourcePlanner."
            )
        if str(target.erp_user_id or "").strip() != preferred:
            raise ErpIdentityLinkConflict(
                "Le UserID ERP de l'AppUser ne correspond pas au compte sélectionné."
            )
        if str(target.employee_external_id or "").strip() != employee_external_id:
            raise ErpIdentityLinkConflict(
                "L'EmployeID de l'AppUser ne correspond pas à RP_Users."
            )

        employee_owner = self._identities.get_by_employee_external_id(
            employee_external_id
        )
        if employee_owner is None or employee_owner.user_id != target.user_id:
            raise ErpIdentityLinkConflict(
                "L'EmployeID est lié à un autre AppUser ou n'est pas résolu de façon unique."
            )

        target_issuer = str(target.issuer or "").strip()
        target_subject = str(target.subject or "").strip()
        if target_issuer or target_subject:
            if target_issuer == issuer_value and target_subject == subject_value:
                self._validate_linked_account(
                    target,
                    preferred_username=preferred,
                )
                return _principal(target, auth_mode=auth_mode)
            raise ErpIdentityLinkConflict(
                "Cet AppUser possède déjà une autre identité OIDC."
            )

        try:
            bound = self._identities.bind_external_identity(
                target.user_id,
                issuer_value,
                subject_value,
            )
        except ValueError as exc:
            raise ErpIdentityLinkConflict(str(exc)) from exc
        except KeyError as exc:
            raise ErpIdentityLinkDenied(str(exc)) from exc

        if self._audit is not None:
            self._audit.record_event(
                actor_user_id=bound.user_id,
                target_user_id=bound.user_id,
                erp_user_id=bound.erp_user_id,
                action=AUDIT_OIDC_IDENTITY_LINKED,
                old_state={
                    "issuer": None,
                    "subject": None,
                },
                new_state={
                    "issuer": bound.issuer,
                    "subject": bound.subject,
                },
            )
        return _principal(bound, auth_mode=auth_mode)


# Compatibility aliases for code/tests that still import the historical names.
# Their semantics are now link-only under IDENTITY-D.
ErpControlledIdentityProvisioningService = ErpPreprovisionedIdentityLinkService
ErpIdentityProvisioningDenied = ErpIdentityLinkDenied
ErpIdentityProvisioningConflict = ErpIdentityLinkConflict
