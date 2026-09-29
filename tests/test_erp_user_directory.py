from __future__ import annotations

import unittest

from sqlalchemy import select

from app.application.erp_user_directory import (
    ErpUserDirectoryService,
    ErpUserSyncService,
    ExternalErpUserRecord,
)
from app.application.security import ROLE_COORDINATOR, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    Base,
    ErpUserDirectoryEntry,
    Resource,
    SqlErpUserDirectoryRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


class StubUserSource:
    def __init__(self, rows: list[ExternalErpUserRecord]) -> None:
        self.rows = rows

    def list_users(self):
        return tuple(self.rows)


def user(
    user_id: str,
    employee_id: str,
    *,
    display_name: str = "Utilisateur ERP",
    user_active: bool = True,
    employee_status: str = "Actif",
) -> ExternalErpUserRecord:
    return ExternalErpUserRecord(
        user_id=user_id,
        employee_external_id=employee_id,
        display_name=display_name,
        first_name="Utilisateur",
        last_name="ERP",
        email="erp.user" + chr(64) + "example.invalid",
        erp_user_active=user_active,
        employee_status=employee_status,
    )


class ErpUserDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync(self, source: StubUserSource):
        with transactional_session(self.factory) as session:
            return ErpUserSyncService(
                source,
                SqlErpUserDirectoryRepository(session),
            ).synchronize()

    def test_sync_is_idempotent_and_never_creates_or_activates_app_user(self) -> None:
        source = StubUserSource([user("ERP-1", "EMP-1")])

        first = self._sync(source)
        second = self._sync(source)

        self.assertEqual((first.created, first.updated, first.unchanged), (1, 0, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 1))
        with self.factory() as session:
            row = session.get(ErpUserDirectoryEntry, "ERP-1")
            assert row is not None
            self.assertFalse(row.local_active)
            self.assertEqual(row.roles_json, "[]")
            self.assertIsNone(session.scalar(select(AppUser)))

    def test_sync_preserves_local_activation_and_roles_while_updating_erp_state(self) -> None:
        source = StubUserSource([user("ERP-2", "EMP-2")])
        self._sync(source)
        with transactional_session(self.factory) as session:
            service = ErpUserDirectoryService(SqlErpUserDirectoryRepository(session))
            service.update_local_access(
                "ERP-2",
                active=True,
                roles=(ROLE_COORDINATOR, ROLE_TECHNICIAN),
            )

        source.rows[0] = user(
            "ERP-2",
            "EMP-2",
            display_name="Nom ERP modifié",
            user_active=False,
            employee_status="Inactif",
        )
        result = self._sync(source)

        self.assertEqual(result.updated, 1)
        with self.factory() as session:
            record = SqlErpUserDirectoryRepository(session).get_by_user_id("ERP-2")
            assert record is not None
            self.assertTrue(record.local_active)
            self.assertEqual(record.roles, (ROLE_COORDINATOR, ROLE_TECHNICIAN))
            self.assertFalse(record.erp_user_active)
            self.assertEqual(record.employee_status, "Inactif")
            self.assertFalse(record.source_admissible)
            self.assertFalse(record.access_ready)
            self.assertEqual(record.oidc_state, "pending")

    def test_projection_marks_ready_pending_then_linked_without_new_persistence(self) -> None:
        self._sync(StubUserSource([user("ERP-LINK", "EMP-LINK")]))
        with transactional_session(self.factory) as session:
            service = ErpUserDirectoryService(
                SqlErpUserDirectoryRepository(session)
            )
            service.update_local_access(
                "ERP-LINK",
                active=True,
                roles=(ROLE_COORDINATOR,),
            )
            pending = SqlErpUserDirectoryRepository(session).get_by_user_id(
                "ERP-LINK"
            )
            assert pending is not None
            self.assertEqual(pending.oidc_state, "pending")
            self.assertTrue(pending.access_ready)

            SqlUserIdentityRepository(session).upsert(
                issuer="issuer",
                subject="subject",
                display_name="Utilisateur lié",
                email=None,
                roles=(ROLE_COORDINATOR,),
                employee_external_id="EMP-LINK",
            )
            linked = SqlErpUserDirectoryRepository(session).get_by_user_id(
                "ERP-LINK"
            )
            assert linked is not None
            self.assertEqual(linked.oidc_state, "linked")
            self.assertTrue(linked.access_ready)

    def test_partial_snapshot_does_not_disable_or_delete_missing_user(self) -> None:
        source = StubUserSource([user("ERP-A", "EMP-A"), user("ERP-B", "EMP-B")])
        self._sync(source)
        with transactional_session(self.factory) as session:
            ErpUserDirectoryService(
                SqlErpUserDirectoryRepository(session)
            ).update_local_access("ERP-B", active=True, roles=(ROLE_TECHNICIAN,))

        source.rows = [user("ERP-A", "EMP-A")]
        self._sync(source)

        with self.factory() as session:
            record = SqlErpUserDirectoryRepository(session).get_by_user_id("ERP-B")
            assert record is not None
            self.assertTrue(record.local_active)
            self.assertEqual(record.roles, (ROLE_TECHNICIAN,))

    def test_projection_links_to_resource_only_by_employee_external_id(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                Resource(
                    id="RESOURCE-1",
                    external_id="EMP-STABLE",
                    name="Nom ressource différent",
                    email="resource" + chr(64) + "example.invalid",
                    active=True,
                    erp_active=True,
                )
            )
        self._sync(
            StubUserSource(
                [
                    user(
                        "ERP-LINK",
                        "EMP-STABLE",
                        display_name="Nom utilisateur sans correspondance textuelle",
                    )
                ]
            )
        )

        with self.factory() as session:
            record = SqlErpUserDirectoryRepository(session).get_by_user_id("ERP-LINK")
            assert record is not None
            self.assertEqual(record.resource_id, "RESOURCE-1")
            self.assertEqual(record.resource_name, "Nom ressource différent")

    def test_same_name_and_email_never_link_different_employee_identity(self) -> None:
        shared_email = "shared" + chr(64) + "example.invalid"
        with transactional_session(self.factory) as session:
            session.add(
                Resource(
                    id="RESOURCE-OTHER",
                    external_id="EMP-OTHER",
                    name="Même personne affichée",
                    email=shared_email,
                    active=True,
                    erp_active=True,
                )
            )

        source = StubUserSource(
            [
                ExternalErpUserRecord(
                    user_id="ERP-NO-FALLBACK",
                    employee_external_id="EMP-EXPECTED",
                    display_name="Même personne affichée",
                    first_name="Même",
                    last_name="Personne",
                    email=shared_email,
                    erp_user_active=True,
                    employee_status="Actif",
                )
            ]
        )
        self._sync(source)

        with self.factory() as session:
            record = SqlErpUserDirectoryRepository(session).get_by_user_id(
                "ERP-NO-FALLBACK"
            )
            assert record is not None
            self.assertEqual(record.employee_external_id, "EMP-EXPECTED")
            self.assertIsNone(record.resource_id)

    def test_activation_requires_local_role_but_inactive_entry_may_keep_none(self) -> None:
        self._sync(StubUserSource([user("ERP-3", "EMP-3")]))
        with transactional_session(self.factory) as session:
            service = ErpUserDirectoryService(SqlErpUserDirectoryRepository(session))
            with self.assertRaisesRegex(Exception, "Au moins un rôle"):
                service.update_local_access("ERP-3", active=True, roles=())
            record = service.update_local_access("ERP-3", active=False, roles=())
            self.assertFalse(record.local_active)


if __name__ == "__main__":
    unittest.main()
