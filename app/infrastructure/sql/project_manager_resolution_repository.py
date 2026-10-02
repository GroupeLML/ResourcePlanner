from __future__ import annotations

from collections.abc import Iterable, Iterator

from sqlalchemy import exists, false, or_, select
from sqlalchemy.orm import Session

from ...application.project_managers import (
    ProjectManagerCoManagerRecord,
    ProjectManagerContactRecord,
    ProjectManagerProjectRecord,
    ProjectManagerResolutionRepositoryPort,
    ProjectManagerUserRecord,
)
from .business_contact_models import BusinessContact
from .identity_models import AppUser
from .models import Project
from .project_manager_models import ProjectCoManager


SQL_IN_BATCH_SIZE = 900


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text
            for value in values
            if (text := _optional_text(value)) is not None
        )
    )


def _chunks(values: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    for index in range(0, len(values), SQL_IN_BATCH_SIZE):
        yield values[index : index + SQL_IN_BATCH_SIZE]


def project_managed_by_user_predicate(
    *,
    employee_external_id: str | None,
    business_contact_id: str | None,
):
    """Return the shared SQL predicate for canonical project-manager scope.

    The predicate is intentionally based only on stable identities. RBAC and workforce
    participation remain separate concerns.
    """

    employee_id = _optional_text(employee_external_id)
    contact_id = _optional_text(business_contact_id)
    clauses = []
    if employee_id is not None:
        clauses.append(Project.project_manager_external_id == employee_id)
    if contact_id is not None:
        clauses.append(
            exists(
                select(1)
                .select_from(ProjectCoManager)
                .where(
                    ProjectCoManager.project_id == Project.id,
                    ProjectCoManager.business_contact_id == contact_id,
                )
            )
        )
    if not clauses:
        return false()
    return or_(*clauses)


class SqlProjectManagerResolutionRepository(ProjectManagerResolutionRepositoryPort):
    """Batch SQL loader for the canonical effective-project-manager projection."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_projects(
        self,
        project_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerProjectRecord, ...]:
        wanted = _normalized(project_ids)
        if not wanted:
            return ()
        records: list[ProjectManagerProjectRecord] = []
        for chunk in _chunks(wanted):
            rows = self._session.execute(
                select(
                    Project.id,
                    Project.co_managers_version,
                    Project.project_manager_external_id,
                    Project.project_manager_name,
                )
                .where(Project.id.in_(chunk))
                .order_by(Project.id)
            ).all()
            records.extend(
                ProjectManagerProjectRecord(
                    project_id=row.id,
                    co_managers_version=int(row.co_managers_version),
                    project_manager_external_id=_optional_text(
                        row.project_manager_external_id
                    ),
                    project_manager_name=_optional_text(row.project_manager_name),
                )
                for row in rows
            )
        return tuple(records)

    def list_co_managers(
        self,
        project_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerCoManagerRecord, ...]:
        wanted = _normalized(project_ids)
        if not wanted:
            return ()
        records: list[ProjectManagerCoManagerRecord] = []
        for chunk in _chunks(wanted):
            rows = self._session.execute(
                select(
                    ProjectCoManager.project_id,
                    ProjectCoManager.business_contact_id,
                )
                .where(ProjectCoManager.project_id.in_(chunk))
                .order_by(
                    ProjectCoManager.project_id,
                    ProjectCoManager.created_at,
                    ProjectCoManager.business_contact_id,
                )
            ).all()
            records.extend(
                ProjectManagerCoManagerRecord(
                    project_id=row.project_id,
                    business_contact_id=row.business_contact_id,
                )
                for row in rows
            )
        return tuple(records)

    def list_users_by_employee_external_ids(
        self,
        employee_external_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerUserRecord, ...]:
        return self._list_users(
            values=employee_external_ids,
            column=AppUser.employee_external_id,
        )

    def list_users_by_business_contact_ids(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerUserRecord, ...]:
        return self._list_users(
            values=business_contact_ids,
            column=AppUser.business_contact_id,
        )

    def _list_users(
        self,
        *,
        values: tuple[str, ...],
        column,
    ) -> tuple[ProjectManagerUserRecord, ...]:
        wanted = _normalized(values)
        if not wanted:
            return ()
        by_id: dict[str, ProjectManagerUserRecord] = {}
        for chunk in _chunks(wanted):
            rows = self._session.execute(
                select(
                    AppUser.id,
                    AppUser.employee_external_id,
                    AppUser.business_contact_id,
                    AppUser.active,
                )
                .where(column.in_(chunk))
                .order_by(AppUser.id)
            ).all()
            for row in rows:
                by_id[row.id] = ProjectManagerUserRecord(
                    app_user_id=row.id,
                    employee_external_id=_optional_text(row.employee_external_id),
                    business_contact_id=_optional_text(row.business_contact_id),
                    active=bool(row.active),
                )
        return tuple(by_id.values())

    def list_contacts(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[ProjectManagerContactRecord, ...]:
        wanted = _normalized(business_contact_ids)
        if not wanted:
            return ()
        by_id: dict[str, ProjectManagerContactRecord] = {}
        for chunk in _chunks(wanted):
            rows = self._session.execute(
                select(
                    BusinessContact.id,
                    BusinessContact.display_name,
                    BusinessContact.active,
                )
                .where(BusinessContact.id.in_(chunk))
                .order_by(BusinessContact.id)
            ).all()
            for row in rows:
                by_id[row.id] = ProjectManagerContactRecord(
                    business_contact_id=row.id,
                    display_name=str(row.display_name or "").strip() or row.id,
                    active=bool(row.active),
                )
        return tuple(by_id.values())
