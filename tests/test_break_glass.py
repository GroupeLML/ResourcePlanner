from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from app.application.break_glass import (
    BreakGlassAuthenticationDenied,
    BreakGlassAuthenticationService,
    BreakGlassBootstrapService,
    BreakGlassRateLimited,
)
from app.application.errors import ApplicationConflictError
from app.application.security import ROLE_ADMIN, ROLE_TECHNICIAN
from app.application.user_admin import UserAdminService
from app.infrastructure.sql.secret_hashing import ScryptSecretHasher
from app.infrastructure.sql import (
    AppUser,
    AuthSecurityAudit,
    Base,
    BreakGlassCredential,
    SqlBreakGlassRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


CREDENTIAL_VALUE = "correct-horse-battery-staple-457"
ROTATED_CREDENTIAL_VALUE = "rotated-correct-horse-battery-457"


class BreakGlassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "break-glass.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _bootstrap(
        self,
        login: str = "emergency-admin",
        credential_value: str = CREDENTIAL_VALUE,
        *,
        rotate: bool = False,
        now: datetime | None = None,
    ):
        with self.factory.begin() as session:
            return BreakGlassBootstrapService(
                SqlUserIdentityRepository(session),
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            ).bootstrap(
                login_name=login,
                credential_value=secret,
                rotate_secret=rotate,
                now=now or datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc),
            )

    def test_bootstrap_is_idempotent_and_keeps_secret_out_of_persisted_plaintext(self) -> None:
        first = self._bootstrap()
        second = self._bootstrap()

        self.assertEqual(first.user_id, second.user_id)
        self.assertEqual(first.credential_id, second.credential_id)
        self.assertEqual(first.credential_version, 1)
        self.assertEqual(second.credential_version, 1)
        self.assertEqual(first.action, "created")
        self.assertEqual(second.action, "reconciled")

        with self.factory() as session:
            user = session.get(AppUser, first.user_id)
            credential = session.get(BreakGlassCredential, first.credential_id)
            self.assertIsNotNone(user)
            self.assertIsNotNone(credential)
            assert user is not None and credential is not None
            self.assertTrue(user.active)
            self.assertIsNone(user.issuer)
            self.assertIsNone(user.subject)
            self.assertIsNone(user.erp_user_id)
            self.assertIsNone(user.employee_external_id)
            self.assertIn(ROLE_ADMIN, user.roles_json)
            self.assertNotEqual(credential.secret_hash, CREDENTIAL_VALUE)
            self.assertNotIn(CREDENTIAL_VALUE, credential.secret_hash)
            self.assertTrue(
                ScryptSecretHasher().verify_secret(CREDENTIAL_VALUE, credential.secret_hash)
            )
            events = session.scalars(
                select(AuthSecurityAudit).order_by(AuthSecurityAudit.created_at)
            ).all()
            self.assertEqual(len(events), 2)
            for event in events:
                self.assertNotIn(CREDENTIAL_VALUE, event.login_name_hash)
                self.assertNotIn(CREDENTIAL_VALUE, event.reason_code)

    def test_existing_secret_requires_explicit_rotation_and_old_secret_stops_working(self) -> None:
        created = self._bootstrap()
        with self.assertRaises(ApplicationConflictError) as caught:
            self._bootstrap(credential_value=ROTATED_CREDENTIAL_VALUE)
        self.assertEqual(caught.exception.code, "break_glass_rotation_required")

        rotated = self._bootstrap(credential_value=ROTATED_CREDENTIAL_VALUE, rotate=True)
        self.assertEqual(rotated.user_id, created.user_id)
        self.assertEqual(rotated.credential_id, created.credential_id)
        self.assertEqual(rotated.credential_version, 2)
        self.assertEqual(rotated.action, "rotated")

        with self.factory.begin() as session:
            service = BreakGlassAuthenticationService(
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            )
            with self.assertRaises(BreakGlassAuthenticationDenied):
                service.authenticate(
                    login_name="emergency-admin",
                    credential_value=CREDENTIAL_VALUE,
                    now=datetime(2026, 9, 29, 20, 1, tzinfo=timezone.utc),
                )

        with self.factory.begin() as session:
            user_id = BreakGlassAuthenticationService(
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            ).authenticate(
                login_name="emergency-admin",
                credential_value=ROTATED_CREDENTIAL_VALUE,
                now=datetime(2026, 9, 29, 20, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(user_id, created.user_id)

    def test_repeated_failures_lock_the_credential(self) -> None:
        self._bootstrap()
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)

        for offset in range(5):
            with self.factory.begin() as session:
                service = BreakGlassAuthenticationService(
                    SqlBreakGlassRepository(session),
                    ScryptSecretHasher(),
                )
                with self.assertRaises(BreakGlassAuthenticationDenied):
                    service.authenticate(
                        login_name="emergency-admin",
                        credential_value="wrong-secret",
                        now=start + timedelta(seconds=offset),
                    )

        with self.factory.begin() as session:
            service = BreakGlassAuthenticationService(
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            )
            with self.assertRaises(BreakGlassRateLimited):
                service.authenticate(
                    login_name="emergency-admin",
                    credential_value=CREDENTIAL_VALUE,
                    now=start + timedelta(seconds=6),
                )

    def test_last_break_glass_admin_cannot_be_disabled_or_lose_admin_role(self) -> None:
        created = self._bootstrap()

        with self.factory.begin() as session:
            service = UserAdminService(
                SqlUserIdentityRepository(session),
                break_glass_protection=SqlBreakGlassRepository(session),
            )
            current = service.list_users()[0]
            with self.assertRaises(ApplicationConflictError) as deactivate:
                service.update_user(
                    created.user_id,
                    display_name=current.display_name,
                    email=current.email,
                    roles=(ROLE_ADMIN,),
                    active=False,
                    actor_user_id="another-admin",
                )
            self.assertEqual(
                deactivate.exception.code,
                "user_admin_last_break_glass_protected",
            )

            with self.assertRaises(ApplicationConflictError) as remove_role:
                service.update_user(
                    created.user_id,
                    display_name=current.display_name,
                    email=current.email,
                    roles=(ROLE_TECHNICIAN,),
                    active=True,
                    actor_user_id="another-admin",
                )
            self.assertEqual(
                remove_role.exception.code,
                "user_admin_last_break_glass_protected",
            )

        self._bootstrap(
            login="emergency-admin-2",
            credential_value="second-correct-horse-battery-457",
        )
        with self.factory.begin() as session:
            service = UserAdminService(
                SqlUserIdentityRepository(session),
                break_glass_protection=SqlBreakGlassRepository(session),
            )
            current = service._repository.get_by_id(created.user_id)
            assert current is not None
            updated = service.update_user(
                created.user_id,
                display_name=current.display_name,
                email=current.email,
                roles=(ROLE_TECHNICIAN,),
                active=True,
                actor_user_id="another-admin",
            )
            self.assertEqual(updated.roles, (ROLE_TECHNICIAN,))


if __name__ == "__main__":
    unittest.main()
