from __future__ import annotations

import unittest

from sqlalchemy.exc import IntegrityError

from app.application.security import IdentityService, ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    ErpUserDirectoryEntry,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


class IdentityPreprovisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed_erp_user(
        self,
        session,
        user_id: str,
        employee_external_id: str,
    ) -> None:
        session.add(
            ErpUserDirectoryEntry(
                user_id=user_id,
                employee_external_id=employee_external_id,
                display_name=f"ERP {user_id}",
                erp_user_active=True,
                employee_status="Actif",
                local_active=False,
                roles_json="[]",
            )
        )

    def test_schema_allows_multiple_accounts_without_oidc_and_keeps_filtered_uniqueness(self) -> None:
        users = Base.metadata.tables["app_users"]
        self.assertTrue(users.c.issuer.nullable)
        self.assertTrue(users.c.subject.nullable)
        self.assertTrue(users.c.erp_user_id.nullable)
        self.assertEqual(
            {fk.column.table.name for fk in users.c.erp_user_id.foreign_keys},
            {"erp_user_directory"},
        )
        self.assertIn(
            "ux_app_users_oidc_identity_not_null",
            {index.name for index in users.indexes},
        )
        self.assertIn(
            "ux_app_users_erp_user_id_not_null",
            {index.name for index in users.indexes},
        )

        with self.factory.begin() as session:
            session.add_all(
                [
                    AppUser(
                        id=f"U{index}",
                        issuer=None,
                        subject=None,
                        display_name=f"Utilisateur {index}",
                        roles_json='["TECHNICIAN"]',
                        active=True,
                    )
                    for index in range(1, 4)
                ]
            )

        with self.factory() as session:
            self.assertEqual(session.query(AppUser).count(), 3)

    def test_database_rejects_partial_oidc_identity(self) -> None:
        for user_id, issuer, subject in (
            ("U-ISSUER", "issuer-x", None),
            ("U-SUBJECT", None, "subject-y"),
        ):
            with self.assertRaises(IntegrityError):
                with self.factory.begin() as session:
                    session.add(
                        AppUser(
                            id=user_id,
                            issuer=issuer,
                            subject=subject,
                            display_name=user_id,
                            roles_json='["TECHNICIAN"]',
                            active=True,
                        )
                    )

    def test_database_rejects_empty_oidc_identity_pair(self) -> None:
        with self.assertRaises(IntegrityError):
            with self.factory.begin() as session:
                session.add(
                    AppUser(
                        id="U-EMPTY",
                        issuer="",
                        subject="",
                        display_name="Identité vide",
                        roles_json='["TECHNICIAN"]',
                        active=True,
                    )
                )

    def test_create_update_and_lookup_preprovisioned_account_without_oidc(self) -> None:
        with self.factory.begin() as session:
            self._seed_erp_user(session, "ERP-1", "EMP-1")
            repository = SqlUserIdentityRepository(session)
            created = repository.create_account(
                display_name="Utilisateur pré-provisionné",
                email="preprovisioned" + chr(64) + "example.invalid",
                roles=(ROLE_TECHNICIAN,),
                active=True,
                employee_external_id="EMP-1",
                erp_user_id="ERP-1",
            )
            self.assertIsNone(created.issuer)
            self.assertIsNone(created.subject)
            self.assertEqual(created.erp_user_id, "ERP-1")
            self.assertEqual(created.employee_external_id, "EMP-1")
            self.assertIsNotNone(created.business_contact_id)
            self.assertIsNone(
                IdentityService(repository).resolve(
                    issuer="issuer-inconnu",
                    subject="subject-inconnu",
                )
            )

            updated = repository.update_account(
                created.user_id,
                display_name="Compte administré",
                email=None,
                roles=(ROLE_ADMIN,),
                active=False,
                employee_external_id="EMP-1",
                erp_user_id="ERP-1",
            )
            self.assertEqual(updated.user_id, created.user_id)
            self.assertEqual(updated.roles, (ROLE_ADMIN,))
            self.assertFalse(updated.active)
            self.assertIsNone(updated.issuer)
            self.assertIsNone(updated.subject)
            self.assertEqual(
                repository.get_by_erp_user_id("ERP-1").user_id,
                created.user_id,
            )

        with self.factory() as session:
            contact = session.get(BusinessContact, created.business_contact_id)
            self.assertIsNotNone(contact)
            assert contact is not None
            self.assertEqual(contact.external_id, "EMP-1")

    def test_bind_external_identity_is_idempotent_and_refuses_rebinding(self) -> None:
        with self.factory.begin() as session:
            self._seed_erp_user(session, "ERP-1", "EMP-1")
            repository = SqlUserIdentityRepository(session)
            created = repository.create_account(
                display_name="Utilisateur",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                employee_external_id="EMP-1",
                erp_user_id="ERP-1",
            )
            bound = repository.bind_external_identity(
                created.user_id,
                "issuer-x",
                "subject-y",
            )
            replay = repository.bind_external_identity(
                created.user_id,
                "issuer-x",
                "subject-y",
            )
            self.assertEqual(bound.user_id, created.user_id)
            self.assertEqual(replay.user_id, created.user_id)
            self.assertEqual(bound.issuer, "issuer-x")
            self.assertEqual(bound.subject, "subject-y")

            with self.assertRaises(ValueError):
                repository.bind_external_identity(
                    created.user_id,
                    "issuer-z",
                    "subject-y",
                )

    def test_bind_refuses_identity_owned_by_another_account(self) -> None:
        with self.factory.begin() as session:
            self._seed_erp_user(session, "ERP-1", "EMP-1")
            self._seed_erp_user(session, "ERP-2", "EMP-2")
            repository = SqlUserIdentityRepository(session)
            first = repository.create_account(
                display_name="Premier",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                employee_external_id="EMP-1",
                erp_user_id="ERP-1",
            )
            second = repository.create_account(
                display_name="Deuxième",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                employee_external_id="EMP-2",
                erp_user_id="ERP-2",
            )
            repository.bind_external_identity(first.user_id, "issuer-x", "subject-y")
            with self.assertRaises(ValueError):
                repository.bind_external_identity(second.user_id, "issuer-x", "subject-y")

    def test_account_identity_links_remain_reserved_when_inactive(self) -> None:
        with self.factory.begin() as session:
            self._seed_erp_user(session, "ERP-1", "EMP-1")
            self._seed_erp_user(session, "ERP-2", "EMP-2")
            repository = SqlUserIdentityRepository(session)
            first = repository.create_account(
                display_name="Premier",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                active=False,
                employee_external_id="EMP-1",
                erp_user_id="ERP-1",
            )
            repository.bind_external_identity(first.user_id, "issuer-x", "subject-y")

            with self.assertRaises(ValueError):
                repository.create_account(
                    display_name="Collision EmployeID",
                    email=None,
                    roles=(ROLE_TECHNICIAN,),
                    employee_external_id="EMP-1",
                    erp_user_id="ERP-2",
                )
            with self.assertRaises(ValueError):
                repository.create_account(
                    display_name="Collision ERP",
                    email=None,
                    roles=(ROLE_TECHNICIAN,),
                    employee_external_id="EMP-2",
                    erp_user_id="ERP-1",
                )

    def test_legacy_upsert_still_resolves_existing_runtime_identity(self) -> None:
        with self.factory.begin() as session:
            repository = SqlUserIdentityRepository(session)
            created = repository.upsert(
                issuer="legacy-issuer",
                subject="legacy-subject",
                display_name="Utilisateur existant",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                active=True,
            )
            principal = IdentityService(repository).resolve(
                issuer="legacy-issuer",
                subject="legacy-subject",
            )

        self.assertIsNotNone(principal)
        assert principal is not None
        self.assertEqual(principal.local_user_id, created.user_id)


if __name__ == "__main__":
    unittest.main()
