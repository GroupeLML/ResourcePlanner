from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.security import UserIdentityRecord, normalize_roles
from .base import new_id
from .business_contact_models import BusinessContact
from .identity_models import AppUser


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class SqlUserIdentityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _record(self, row: AppUser) -> UserIdentityRecord:
        raw_roles = json.loads(row.roles_json or "[]")
        roles = normalize_roles(tuple(str(role) for role in raw_roles))
        contact = (
            self._session.get(BusinessContact, row.business_contact_id)
            if row.business_contact_id
            else None
        )
        return UserIdentityRecord(
            user_id=row.id,
            issuer=row.issuer,
            subject=row.subject,
            display_name=row.display_name,
            email=row.email,
            roles=roles,
            active=bool(row.active),
            employee_external_id=_optional_text(row.employee_external_id),
            business_contact_id=row.business_contact_id,
            phone=_optional_text(contact.phone) if contact is not None else None,
        )

    def _ensure_business_contact(self, row: AppUser) -> BusinessContact:
        contact = (
            self._session.get(BusinessContact, row.business_contact_id)
            if row.business_contact_id
            else None
        )
        external_id = _optional_text(row.employee_external_id)
        if contact is None and external_id is not None:
            contact = self._session.scalar(
                select(BusinessContact).where(
                    BusinessContact.external_system == "RESOURCEPLANNER",
                    BusinessContact.external_entity == "EMPLOYEE",
                    BusinessContact.external_id == external_id,
                )
            )

        if contact is None:
            contact = BusinessContact(
                id=new_id(),
                display_name=row.display_name,
                email=_optional_text(row.email),
                phone=None,
                active=bool(row.active),
                source="APP_USER",
                external_system="RESOURCEPLANNER" if external_id else None,
                external_entity="EMPLOYEE" if external_id else None,
                external_id=external_id,
                version=1,
            )
            self._session.add(contact)
            self._session.flush()

        row.business_contact_id = contact.id
        changed = False
        for field, value in (
            ("display_name", row.display_name),
            ("email", _optional_text(row.email)),
            ("active", bool(row.active)),
            ("source", "APP_USER"),
        ):
            if getattr(contact, field) != value:
                setattr(contact, field, value)
                changed = True

        if external_id is not None:
            for field, value in (
                ("external_system", "RESOURCEPLANNER"),
                ("external_entity", "EMPLOYEE"),
                ("external_id", external_id),
            ):
                if getattr(contact, field) != value:
                    setattr(contact, field, value)
                    changed = True

        if changed:
            contact.version = int(contact.version or 1) + 1
        self._session.flush()
        return contact

    def list_users(self) -> tuple[UserIdentityRecord, ...]:
        rows = self._session.scalars(
            select(AppUser).order_by(AppUser.display_name, AppUser.issuer, AppUser.subject)
        ).all()
        return tuple(self._record(row) for row in rows)

    def get_by_id(self, user_id: str) -> UserIdentityRecord | None:
        row = self._session.get(AppUser, str(user_id).strip())
        return self._record(row) if row is not None else None

    def get_by_external_identity(self, issuer: str, subject: str) -> UserIdentityRecord | None:
        row = self._session.scalar(
            select(AppUser).where(
                AppUser.issuer == str(issuer).strip(),
                AppUser.subject == str(subject).strip(),
            )
        )
        return self._record(row) if row is not None else None

    def get_by_employee_external_id(
        self,
        employee_external_id: str,
    ) -> UserIdentityRecord | None:
        employee_value = _optional_text(employee_external_id)
        if employee_value is None:
            return None
        row = self._session.scalar(
            select(AppUser).where(
                AppUser.employee_external_id == employee_value
            )
        )
        return self._record(row) if row is not None else None

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
    ) -> UserIdentityRecord:
        issuer_value = str(issuer).strip()
        subject_value = str(subject).strip()
        display_name_value = str(display_name).strip()
        if not issuer_value or not subject_value or not display_name_value:
            raise ValueError("issuer, subject et display_name sont requis")
        normalized_roles = normalize_roles(roles)
        if not normalized_roles:
            raise ValueError("Au moins un rôle RessourcePlanner est requis")

        row = self._session.scalar(
            select(AppUser).where(
                AppUser.issuer == issuer_value,
                AppUser.subject == subject_value,
            )
        )
        employee_value = _optional_text(employee_external_id)
        if row is None:
            row = AppUser(
                issuer=issuer_value,
                subject=subject_value,
                display_name=display_name_value,
                email=_optional_text(email),
                employee_external_id=employee_value,
                roles_json=json.dumps(list(normalized_roles), separators=(",", ":")),
                active=bool(active),
            )
            self._session.add(row)
        else:
            row.display_name = display_name_value
            row.email = _optional_text(email)
            if employee_value is not None:
                row.employee_external_id = employee_value
            row.roles_json = json.dumps(list(normalized_roles), separators=(",", ":"))
            row.active = bool(active)
        self._session.flush()
        self._ensure_business_contact(row)
        return self._record(row)

    def set_business_phone(
        self,
        user_id: str,
        phone: str | None,
    ) -> UserIdentityRecord:
        row = self._session.get(AppUser, str(user_id).strip())
        if row is None:
            raise KeyError(f"Utilisateur {user_id} introuvable")
        contact = self._ensure_business_contact(row)
        phone_value = _optional_text(phone)
        if contact.phone != phone_value:
            contact.phone = phone_value
            contact.version = int(contact.version or 1) + 1
            self._session.flush()
        return self._record(row)
