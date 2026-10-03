from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.application.operational_contacts import OperationalContactService
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.operational_contact_repository import (
    SqlOperationalContactRepository,
)
from app.infrastructure.sql.project_communication_repository import (
    SqlProjectCommunicationRepository,
)
from app.infrastructure.sql.project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)
from app.application.project_managers import ProjectManagerResolutionService
from app.infrastructure.sql.user_view_context_repository import (
    SqlUserViewContextRepository,
)
from app.infrastructure.sql.web_query_repository import SqlPlannerQueryRepositoryWeb


DAY = date(2026, 10, 2)


class ProjectManager573ETransverseAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "573e-transverse.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

        with self.factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-A",
                        display_name="Principal A",
                        email="configured-email-a",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-B",
                        display_name="Legacy B",
                        email="configured-email-b",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-C",
                        display_name="Co-manager C",
                        email="configured-email-c",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    AppUser(
                        id="U-A",
                        display_name="Principal A",
                        email=None,
                        employee_external_id="EMP-A",
                        business_contact_id="C-A",
                        roles_json='["PROJECT_MANAGER"]',
                        active=True,
                    ),
                    AppUser(
                        id="U-B",
                        display_name="Legacy B",
                        email=None,
                        employee_external_id=None,
                        business_contact_id="C-B",
                        roles_json='["PROJECT_MANAGER"]',
                        active=True,
                    ),
                    AppUser(
                        id="U-C",
                        display_name="Co-manager C",
                        email=None,
                        employee_external_id=None,
                        business_contact_id="C-C",
                        roles_json='["PROJECT_MANAGER"]',
                        active=True,
                    ),
                    Project(
                        id="P",
                        number="P-573E",
                        name="Projet transverse 573E",
                        project_manager_external_id="EMP-A",
                        project_manager_name="Principal ERP A",
                        project_manager_contact_id="C-B",
                        status="Actif",
                    ),
                    Resource(
                        id="R",
                        external_id=None,
                        name="Ressource transverse",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ProjectCoManager(
                        project_id="P",
                        business_contact_id="C-C",
                        created_by_user_id="U-C",
                    ),
                    WorkforceRequest(
                        id="D",
                        legacy_demand_number="DM-573E",
                        project_id="P",
                        status="Soumise",
                    ),
                    TaskCatalogEntry(
                        id="T",
                        project_number="P-573E",
                        task_code="216",
                        label="Programmation",
                        account_group="DEPMO",
                        active=True,
                        workforce_eligible=True,
                        budget_hours=Decimal("8"),
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    RequestLine(
                        id="L",
                        workforce_request_id="D",
                        position=0,
                        kind="WORKFORCE",
                        slot_count=1,
                        active=True,
                    ),
                    ResourceRequirement(
                        id="REQ",
                        project_id="P",
                        assigned_resource_id="R",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        origin="AD_HOC",
                        approved_contact_context_status="NOT_APPLICABLE",
                        approval_reference_status="NOT_APPLICABLE",
                    ),
                ]
            )
            session.flush()
            session.add(
                Shift(
                    id="S",
                    resource_requirement_id="REQ",
                    resource_id="R",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def test_a_b_c_scenario_uses_canonical_a_and_explicit_c_never_legacy_b(self) -> None:
        with self.factory() as session:
            managers = ProjectManagerResolutionService(
                SqlProjectManagerResolutionRepository(session)
            ).resolve_project("P")
            assert managers.primary is not None
            self.assertEqual(managers.primary.business_contact_id, "C-A")
            self.assertEqual(
                tuple(row.business_contact_id for row in managers.co_managers),
                ("C-C",),
            )
            self.assertNotEqual(
                managers.primary.business_contact_id,
                "C-B",
            )

            scope = SqlUserViewContextRepository(session)
            self.assertEqual(
                scope.list_managed_project_ids("U-A", "EMP-A"),
                ("P",),
            )
            self.assertEqual(
                scope.list_managed_project_ids("U-C", None),
                ("P",),
            )
            self.assertEqual(
                scope.list_managed_project_ids("U-B", None),
                (),
            )

            operational = OperationalContactService(
                SqlOperationalContactRepository(session)
            )
            responsible = operational.resolve_request_line("L")
            self.assertEqual(
                responsible.operational_responsible.contact_id,
                "C-A",
            )

            assignments = SqlProjectCommunicationRepository(
                session,
                operational_contacts=operational,
            ).list_assignments(
                week_start=DAY,
                week_end=DAY,
            )
            self.assertEqual(len(assignments), 1)
            self.assertEqual(
                assignments[0].project_manager.contact_id,
                "C-A",
            )
            self.assertEqual(
                assignments[0].project_manager.email,
                "configured-email-a",
            )

            medium_term = SqlPlannerQueryRepositoryWeb(
                session
            ).medium_term_budget_projection(
                project_number="P-573E",
            )
            assert medium_term is not None
            self.assertEqual(len(medium_term.tasks), 1)
            task = medium_term.tasks[0]
            self.assertEqual(task.manager_group_key, "erp:EMP-A")
            self.assertEqual(task.project_manager_contact_id, "C-A")
            self.assertEqual(task.manager_display_name, "Principal A")
            self.assertNotEqual(task.project_manager_contact_id, "C-B")


if __name__ == "__main__":
    unittest.main()
