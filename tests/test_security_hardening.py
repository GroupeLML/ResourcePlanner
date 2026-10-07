from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlparse
import unittest

from fastapi.testclient import TestClient

from app.application.identity_provisioning import AutoProvisioningPolicy
from app.application.security import ROLE_ADMIN
from app.infrastructure.acumatica.oidc import OidcIdentity
from app.infrastructure.sql import (
    Base,
    ErpUserDirectoryEntry,
    Resource,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.oidc import OidcRuntime, oidc_session_auth_resolver


ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://identity.example.invalid"
SESSION_COOKIE = "rp_security_session"


class FakeOidcClient:
    def __init__(self) -> None:
        self.identity = OidcIdentity(
            issuer=ISSUER,
            subject="admin-security",
            display_name="Administrateur sécurité",
            email=None,
        )
        self.last_verifier: str | None = None
        self.last_nonce: str | None = None

    async def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        self.last_verifier = code_verifier
        self.last_nonce = nonce
        return "https://identity.example.invalid/authorize?" + urlencode(
            {"state": state, "nonce": nonce}
        )

    async def exchange_code(self, *, code: str, code_verifier: str, nonce: str) -> OidcIdentity:
        if code != "valid-code":
            raise ValueError("invalid code")
        if code_verifier != self.last_verifier or nonce != self.last_nonce:
            raise ValueError("invalid transaction")
        return self.identity


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "security.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ADMIN-SECURITY",
                    employee_external_id="EMP-SECURITY",
                    display_name="Administrateur sécurité",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json='["ADMIN"]',
                )
            )
            session.flush()
            identities = SqlUserIdentityRepository(session)
            account = identities.create_account(
                display_name="Administrateur sécurité",
                email=None,
                roles=(ROLE_ADMIN,),
                active=True,
                employee_external_id="EMP-SECURITY",
                erp_user_id="ADMIN-SECURITY",
            )
            identities.bind_external_identity(
                account.user_id,
                ISSUER,
                "admin-security",
            )
            session.add(
                Resource(
                    id="AUDIT",
                    name="Ressource audit",
                    active=True,
                    sort_order=1,
                )
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(self):
        fake = FakeOidcClient()
        runtime = OidcRuntime(
            client=fake,  # type: ignore[arg-type]
            cookie_name=SESSION_COOKIE,
            secure_cookie=True,
            cookie_samesite="none",
            auto_provisioning=AutoProvisioningPolicy(enabled=False),
        )
        app = create_api_app(
            self.database_url,
            auth_resolver=oidc_session_auth_resolver(SESSION_COOKIE),
            oidc_runtime=runtime,
        )
        return app

    @staticmethod
    def _state(login_response) -> str:
        return parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    def test_oidc_callback_is_bound_to_browser_that_started_login(self) -> None:
        app = self._app()
        with (
            TestClient(app, base_url="https://planner.example.invalid") as browser_a,
            TestClient(app, base_url="https://planner.example.invalid") as browser_b,
        ):
            login = browser_a.get("/api/v1/auth/login", follow_redirects=False)
            self.assertEqual(login.status_code, 302)
            state = self._state(login)

            foreign = browser_b.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(foreign.status_code, 400)
            self.assertEqual(
                foreign.json()["error"]["code"],
                "oidc_browser_binding_missing",
            )

            original = browser_a.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(original.status_code, 303)
            self.assertEqual(browser_a.get("/api/v1/auth/me").status_code, 200)

            replay = browser_a.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(replay.status_code, 400)

    def test_oidc_mutations_require_session_bound_csrf_and_same_origin(self) -> None:
        app = self._app()
        with TestClient(app, base_url="https://planner.example.invalid") as client:
            login = client.get("/api/v1/auth/login", follow_redirects=False)
            state = self._state(login)
            callback = client.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303)
            csrf = client.cookies.get("resourceplanner_csrf")
            self.assertTrue(csrf)

            missing = client.post(
                "/api/v1/resources/AUDIT/deactivate",
                headers={"Origin": "https://planner.example.invalid"},
            )
            self.assertEqual(missing.status_code, 403)
            self.assertEqual(missing.json()["error"]["code"], "csrf_validation_failed")

            cross_site = client.post(
                "/api/v1/resources/AUDIT/deactivate",
                headers={
                    "Origin": "https://attacker.example.invalid",
                    "X-CSRF-Token": str(csrf),
                },
            )
            self.assertEqual(cross_site.status_code, 403)
            self.assertEqual(
                cross_site.json()["error"]["code"],
                "csrf_validation_failed",
            )

            same_origin = client.post(
                "/api/v1/resources/AUDIT/deactivate",
                headers={
                    "Origin": "https://planner.example.invalid",
                    "X-CSRF-Token": str(csrf),
                },
            )
            self.assertEqual(same_origin.status_code, 200, same_origin.text)

    def test_compose_frontend_bind_address_is_environment_configurable(self) -> None:
        root_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        synology_compose = (ROOT / "deploy" / "synology" / "compose.yml").read_text(
            encoding="utf-8"
        )
        expected_binding = (
            "${RESOURCEPLANNER_BIND_ADDRESS:-0.0.0.0}:"
            "${RESOURCEPLANNER_HTTP_PORT:-8080}:8080"
        )

        self.assertIn(expected_binding, root_compose)
        self.assertIn(expected_binding, synology_compose)
        self.assertNotIn(
            '127.0.0.1:${RESOURCEPLANNER_HTTP_PORT:-8080}:8080',
            root_compose,
        )


if __name__ == "__main__":
    unittest.main()
