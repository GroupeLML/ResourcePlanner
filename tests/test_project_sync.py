from __future__ import annotations

import unittest

from sqlalchemy import select

from app.application.errors import ApplicationConflictError
from app.application.project_sync import ExternalProjectRecord, ProjectSyncService
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    ErpUserDirectoryEntry,
    Project,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.project_sync_repository import SqlProjectSyncRepository


class StubProjectSource:
    def __init__(self, rows: list[ExternalProjectRecord]) -> None:
        self.rows = rows

    def list_projects(self) -> tuple[ExternalProjectRecord, ...]:
        return tuple(self.rows)


class ProjectSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync(self, source: StubProjectSource):
        with transactional_session(self.factory) as session:
            return ProjectSyncService(
                source,
                SqlProjectSyncRepository(session),
            ).synchronize()

    def _seed_app_user(
        self,
        *,
        employee_external_id: str,
        contact_id: str,
        display_name: str = "Chargé lié",
        email: str | None = None,
    ) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                BusinessContact(
                    id=contact_id,
                    display_name=display_name,
                    email=email,
                    source="APP_USER",
                    external_system="RESOURCEPLANNER",
                    external_entity="EMPLOYEE",
                    external_id=employee_external_id,
                )
            )
            session.flush()
            session.add(
                AppUser(
                    issuer=f"urn:test:{employee_external_id}",
                    subject=f"subject:{employee_external_id}",
                    display_name=display_name,
                    email=email,
                    employee_external_id=employee_external_id,
                    business_contact_id=contact_id,
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                )
            )

    def test_sync_is_idempotent_and_updates_erp_fields(self) -> None:
        source = StubProjectSource(
            [
                ExternalProjectRecord(
                    external_id="ERP-1",
                    number="P-100",
                    name="Projet initial",
                    client="Client A",
                    project_manager_external_id="USR-1",
                    project_manager_name="Alice",
                    status="Active",
                )
            ]
        )

        first = self._sync(source)
        second = self._sync(source)
        self.assertEqual((first.created, first.updated, first.unchanged), (1, 0, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 1))

        source.rows[0] = ExternalProjectRecord(
            external_id="ERP-1",
            number="P-100",
            name="Projet renommé",
            client="Client B",
            project_manager_external_id="USR-2",
            project_manager_name="Bob",
            status="Completed",
        )
        third = self._sync(source)
        self.assertEqual((third.created, third.updated, third.unchanged), (0, 1, 0))

        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-100"))
            assert row is not None
            self.assertEqual(row.erp_external_id, "ERP-1")
            self.assertEqual(row.name, "Projet renommé")
            self.assertEqual(row.client, "Client B")
            self.assertEqual(row.project_manager_name, "Bob")
            self.assertEqual(row.status, "Completed")

    def test_legacy_project_is_adopted_by_number_without_duplication(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="LOCAL-1",
                    number="P-200",
                    name="Projet local historique",
                    status="Actif",
                )
            )

        result = self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-200",
                        number="P-200",
                        name="Projet ERP",
                        client="Client ERP",
                    )
                ]
            )
        )
        self.assertEqual(result.updated, 1)

        with self.factory() as session:
            rows = session.scalars(select(Project)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, "LOCAL-1")
            self.assertEqual(rows[0].erp_external_id, "ERP-200")
            self.assertEqual(rows[0].name, "Projet ERP")

    def test_existing_external_identity_is_never_reassigned_silently(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="LOCAL-2",
                    erp_external_id="ERP-OLD",
                    number="P-300",
                    name="Projet lié",
                    status="Actif",
                )
            )

        with self.assertRaises(ApplicationConflictError) as raised:
            self._sync(
                StubProjectSource(
                    [
                        ExternalProjectRecord(
                            external_id="ERP-NEW",
                            number="P-300",
                            name="Projet entrant",
                        )
                    ]
                )
            )
        self.assertEqual(raised.exception.code, "project_sync_external_id_conflict")

    def test_incomplete_import_preserves_local_project_contact_and_existing_manager_identity(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                BusinessContact(
                    id="BC-PM",
                    display_name="Chargé local",
                    phone="555-0300",
                )
            )
            session.flush()
            session.add(
                Project(
                    id="P-LOCAL",
                    erp_external_id="ERP-PM",
                    number="P-PM",
                    name="Projet PM",
                    project_manager_external_id="EMP-PM",
                    project_manager_name="Chargé existant",
                    project_manager_contact_id="BC-PM",
                    status="Active",
                )
            )

        result = self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-PM",
                        number="P-PM",
                        name="Projet PM renommé",
                        client="Client",
                        project_manager_external_id=None,
                        project_manager_name=None,
                        status="Active",
                    )
                ]
            )
        )
        self.assertEqual(result.updated, 1)

        with self.factory() as session:
            row = session.get(Project, "P-LOCAL")
            assert row is not None
            self.assertEqual(row.project_manager_external_id, "EMP-PM")
            self.assertEqual(row.project_manager_name, "Chargé existant")
            self.assertEqual(row.project_manager_contact_id, "BC-PM")


    def test_padded_project_manager_employee_id_links_business_contact(self) -> None:
        self._seed_app_user(
            employee_external_id="TROTJCHA",
            contact_id="BC-TROTJCHA",
        )
        source = StubProjectSource(
            [
                ExternalProjectRecord(
                    external_id="ERP-TROT",
                    number="P-TROT",
                    name="Projet Trot",
                    project_manager_external_id="  TROTJCHA  ",
                    project_manager_name="Jean-Charles",
                    status="Actif",
                )
            ]
        )

        first = self._sync(source)
        second = self._sync(source)

        self.assertEqual((first.created, first.updated, first.unchanged), (1, 0, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 1))
        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-TROT"))
            assert row is not None
            self.assertEqual(row.project_manager_external_id, "TROTJCHA")
            self.assertEqual(row.project_manager_contact_id, "BC-TROTJCHA")
            self.assertEqual(len(session.scalars(select(BusinessContact)).all()), 1)
            self.assertEqual(len(session.scalars(select(AppUser)).all()), 1)

    def test_unresolved_project_manager_preserves_erp_identity_without_contact(self) -> None:
        self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-UNRESOLVED",
                        number="P-UNRESOLVED",
                        name="Projet sans AppUser",
                        project_manager_external_id="  EMP-NOT-LINKED  ",
                        project_manager_name="Nom descriptif ERP",
                    )
                ]
            )
        )

        with self.factory() as session:
            row = session.scalar(
                select(Project).where(Project.number == "P-UNRESOLVED")
            )
            assert row is not None
            self.assertEqual(row.project_manager_external_id, "EMP-NOT-LINKED")
            self.assertEqual(row.project_manager_name, "Nom descriptif ERP")
            self.assertIsNone(row.project_manager_contact_id)
            self.assertEqual(session.scalars(select(BusinessContact)).all(), [])

    def test_matching_display_name_with_different_employee_id_does_not_link(self) -> None:
        self._seed_app_user(
            employee_external_id="EMP-OTHER",
            contact_id="BC-OTHER",
            display_name="Même nom",
            email="same" + chr(64) + "example.invalid",
        )
        self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-NAME",
                        number="P-NAME",
                        name="Projet nom",
                        project_manager_external_id="EMP-EXPECTED",
                        project_manager_name="Même nom",
                    )
                ]
            )
        )

        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-NAME"))
            assert row is not None
            self.assertEqual(row.project_manager_external_id, "EMP-EXPECTED")
            self.assertIsNone(row.project_manager_contact_id)

    def test_rp_user_user_id_is_not_project_manager_employee_identity(self) -> None:
        self._seed_app_user(
            employee_external_id="EMP-OTHER",
            contact_id="BC-USERID",
        )
        with transactional_session(self.factory) as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="TROTJCHA",
                    employee_external_id="EMP-OTHER",
                    display_name="Compte ERP descriptif",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json='["PROJECT_MANAGER"]',
                )
            )

        self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-USERID",
                        number="P-USERID",
                        name="Projet UserID distinct",
                        project_manager_external_id="TROTJCHA",
                    )
                ]
            )
        )

        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-USERID"))
            assert row is not None
            self.assertIsNone(row.project_manager_contact_id)

    def test_multiple_rp_users_for_employee_do_not_choose_by_user_id(self) -> None:
        self._seed_app_user(
            employee_external_id="TROTJCHA",
            contact_id="BC-MULTI",
        )
        with transactional_session(self.factory) as session:
            for user_id in ("JCTROTTIER", "JCTROTTIER-ALT"):
                session.add(
                    ErpUserDirectoryEntry(
                        user_id=user_id,
                        employee_external_id="TROTJCHA",
                        display_name=f"Compte {user_id}",
                        erp_user_active=True,
                        employee_status="Actif",
                        local_active=False,
                        roles_json="[]",
                    )
                )

        result = self._sync(
            StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-MULTI",
                        number="P-MULTI",
                        name="Projet multi comptes",
                        project_manager_external_id="TROTJCHA",
                    )
                ]
            )
        )
        self.assertEqual(result.created, 1)
        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-MULTI"))
            assert row is not None
            self.assertEqual(row.project_manager_contact_id, "BC-MULTI")

    def test_manager_change_to_unlinked_employee_clears_stale_contact(self) -> None:
        self._seed_app_user(
            employee_external_id="TROTJCHA",
            contact_id="BC-OLD",
        )
        source = StubProjectSource(
            [
                ExternalProjectRecord(
                    external_id="ERP-CHANGE",
                    number="P-CHANGE",
                    name="Projet changement",
                    project_manager_external_id="TROTJCHA",
                )
            ]
        )
        self._sync(source)
        source.rows[0] = ExternalProjectRecord(
            external_id="ERP-CHANGE",
            number="P-CHANGE",
            name="Projet changement",
            project_manager_external_id="EMP-UNLINKED",
            project_manager_name="Nouveau chargé",
        )

        changed = self._sync(source)

        self.assertEqual(changed.updated, 1)
        with self.factory() as session:
            row = session.scalar(select(Project).where(Project.number == "P-CHANGE"))
            assert row is not None
            self.assertEqual(row.project_manager_external_id, "EMP-UNLINKED")
            self.assertEqual(row.project_manager_name, "Nouveau chargé")
            self.assertIsNone(row.project_manager_contact_id)

    def test_missing_project_is_preserved_and_explicit_inactive_status_is_synced(self) -> None:
        source = StubProjectSource(
            [
                ExternalProjectRecord("ERP-A", "P-A", "Projet A"),
                ExternalProjectRecord("ERP-B", "P-B", "Projet B"),
            ]
        )
        self._sync(source)

        source.rows = [
            ExternalProjectRecord(
                "ERP-A",
                "P-A",
                "Projet A",
                status="Inactive",
            )
        ]
        result = self._sync(source)
        self.assertEqual(result.updated, 1)

        with self.factory() as session:
            rows = {row.number: row for row in session.scalars(select(Project)).all()}
            self.assertEqual(set(rows), {"P-A", "P-B"})
            self.assertEqual(rows["P-A"].status, "Inactive")
            self.assertEqual(rows["P-B"].name, "Projet B")


if __name__ == "__main__":
    unittest.main()
