from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .errors import ApplicationConflictError, ApplicationValidationError
from .security import ROLE_ADMIN, UserIdentityRecord
from .user_admin import UserAdminRepositoryPort


BREAK_GLASS_AUTH_MODE = "break_glass"
BREAK_GLASS_ISSUER = "urn:resourceplanner:break-glass"
AUDIT_BOOTSTRAP_CREATED = "BREAK_GLASS_BOOTSTRAP_CREATED"
AUDIT_BOOTSTRAP_RECONCILED = "BREAK_GLASS_BOOTSTRAP_RECONCILED"
AUDIT_SECRET_ROTATED = "BREAK_GLASS_SECRET_ROTATED"
AUDIT_LOGIN_SUCCEEDED = "BREAK_GLASS_LOGIN_SUCCEEDED"
AUDIT_LOGIN_FAILED = "BREAK_GLASS_LOGIN_FAILED"
AUDIT_LOGIN_RATE_LIMITED = "BREAK_GLASS_LOGIN_RATE_LIMITED"


@dataclass(frozen=True, slots=True)
class BreakGlassPolicy:
    max_attempts: int = 5
    attempt_window: timedelta = timedelta(minutes=15)
    lock_duration: timedelta = timedelta(minutes=15)
    minimum_secret_length: int = 20


@dataclass(frozen=True, slots=True)
class BreakGlassCredentialRecord:
    credential_id: str
    user_id: str
    login_name: str
    secret_hash: str
    credential_version: int
    active: bool
    failed_attempt_count: int
    first_failed_at: datetime | None
    locked_until: datetime | None
    user_active: bool
    user_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BreakGlassBootstrapResult:
    action: str
    credential_id: str
    user_id: str
    credential_version: int


class BreakGlassSecretHasherPort(Protocol):
    def hash_secret(self, credential_value: str) -> str: ...

    def verify_secret(self, credential_value: str, encoded_hash: str) -> bool: ...


