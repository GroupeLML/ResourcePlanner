from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.project_manager_admin import (
    ProjectManagerAdminProjectRecord,
    ProjectManagerAdminProjectRepositoryPort,
)
from .models import Project


class SqlProjectManagerAdminProjectRepository(ProjectManagerAdminProjectRepositoryPort):
    """Resolve the public project number to the canonical persisted project identity."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_project(
        self,
        project_number: str,
    ) -> ProjectManagerAdminProjectRecord | None:
        row = self._session.execute(
            select(Project.id, Project.number).where(Project.number == project_number)
        ).one_or_none()
        if row is None:
            return None
        return ProjectManagerAdminProjectRecord(
            project_id=str(row.id),
            project_number=str(row.number),
        )
