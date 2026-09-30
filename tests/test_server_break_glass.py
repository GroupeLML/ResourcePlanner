from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.break_glass import BreakGlassBootstrapService
from app.infrastructure.sql.secret_hashing import ScryptSecretHasher
from app.infrastructure.sql import (
    AuthSecurityAudit,
    AuthSession,
    Base,
    SqlBreakGlassRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.break_glass import BreakGlassRuntime
from app.server.oidc import oidc_session_auth_resolver


COOKIE = "rp_break_glass_test"
CREDENTIAL_FIELD = "secret"
CREDENTIAL_VALUE = "server-correct-horse-battery-staple-457"


class ServerBreakGlassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "server-break-glass.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        self.runtime = BreakGlassRuntime(
            cookie_name=COOKIE,
            secure_cookie=False,
            cookie_samesite="lax",
        )
        self.app = create_api_app(
            self.database_url,
            auth_resolver=oidc_session_auth_resolver(COOKIE),
            break_glass_runtime=self.runtime,
        )
        with self.app.state.session_factory.begin() as session:
            self.bootstrap = BreakGlassBootstrapService(
                SqlUserIdentityRepository(session),
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            ).bootstrap(
                login_name="emergency-admin",
                credential_value=CREDENTIAL_VALUE,
                now=__import__("datetime").datetime(
                    2026,
                    9,
                    29,
                    20,
                    0,
                    tzinfo=__import__("datetime").timezone.utc,
                ),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_break_glass_login_uses_server_session_without_oidc_runtime(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/v1/auth/break-glass",
                json={"login_name": "emergency-admin", CREDENTIAL_FIELD: CREDENTIAL_VALUE},
            )
            self.assertEqual(response.status_code, 204, response.text)
            self.assertNotIn(CREDENTIAL_VALUE, response.text)

            session_token = client.cookies.get(COOKIE)
            csrf_token = client.cookies.get("resourceplanner_csrf")
            self.assertIsNotNone(session_token)
            self.assertIsNotNone(csrf_token)

            me = client.get("/api/v1/auth/me")
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()["auth_mode"], "break_glass")
            self.assertEqual(
                me.json()["issuer"],
                "urn:resourceplanner:break-glass",
            )
            self.assertEqual(me.json()["roles"], ["ADMIN"])

            with self.app.state.session_factory() as session:
                stored = session.scalar(select(AuthSession))
                self.assertIsNotNone(stored)
                assert stored is not None
                self.assertEqual(stored.auth_mode, "break_glass")
                self.assertNotEqual(stored.token_hash, session_token)
                audits = session.scalars(select(AuthSecurityAudit)).all()
                self.assertTrue(any(event.success for event in audits))

    def test_wrong_credential_is_refused_without_secret_echo(self) -> None:
        wrong = "wrong-value-that-must-never-appear-in-response"
        with TestClient(self.app) as client:
            response = client.post(
                "/api/v1/auth/break-glass",
                json={"login_name": "emergency-admin", CREDENTIAL_FIELD: wrong},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"],
            "break_glass_authentication_failed",
        )
        self.assertNotIn(wrong, response.text)
        self.assertNotIn(CREDENTIAL_VALUE, response.text)

    def test_rate_limit_is_enforced_after_repeated_failures(self) -> None:
        with TestClient(self.app) as client:
            for _ in range(5):
                response = client.post(
                    "/api/v1/auth/break-glass",
                    json={
                        "login_name": "emergency-admin",
                        CREDENTIAL_FIELD: "wrong-break-glass-secret",
                    },
                )
                self.assertEqual(response.status_code, 401, response.text)

            limited = client.post(
                "/api/v1/auth/break-glass",
                json={"login_name": "emergency-admin", CREDENTIAL_FIELD: CREDENTIAL_VALUE},
            )

        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(
            limited.json()["error"]["code"],
            "break_glass_rate_limited",
        )
        self.assertIn("Retry-After", limited.headers)

    def test_break_glass_mutations_require_existing_csrf_guard(self) -> None:
        with TestClient(self.app) as client:
            login = client.post(
                "/api/v1/auth/break-glass",
                json={"login_name": "emergency-admin", CREDENTIAL_FIELD: CREDENTIAL_VALUE},
            )
            self.assertEqual(login.status_code, 204)
            me = client.get("/api/v1/auth/me").json()
            user_id = me["local_user_id"]
            payload = {
                "display_name": "Administrateur break-glass",
                "email": None,
                "phone": None,
                "roles": ["ADMIN"],
                "active": True,
            }

            missing_csrf = client.patch(
                f"/api/v1/admin/users/{user_id}",
                json=payload,
            )
            self.assertEqual(missing_csrf.status_code, 403)
            self.assertEqual(
                missing_csrf.json()["error"]["code"],
                "csrf_validation_failed",
            )

            csrf = client.cookies.get("resourceplanner_csrf")
            accepted = client.patch(
                f"/api/v1/admin/users/{user_id}",
                json=payload,
                headers={"X-CSRF-Token": str(csrf)},
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)


if __name__ == "__main__":
    unittest.main()
