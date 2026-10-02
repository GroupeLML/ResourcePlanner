from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event, select
from sqlalchemy.dialects import mssql

from app.application.project_managers import (
    DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_INACTIVE,
    DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED,
    DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE,
    DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED,
    DIAGNOSTIC_ERP_PROJECT_MANAGER_MISSING,
    DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_INACTIVE,
    DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_NOT_LINKED,
    DIAGNOSTIC_PROJECT_CO_MANAGER_CONTACT_INACTIVE,
    DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE,
    DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,
    PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT,
    PROJECT_MANAGER_RESOLUTION_RESOLVED,
    PROJECT_MANAGER_RESOLUTION_UNRESOLVED_CONTACT,
    PROJECT_MANAGER_RESOLUTION_UNRESOLVED_USER,
    PROJECT_MANAGER_SOURCE_ERP,
    PROJECT_MANAGER_SOURCE_RP,
    ProjectManagerCoManagerRecord,
    ProjectManagerContactRecord,
    ProjectManagerProjectRecord,
    ProjectManagerResolutionService,
    ProjectManagerUserRecord,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    Resource,
    ResourceRequirement,
    SqlProjectManagerResolutionRepository,
    create_session_factory,
    create_sql_engine,
    project_managed_by_user_predicate,
)


DAY = date(2026, 10, 2)


class _FakeResolutionRepository:
    def __init__(
        self,
        *,
        projects=(),
        co_managers=(),
        users_by_employee=(),
        users_by_contact=(),
        contacts=(),
    ) -> None:
        self.projects = tuple(projects)
        self.co_managers = tuple(co_managers)
        self.users_by_employee = tuple(users_by_employee)
        self.users_by_contact = tuple(users_by_contact)
        self.contacts = tuple(contacts)

    def list_projects(self, project_ids):
        wanted = set(project_ids)
        return tuple(row for row in self.projects if row.project_id in wanted)

    def list_co_managers(self, project_ids):
        wanted = set(project_ids)
        return tuple(row for row in self.co_managers if row.project_id in wanted)

    def list_users_by_employee_external_ids(self, employee_external_ids):
        wanted = set(employee_external_ids)
        return tuple(
            row
            for row in self.users_by_employee
            if row.employee_external_id in wanted
        )

    def list_users_by_business_contact_ids(self, business_contact_ids):
        wanted = set(business_contact_ids)
        return tuple(
            row
            for row in self.users_by_contact
            if row.business_contact_id in wanted
        )

    def list_contacts(self, business_contact_ids):
        wanted = set(business_contact_ids)
        return tuple(
            row
            for row in self.contacts
            if row.business_contact_id in wanted
        )


class ProjectManagerResolutionPureTests(unittest.TestCase):
    def test_ambiguous_primary_fails_closed_without_arbitrary_user(self) -> None:
        repository = _FakeResolutionRepository(
            projects=(
                ProjectManagerProjectRecord(
                    project_id="P-CONFLICT",
                    co_managers_version=1,
                    project_manager_external_id="EMP-X",
                    project_manager_name="ERP X",
                ),
            ),
            users_by_employee=(
                ProjectManagerUserRecord(
                    app_user_id="U-X1",
                    employee_external_id="EMP-X",
                    business_contact_id="C-X1",
                    active=True,
                ),
                ProjectManagerUserRecord(
                    app_user_id="U-X2",
                    employee_external_id="EMP-X",
                    business_contact_id="C-X2",
                    active=True,
                ),
            ),
        )

        resolved = ProjectManagerResolutionService(repository).resolve_project(
            "P-CONFLICT"
        )

        assert resolved.primary is not None
        self.assertEqual(
            resolved.primary.resolution_status,
            PROJECT_MANAGER_RESOLUTION_IDENTITY_CONFLICT,
        )
        self.assertIsNone(resolved.primary.app_user_id)
        self.assertIsNone(resolved.primary.business_contact_id)
        self.assertIn(
            DIAGNOSTIC_PROJECT_MANAGER_IDENTITY_CONFLICT,
            resolved.primary.diagnostics,
        )

    def test_broken_references_are_explicit_and_never_name_resolved(self) -> None:
        repository = _FakeResolutionRepository(
            projects=(
                ProjectManagerProjectRecord(
                    project_id="P-BROKEN",
                    co_managers_version=4,
                    project_manager_external_id="EMP-BROKEN",
                    project_manager_name="Libellé ERP",
                ),
            ),
            co_managers=(
                ProjectManagerCoManagerRecord(
                    project_id="P-BROKEN",
                    business_contact_id="C-BROKEN-CO",
                ),
            ),
            users_by_employee=(
                ProjectManagerUserRecord(
                    app_user_id="U-BROKEN",
                    employee_external_id="EMP-BROKEN",
                    business_contact_id="C-BROKEN-PRIMARY",
                    active=True,
                ),
            ),
        )

        resolved = ProjectManagerResolutionService(repository).resolve_project(
            "P-BROKEN"
        )

        assert resolved.primary is not None
        self.assertEqual(resolved.primary.display_name, "Libellé ERP")
        self.assertIn(
            DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE,
            resolved.primary.diagnostics,
        )
        self.assertEqual(len(resolved.co_managers), 1)
        self.assertEqual(resolved.co_managers[0].display_name, "C-BROKEN-CO")
        self.assertIn(
            DIAGNOSTIC_PROJECT_MANAGER_BROKEN_REFERENCE,
            resolved.co_managers[0].diagnostics,
        )


class ProjectManagerResolutionSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        database_path = Path(self._temp.name) / "project-managers.db"
        self.database_url = f"sqlite:///{database_path.as_posix()}"
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(
                AppUser(
                    id="U-ACTOR",
                    display_name="Admin",
                    email=None,
                    roles_json='["ADMIN"]',
                    active=True,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp.cleanup()

    @staticmethod
    def _contact(
        contact_id: str,
        name: str,
        *,
        active: bool = True,
    ) -> BusinessContact:
        return BusinessContact(
            id=contact_id,
            display_name=name,
            active=active,
        )

    @staticmethod
    def _user(
        user_id: str,
        *,
        employee_external_id: str | None = None,
        business_contact_id: str | None = None,
        active: bool = True,
        roles_json: str = '["PROJECT_MANAGER"]',
    ) -> AppUser:
        return AppUser(
            id=user_id,
            display_name=user_id,
            email=None,
            employee_external_id=employee_external_id,
            business_contact_id=business_contact_id,
            roles_json=roles_json,
            active=active,
        )

    def _service(self, session) -> ProjectManagerResolutionService:
        return ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(session)
        )

    def test_primary_resolution_states_and_resource_independence(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    self._contact("C-1", "Contact One"),
                    self._contact("C-4", "Contact Four"),
                    self._contact("C-5", "Contact Five", active=False),
                    self._user(
                        "U-1",
                        employee_external_id="EMP-1",
                        business_contact_id="C-1",
                    ),
                    self._user(
                        "U-3",
                        employee_external_id="EMP-3",
                        business_contact_id=None,
                    ),
                    self._user(
                        "U-4",
                        employee_external_id="EMP-4",
                        business_contact_id="C-4",
                        active=False,
                    ),
                    self._user(
                        "U-5",
                        employee_external_id="EMP-5",
                        business_contact_id="C-5",
                    ),
                    Project(
                        id="P-1",
                        number="P-1",
                        name="P1",
                        project_manager_external_id="EMP-1",
                        project_manager_name="ERP One",
                    ),
                    Project(
                        id="P-2",
                        number="P-2",
                        name="P2",
                        project_manager_external_id="EMP-2",
                        project_manager_name="ERP Two",
                    ),
                    Project(
                        id="P-3",
                        number="P-3",
                        name="P3",
                        project_manager_external_id="EMP-3",
                        project_manager_name="ERP Three",
                    ),
                    Project(
                        id="P-4",
                        number="P-4",
                        name="P4",
                        project_manager_external_id="EMP-4",
                        project_manager_name="ERP Four",
                    ),
                    Project(
                        id="P-5",
                        number="P-5",
                        name="P5",
                        project_manager_external_id="EMP-5",
                        project_manager_name="ERP Five",
                    ),
                    Project(
                        id="P-6",
                        number="P-6",
                        name="P6",
                        project_manager_external_id=None,
                        project_manager_name="Nom seul ignoré",
                    ),
                ]
            )

        with self.factory() as session:
            resolved = self._service(session).resolve_projects(
                ["P-1", "P-2", "P-3", "P-4", "P-5", "P-6"]
            )
            self.assertEqual(tuple(session.scalars(select(Resource.id)).all()), ())

        primary = resolved["P-1"].primary
        assert primary is not None
        self.assertEqual(primary.sources, (PROJECT_MANAGER_SOURCE_ERP,))
        self.assertEqual(primary.display_name, "Contact One")
        self.assertEqual(primary.resolution_status, PROJECT_MANAGER_RESOLUTION_RESOLVED)
        self.assertTrue(primary.user_active)
        self.assertTrue(primary.contact_active)

        primary = resolved["P-2"].primary
        assert primary is not None
        self.assertEqual(
            primary.resolution_status,
            PROJECT_MANAGER_RESOLUTION_UNRESOLVED_USER,
        )
        self.assertIn(
            DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED,
            primary.diagnostics,
        )

        primary = resolved["P-3"].primary
        assert primary is not None
        self.assertEqual(
            primary.resolution_status,
            PROJECT_MANAGER_RESOLUTION_UNRESOLVED_CONTACT,
        )
        self.assertIn(
            DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED,
            primary.diagnostics,
        )

        primary = resolved["P-4"].primary
        assert primary is not None
        self.assertEqual(primary.resolution_status, PROJECT_MANAGER_RESOLUTION_RESOLVED)
        self.assertFalse(primary.user_active)
        self.assertIn(
            DIAGNOSTIC_ERP_PROJECT_MANAGER_APP_USER_INACTIVE,
            primary.diagnostics,
        )

        primary = resolved["P-5"].primary
        assert primary is not None
        self.assertEqual(primary.resolution_status, PROJECT_MANAGER_RESOLUTION_RESOLVED)
        self.assertFalse(primary.contact_active)
        self.assertEqual(primary.display_name, "ERP Five")
        self.assertIn(
            DIAGNOSTIC_ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE,
            primary.diagnostics,
        )

        self.assertIsNone(resolved["P-6"].primary)
        self.assertIn(
            DIAGNOSTIC_ERP_PROJECT_MANAGER_MISSING,
            resolved["P-6"].diagnostics,
        )

    def test_co_managers_remain_visible_without_app_user_or_when_inactive(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    self._contact("C-A", "Co A"),
                    self._contact("C-B", "Co B"),
                    self._contact("C-C", "Co C", active=False),
                    self._user(
                        "U-A",
                        business_contact_id="C-A",
                        employee_external_id=None,
                    ),
                    self._user(
                        "U-C",
                        business_contact_id="C-C",
                        employee_external_id=None,
                        active=False,
                    ),
                    Project(id="P-A", number="P-A", name="A"),
                    Project(id="P-B", number="P-B", name="B"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ProjectCoManager(
                        project_id="P-A",
                        business_contact_id="C-A",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-A",
                        business_contact_id="C-B",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-A",
                        business_contact_id="C-C",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-B",
                        business_contact_id="C-A",
                        created_by_user_id="U-ACTOR",
                    ),
                ]
            )

        with self.factory() as session:
            resolved = self._service(session).resolve_projects(["P-A", "P-B"])

        by_contact = {
            manager.business_contact_id: manager
            for manager in resolved["P-A"].co_managers
        }
        self.assertEqual(set(by_contact), {"C-A", "C-B", "C-C"})
        self.assertEqual(by_contact["C-A"].app_user_id, "U-A")
        self.assertIsNone(by_contact["C-A"].employee_external_id)
        self.assertIsNone(by_contact["C-B"].app_user_id)
        self.assertIn(
            DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_NOT_LINKED,
            by_contact["C-B"].diagnostics,
        )
        self.assertFalse(by_contact["C-C"].contact_active)
        self.assertFalse(by_contact["C-C"].user_active)
        self.assertIn(
            DIAGNOSTIC_PROJECT_CO_MANAGER_CONTACT_INACTIVE,
            by_contact["C-C"].diagnostics,
        )
        self.assertIn(
            DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_INACTIVE,
            by_contact["C-C"].diagnostics,
        )
        self.assertEqual(
            resolved["P-B"].co_managers[0].business_contact_id,
            "C-A",
        )

    def test_primary_and_persisted_co_manager_deduplicate_with_double_provenance(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    self._contact("C-D", "Double"),
                    self._user(
                        "U-D",
                        employee_external_id="EMP-D",
                        business_contact_id="C-D",
                    ),
                    Project(
                        id="P-D",
                        number="P-D",
                        name="Double",
                        project_manager_external_id="EMP-D",
                        project_manager_name="ERP Double",
                        co_managers_version=7,
                    ),
                ]
            )
            session.flush()
            session.add(
                ProjectCoManager(
                    project_id="P-D",
                    business_contact_id="C-D",
                    created_by_user_id="U-ACTOR",
                )
            )

        with self.factory() as session:
            resolved = self._service(session).resolve_project("P-D")

        assert resolved.primary is not None
        self.assertEqual(
            resolved.primary.sources,
            (PROJECT_MANAGER_SOURCE_ERP, PROJECT_MANAGER_SOURCE_RP),
        )
        self.assertEqual(resolved.primary.business_contact_id, "C-D")
        self.assertEqual(resolved.co_managers, ())
        self.assertEqual(resolved.co_managers_version, 7)

    def test_batch_resolution_uses_fixed_query_families_not_queries_per_project(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    self._contact("C-P1", "Primary 1"),
                    self._contact("C-P3", "Co 3"),
                    self._contact("C-P1-A", "Co 1A"),
                    self._contact("C-P1-B", "Co 1B"),
                    self._user(
                        "U-P1",
                        employee_external_id="EMP-P1",
                        business_contact_id="C-P1",
                    ),
                    self._user(
                        "U-P1-A",
                        business_contact_id="C-P1-A",
                    ),
                    self._user(
                        "U-P1-B",
                        business_contact_id="C-P1-B",
                    ),
                    self._user(
                        "U-P3",
                        business_contact_id="C-P3",
                    ),
                    Project(
                        id="P-B1",
                        number="P-B1",
                        name="Batch 1",
                        project_manager_external_id="EMP-P1",
                        project_manager_name="Principal 1",
                    ),
                    Project(
                        id="P-B2",
                        number="P-B2",
                        name="Batch 2",
                        project_manager_external_id="EMP-MISSING",
                        project_manager_name="Principal manquant",
                    ),
                    Project(
                        id="P-B3",
                        number="P-B3",
                        name="Batch 3",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ProjectCoManager(
                        project_id="P-B1",
                        business_contact_id="C-P1-A",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-B1",
                        business_contact_id="C-P1-B",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-B3",
                        business_contact_id="C-P3",
                        created_by_user_id="U-ACTOR",
                    ),
                ]
            )

        statements: list[str] = []

        def record_select(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_select)
        try:
            with self.factory() as session:
                resolved = self._service(session).resolve_projects(
                    ["P-B1", "P-B2", "P-B3"]
                )
        finally:
            event.remove(self.engine, "before_cursor_execute", record_select)

        self.assertEqual(set(resolved), {"P-B1", "P-B2", "P-B3"})
        self.assertEqual(len(resolved["P-B1"].co_managers), 2)
        assert resolved["P-B2"].primary is not None
        self.assertEqual(
            resolved["P-B2"].primary.resolution_status,
            PROJECT_MANAGER_RESOLUTION_UNRESOLVED_USER,
        )
        self.assertIsNone(resolved["P-B3"].primary)
        self.assertEqual(len(resolved["P-B3"].co_managers), 1)
        self.assertEqual(
            len(statements),
            5,
            "Expected one query family each for projects, co-managers, "
            "users-by-employee, users-by-contact and contacts.",
        )

    def test_scope_predicate_matches_projection_and_ignores_workforce_and_rbac(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    self._contact("C-M", "Manager"),
                    self._contact("C-CO", "Co no employee"),
                    self._contact("C-NOUSER", "No user"),
                    self._user(
                        "U-M",
                        employee_external_id="EMP-M",
                        business_contact_id="C-M",
                        roles_json='["TECHNICIAN"]',
                    ),
                    self._user(
                        "U-CO",
                        employee_external_id=None,
                        business_contact_id="C-CO",
                        roles_json='[]',
                    ),
                    Project(
                        id="P-PRIMARY",
                        number="P-PRIMARY",
                        name="Primary",
                        project_manager_external_id="EMP-M",
                    ),
                    Project(id="P-CO", number="P-CO", name="Co"),
                    Project(
                        id="P-CO-NOEMP",
                        number="P-CO-NOEMP",
                        name="Co no employee",
                    ),
                    Project(id="P-WORK", number="P-WORK", name="Workforce only"),
                    Project(id="P-NOUSER", number="P-NOUSER", name="No user co"),
                    Project(id="P-OTHER", number="P-OTHER", name="Other"),
                    Resource(
                        id="R-M",
                        external_id="EMP-M",
                        name="Resource manager",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ProjectCoManager(
                        project_id="P-CO",
                        business_contact_id="C-M",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-CO-NOEMP",
                        business_contact_id="C-CO",
                        created_by_user_id="U-ACTOR",
                    ),
                    ProjectCoManager(
                        project_id="P-NOUSER",
                        business_contact_id="C-NOUSER",
                        created_by_user_id="U-ACTOR",
                    ),
                    ResourceRequirement(
                        id="REQ-WORK",
                        project_id="P-WORK",
                        assigned_resource_id="R-M",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        origin="AD_HOC",
                    ),
                ]
            )

        all_project_ids = (
            "P-PRIMARY",
            "P-CO",
            "P-CO-NOEMP",
            "P-WORK",
            "P-NOUSER",
            "P-OTHER",
        )
        with self.factory() as session:
            u_m = session.get(AppUser, "U-M")
            u_co = session.get(AppUser, "U-CO")
            assert u_m is not None
            assert u_co is not None

            sql_for_manager = set(
                session.scalars(
                    select(Project.id).where(
                        project_managed_by_user_predicate(
                            employee_external_id=u_m.employee_external_id,
                            business_contact_id=u_m.business_contact_id,
                        )
                    )
                ).all()
            )
            sql_for_co = set(
                session.scalars(
                    select(Project.id).where(
                        project_managed_by_user_predicate(
                            employee_external_id=u_co.employee_external_id,
                            business_contact_id=u_co.business_contact_id,
                        )
                    )
                ).all()
            )
            sql_for_unlinked = set(
                session.scalars(
                    select(Project.id).where(
                        project_managed_by_user_predicate(
                            employee_external_id=None,
                            business_contact_id=None,
                        )
                    )
                ).all()
            )
            projection = self._service(session).resolve_projects(
                list(all_project_ids)
            )

        projected_for_manager = {
            project_id
            for project_id, managers in projection.items()
            if (
                managers.primary is not None
                and managers.primary.app_user_id == "U-M"
            )
            or any(
                co_manager.app_user_id == "U-M"
                for co_manager in managers.co_managers
            )
        }
        projected_for_co = {
            project_id
            for project_id, managers in projection.items()
            if (
                managers.primary is not None
                and managers.primary.app_user_id == "U-CO"
            )
            or any(
                co_manager.app_user_id == "U-CO"
                for co_manager in managers.co_managers
            )
        }

        self.assertEqual(sql_for_manager, {"P-PRIMARY", "P-CO"})
        self.assertNotIn("P-WORK", sql_for_manager)
        self.assertEqual(sql_for_co, {"P-CO-NOEMP"})
        self.assertEqual(sql_for_unlinked, set())
        self.assertEqual(projected_for_manager, sql_for_manager)
        self.assertEqual(projected_for_co, sql_for_co)

        no_user_co = next(
            manager
            for manager in projection["P-NOUSER"].co_managers
            if manager.business_contact_id == "C-NOUSER"
        )
        self.assertIsNone(no_user_co.app_user_id)
        self.assertIn(
            DIAGNOSTIC_PROJECT_CO_MANAGER_APP_USER_NOT_LINKED,
            no_user_co.diagnostics,
        )

    def test_scope_predicate_compiles_for_sql_server_with_exists_and_no_legacy_fk(self) -> None:
        statement = select(Project.id).where(
            project_managed_by_user_predicate(
                employee_external_id="EMP-M",
                business_contact_id="C-M",
            )
        )
        sql = str(
            statement.compile(
                dialect=mssql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).upper()

        self.assertIn("PROJECT_MANAGER_EXTERNAL_ID", sql)
        self.assertIn("EXISTS", sql)
        self.assertIn("PROJECT_CO_MANAGERS", sql)
        self.assertNotIn("PROJECT_MANAGER_CONTACT_ID", sql)


if __name__ == "__main__":
    unittest.main()
