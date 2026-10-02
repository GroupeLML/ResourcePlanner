from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .errors import ApplicationNotFoundError


PROJECT_MANAGER_SOURCE_ERP = "ERP"
PROJECT_MANAGER_SOURCE_RP = "RP"

PROJECT_MANAGER_RESOLUTION_RESOLVED = "RESOLVED"
PROJECT_MANAGER_RESOLUTION_UNRESOLVED_USER = "UNRESOLVED_USER"
PROJECT_MANAGER_RESOLUTION_UNRESOLVED_CONTACT = "UNRESOLVED_CONTACT"
PROJECT_MANAGER_RESOLUTION_INVALID_REFERENCE = "INVALID_REFERENCE"
PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT = "IDENTITY_CONFLICT"

DIAGNOSTIC_ERP_PROJECT_MANAGER_MISSING = "ERP_PROJECT_MANAGER_MISSING"
DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED = (
    "ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED"
)
DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED = (
    "ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED"
)
DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_INACTIVE = (
    "ERP_PROJECT_MANAGER_APP_USER_INACTIVE"
)
DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE = (
    "ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE"
)
DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT = "PROJECT_MANAGER_IDENTITY_CONFLICT"
DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE = "PROJECT_MANAGER_BROKEN_REFERENCE"
DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_NOT_LINKED = (
    "PROJECT_CO_MANAGER_APP_USER_NOT_LINKED"
)
DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_INACTIVE = (
    "PROJECT_CO_MANAGER_APP_USER_INACTIVE"
)
DIAGNOSTIC_PROJECT_CO_MANAGER_CONTACT_INACTIVE = (
    "PROJECT_CO_MANAGER_CONTACT_INACTIVE"
)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _merge_codes(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


@dataclass(frozen=True, slots=True)
class ProjectManagerProjectRecord:
    project_id: str
    co_managers_version: int
    project_manager_external_id: str | None
    project_manager_name: str | None


@dataclass(frozen=True, slots=True)
class ProjectManagerUserRecord:
    app_user_id: str
    employee_external_id: str | None
    business_contact_id: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class ProjectManagerContactRecord:
    business_contact_id: str
    display_name: str
    active: bool


@dataclass(frozen=True, slots=True)
class ProjectManagerCoManagerRecord:
    project_id: str
    business_contact_id: str


@dataclass(frozen=True, slots=True)
class EffectiveProjectManager:
    sources: tuple[str, ...]
    employee_external_id: str | None
    erp_display_name: str | None
    app_user_id: str | None
    business_contact_id: str | None
    display_name: str
    user_active: bool | None
    contact_active: bool | None
    resolution_status: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectiveProjectManagers:
    project_id: str
    co_managers_version: int
    primary: EffectiveProjectManager | None
    co_managers: tuple[EffectiveProjectManager, ...]
    diagnostics: tuple[str, ...]


class ProjectManagerResolutionRepositoryPort(Protocol):
    def list_projects(
        self,
        project_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerProjectRecord, ...]: ...

    def list_co_managers(
        self,
        project_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerCoManagerRecord, ...]: ...

    def list_users_by_employee_external_ids(
        self,
        employee_external_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerUserRecord, ...]: ...

    def list_users_by_business_contact_ids(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerUserRecord, ...]: ...

    def list_contacts(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerContactRecord, ...]: ...


class ProjectManagerResolutionService:
    """Resolve effective project managers once, in batch, from stable identities."""

    def __init__(self, repository: ProjectManagerResolutionRepositoryPort) -> None:
        self._repository = repository

    @staticmethod
    def _normalize_ids(project_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for raw in project_ids
                if (value := _optional_text(raw)) is not None
            )
        )

    @staticmethod
    def _group_users(
        rows: tuple[ProjectManagerUserRecord, ...],
        *,
        attribute: str,
    ) -> dict[str, tuple[ProjectManagerUserRecord, ...]]:
        grouped: dict[str, list[ProjectManagerUserRecord]] = {}
        for row in rows:
            key = _optional_text(getattr(row, attribute))
            if key is not None:
                grouped.setdefault(key, []).append(row)
        return {key: tuple(values) for key, values in grouped.items()}

    @staticmethod
    def _group_contacts(
        rows: tuple[ProjectManagerContactRecord, ...],
    ) -> dict[str, tuple[ProjectManagerContactRecord, ...]]:
        grouped: dict[str, list[ProjectManagerContactRecord]] = {}
        for row in rows:
            key = _optional_text(row.business_contact_id)
            if key is not None:
                grouped.setdefault(key, []).append(row)
        return {key: tuple(values) for key, values in grouped.items()}

    def _resolve_primary(
        self,
        project: ProjectManagerProjectRecord,
        users_by_employee: dict[str, tuple[ProjectManagerUserRecord, ...]],
        contacts_by_id: dict[str, tuple[ProjectManagerContactRecord, ...]],
    ) -> tuple[EffectiveProjectManager | None, tuple[str, ...]]:
        employee_external_id = _optional_text(project.project_manager_external_id)
        erp_display_name = _optional_text(project.project_manager_name)
        if employee_external_id is None:
            return None, (DIAGNOSTIC_ERP_PROJECT_MANAGER_MISSING,)

        fallback_display = erp_display_name or employee_external_id
        user_matches = users_by_employee.get(employee_external_id, ())
        if len(user_matches) > 1:
            diagnostics = (DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,)
            return (
                EffectiveProjectManager(
                    sources=(PROJECT_MANAGER_SOURCE_ERP,),
                    employee_external_id=employee_external_id,
                    erp_display_name=erp_display_name,
                    app_user_id=None,
                    business_contact_id=None,
                    display_name=fallback_display,
                    user_active=None,
                    contact_active=None,
                    resolution_status=PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT,
                    diagnostics=diagnostics,
                ),
                diagnostics,
            )
        if not user_matches:
            diagnostics = (DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED,)
            return (
                EffectiveProjectManager(
                    sources=(PROJECT_MANAGER_SOURCE_ERP,),
                    employee_external_id=employee_external_id,
                    erp_display_name=erp_display_name,
                    app_user_id=None,
                    business_contact_id=None,
                    display_name=fallback_display,
                    user_active=None,
                    contact_active=None,
                    resolution_status=PROJECT_MANAGER_RESOLUTION_UNRESOLVED_USER,
                    diagnostics=diagnostics,
                ),
                diagnostics,
            )

        user = user_matches[0]
        diagnostics: tuple[str, ...] = ()
        if not user.active:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_INACTIVE,),
            )

        business_contact_id = _optional_text(user.business_contact_id)
        if business_contact_id is None:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED,),
            )
            return (
                EffectiveProjectManager(
                    sources=(PROJECT_MANAGER_SOURCE_ERP,),
                    employee_external_id=employee_external_id,
                    erp_display_name=erp_display_name,
                    app_user_id=user.app_user_id,
                    business_contact_id=None,
                    display_name=fallback_display,
                    user_active=bool(user.active),
                    contact_active=None,
                    resolution_status=PROJECT_MANAGER_RESOLUTION_UNRESOLVED_CONTACT,
                    diagnostics=diagnostics,
                ),
                diagnostics,
            )

        contact_matches = contacts_by_id.get(business_contact_id, ())
        if len(contact_matches) > 1:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,),
            )
            return (
                EffectiveProjectManager(
                    sources=(PROJECT_MANAGER_SOURCE_ERP,),
                    employee_external_id=employee_external_id,
                    erp_display_name=erp_display_name,
                    app_user_id=user.app_user_id,
                    business_contact_id=business_contact_id,
                    display_name=fallback_display,
                    user_active=bool(user.active),
                    contact_active=None,
                    resolution_status=PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT,
                    diagnostics=diagnostics,
                ),
                diagnostics,
            )
        if not contact_matches:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE,),
            )
            return (
                EffectiveProjectManager(
                    sources=(PROJECT_MANAGER_SOURCE_ERP,),
                    employee_external_id=employee_external_id,
                    erp_display_name=erp_display_name,
                    app_user_id=user.app_user_id,
                    business_contact_id=business_contact_id,
                    display_name=fallback_display,
                    user_active=bool(user.active),
                    contact_active=None,
                    resolution_status=PROJECT_MANAGER_RESOLUTION_INVALID_REFERENCE,
                    diagnostics=diagnostics,
                ),
                diagnostics,
            )

        contact = contact_matches[0]
        if not contact.active:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE,),
            )
        display_name = (
            _optional_text(contact.display_name)
            if contact.active
            else None
        ) or fallback_display
        return (
            EffectiveProjectManager(
                sources=(PROJECT_MANAGER_SOURCE_ERP,),
                employee_external_id=employee_external_id,
                erp_display_name=erp_display_name,
                app_user_id=user.app_user_id,
                business_contact_id=business_contact_id,
                display_name=display_name,
                user_active=bool(user.active),
                contact_active=bool(contact.active),
                resolution_status=PROJECT_MANAGER_RESOLUTION_RESOLVED,
                diagnostics=diagnostics,
            ),
            diagnostics,
        )

    def _resolve_co_manager(
        self,
        row: ProjectManagerCoManagerRecord,
        users_by_contact: dict[str, tuple[ProjectManagerUserRecord, ...]],
        contacts_by_id: dict[str, tuple[ProjectManagerContactRecord, ...]],
    ) -> EffectiveProjectManager:
        business_contact_id = _optional_text(row.business_contact_id) or ""
        diagnostics: tuple[str, ...] = ()

        contact_matches = contacts_by_id.get(business_contact_id, ())
        if len(contact_matches) > 1:
            diagnostics = (DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,)
            contact = None
            resolution_status = PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT
        elif not contact_matches:
            diagnostics = (DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE,)
            contact = None
            resolution_status = PROJECT_MANAGER_RESOLUTION_INVALID_REFERENCE
        else:
            contact = contact_matches[0]
            resolution_status = PROJECT_MANAGER_RESOLUTION_RESOLVED
            if not contact.active:
                diagnostics = _merge_codes(
                    diagnostics,
                    (DIAGNOSTIC_PROJECT_CO_MANAGER_CONTACT_INACTIVE,),
                )

        user_matches = users_by_contact.get(business_contact_id, ())
        if len(user_matches) > 1:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,),
            )
            user = None
            resolution_status = PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT
        elif not user_matches:
            diagnostics = _merge_codes(
                diagnostics,
                (DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_NOT_LINKED,),
            )
            user = None
        else:
            user = user_matches[0]
            if not user.active:
                diagnostics = _merge_codes(
                    diagnostics,
                    (DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_INACTIVE,),
                )

        return EffectiveProjectManager(
            sources=(PROJECT_MANAGER_SOURCE_RP,),
            employee_external_id=(
                _optional_text(user.employee_external_id)
                if user is not None
                else None
            ),
            erp_display_name=None,
            app_user_id=user.app_user_id if user is not None else None,
            business_contact_id=business_contact_id,
            display_name=(
                _optional_text(contact.display_name)
                if contact is not None
                else None
            )
            or business_contact_id,
            user_active=bool(user.active) if user is not None else None,
            contact_active=bool(contact.active) if contact is not None else None,
            resolution_status=resolution_status,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _same_stable_identity(
        primary: EffectiveProjectManager,
        co_manager: EffectiveProjectManager,
    ) -> bool:
        if (
            primary.app_user_id is not None
            and co_manager.app_user_id is not None
            and primary.app_user_id == co_manager.app_user_id
        ):
            return True
        return bool(
            primary.business_contact_id
            and co_manager.business_contact_id
            and primary.business_contact_id == co_manager.business_contact_id
        )

    def resolve_projects(
        self,
        project_ids: tuple[str, ...] | list[str],
    ) -> dict[str, EffectiveProjectManagers]:
        wanted_ids = self._normalize_ids(project_ids)
        if not wanted_ids:
            return {}

        projects = self._repository.list_projects(wanted_ids)
        project_ids_found = tuple(project.project_id for project in projects)
        co_manager_rows = self._repository.list_co_managers(project_ids_found)

        employee_external_ids = tuple(
            dict.fromkeys(
                employee_external_id
                for project in projects
                if (
                    employee_external_id := _optional_text(
                        project.project_manager_external_id
                    )
                )
                is not None
            )
        )
        co_manager_contact_ids = tuple(
            dict.fromkeys(
                contact_id
                for row in co_manager_rows
                if (contact_id := _optional_text(row.business_contact_id))
                is not None
            )
        )

        users_by_employee_rows = (
            self._repository.list_users_by_employee_external_ids(
                employee_external_ids
            )
        )
        users_by_contact_rows = (
            self._repository.list_users_by_business_contact_ids(
                co_manager_contact_ids
            )
        )

        contact_ids = tuple(
            dict.fromkeys(
                contact_id
                for contact_id in (
                    *co_manager_contact_ids,
                    *(
                        _optional_text(user.business_contact_id)
                        for user in users_by_employee_rows
                    ),
                    *(
                        _optional_text(user.business_contact_id)
                        for user in users_by_contact_rows
                    ),
                )
                if contact_id is not None
            )
        )
        contacts = self._repository.list_contacts(contact_ids)

        users_by_employee = self._group_users(
            users_by_employee_rows,
            attribute="employee_external_id",
        )
        users_by_contact = self._group_users(
            users_by_contact_rows,
            attribute="business_contact_id",
        )
        contacts_by_id = self._group_contacts(contacts)

        co_managers_by_project: dict[str, list[ProjectManagerCoManagerRecord]] = {}
        for row in co_manager_rows:
            co_managers_by_project.setdefault(row.project_id, []).append(row)

        resolved: dict[str, EffectiveProjectManagers] = {}
        for project in projects:
            primary, project_diagnostics = self._resolve_primary(
                project,
                users_by_employee,
                contacts_by_id,
            )
            effective_co_managers: list[EffectiveProjectManager] = []
            for row in co_managers_by_project.get(project.project_id, ()):
                co_manager = self._resolve_co_manager(
                    row,
                    users_by_contact,
                    contacts_by_id,
                )
                if (
                    primary is not None
                    and self._same_stable_identity(primary, co_manager)
                ):
                    primary = replace(
                        primary,
                        sources=tuple(
                            dict.fromkeys((*primary.sources, *co_manager.sources))
                        ),
                        diagnostics=_merge_codes(
                            primary.diagnostics,
                            co_manager.diagnostics,
                        ),
                    )
                    project_diagnostics = _merge_codes(
                        project_diagnostics,
                        co_manager.diagnostics,
                    )
                    continue
                effective_co_managers.append(co_manager)
                project_diagnostics = _merge_codes(
                    project_diagnostics,
                    co_manager.diagnostics,
                )

            resolved[project.project_id] = EffectiveProjectManagers(
                project_id=project.project_id,
                co_managers_version=int(project.co_managers_version),
                primary=primary,
                co_managers=tuple(effective_co_managers),
                diagnostics=project_diagnostics,
            )
        return resolved

    def resolve_project(self, project_id: str) -> EffectiveProjectManagers:
        wanted_id = _optional_text(project_id)
        if wanted_id is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": str(project_id or "")},
            )
        resolved = self.resolve_projects((wanted_id,))
        try:
            return resolved[wanted_id]
        except KeyError as exc:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": wanted_id},
            ) from exc
