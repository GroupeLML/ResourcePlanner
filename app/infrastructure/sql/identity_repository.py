from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...application.security import UserIdentityRecord, normalize_roles
from .approval_scope_models import ApprovalScopeApprover, AssetTypeApprovalScopeMapping
from .asset_models import AssetApprover
from .base import new_id
from .business_contact_models import BusinessContact
from .erp_user_models import ErpUserDirectoryEntry
from .identity_models import AppUser
from .planning_version import SqlPlanningMutationVersionRepository


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _required_text(value: object, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field} est requis")
    return text


class SqlUserIdentityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _controls_asset_authority(self, user_id: str) -> bool:
        identifier = str(user_id or "").strip()
        if not identifier:
            return False
        specific = self._session.scalar(
            select(AssetApprover.asset_id).where(
                AssetApprover.app_user_id == identifier
            )
        )
        if specific is not None:
            return True
        scoped = self._session.scalar(
            select(ApprovalScopeApprover.approval_scope_id)
            .join(
                AssetTypeApprovalScopeMapping,
                AssetTypeApprovalScopeMapping.approval_scope_id
                == ApprovalScopeApprover.approval_scope_id,
            )
            .where(ApprovalScopeApprover.app_user_id == identifier)
        )
        return scoped is not None

    def _guard_asset_authority_change(
        self,
        row: AppUser,
        *,
        roles_json: str,
        active: bool,
    ) -> None:
        if (
            row.roles_json != roles_json
            or bool(row.active) != bool(active)
        ) and self._controls_asset_authority(row.id):
            SqlPlanningMutationVersionRepository(self._session).acquire()

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
            issuer=_optional_text(row.issuer),
            subject=_optional_text(row.subject),
            display_name=row.display_name,
            email=row.email,
            roles=roles,
            active=bool(row.active),
            employee_external_id=_optional_text(row.employee_external_id),
            erp_user_id=_optional_text(row.erp_user_id),
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

    def _roles_json(
        self,
        roles: tuple[str, ...] | list[str] | set[str],
    ) -> str:
        normalized_roles = normalize_roles(roles)
        if not normalized_roles:
            raise ValueError("Au moins un rôle RessourcePlanner est requis")
        return json.dumps(list(normalized_roles), separators=(",", ":"))

    def _validate_erp_user_id(self, erp_user_id: str | None) -> str | None:
        value = _optional_text(erp_user_id)
        if value is not None and self._session.get(ErpUserDirectoryEntry, value) is None:
            raise ValueError(f"Utilisateur ERP {value} introuvable")
        return value

    def _assert_account_links_available(
        self,
        *,
        current_user_id: str | None,
        employee_external_id: str | None,
        erp_user_id: str | None,
    ) -> None:
        if employee_external_id is not None:
            owner = self._session.scalar(
                select(AppUser.id).where(
                    AppUser.employee_external_id == employee_external_id
                )
            )
            if owner is not None and owner != current_user_id:
                raise ValueError("Cet EmployeID est déjà lié à un autre AppUser")
        if erp_user_id is not None:
            owner = self._session.scalar(
                select(AppUser.id).where(AppUser.erp_user_id == erp_user_id)
            )
            if owner is not None and owner != current_user_id:
                raise ValueError("Ce UserID ERP est déjà lié à un autre AppUser")

    def list_users(self) -> tuple[UserIdentityRecord, ...]:
        rows = self._session.scalars(
            select(AppUser).order_by(AppUser.display_name, AppUser.issuer, AppUser.subject)
        ).all()
        return tuple(self._record(row) for row in rows)

    def get_by_id(self, user_id: str) -> UserIdentityRecord | None:
        value = _optional_text(user_id)
        if value is None:
            return None
        row = self._session.get(AppUser, value)
        return self._record(row) if row is not None else None

    def get_by_external_identity(self, issuer: str, subject: str) -> UserIdentityRecord | None:
        issuer_value = _optional_text(issuer)
        subject_value = _optional_text(subject)
        if issuer_value is None or subject_value is None:
            return None
        row = self._session.scalar(
            select(AppUser).where(
                AppUser.issuer == issuer_value,
                AppUser.subject == subject_value,
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

    def get_by_erp_user_id(self, erp_user_id: str) -> UserIdentityRecord | None:
        value = _optional_text(erp_user_id)
        if value is None:
            return None
        row = self._session.scalar(
            select(AppUser).where(AppUser.erp_user_id == value)
        )
        return self._record(row) if row is not None else None

    def create_account(
        self,
        *,
        display_name: str,
        email: str | None,
        roles: tuple[str, ...] | list[str] | set[str],
        active: bool = True,
        employee_external_id: str | None = None,
        erp_user_id: str | None = None,
    ) -> UserIdentityRecord:
        display_name_value = _required_text(display_name, "display_name")
        employee_value = _optional_text(employee_external_id)
        erp_value = self._validate_erp_user_id(erp_user_id)
        self._assert_account_links_available(
            current_user_id=None,
            employee_external_id=employee_value,
            erp_user_id=erp_value,
        )
        row = AppUser(
            issuer=None,
            subject=None,
            display_name=display_name_value,
            email=_optional_text(email),
            employee_external_id=employee_value,
            erp_user_id=erp_value,
            roles_json=self._roles_json(roles),
            active=bool(active),
        )
        self._session.add(row)
        self._session.flush()
        self._ensure_business_contact(row)
        return self._record(row)

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
    ) -> UserIdentityRecord:
        user_id_value = _required_text(app_user_id, "app_user_id")
        row = self._session.get(AppUser, user_id_value)
        if row is None:
            raise KeyError(f"Utilisateur {user_id_value} introuvable")
        employee_value = _optional_text(employee_external_id)
        erp_value = self._validate_erp_user_id(erp_user_id)
        self._assert_account_links_available(
            current_user_id=row.id,
            employee_external_id=employee_value,
            erp_user_id=erp_value,
        )
        roles_json = self._roles_json(roles)
        self._guard_asset_authority_change(
            row,
            roles_json=roles_json,
            active=bool(active),
        )
        row.display_name = _required_text(display_name, "display_name")
        row.email = _optional_text(email)
        row.roles_json = roles_json
        row.active = bool(active)
        row.employee_external_id = employee_value
        row.erp_user_id = erp_value
        self._session.flush()
        self._ensure_business_contact(row)
        return self._record(row)

    def bind_external_identity(
        self,
        app_user_id: str,
        issuer: str,
        subject: str,
    ) -> UserIdentityRecord:
        user_id_value = _required_text(app_user_id, "app_user_id")
        issuer_value = _required_text(issuer, "issuer")
        subject_value = _required_text(subject, "subject")
        row = self._session.get(AppUser, user_id_value)
        if row is None:
            raise KeyError(f"Utilisateur {user_id_value} introuvable")

        current_issuer = _optional_text(row.issuer)
        current_subject = _optional_text(row.subject)
        if current_issuer is not None or current_subject is not None:
            if current_issuer == issuer_value and current_subject == subject_value:
                return self._record(row)
            raise ValueError("Cet AppUser possède déjà une autre identité OIDC")

        owner = self._session.scalar(
            select(AppUser.id).where(
                AppUser.issuer == issuer_value,
                AppUser.subject == subject_value,
            )
        )
        if owner is not None and owner != user_id_value:
            raise ValueError("Cette identité OIDC appartient déjà à un autre AppUser")

        try:
            result = self._session.execute(
                update(AppUser)
                .where(
                    AppUser.id == user_id_value,
                    AppUser.issuer.is_(None),
                    AppUser.subject.is_(None),
                )
                .values(issuer=issuer_value, subject=subject_value)
            )
            if result.rowcount != 1:
                raise ValueError("Cet AppUser possède déjà une autre identité OIDC")
            self._session.flush()
        except IntegrityError as exc:
            raise ValueError(
                "Cette identité OIDC appartient déjà à un autre AppUser"
            ) from exc

        row = self._session.get(AppUser, user_id_value, populate_existing=True)
        if row is None:
            raise KeyError(f"Utilisateur {user_id_value} introuvable")
        if row.issuer == issuer_value and row.subject == subject_value:
            return self._record(row)
        raise ValueError("Cet AppUser possède déjà une autre identité OIDC")

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
        """Compatibility path for existing callers until IDENTITY-D.

        Account administration and external identity binding must use the explicit
        primitives above. This method intentionally preserves the pre-IDENTITY-B
        runtime behavior for the current OIDC/admin callers.
        """

        issuer_value = _required_text(issuer, "issuer")
        subject_value = _required_text(subject, "subject")
        display_name_value = _required_text(display_name, "display_name")
        roles_json = self._roles_json(roles)

        row = self._session.scalar(
            select(AppUser).where(
                AppUser.issuer == issuer_value,
                AppUser.subject == subject_value,
            )
        )
        employee_value = _optional_text(employee_external_id)
        if row is None:
            self._assert_account_links_available(
                current_user_id=None,
                employee_external_id=employee_value,
                erp_user_id=None,
            )
            row = AppUser(
                issuer=issuer_value,
                subject=subject_value,
                display_name=display_name_value,
                email=_optional_text(email),
                employee_external_id=employee_value,
                roles_json=roles_json,
                active=bool(active),
            )
            self._session.add(row)
        else:
            self._assert_account_links_available(
                current_user_id=row.id,
                employee_external_id=employee_value,
                erp_user_id=_optional_text(row.erp_user_id),
            )
            self._guard_asset_authority_change(
                row,
                roles_json=roles_json,
                active=bool(active),
            )
            row.display_name = display_name_value
            row.email = _optional_text(email)
            if employee_value is not None:
                row.employee_external_id = employee_value
            row.roles_json = roles_json
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
