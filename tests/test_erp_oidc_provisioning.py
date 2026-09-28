from __future__ import annotations

import json
import unittest

from sqlalchemy import select

from app.application.identity_provisioning import (
    ErpControlledIdentityProvisioningService,
    ErpIdentityProvisioningConflict,
    ErpIdentityProvisioningDenied,
)
from app.application.security import ROLE_ADMIN, ROLE_COORDINATOR, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    Base,
    ErpUserDirectoryEntry,
    SqlErpUserDirectoryRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


ISSUER = "https://identity.example.invalid"


class ErpOidcProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _erp_user(
        self,
        *,
        user_id: str = "ERPUSER",
        employee_id: str = "EMP-1",
        local_active: bool = True,
        roles: tuple[str, ...] = (ROLE_COORDINATOR,),
        erp_user_active: bool = True,
        employee_status: str = "Actif",
        display_name: str = "Utilisateur ERP",
        email: str | None = None,
    ) -> None:
        with self.factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id=user_id,
                    employee_external_id=employee_id,
                    display_name=display_name,
                    email=email,
                    erp_user_active=erp_user_active,
                    employee_status=employee_status,
                    local_active=local_active,
                    roles_json=json.dumps(list(roles)),
                )
            )

    def _resolve(
        self,
        *,
        subject: str = "subject-1",
        preferred_username: str | None = "ERPUSER",
        display_name: str = "Nom OIDC",
        email: str | None = None,
    ):
        with self.factory.begin() as session:
            return ErpControlledIdentityProvisioningService(
                SqlUserIdentityRepository(session),
                SqlErpUserDirectoryRepository(session),
            ).resolve_or_provision(
                issuer=ISSUER,
                subject=subject,
                preferred_username=preferred_username,
                display_name=display_name,
                email=email,
            )

    def test_first_login_creates_app_user_with_authoritative_identity_employee_and_local_roles(self) -> None:
        self._erp_user(roles=(ROLE_ADMIN,))
        principal = self._resolve()

        assert principal is not None
        self.assertEqual(principal.issuer, ISSUER)
        self.assertEqual(principal.subject, "subject-1")
        self.assertEqual(principal.employee_external_id, "EMP-1")
        self.assertEqual(principal.roles, (ROLE_ADMIN,))
        with self.factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.subject == "subject-1"))
            assert row is not None
            self.assertIsNotNone(row.business_contact_id)

    def test_replay_keeps_the_same_stable_app_user(self) -> None:
        self._erp_user()
        first = self._resolve()
        second = self._resolve()

        assert first is not None and second is not None
        self.assertEqual(first.local_user_id, second.local_user_id)
        with self.factory() as session:
            rows = session.scalars(
                select(AppUser).where(AppUser.subject == "subject-1")
            ).all()
            self.assertEqual(len(rows), 1)

    def test_missing_preferred_username_does_not_create_unknown_user(self) -> None:
        self._erp_user()
        principal = self._resolve(preferred_username=None)

        self.assertIsNone(principal)
        with self.factory() as session:
            self.assertIsNone(session.scalar(select(AppUser)))

    def test_unknown_preferred_username_is_denied_without_name_or_email_fallback(self) -> None:
        shared_email = "same" + chr(64) + "example.invalid"
        self._erp_user(
            user_id="REAL-ID",
            display_name="Même nom",
            email=shared_email,
        )
        with self.assertRaises(ErpIdentityProvisioningDenied):
            self._resolve(
                preferred_username="OTHER-ID",
                display_name="Même nom",
                email=shared_email,
            )
        with self.factory() as session:
            self.assertIsNone(session.scalar(select(AppUser)))

    def test_locally_inactive_erp_user_is_denied(self) -> None:
        self._erp_user(local_active=False)
        with self.assertRaises(ErpIdentityProvisioningDenied):
            self._resolve()

    def test_source_inadmissible_erp_user_is_denied(self) -> None:
        self._erp_user(erp_user_active=False)
        with self.assertRaises(ErpIdentityProvisioningDenied):
            self._resolve()

    def test_erp_user_without_local_role_is_denied(self) -> None:
        self._erp_user(roles=())
        with self.assertRaises(ErpIdentityProvisioningDenied):
            self._resolve()

    def test_employee_conflict_is_explicit_and_leaves_no_partial_identity(self) -> None:
        self._erp_user(employee_id="EMP-CONFLICT")
        with self.factory.begin() as session:
            SqlUserIdentityRepository(session).upsert(
                issuer=ISSUER,
                subject="other-subject",
                display_name="Autre utilisateur",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                employee_external_id="EMP-CONFLICT",
            )

        with self.assertRaises(ErpIdentityProvisioningConflict):
            self._resolve(subject="new-subject")

        with self.factory() as session:
            self.assertIsNone(
                session.scalar(select(AppUser).where(AppUser.subject == "new-subject"))
            )

    def test_existing_oidc_identity_is_reused_and_missing_employee_link_is_cohered(self) -> None:
        self._erp_user(employee_id="EMP-STABLE", roles=(ROLE_ADMIN,))
        with self.factory.begin() as session:
            existing = SqlUserIdentityRepository(session).upsert(
                issuer=ISSUER,
                subject="subject-existing",
                display_name="Ancien nom",
                email=None,
                roles=(ROLE_TECHNICIAN,),
            )
            existing_id = existing.user_id

        principal = self._resolve(
            subject="subject-existing",
            preferred_username="ERPUSER",
        )

        assert principal is not None
        self.assertEqual(principal.local_user_id, existing_id)
        self.assertEqual(principal.employee_external_id, "EMP-STABLE")
        self.assertEqual(principal.roles, (ROLE_ADMIN,))

    def test_existing_identity_employee_mismatch_is_never_changed_silently(self) -> None:
        self._erp_user(employee_id="EMP-EXPECTED")
        with self.factory.begin() as session:
            SqlUserIdentityRepository(session).upsert(
                issuer=ISSUER,
                subject="subject-existing",
                display_name="Existant",
                email=None,
                roles=(ROLE_COORDINATOR,),
                employee_external_id="EMP-OTHER",
            )

        with self.assertRaises(ErpIdentityProvisioningConflict):
            self._resolve(subject="subject-existing")

        with self.factory() as session:
            row = session.scalar(
                select(AppUser).where(AppUser.subject == "subject-existing")
            )
            assert row is not None
            self.assertEqual(row.employee_external_id, "EMP-OTHER")


if __name__ == "__main__":
    unittest.main()
