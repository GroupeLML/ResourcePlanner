from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.infrastructure.sql import (
    AppUser,
    BusinessContact,
    Project,
    ProjectCoManager,
    ProjectManagerAudit,
    SqlProjectCoManagerRepository,
    create_session_factory,
    create_sql_engine,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class ProjectCoManagerSqlServerConcurrencyTests(unittest.TestCase):
    def test_two_mutations_with_same_expected_version_have_one_winner(self) -> None:
        marker = uuid4().hex[:12]
        project_id = f"573A-P-{marker}"
        project_number = f"573A-{marker}"
        actor_id = f"573A-U-{marker}"
        contact_ids = (f"573A-C1-{marker}", f"573A-C2-{marker}")

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_id,
                            number=project_number,
                            name="573A SQL Server CAS",
                        ),
                        AppUser(
                            id=actor_id,
                            display_name="573A SQL Server CAS",
                            email=None,
                            roles_json='["ADMIN"]',
                            active=True,
                        ),
                        BusinessContact(
                            id=contact_ids[0],
                            display_name="Contact 1",
                            active=True,
                        ),
                        BusinessContact(
                            id=contact_ids[1],
                            display_name="Contact 2",
                            active=True,
                        ),
                    ]
                )

            def add(contact_id: str) -> str:
                with factory.begin() as session:
                    try:
                        SqlProjectCoManagerRepository(session).add_co_manager(
                            project_id,
                            contact_id,
                            actor_user_id=actor_id,
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "project_co_managers_version_conflict":
                            raise
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(add, contact_ids))

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                project = session.get(Project, project_id)
                assert project is not None
                self.assertEqual(project.co_managers_version, 2)
                rows = list(
                    session.scalars(
                        select(ProjectCoManager).where(
                            ProjectCoManager.project_id == project_id
                        )
                    ).all()
                )
                audits = list(
                    session.scalars(
                        select(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == project_id
                        )
                    ).all()
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(len(audits), 1)
                self.assertEqual(audits[0].resulting_version, 2)
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == project_id
                        )
                    )
                    session.execute(
                        delete(ProjectCoManager).where(
                            ProjectCoManager.project_id == project_id
                        )
                    )
                    session.execute(delete(Project).where(Project.id == project_id))
                    session.execute(delete(AppUser).where(AppUser.id == actor_id))
                    session.execute(
                        delete(BusinessContact).where(
                            BusinessContact.id.in_(contact_ids)
                        )
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