class BreakGlassRepositoryPort(Protocol):
    def get_for_login(
        self,
        login_name: str,
        *,
        for_update: bool = False,
    ) -> BreakGlassCredentialRecord | None: ...

    def get_by_user_id(
        self,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> BreakGlassCredentialRecord | None: ...

    def create_credential(
        self,
        *,
        user_id: str,
        login_name: str,
        secret_hash: str,
        now: datetime,
    ) -> BreakGlassCredentialRecord: ...

    def rotate_secret(
        self,
        credential_id: str,
        *,
        secret_hash: str,
        now: datetime,
    ) -> BreakGlassCredentialRecord: ...

    def record_success(
        self,
        credential_id: str,
        *,
        now: datetime,
    ) -> BreakGlassCredentialRecord: ...

    def record_failure(
        self,
        credential_id: str,
        *,
        now: datetime,
        policy: BreakGlassPolicy,
    ) -> BreakGlassCredentialRecord: ...

    def record_event(
        self,
        *,
        event_type: str,
        success: bool,
        login_name: str,
        reason_code: str,
        credential_id: str | None = None,
        target_user_id: str | None = None,
        now: datetime,
    ) -> None: ...

    def is_last_active_admin_access(self, user_id: str) -> bool: ...


class BreakGlassAuthenticationDenied(RuntimeError):
    pass


class BreakGlassRateLimited(RuntimeError):
    pass


def normalize_break_glass_login(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized:
        raise ApplicationValidationError(
            "Le nom de connexion break-glass est requis.",
            code="break_glass_login_required",
        )
    if len(normalized) > 128:
        raise ApplicationValidationError(
            "Le nom de connexion break-glass est trop long.",
            code="break_glass_login_invalid",
        )
    return normalized


def validate_break_glass_secret(credential_value: object, policy: BreakGlassPolicy) -> str:
    value = str(credential_value or "")
    if len(value) < policy.minimum_secret_length:
        raise ApplicationValidationError(
            f"Le secret break-glass doit contenir au moins {policy.minimum_secret_length} caractères.",
            code="break_glass_secret_too_short",
        )
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BreakGlassAuthenticationService:
    def __init__(
        self,
        repository: BreakGlassRepositoryPort,
        hasher: BreakGlassSecretHasherPort,
        *,
        policy: BreakGlassPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._policy = policy or BreakGlassPolicy()

    @property
    def policy(self) -> BreakGlassPolicy:
        return self._policy

    def authenticate(
        self,
        *,
        login_name: str,
        credential_value: str,
        now: datetime,
    ) -> str:
        login = normalize_break_glass_login(login_name)
        credential = self._repository.get_for_login(login, for_update=True)
        current = _aware(now)

        if credential is None:
            # Spend the same class of CPU work as a real credential verification
            # without revealing whether a login exists.
            self._hasher.hash_secret(str(credential_value or ""))
            self._repository.record_event(
                event_type=AUDIT_LOGIN_FAILED,
                success=False,
                login_name=login,
                reason_code="unknown_or_invalid",
                now=current,
            )
            raise BreakGlassAuthenticationDenied()

        if credential.locked_until is not None and _aware(credential.locked_until) > current:
            self._repository.record_event(
                event_type=AUDIT_LOGIN_RATE_LIMITED,
                success=False,
                login_name=login,
                reason_code="rate_limited",
                credential_id=credential.credential_id,
                target_user_id=credential.user_id,
                now=current,
            )
            raise BreakGlassRateLimited()

        valid_secret = self._hasher.verify_secret(str(credential_value or ""), credential.secret_hash)
        valid_admin = (
            credential.active
            and credential.user_active
            and ROLE_ADMIN in credential.user_roles
        )
        if not valid_secret or not valid_admin:
            updated = self._repository.record_failure(
                credential.credential_id,
                now=current,
                policy=self._policy,
            )
            reason = "unknown_or_invalid"
            if valid_secret and not valid_admin:
                reason = "credential_or_admin_inactive"
            self._repository.record_event(
                event_type=AUDIT_LOGIN_FAILED,
                success=False,
                login_name=login,
                reason_code=reason,
                credential_id=credential.credential_id,
                target_user_id=credential.user_id,
                now=current,
            )
            if updated.locked_until is not None and _aware(updated.locked_until) > current:
                # The attempt that reaches the threshold is still reported as a
                # normal authentication failure. Subsequent attempts are throttled.
                pass
            raise BreakGlassAuthenticationDenied()

        self._repository.record_success(credential.credential_id, now=current)
        self._repository.record_event(
            event_type=AUDIT_LOGIN_SUCCEEDED,
            success=True,
            login_name=login,
            reason_code="authenticated",
            credential_id=credential.credential_id,
            target_user_id=credential.user_id,
            now=current,
        )
        return credential.user_id


class BreakGlassBootstrapService:
    def __init__(
        self,
        identities: UserAdminRepositoryPort,
        repository: BreakGlassRepositoryPort,
        hasher: BreakGlassSecretHasherPort,
        *,
        policy: BreakGlassPolicy | None = None,
    ) -> None:
        self._identities = identities
        self._repository = repository
        self._hasher = hasher
        self._policy = policy or BreakGlassPolicy()

    def _assert_reserved_identity(self, user: UserIdentityRecord) -> None:
        if (
            user.issuer
            or user.subject
            or user.erp_user_id
            or user.employee_external_id
        ):
            raise ApplicationConflictError(
                "Le compte break-glass doit rester indépendant d'OIDC, d'Acumatica et des ressources ERP.",
                code="break_glass_identity_not_reserved",
                context={"user_id": user.user_id},
            )

    def bootstrap(
        self,
        *,
        login_name: str,
        credential_value: str,
        display_name: str | None = None,
        email: str | None = None,
        rotate_secret: bool = False,
        now: datetime,
    ) -> BreakGlassBootstrapResult:
        login = normalize_break_glass_login(login_name)
        secret_value = validate_break_glass_secret(credential_value, self._policy)
        current = _aware(now)
        credential = self._repository.get_for_login(login, for_update=True)

        if credential is None:
            user = self._identities.create_account(
                display_name=str(display_name or "").strip() or "Administrateur break-glass",
                email=str(email).strip() if email else None,
                roles=(ROLE_ADMIN,),
                active=True,
                employee_external_id=None,
                erp_user_id=None,
            )
            self._assert_reserved_identity(user)
            credential = self._repository.create_credential(
                user_id=user.user_id,
                login_name=login,
                secret_hash=self._hasher.hash_secret(secret_value),
                now=current,
            )
            self._repository.record_event(
                event_type=AUDIT_BOOTSTRAP_CREATED,
                success=True,
                login_name=login,
                reason_code="created",
                credential_id=credential.credential_id,
                target_user_id=user.user_id,
                now=current,
            )
            return BreakGlassBootstrapResult(
                action="created",
                credential_id=credential.credential_id,
                user_id=user.user_id,
                credential_version=credential.credential_version,
            )

        user = self._identities.get_by_id(credential.user_id)
        if user is None:
            raise ApplicationConflictError(
                "Le credential break-glass référence un AppUser introuvable.",
                code="break_glass_user_missing",
                context={"credential_id": credential.credential_id},
            )
        self._assert_reserved_identity(user)

        reconciled_roles = tuple(dict.fromkeys((*user.roles, ROLE_ADMIN)))
        requested_display_name = str(display_name or "").strip() or None
        effective_display_name = requested_display_name or user.display_name
        effective_email = str(email).strip() if email is not None else user.email
        if (
            not user.active
            or ROLE_ADMIN not in user.roles
            or user.display_name != effective_display_name
            or user.email != effective_email
        ):
            user = self._identities.update_account(
                user.user_id,
                display_name=effective_display_name,
                email=effective_email,
                roles=reconciled_roles,
                active=True,
                employee_external_id=None,
                erp_user_id=None,
            )

        action = "reconciled"
        if rotate_secret:
            credential = self._repository.rotate_secret(
                credential.credential_id,
                secret_hash=self._hasher.hash_secret(secret_value),
                now=current,
            )
            action = "rotated"
            event_type = AUDIT_SECRET_ROTATED
            reason_code = "rotated"
        else:
            if not self._hasher.verify_secret(secret_value, credential.secret_hash):
                raise ApplicationConflictError(
                    "Le credential existe déjà; utiliser la rotation explicite pour remplacer son secret.",
                    code="break_glass_rotation_required",
                    context={"credential_id": credential.credential_id},
                )
            event_type = AUDIT_BOOTSTRAP_RECONCILED
            reason_code = "reconciled"

        self._repository.record_event(
            event_type=event_type,
            success=True,
            login_name=login,
            reason_code=reason_code,
            credential_id=credential.credential_id,
            target_user_id=user.user_id,
            now=current,
        )
        return BreakGlassBootstrapResult(
            action=action,
            credential_id=credential.credential_id,
            user_id=user.user_id,
            credential_version=credential.credential_version,
        )
