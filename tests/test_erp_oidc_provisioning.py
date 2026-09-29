from __future__ import annotations

import json
import unittest

from sqlalchemy import select

from app.application.identity_provisioning import (
    AUDIT_OIDC_IDENTITY_LINKED,
    ErpIdentityLinkConflict,
    ErpIdentityLinkDenied,
    ErpPreprovisionedIdentityLinkService,
)
from app.application.security import ROLE_ADMIN, ROLE_COORDINATOR, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    Base,
    ErpUserDirectoryEntry,
    IdentityAdminAudit,
    SqlErpUserDirectoryRepository,
    SqlIdentityAdminAuditRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


ISSUER = "https://identity.example.invalid"


class FailingAudit:
    def record_event(self, **_kwargs) -> None:
        raise RuntimeError("audit failure")


class ErpOidcProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _preprovision(
        self,
        *,
        user_id: str = "ERPUSER",
        employee_id: str = "EMP-1",
        roles: tuple[str, ...] = (ROLE_COORDINATOR,),
        active: bool = True,
        erp_user_active: bool = True,
        employee_status: str = "Actif",
        directory_roles: tuple[str, ...] | None = None,
        display_name: str = "Utilisateur administré",
    ) -> str:
        with self.factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id=user_id,
                    employee_external_id=employee_id,
                    display_name="Utilisateur ERP",
                    email=None,
                    erp_user_active=erp_user_active,
                    employee_status=employee_status,
                    local_active=active,
                    roles_json=json.dumps(list(directory_roles or roles)),
                )
            )
            session.flush()
            account = SqlUserIdentityRepository(session).create_account(
                display_name=display_name,
                email=None,
                roles=roles,
                active=active,
                employee_external_id=employee_id,
                erp_user_id=user_id,
            )
            return account.user_id

    def _resolve(
        self,
        *,
        subject: str = "subject-1",
        preferred_username: str | None = "ERPUSER",
        audit=True,
    ):
        with self.factory.begin() as session:
            service = ErpPreprovisionedIdentityLinkService(
                SqlUserIdentityRepository(session),
                SqlErpUserDirectoryRepository(session),
                (
                    SqlIdentityAdminAuditRepository(session)
                    if audit is True
                    else audit
                ),
            )
            return service.resolve_or_link(
                issuer=ISSUER,
                subject=subject,
                preferred_username=preferred_username,
            )

    def test_first_login_binds_existing_app_user_without_changing_authorization(self) -> None:
        account_id = self._preprovision(
            roles=(ROLE_ADMIN,),
            directory_roles=(ROLE_TECHNICIAN,),
        )

        principal = self._resolve()

        assert principal is not None
        self.assertEqual(principal.local_user_id, account_id)
        self.assertEqual(principal.issuer, ISSUER)
        self.assertEqual(principal.subject, "subject-1")
        self.assertEqual(principal.employee_external_id, "EMP-1")
        self.assertEqual(principal.roles, (ROLE_ADMIN,))
        self.assertEqual(principal.display_name, "Utilisateur administré")
        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertEqual(row.erp_user_id, "ERPUSER")
            self.assertEqual(row.employee_external_id, "EMP-1")
            self.assertEqual(json.loads(row.roles_json), [ROLE_ADMIN])
            self.assertTrue(row.active)
            self.assertIsNotNone(row.business_contact_id)
            audits = session.scalars(
                select(IdentityAdminAudit).where(
                    IdentityAdminAudit.target_user_id == account_id,
                    IdentityAdminAudit.action == AUDIT_OIDC_IDENTITY_LINKED,
                )
            ).all()
            self.assertEqual(len(audits), 1)

    def test_replay_resolves_same_app_user_without_duplicate_link_audit(self) -> None:
        account_id = self._preprovision()
        first = self._resolve()
        second = self._resolve()

        assert first is not None and second is not None
        self.assertEqual(first.local_user_id, account_id)
        self.assertEqual(second.local_user_id, account_id)
        with self.factory() as session:
            self.assertEqual(
                len(session.scalars(select(AppUser)).all()),
                1,
            )
            audits = session.scalars(
                select(IdentityAdminAudit).where(
                    IdentityAdminAudit.action == AUDIT_OIDC_IDENTITY_LINKED
                )
            ).all()
            self.assertEqual(len(audits), 1)

    def test_existing_pair_requires_current_erp_admissibility(self) -> None:
        self._preprovision()
        self._resolve()
        with self.factory.begin() as session:
            row = session.get(ErpUserDirectoryEntry, "ERPUSER")
            assert row is not None
            row.erp_user_active = False

        with self.assertRaises(ErpIdentityLinkDenied):
            self._resolve(preferred_username=None)

    def test_existing_pair_cannot_be_redirected_by_changed_preferred_username(self) -> None:
        self._preprovision()
        self._resolve()
        with self.factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERP-OTHER",
                    employee_external_id="EMP-OTHER",
                    display_name="Autre ERP",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=False,
                    roles_json="[]",
                )
            )

        with self.assertRaises(ErpIdentityLinkConflict):
            self._resolve(preferred_username="ERP-OTHER")

    def test_missing_preferred_username_does_not_create_unknown_user(self) -> None:
        self._preprovision()
        principal = self._resolve(
            subject="unknown-subject",
            preferred_username=None,
        )

        self.assertIsNone(principal)
        with self.factory() as session:
            rows = session.scalars(select(AppUser)).all()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].issuer)
            self.assertIsNone(rows[0].subject)

    def test_unknown_preferred_username_is_denied_without_fallback(self) -> None:
        self._preprovision(user_id="REAL-ID")
        with self.assertRaises(ErpIdentityLinkDenied):
            self._resolve(
                subject="unknown-subject",
                preferred_username="OTHER-ID",
            )

    def test_existing_erp_user_without_preprovisioned_app_user_is_denied(self) -> None:
        with self.factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERPUSER",
                    employee_external_id="EMP-1",
                    display_name="ERP seulement",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json=json.dumps([ROLE_ADMIN]),
                )
            )

        with self.assertRaises(ErpIdentityLinkDenied):
            self._resolve()
        with self.factory() as session:
            self.assertIsNone(session.scalar(select(AppUser)))

    def test_inactive_app_user_is_denied_without_reactivation(self) -> None:
        account_id = self._preprovision(active=False)
        with self.assertRaises(ErpIdentityLinkDenied):
            self._resolve()
        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertFalse(row.active)
            self.assertIsNone(row.issuer)
            self.assertIsNone(row.subject)

    def test_source_inadmissible_erp_user_is_denied(self) -> None:
        account_id = self._preprovision(erp_user_active=False)
        with self.assertRaises(ErpIdentityLinkDenied):
            self._resolve()
        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertIsNone(row.issuer)

    def test_employee_mismatch_is_explicit_and_leaves_identity_pending(self) -> None:
        account_id = self._preprovision()
        with self.factory.begin() as session:
            directory = session.get(ErpUserDirectoryEntry, "ERPUSER")
            assert directory is not None
            directory.employee_external_id = "EMP-CHANGED"

        with self.assertRaises(ErpIdentityLinkConflict):
            self._resolve()

        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertEqual(row.employee_external_id, "EMP-1")
            self.assertIsNone(row.issuer)
            self.assertIsNone(row.subject)

    def test_second_oidc_pair_for_same_account_is_refused(self) -> None:
        account_id = self._preprovision()
        first = self._resolve(subject="subject-first")
        assert first is not None
        self.assertEqual(first.local_user_id, account_id)

        with self.assertRaises(ErpIdentityLinkConflict):
            self._resolve(subject="subject-second")

        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertEqual(row.subject, "subject-first")

    def test_audit_failure_rolls_back_first_identity_link(self) -> None:
        account_id = self._preprovision()
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            self._resolve(audit=FailingAudit())

        with self.factory() as session:
            row = session.get(AppUser, account_id)
            assert row is not None
            self.assertIsNone(row.issuer)
            self.assertIsNone(row.subject)


if __name__ == "__main__":
    unittest.main()
