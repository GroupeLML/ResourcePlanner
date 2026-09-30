from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.break_glass import (
    BreakGlassCredentialRecord,
    BreakGlassPolicy,
)
from ...application.security import ROLE_ADMIN, normalize_roles
from .base import new_id
from .identity_models import AppUser, AuthSecurityAudit, BreakGlassCredential


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _login_hash(login_name: str) -> str:
    return hashlib.sha256(str(login_name).encode("utf-8")).hexdigest()


class SqlBreakGlassRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _record(self, row: BreakGlassCredential) -> BreakGlassCredentialRecord:
        user = self._session.get(AppUser, row.user_id)
        if user is None:
            roles: tuple[str, ...] = ()
            user_active = False
        else:
            try:
                roles = normalize_roles(tuple(json.loads(user.roles_json or "[]")))
            except (TypeError, ValueError, json.JSONDecodeError):
                roles = ()
            user_active = bool(user.active)
        return BreakGlassCredentialRecord(
            credential_id=row.id,
            user_id=row.user_id,
            login_name=row.login_name,
            secret_hash=row.secret_hash,
            credential_version=int(row.credential_version or 1),
            active=bool(row.active),
            failed_attempt_count=int(row.failed_attempt_count or 0),
            first_failed_at=row.first_failed_at,
            locked_until=row.locked_until,
            user_active=user_active,
            user_roles=roles,
        )

    def get_for_login(
        self,
        login_name: str,
        *,
        for_update: bool = False,
    ) -> BreakGlassCredentialRecord | None:
        statement = select(BreakGlassCredential).where(
            BreakGlassCredential.login_name == str(login_name).strip().casefold()
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return self._record(row) if row is not None else None

    def get_by_user_id(
        self,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> BreakGlassCredentialRecord | None:
        statement = select(BreakGlassCredential).where(
            BreakGlassCredential.user_id == str(user_id).strip()
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return self._record(row) if row is not None else None

    def create_credential(
        self,
        *,
        user_id: str,
        login_name: str,
        secret_hash: str,
        now: datetime,
    ) -> BreakGlassCredentialRecord:
        row = BreakGlassCredential(
            id=new_id(),
            user_id=str(user_id).strip(),
            login_name=str(login_name).strip().casefold(),
            secret_hash=str(secret_hash),
            credential_version=1,
            active=True,
            failed_attempt_count=0,
            first_failed_at=None,
            locked_until=None,
            last_success_at=None,
            rotated_at=_aware(now),
        )
        self._session.add(row)
        self._session.flush()
        return self._record(row)

    def _get_credential_row(self, credential_id: str) -> BreakGlassCredential:
        row = self._session.get(BreakGlassCredential, str(credential_id).strip())
        if row is None:
            raise KeyError(f"Credential break-glass {credential_id} introuvable")
        return row

    def rotate_secret(
        self,
        credential_id: str,
        *,
        secret_hash: str,
        now: datetime,
    ) -> BreakGlassCredentialRecord:
        row = self._get_credential_row(credential_id)
        row.secret_hash = str(secret_hash)
        row.credential_version = int(row.credential_version or 1) + 1
        row.active = True
        row.failed_attempt_count = 0
        row.first_failed_at = None
        row.locked_until = None
        row.rotated_at = _aware(now)
        self._session.flush()
        return self._record(row)

    def record_success(
        self,
        credential_id: str,
        *,
        now: datetime,
    ) -> BreakGlassCredentialRecord:
        row = self._get_credential_row(credential_id)
        row.failed_attempt_count = 0
        row.first_failed_at = None
        row.locked_until = None
        row.last_success_at = _aware(now)
        self._session.flush()
        return self._record(row)

    def record_failure(
        self,
        credential_id: str,
        *,
        now: datetime,
        policy: BreakGlassPolicy,
    ) -> BreakGlassCredentialRecord:
        row = self._get_credential_row(credential_id)
        current = _aware(now)
        first_failed_at = (
            _aware(row.first_failed_at)
            if row.first_failed_at is not None
            else None
        )
        if (
            first_failed_at is None
            or current - first_failed_at >= policy.attempt_window
        ):
            row.first_failed_at = current
            row.failed_attempt_count = 1
        else:
            row.failed_attempt_count = int(row.failed_attempt_count or 0) + 1

        if int(row.failed_attempt_count or 0) >= policy.max_attempts:
            row.locked_until = current + policy.lock_duration
        self._session.flush()
        return self._record(row)

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
    ) -> None:
        self._session.add(
            AuthSecurityAudit(
                id=new_id(),
                event_type=str(event_type).strip(),
                success=bool(success),
                credential_id=str(credential_id).strip() if credential_id else None,
                target_user_id=str(target_user_id).strip() if target_user_id else None,
                login_name_hash=_login_hash(str(login_name).strip().casefold()),
                reason_code=str(reason_code).strip(),
                created_at=_aware(now),
            )
        )
        self._session.flush()

    def is_last_active_admin_access(self, user_id: str) -> bool:
        target = str(user_id or "").strip()
        if not target:
            return False

        rows = self._session.execute(
            select(BreakGlassCredential.user_id, AppUser.roles_json)
            .join(AppUser, AppUser.id == BreakGlassCredential.user_id)
            .where(
                BreakGlassCredential.active == true(),
                AppUser.active == true(),
            )
        ).all()

        admin_users: set[str] = set()
        for candidate_user_id, roles_json in rows:
            try:
                roles = normalize_roles(tuple(json.loads(roles_json or "[]")))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if ROLE_ADMIN in roles:
                admin_users.add(str(candidate_user_id))

        return target in admin_users and len(admin_users) == 1
