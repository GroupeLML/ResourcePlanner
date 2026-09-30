from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .errors import ApplicationNotFoundError, ApplicationValidationError
from .security import OidcIdentityState, normalize_roles


@dataclass(frozen=True, slots=True)
class ExternalErpUserRecord:
    user_id: str
    employee_external_id: str
    display_name: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    erp_user_active: bool = False
    employee_status: str | None = None


class ErpUserSourcePort(Protocol):
    def list_users(self) -> Sequence[ExternalErpUserRecord]: ...


@dataclass(frozen=True, slots=True)
class ErpUserDirectoryRecord:
    user_id: str
    employee_external_id: str
    display_name: str
    first_name: str | None
    last_name: str | None
    email: str | None
    erp_user_active: bool
    employee_status: str | None
    local_active: bool
    roles: tuple[str, ...]
    resource_id: str | None
    resource_name: str | None
    resource_erp_active: bool | None
    oidc_state: OidcIdentityState = "pending"
    oidc_user_active: bool | None = None
    app_user_id: str | None = None
    issuer: str | None = None
    subject: str | None = None

    @property
    def source_admissible(self) -> bool:
        return self.erp_user_active and (self.employee_status or "").casefold() == "actif"

    @property
    def access_ready(self) -> bool:
        return (
            self.source_admissible
            and self.local_active
            and bool(self.roles)
            and bool(str(self.employee_external_id or "").strip())
            and self.oidc_state != "conflict"
            and self.oidc_user_active is not False
        )


class ErpUserDirectoryRepositoryPort(Protocol):
    def upsert_external_user(self, user: ExternalErpUserRecord) -> str: ...
    def list_users(self) -> tuple[ErpUserDirectoryRecord, ...]: ...
    def get_by_user_id(self, user_id: str) -> ErpUserDirectoryRecord | None: ...
    def update_local_access(
        self,
        user_id: str,
        *,
        active: bool,
        roles: tuple[str, ...],
    ) -> ErpUserDirectoryRecord: ...


@dataclass(frozen=True, slots=True)
class ErpUserSyncResult:
    received: int
    created: int
    updated: int
    unchanged: int
    errors: int


class ErpUserSyncService:
    def __init__(
        self,
        source: ErpUserSourcePort,
        repository: ErpUserDirectoryRepositoryPort,
    ) -> None:
        self._source = source
        self._repository = repository

    def synchronize(self) -> ErpUserSyncResult:
        users = tuple(self._source.list_users())
        created = updated = unchanged = errors = 0
        for user in users:
            try:
                action = self._repository.upsert_external_user(user)
            except ValueError:
                errors += 1
                continue
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            elif action == "unchanged":
                unchanged += 1
            else:
                raise ValueError(f"Action de synchronisation utilisateur ERP inconnue: {action}")
        return ErpUserSyncResult(
            received=len(users),
            created=created,
            updated=updated,
            unchanged=unchanged,
            errors=errors,
        )


class ErpUserDirectoryService:
    def __init__(self, repository: ErpUserDirectoryRepositoryPort) -> None:
        self._repository = repository

    def list_users(self) -> tuple[ErpUserDirectoryRecord, ...]:
        return self._repository.list_users()

    def update_local_access(
        self,
        user_id: str,
        *,
        active: bool,
        roles: tuple[str, ...] | list[str] | set[str],
    ) -> ErpUserDirectoryRecord:
        user_id_value = str(user_id or "").strip()
        if not user_id_value:
            raise ApplicationValidationError(
                "UserID est requis.",
                code="erp_user_required_id",
            )
        try:
            normalized_roles = normalize_roles(roles)
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc),
                code="erp_user_invalid_roles",
            ) from exc
        if active and not normalized_roles:
            raise ApplicationValidationError(
                "Au moins un rôle RessourcePlanner est requis pour activer un utilisateur ERP.",
                code="erp_user_roles_required",
            )
        if self._repository.get_by_user_id(user_id_value) is None:
            raise ApplicationNotFoundError(
                "Utilisateur ERP introuvable.",
                code="erp_user_not_found",
                context={"user_id": user_id_value},
            )
        return self._repository.update_local_access(
            user_id_value,
            active=bool(active),
            roles=normalized_roles,
        )
