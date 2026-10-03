from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from sqlalchemy import delete, event, func, select

from app.application.project_manager_legacy_diagnostic import (
    CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE,
    LEGACY_ABSENT,
    LEGACY_DIVERGES_FROM_CANONICAL,
    LEGACY_MATCHES_CANONICAL,
    LEGACY_REFERENCE_BROKEN,
    LegacyProjectManagerContactRecord,
    LegacyProjectManagerDiagnosticService,
    LegacyProjectManagerProjectRecord,
)
from app.application.project_managers import (
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
    ProjectManagerAudit,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.project_manager_legacy_diagnostic_repository import (
    SqlLegacyProjectManagerDiagnosticRepository,
)
from app.infrastructure.sql.project_manager_resolution_repository import (
    SqlProjectManagerResolutionRepository,
)


class _LegacyRepository:
    def __init__(self, projects=(), contacts=()) -> None:
        self.projects = tuple(projects)
        self.contacts = tuple(contacts)

    def list_projects(self):
        return self.projects

    def list_contacts(self, business_contact_ids):
        wanted = set(business_contact_ids)
        return tuple(
            row
            for row in self.contacts
            if row.business_contact_id in wanted
        )


class _ResolutionRepository:
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


class ProjectManagerLegacyDiagnosticPureTests(unittest.TestCase):
    def test_classifies_history_without_promoting_legacy_identity(self) -> None:
        projects = tuple(
            LegacyProjectManagerProjectRecord(
                project_id=project_id,
                project_number=project_id,
                legacy_project_manager_contact_id=legacy_id,
            )
            for project_id, legacy_id in (
                ("P-NULL", None),
                ("P-MATCH", "C-A"),
                ("P-DIVERGE", "C-B"),
                ("P-UNRESOLVED", "C-B"),
                ("P-BROKEN", "C-MISSING"),
                ("P-INACTIVE", "C-INACTIVE"),
            )
        )
        canonical_projects = tuple(
            ProjectManagerProjectRecord(
                project_id=project.project_id,
                co_managers_version=1,
                project_manager_external_id=(
                    "EMP-U"
                    if project.project_id == "P-UNRESOLVED"
                    else "EMP-A"
                ),
                project_manager_name="Principal ERP",
            )
            for project in projects
        )
        resolution = _ResolutionRepository(
            projects=canonical_projects,
            users_by_employee=(
                ProjectManagerUserRecord(
                    app_user_id="U-A",
                    employee_external_id="EMP-A",
                    business_contact_id="C-A",
                    active=True,
                ),
            ),
            contacts=(
                ProjectManagerContactRecord(
                    business_contact_id="C-A",
                    display_name="Principal A",
                    active=True,
                ),
            ),
        )
        legacy = _LegacyRepository(
            projects=projects,
            contacts=(
                LegacyProjectManagerContactRecord("C-A", "Principal A", True),
                LegacyProjectManagerContactRecord("C-B", "Historique B", True),
                LegacyProjectManagerContactRecord(
                    "C-INACTIVE",
                    "Historique inactif",
                    False,
                ),
            ),
        )

        report = LegacyProjectManagerDiagnosticService(
            legacy,
            ProjectManagerResolutionService(resolution),
        ).inspect()
        by_project = {row.project_id: row for row in report.projects}

        self.assertEqual(by_project["P-NULL"].classification, LEGACY_ABSENT)
        self.assertEqual(
            by_project["P-MATCH"].classification,
            LEGACY_MATCHES_CANONICAL,
        )
        self.assertEqual(
            by_project["P-DIVERGE"].classification,
            LEGACY_DIVERGES_FROM_CANONICAL,
        )
        self.assertEqual(
            by_project["P-UNRESOLVED"].classification,
            CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE,
        )
        self.assertEqual(
            by_project["P-BROKEN"].classification,
            LEGACY_REFERENCE_BROKEN,
        )
        self.assertEqual(
            by_project["P-INACTIVE"].classification,
            LEGACY_DIVERGES_FROM_CANONICAL,
        )
        self.assertEqual(report.summary.projects_scanned, 6)
        self.assertEqual(report.summary.legacy_null, 1)
        self.assertEqual(report.summary.legacy_matching, 1)
        self.assertEqual(report.summary.legacy_divergent, 2)
        self.assertEqual(report.summary.legacy_broken, 1)
        self.assertEqual(report.summary.canonical_unresolved, 1)
        self.assertEqual(report.summary.legacy_inactive, 1)
        self.assertTrue(report.review_required)
        self.assertEqual(
            by_project["P-UNRESOLVED"].canonical_primary.employee_external_id,
            "EMP-U",
        )
        self.assertNotEqual(
            by_project["P-UNRESOLVED"].canonical_primary.business_contact_id,
            "C-B",
        )


class ProjectManagerLegacyDiagnosticSqliteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "legacy-manager.db"
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
                        active=True,
                    ),
                    BusinessContact(
                        id="C-B",
                        display_name="Historique B",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-C",
                        display_name="Co chargé C",
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
                        id="U-C",
                        display_name="Co chargé C",
                        email=None,
                        business_contact_id="C-C",
                        roles_json='["PROJECT_MANAGER"]',
                        active=True,
                    ),
                    Project(
                        id="P",
                        number="P-573E",
                        name="Projet 573E",
                        project_manager_external_id="EMP-A",
                        project_manager_name="Principal ERP A",
                        project_manager_contact_id="C-B",
                    ),
                ]
            )
            session.flush()
            session.add(
                ProjectCoManager(
                    project_id="P",
                    business_contact_id="C-C",
                    created_by_user_id="U-C",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _snapshot(self):
        with self.factory() as session:
            project = session.get(Project, "P")
            assert project is not None
            return {
                "legacy": project.project_manager_contact_id,
                "version": project.co_managers_version,
                "co_managers": tuple(
                    session.execute(
                        select(
                            ProjectCoManager.project_id,
                            ProjectCoManager.business_contact_id,
                        ).order_by(
                            ProjectCoManager.project_id,
                            ProjectCoManager.business_contact_id,
                        )
                    ).all()
                ),
                "audits": int(
                    session.scalar(
                        select(func.count()).select_from(ProjectManagerAudit)
                    )
                    or 0
                ),
            }

    def test_sql_diagnostic_is_read_only_and_batch_bounded(self) -> None:
        before = self._snapshot()
        selects: list[str] = []

        def record_select(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_select)
        try:
            with self.factory() as session:
                report = LegacyProjectManagerDiagnosticService(
                    SqlLegacyProjectManagerDiagnosticRepository(session),
                    ProjectManagerResolutionService(
                        SqlProjectManagerResolutionRepository(session)
                    ),
                ).inspect()
        finally:
            event.remove(self.engine, "before_cursor_execute", record_select)

        after = self._snapshot()
        self.assertEqual(before, after)
        self.assertEqual(len(report.projects), 1)
        row = report.projects[0]
        self.assertEqual(row.classification, LEGACY_DIVERGES_FROM_CANONICAL)
        assert row.canonical_primary is not None
        self.assertEqual(row.canonical_primary.business_contact_id, "C-A")
        self.assertEqual(
            tuple(manager.business_contact_id for manager in (
                ProjectManagerResolutionService(
                    SqlProjectManagerResolutionRepository(
                        self.factory()
                    )
                ).resolve_project("P").co_managers
            )),
            ("C-C",),
        )
        self.assertLessEqual(
            len(selects),
            7,
            "Le diagnostic doit rester borné par familles de requêtes, pas par projet.",
        )


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 573E réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class ProjectManagerLegacyDiagnosticSqlServerTests(unittest.TestCase):
    def test_live_sqlserver_diagnostic_preserves_state(self) -> None:
        marker = uuid4().hex[:10]
        ids = {
            "project": f"573E-P-{marker}",
            "a": f"573E-A-{marker}",
            "b": f"573E-B-{marker}",
            "c": f"573E-C-{marker}",
            "ua": f"573E-UA-{marker}",
            "uc": f"573E-UC-{marker}",
        }
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        BusinessContact(
                            id=ids["a"],
                            display_name="573E principal",
                            active=True,
                        ),
                        BusinessContact(
                            id=ids["b"],
                            display_name="573E historique",
                            active=True,
                        ),
                        BusinessContact(
                            id=ids["c"],
                            display_name="573E co-manager",
                            active=True,
                        ),
                    ]
                )
                session.flush()
                session.add_all(
                    [
                        AppUser(
                            id=ids["ua"],
                            display_name="573E principal",
                            email=None,
                            employee_external_id=f"573E-EMP-{marker}",
                            business_contact_id=ids["a"],
                            roles_json='["PROJECT_MANAGER"]',
                            active=True,
                        ),
                        AppUser(
                            id=ids["uc"],
                            display_name="573E co-manager",
                            email=None,
                            business_contact_id=ids["c"],
                            roles_json='["PROJECT_MANAGER"]',
                            active=True,
                        ),
                        Project(
                            id=ids["project"],
                            number=f"573E-{marker}",
                            name="573E SQL Server live",
                            project_manager_external_id=f"573E-EMP-{marker}",
                            project_manager_name="573E principal ERP",
                            project_manager_contact_id=ids["b"],
                        ),
                    ]
                )
                session.flush()
                session.add(
                    ProjectCoManager(
                        project_id=ids["project"],
                        business_contact_id=ids["c"],
                        created_by_user_id=ids["uc"],
                    )
                )

            with factory() as session:
                project = session.get(Project, ids["project"])
                assert project is not None
                before = (
                    project.project_manager_contact_id,
                    project.co_managers_version,
                    int(
                        session.scalar(
                            select(func.count())
                            .select_from(ProjectManagerAudit)
                            .where(ProjectManagerAudit.project_id == ids["project"])
                        )
                        or 0
                    ),
                )
                report = LegacyProjectManagerDiagnosticService(
                    SqlLegacyProjectManagerDiagnosticRepository(session),
                    ProjectManagerResolutionService(
                        SqlProjectManagerResolutionRepository(session)
                    ),
                ).inspect()
                row = next(
                    item for item in report.projects
                    if item.project_id == ids["project"]
                )
                self.assertEqual(
                    row.classification,
                    LEGACY_DIVERGES_FROM_CANONICAL,
                )

            with factory() as session:
                project = session.get(Project, ids["project"])
                assert project is not None
                after = (
                    project.project_manager_contact_id,
                    project.co_managers_version,
                    int(
                        session.scalar(
                            select(func.count())
                            .select_from(ProjectManagerAudit)
                            .where(ProjectManagerAudit.project_id == ids["project"])
                        )
                        or 0
                    ),
                )
            self.assertEqual(before, after)
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == ids["project"]
                        )
                    )
                    session.execute(
                        delete(ProjectCoManager).where(
                            ProjectCoManager.project_id == ids["project"]
                        )
                    )
                    session.execute(
                        delete(Project).where(Project.id == ids["project"])
                    )
                    session.execute(
                        delete(AppUser).where(
                            AppUser.id.in_((ids["ua"], ids["uc"]))
                        )
                    )
                    session.execute(
                        delete(BusinessContact).where(
                            BusinessContact.id.in_(
                                (ids["a"], ids["b"], ids["c"])
                            )
                        )
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
