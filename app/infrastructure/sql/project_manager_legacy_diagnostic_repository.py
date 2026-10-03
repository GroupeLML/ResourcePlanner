from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.project_manager_legacy_diagnostic import (
    LegacyProjectManagerContactRecord,
    LegacyProjectManagerDiagnosticRepositoryPort,
    LegacyProjectManagerProjectRecord,
)
from .business_contact_models import BusinessContact
from .models import Project
from .project_manager_resolution_repository import SQL_IN_BATCH_SIZE


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class SqlLegacyProjectManagerDiagnosticRepository(
    LegacyProjectManagerDiagnosticRepositoryPort
):
    """Batch read-only access to the deprecated Project manager FK."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_projects(self) -> tuple[LegacyProjectManagerProjectRecord, ...]:
        rows = self._session.execute(
            select(
                Project.id,
                Project.number,
                Project.project_manager_contact_id,
            ).order_by(Project.number, Project.id)
        ).all()
        return tuple(
            LegacyProjectManagerProjectRecord(
                project_id=row.id,
                project_number=row.number,
                legacy_project_manager_contact_id=_optional_text(
                    row.project_manager_contact_id
                ),
            )
            for row in rows
        )

    def list_contacts(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[LegacyProjectManagerContactRecord, ...]:
        wanted = tuple(
            dict.fromkeys(
                value
                for raw in business_contact_ids
                if (value := _optional_text(raw)) is not None
            )
        )
        if not wanted:
            return ()

        result: dict[str, LegacyProjectManagerContactRecord] = {}
        for index in range(0, len(wanted), SQL_IN_BATCH_SIZE):
            chunk = wanted[index : index + SQL_IN_BATCH_SIZE]
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
                result[row.id] = LegacyProjectManagerContactRecord(
                    business_contact_id=row.id,
                    display_name=str(row.display_name or "").strip() or row.id,
                    active=bool(row.active),
                )
        return tuple(result.values())
