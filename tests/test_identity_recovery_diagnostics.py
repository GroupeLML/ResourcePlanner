from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from app.application.security import ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    AuthSession,
    Base,
    ErpUserDirectoryEntry,
    Resource,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)
from tools.diagnose_identity_recovery import (
    apply_deterministic_identity_recovery,
    build_report,
    inspect_identity_recovery,
)


class IdentityRecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _directory(self, session, user_id: str, employee_id: str) -> None:
        session.add(
            ErpUserDirectoryEntry(
                user_id=user_id,
                employee_external_id=employee_id,
                display_name=user_id,
                erp_user_active=True,
                employee_status="Actif",
                local_active=False,
                roles_json="[]",
            )
        )

    def _linked_user(
        self,
        session,
        *,
        user_id: str,
        employee_id: str | None,
        erp_user_id: str | None = None,
        subject: str | None = None,
    ) -> None:
        session.add(
            AppUser(
                id=user_id,
                issuer="https://identity.example.invalid",
                subject=subject or f"subject-{user_id}",
                display_name=user_id,
                employee_external_id=employee_id,
                erp_user_id=erp_user_id,
                roles_json=json.dumps([ROLE_TECHNICIAN]),
                active=True,
            )
        )

    def test_diagnostics_are_read_only_and_classify_unresolved_history(self) -> None:
        with self.factory.begin() as session:
            self._directory(session, "ERP-UNIQUE", "EMP-UNIQUE")
            self._directory(session, "ERP-AMB-A", "EMP-AMB")
            self._directory(session, "ERP-AMB-B", "EMP-AMB")
            self._directory(session, "ERP-OWNED", "EMP-OWNED")
            self._directory(session, "ERP-MISMATCH", "EMP-SOURCE")
            self._directory(session, "ERP-CLEAN", "EMP-CLEAN")

            self._linked_user(
                session,
                user_id="U-UNIQUE",
                employee_id="EMP-UNIQUE",
            )
            self._linked_user(
                session,
                user_id="U-AMB",
                employee_id="EMP-AMB",
            )
            self._linked_user(
                session,
                user_id="U-MISSING",
                employee_id="EMP-MISSING",
            )
            self._linked_user(
                session,
                user_id="U-NO-EMPLOYEE",
                employee_id=None,
            )
            self._linked_user(
                session,
                user_id="U-OWNED-LEGACY",
                employee_id="EMP-OWNED",
            )
            session.add(
                AppUser(
                    id="U-OWNER",
                    issuer=None,
                    subject=None,
                    display_name="Owner",
                    employee_external_id="EMP-OWNER-DIFFERENT",
                    erp_user_id="ERP-OWNED",
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )
            self._linked_user(
                session,
                user_id="U-MISMATCH",
                employee_id="EMP-LOCAL",
                erp_user_id="ERP-MISMATCH",
            )
            self._linked_user(
                session,
                user_id="U-CLEAN",
                employee_id="EMP-CLEAN",
                erp_user_id="ERP-CLEAN",
            )

        with self.factory() as session:
            diagnostics = inspect_identity_recovery(session)
            report = build_report(session)

            by_id = {item.app_user_id: item for item in diagnostics}
            self.assertEqual(
                by_id["U-UNIQUE"].reason,
                "deterministic_backfill_pending",
            )
            self.assertEqual(
                by_id["U-UNIQUE"].candidate_erp_user_ids,
                ("ERP-UNIQUE",),
            )
            self.assertEqual(by_id["U-AMB"].reason, "erp_user_ambiguous")
            self.assertEqual(
                by_id["U-AMB"].candidate_erp_user_ids,
                ("ERP-AMB-A", "ERP-AMB-B"),
            )
            self.assertEqual(by_id["U-MISSING"].reason, "erp_user_not_found")
            self.assertEqual(
                by_id["U-NO-EMPLOYEE"].reason,
                "employee_external_id_missing",
            )
            self.assertEqual(
                by_id["U-OWNED-LEGACY"].reason,
                "erp_user_already_owned",
            )
            self.assertEqual(
                by_id["U-MISMATCH"].reason,
                "employee_external_id_mismatch",
            )
            self.assertNotIn("U-CLEAN", by_id)
            self.assertEqual(report["status"], "review_required")
            self.assertEqual(report["unresolved_count"], 6)
            self.assertFalse(report["mutation_performed"])
            self.assertEqual(report["recovered_count"], 0)

            unique = session.get(AppUser, "U-UNIQUE")
            assert unique is not None
            self.assertIsNone(unique.erp_user_id)
            self.assertEqual(unique.subject, "subject-U-UNIQUE")

    def test_deterministic_recovery_preserves_identity_authorities_and_is_idempotent(self) -> None:
        with self.factory.begin() as session:
            self._directory(session, "ERP-RECOVER", "EMP-RECOVER")
            self._directory(session, "ERP-AMB-A", "EMP-AMB")
            self._directory(session, "ERP-AMB-B", "EMP-AMB")

            record = SqlUserIdentityRepository(session).upsert(
                issuer="issuer-recover",
                subject="subject-recover",
                display_name="Utilisateur historique",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                employee_external_id="EMP-RECOVER",
            )
            session.add(
                AuthSession(
                    id="SESSION-RECOVER",
                    token_hash="a" * 64,
                    csrf_token_hash=None,
                    user_id=record.user_id,
                    auth_mode="oidc",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
            self._linked_user(
                session,
                user_id="U-AMB",
                employee_id="EMP-AMB",
            )

        with self.factory.begin() as session:
            before = session.get(AppUser, record.user_id)
            assert before is not None
            stable = (
                before.id,
                before.employee_external_id,
                before.business_contact_id,
                before.issuer,
                before.subject,
                before.roles_json,
                before.active,
            )

            recovered = apply_deterministic_identity_recovery(session)

            after = session.get(AppUser, record.user_id)
            assert after is not None
            self.assertEqual(recovered, (record.user_id,))
            self.assertEqual(after.erp_user_id, "ERP-RECOVER")
            self.assertEqual(
                (
                    after.id,
                    after.employee_external_id,
                    after.business_contact_id,
                    after.issuer,
                    after.subject,
                    after.roles_json,
                    after.active,
                ),
                stable,
            )
            auth_session = session.get(AuthSession, "SESSION-RECOVER")
            assert auth_session is not None
            self.assertEqual(auth_session.user_id, record.user_id)
            ambiguous = session.get(AppUser, "U-AMB")
            assert ambiguous is not None
            self.assertIsNone(ambiguous.erp_user_id)
            self.assertIsNone(
                session.scalar(
                    select(Resource).where(Resource.external_id == "EMP-RECOVER")
                )
            )

        with self.factory.begin() as session:
            self.assertEqual(apply_deterministic_identity_recovery(session), ())
            recovered_user = session.get(AppUser, record.user_id)
            assert recovered_user is not None
            self.assertEqual(recovered_user.erp_user_id, "ERP-RECOVER")
            ambiguous = session.get(AppUser, "U-AMB")
            assert ambiguous is not None
            self.assertIsNone(ambiguous.erp_user_id)

    def test_clean_history_reports_ok(self) -> None:
        with self.factory.begin() as session:
            self._directory(session, "ERP-CLEAN", "EMP-CLEAN")
            self._linked_user(
                session,
                user_id="U-CLEAN",
                employee_id="EMP-CLEAN",
                erp_user_id="ERP-CLEAN",
            )

        with self.factory() as session:
            self.assertEqual(
                build_report(session),
                {
                    "status": "ok",
                    "unresolved_count": 0,
                    "diagnostics": [],
                    "mutation_performed": False,
                    "recovered_count": 0,
                    "recovered_app_user_ids": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
