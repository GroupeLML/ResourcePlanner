from __future__ import annotations

import json
import unittest

from sqlalchemy import select

from app.application.security import ROLE_ADMIN, ROLE_TECHNICIAN
from app.application.user_admin import UserAdminService
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    ErpUserDirectoryEntry,
    Resource,
    SqlErpUserDirectoryRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


class FailingAudit:
    def record_event(self, **_kwargs) -> None:
        raise RuntimeError("audit failure")


class IdentityAdminPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(
                AppUser(
                    id="admin-actor",
                    issuer="urn:test",
                    subject="subject-admin",
                    display_name="Administrateur",
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERP-TARGET",
                    employee_external_id="EMP-TARGET",
                    display_name="Utilisateur cible",
                    email="target" + chr(64) + "example.invalid",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=False,
                    roles_json="[]",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_audit_failure_rolls_back_preprovision_contact_mirror_and_account(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            with self.factory.begin() as session:
                service = UserAdminService(
                    SqlUserIdentityRepository(session),
                    erp_directory=SqlErpUserDirectoryRepository(session),
                    audit=FailingAudit(),
                )
                service.update_erp_user_access(
                    "ERP-TARGET",
                    active=True,
                    roles=(ROLE_TECHNICIAN,),
                    actor_user_id="admin-actor",
                )

        with self.factory() as session:
            self.assertIsNone(
                session.scalar(
                    select(AppUser).where(AppUser.erp_user_id == "ERP-TARGET")
                )
            )
            directory = session.get(ErpUserDirectoryEntry, "ERP-TARGET")
            assert directory is not None
            self.assertFalse(directory.local_active)
            self.assertEqual(directory.roles_json, "[]")
            self.assertIsNone(
                session.scalar(
                    select(BusinessContact).where(
                        BusinessContact.external_id == "EMP-TARGET"
                    )
                )
            )
            self.assertIsNone(
                session.scalar(
                    select(Resource).where(Resource.external_id == "EMP-TARGET")
                )
            )


if __name__ == "__main__":
    unittest.main()
