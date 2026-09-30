from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.erp_user_directory import (
    ErpUserDirectoryRecord,
    ExternalErpUserRecord,
)
from ...application.security import (
    OIDC_STATE_CONFLICT,
    normalize_roles,
    oidc_identity_state,
)
from .erp_user_models import ErpUserDirectoryEntry
from .identity_models import AppUser
from .models import Resource


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} est requis")
    return text


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class SqlErpUserDirectoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _record(self, row: ErpUserDirectoryEntry) -> ErpUserDirectoryRecord:
        resource = self._session.scalar(
            select(Resource).where(Resource.external_id == row.employee_external_id)
        )
        raw_directory_roles = json.loads(row.roles_json or "[]")
        directory_roles = normalize_roles(
            tuple(str(role) for role in raw_directory_roles)
        )
        linked_user = self._session.scalar(
            select(AppUser).where(AppUser.erp_user_id == row.user_id)
        )
        candidate_user_ids = self._session.scalars(
            select(ErpUserDirectoryEntry.user_id).where(
                ErpUserDirectoryEntry.employee_external_id
                == row.employee_external_id
            )
        ).all()

        oidc_state = oidc_identity_state(None, None)
        if linked_user is None:
            # Compatibility only for identities already linked before erp_user_id
            # existed. Never adopt a pending account by EmployeID, and never guess
            # among multiple RP_Users UserID values.
            legacy_user = self._session.scalar(
                select(AppUser).where(
                    AppUser.employee_external_id == row.employee_external_id,
                    AppUser.erp_user_id.is_(None),
                    AppUser.issuer.is_not(None),
                    AppUser.subject.is_not(None),
                )
            )
            if legacy_user is not None and len(candidate_user_ids) == 1:
                linked_user = legacy_user
            elif legacy_user is not None:
                oidc_state = OIDC_STATE_CONFLICT

        issuer = None
        subject = None
        if linked_user is not None:
            issuer = _optional(linked_user.issuer)
            subject = _optional(linked_user.subject)
            oidc_state = oidc_identity_state(issuer, subject)
            raw_roles = json.loads(linked_user.roles_json or "[]")
            roles = normalize_roles(tuple(str(role) for role in raw_roles))
            local_active = bool(linked_user.active)
        else:
            roles = directory_roles
            local_active = bool(row.local_active)

        return ErpUserDirectoryRecord(
            user_id=row.user_id,
            employee_external_id=row.employee_external_id,
            display_name=row.display_name,
            first_name=_optional(row.first_name),
            last_name=_optional(row.last_name),
            email=_optional(row.email),
            erp_user_active=bool(row.erp_user_active),
            employee_status=_optional(row.employee_status),
            local_active=local_active,
            roles=roles,
            resource_id=resource.id if resource is not None else None,
            resource_name=resource.name if resource is not None else None,
            resource_erp_active=bool(resource.erp_active) if resource is not None else None,
            oidc_state=oidc_state,
            oidc_user_active=(
                bool(linked_user.active) if linked_user is not None else None
            ),
            app_user_id=linked_user.id if linked_user is not None else None,
            issuer=issuer,
            subject=subject,
        )

    def upsert_external_user(self, user: ExternalErpUserRecord) -> str:
        user_id = _required(user.user_id, "user_id")
        employee_external_id = _required(user.employee_external_id, "employee_external_id")
        display_name = _required(user.display_name, "display_name")
        row = self._session.get(ErpUserDirectoryEntry, user_id)
        values = {
            "employee_external_id": employee_external_id,
            "display_name": display_name,
            "first_name": _optional(user.first_name),
            "last_name": _optional(user.last_name),
            "email": _optional(user.email),
            "erp_user_active": bool(user.erp_user_active),
            "employee_status": _optional(user.employee_status),
        }
        if row is None:
            self._session.add(
                ErpUserDirectoryEntry(
                    user_id=user_id,
                    **values,
                    local_active=False,
                    roles_json="[]",
                )
            )
            self._session.flush()
            return "created"

        changed = False
        for field, value in values.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
        if changed:
            self._session.flush()
            return "updated"
        return "unchanged"

    def list_users(self) -> tuple[ErpUserDirectoryRecord, ...]:
        rows = self._session.scalars(
            select(ErpUserDirectoryEntry).order_by(
                ErpUserDirectoryEntry.display_name,
                ErpUserDirectoryEntry.user_id,
            )
        ).all()
        return tuple(self._record(row) for row in rows)

    def get_by_user_id(self, user_id: str) -> ErpUserDirectoryRecord | None:
        row = self._session.get(ErpUserDirectoryEntry, str(user_id).strip())
        return self._record(row) if row is not None else None

    def update_local_access(
        self,
        user_id: str,
        *,
        active: bool,
        roles: tuple[str, ...],
    ) -> ErpUserDirectoryRecord:
        row = self._session.get(ErpUserDirectoryEntry, str(user_id).strip())
        if row is None:
            raise KeyError(user_id)
        row.local_active = bool(active)
        row.roles_json = json.dumps(list(normalize_roles(roles)), separators=(",", ":"))
        self._session.flush()
        return self._record(row)
