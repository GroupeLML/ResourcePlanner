from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.infrastructure.sql import (
    BusinessContact,
    PlanningChangeHistory,
    Project,
    SqlOperationalResponsibilityMutationRepository,
    create_session_factory,
    create_sql_engine,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 594D réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class OperationalResponsibilitySqlServerConcurrencyTests(unittest.TestCase):
    def test_project_override_cas_has_one_winner_and_resolves_persisted_contact(
        self,
    ) -> None:
        marker = uuid4().hex[:12]
        project_id = f"594D-P-{marker}"
        project_number = f"594D-{marker}"
        contacts = (f"594D-C1-{marker}", f"594D-C2-{marker}")

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_id,
                            number=project_number,
                            name="594D SQL Server CAS",
                        ),
                        BusinessContact(
                            id=contacts[0],
                            display_name="Responsable SQL A",
                            active=True,
                        ),
                        BusinessContact(
                            id=contacts[1],
                            display_name="Responsable SQL B",
                            active=True,
                        ),
                    ]
                )

            def mutate(contact_id: str) -> str:
                with factory.begin() as session:
                    try:
                        SqlOperationalResponsibilityMutationRepository(
                            session
                        ).set_project_override(
                            project_id,
                            contact_id,
                            actor_user_id=f"actor-{marker}",
                            expected_version=1,
                            idempotency_key=f"594d-{contact_id}",
                        )
                    except ApplicationConflictError as exc:
                        if (
                            exc.code
                            != "project_operational_responsibility_version_conflict"
                        ):
                            raise
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(mutate, contacts))

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                project = session.get(Project, project_id)
                assert project is not None
                self.assertEqual(
                    int(project.operational_responsible_override_version or 0),
                    2,
                )
                self.assertIn(
                    project.operational_responsible_override_contact_id,
                    contacts,
                )
                audits = list(
                    session.scalars(
                        select(PlanningChangeHistory).where(
                            PlanningChangeHistory.entity_type == "PROJECT",
                            PlanningChangeHistory.entity_id == project_id,
                            PlanningChangeHistory.action
                            == "Modification responsable opérationnel projet",
                        )
                    ).all()
                )
                self.assertEqual(len(audits), 1)
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(PlanningChangeHistory).where(
                            PlanningChangeHistory.entity_type == "PROJECT",
                            PlanningChangeHistory.entity_id == project_id,
                        )
                    )
                    session.execute(delete(Project).where(Project.id == project_id))
                    session.execute(
                        delete(BusinessContact).where(
                            BusinessContact.id.in_(contacts)
                        )
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
