from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlparse
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.identity_provisioning import AutoProvisioningPolicy
from app.application.security import ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.acumatica.oidc import OidcIdentity, OidcProtocolError
from app.infrastructure.sql import (
    AppUser,
    AuthSession,
    Base,
    ErpUserDirectoryEntry,
    SqlAuthSessionRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.oidc import OidcRuntime, oidc_session_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate


ISSUER = "https://identity.example.invalid"
COOKIE = "rp_test_session"
DIAGNOSTICS = {
    "claim_names": ["aud", "email", "exp", "iss", "name", "preferred_username", "sub"],
    "identity_candidates": {
        "iss": ISSUER,
        "sub": "subject-1",
        "preferred_username": "ERPUSER42",
        "name": "Utilisateur OIDC",
        "email": "person" + chr(64) + "example.invalid",
    },
}


class FakeOidcClient:
    def __init__(
        self,
        identity: OidcIdentity,
        *,
        exchange_error: Exception | None = None,
    ) -> None:
        self.identity = identity
        self.exchange_error = exchange_error
        self.last_verifier: str | None = None
        self.last_nonce: str | None = None

    async def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        self.last_verifier = code_verifier
        self.last_nonce = nonce
        return "https://identity.example.invalid/authorize?" + urlencode(
            {
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
            }
        )

    async def exchange_code(self, *, code: str, code_verifier: str, nonce: str) -> OidcIdentity:
        if code != "valid-code":
            raise ValueError("invalid code")
        if code_verifier != self.last_verifier or nonce != self.last_nonce:
            raise ValueError("invalid transaction")
        if self.exchange_error is not None:
            raise self.exchange_error
        return self.identity


class ServerOidcTests(unittest.TestCase):
    @classmethod
    def _seed_database(cls, session) -> None:
        user = SqlUserIdentityRepository(session).upsert(
            issuer=ISSUER,
            subject="subject-1",
            display_name="Technicien OIDC",
            email=None,
            roles=(ROLE_TECHNICIAN,),
        )
        cls.user_id = user.user_id

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="oidc.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "oidc.db"
        self.database_url = self._database_template.copy_to(self.temp.name)
        self.engine = create_sql_engine(self.database_url)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _app(
        self,
        fake_client: FakeOidcClient,
        *,
        secure_cookie: bool = False,
        cookie_samesite: str = "lax",
        auto_provision: bool = False,
        claim_diagnostics: bool = False,
    ):
        runtime = OidcRuntime(
            client=fake_client,  # type: ignore[arg-type]
            cookie_name=COOKIE,
            session_hours=8,
            secure_cookie=secure_cookie,
            cookie_samesite=cookie_samesite,
            auto_provisioning=AutoProvisioningPolicy(enabled=auto_provision),
            claim_diagnostics_enabled=claim_diagnostics,
        )
        return create_api_app(
            self.database_url,
            auth_resolver=oidc_session_auth_resolver(COOKIE),
            oidc_runtime=runtime,
        )

    def _seed_erp_user(
        self,
        app,
        *,
        user_id: str = "ERPUSER42",
        employee_external_id: str = "EMP-42",
        local_active: bool = True,
        roles: tuple[str, ...] = (ROLE_ADMIN,),
    ) -> None:
        with app.state.session_factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id=user_id,
                    employee_external_id=employee_external_id,
                    display_name="Utilisateur ERP",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=local_active,
                    roles_json=json.dumps(list(roles)),
                )
            )

    def _login_callback(self, client: TestClient) -> object:
        login = client.get("/api/v1/auth/login", follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        return client.get(
            f"/api/v1/auth/callback?code=valid-code&state={state}",
            follow_redirects=False,
        )

    def test_login_callback_creates_opaque_session_then_logout_revokes_it(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Nom fournisseur ignoré pour les rôles",
                email=None,
            )
        )
        app = self._app(fake)
        with TestClient(app) as client:
            before = client.get("/api/v1/auth/me")
            login = client.get("/api/v1/auth/login", follow_redirects=False)
            self.assertEqual(login.status_code, 302)
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

            callback = client.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303)
            self.assertEqual(callback.headers["location"], "/")
            set_cookie = callback.headers["set-cookie"].lower()
            self.assertIn("httponly", set_cookie)
            self.assertIn("samesite=lax", set_cookie)

            session_token = client.cookies.get(COOKIE)
            self.assertIsNotNone(session_token)
            me = client.get("/api/v1/auth/me")
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["display_name"], "Technicien OIDC")
            self.assertEqual(me.json()["roles"], [ROLE_TECHNICIAN])
            self.assertEqual(me.json()["auth_mode"], "oidc")
            self.assertIsNone(me.json()["employee_external_id"])

            with app.state.session_factory.begin() as session:
                stored = session.scalar(select(AuthSession))
                self.assertIsNotNone(stored)
                assert stored is not None
                self.assertNotEqual(stored.token_hash, session_token)
                self.assertNotIn(session_token or "", stored.token_hash)

            csrf = client.cookies.get("resourceplanner_csrf")
            self.assertIsNotNone(csrf)
            logout = client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": str(csrf)},
            )
            self.assertEqual(logout.status_code, 204)
            after = client.get("/api/v1/auth/me")

        self.assertEqual(before.status_code, 401)
        self.assertEqual(before.json()["error"]["code"], "authentication_required")
        self.assertEqual(after.status_code, 401)

    def test_controlled_erp_first_login_creates_app_user_and_session_with_diagnostics_disabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-erp-controlled",
                display_name="Nom OIDC",
                email=None,
                preferred_username="  ERPUSER42  ",
            )
        )
        app = self._app(fake, claim_diagnostics=False)
        self._seed_erp_user(app)

        with TestClient(app) as client:
            callback = self._login_callback(client)
            me = client.get("/api/v1/auth/me")

        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["subject"], "subject-erp-controlled")
        self.assertEqual(me.json()["roles"], [ROLE_ADMIN])
        self.assertEqual(me.json()["employee_external_id"], "EMP-42")

        with app.state.session_factory() as session:
            rows = session.scalars(
                select(AppUser).where(
                    AppUser.subject == "subject-erp-controlled"
                )
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(rows[0].business_contact_id)

    def test_preferred_username_unknown_is_refused_even_when_legacy_auto_provision_is_enabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-not-in-directory",
                display_name="Utilisateur inconnu",
                email=None,
                preferred_username="UNKNOWN-ERP-USER",
            )
        )
        app = self._app(fake, auto_provision=True)

        with TestClient(app) as client:
            callback = self._login_callback(client)

        self.assertEqual(callback.status_code, 403)
        self.assertEqual(
            callback.json()["error"]["code"],
            "oidc_user_not_registered",
        )
        with app.state.session_factory() as session:
            self.assertIsNone(
                session.scalar(
                    select(AppUser).where(
                        AppUser.subject == "subject-not-in-directory"
                    )
                )
            )

    def test_cross_site_cookie_mode_sets_none_and_secure(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
            )
        )
        app = self._app(fake, secure_cookie=True, cookie_samesite="none")
        with TestClient(app, base_url="https://planner.example.invalid") as client:
            callback = self._login_callback(client)

        self.assertEqual(callback.status_code, 303)
        set_cookie = callback.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("secure", set_cookie)
        self.assertIn("samesite=none", set_cookie)

    def test_token_failure_diagnostics_are_absent_when_flag_is_disabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
            ),
            exchange_error=OidcProtocolError(
                "provider detail must stay hidden",
                failure_stage="token_endpoint",
                http_status=400,
                provider_error="invalid_grant",
            ),
        )
        app = self._app(fake)
        with TestClient(app) as client:
            callback = self._login_callback(client)

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(
            callback.json(),
            {
                "error": {
                    "code": "oidc_token_invalid",
                    "message": "La réponse OIDC n'a pas pu être validée.",
                    "context": {},
                }
            },
        )

    def test_token_failure_diagnostics_surface_only_safe_fields_when_enabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
            ),
            exchange_error=OidcProtocolError(
                "provider detail must stay hidden",
                failure_stage="token_endpoint",
                http_status=400,
                provider_error="invalid_grant",
            ),
        )
        app = self._app(fake, claim_diagnostics=True)
        with TestClient(app) as client:
            callback = self._login_callback(client)

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(callback.json()["error"]["code"], "oidc_token_invalid")
        self.assertEqual(
            callback.json()["error"]["context"],
            {
                "oidc_failure_stage": "token_endpoint",
                "http_status": 400,
                "provider_error": "invalid_grant",
            },
        )
        self.assertNotIn("provider detail", callback.text)

    def test_claim_diagnostic_route_is_not_registered_when_disabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
                diagnostic_claims=DIAGNOSTICS,
            )
        )
        app = self._app(fake)
        with TestClient(app) as client:
            callback = self._login_callback(client)
            self.assertEqual(callback.status_code, 303)
            response = client.get("/api/v1/auth/oidc-claims")

        self.assertEqual(response.status_code, 404)

    def test_claim_diagnostic_route_requires_valid_session_and_returns_projection(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
                diagnostic_claims=DIAGNOSTICS,
            )
        )
        app = self._app(fake, claim_diagnostics=True)
        with TestClient(app) as client:
            before = client.get("/api/v1/auth/oidc-claims")
            callback = self._login_callback(client)
            response = client.get("/api/v1/auth/oidc-claims")

        self.assertEqual(before.status_code, 401)
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DIAGNOSTICS)

    def test_unknown_identity_remains_refused_but_can_surface_opt_in_diagnostics(self) -> None:
        diagnostics = {
            "claim_names": list(DIAGNOSTICS["claim_names"]),
            "identity_candidates": {
                **DIAGNOSTICS["identity_candidates"],
                "sub": "unknown-subject",
            },
        }
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="unknown-subject",
                display_name="Utilisateur inconnu",
                email=None,
                diagnostic_claims=diagnostics,
            )
        )
        app = self._app(fake, claim_diagnostics=True)
        with TestClient(app) as client:
            callback = self._login_callback(client)
            me = client.get("/api/v1/auth/me")

        self.assertEqual(callback.status_code, 403)
        self.assertEqual(callback.json()["error"]["code"], "oidc_user_not_registered")
        self.assertEqual(
            callback.json()["error"]["context"]["oidc_claim_diagnostics"],
            diagnostics,
        )
        self.assertEqual(me.status_code, 401)

    def test_callback_rejects_identity_not_registered_locally_by_default(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="unknown-subject",
                display_name="Utilisateur inconnu",
                email=None,
            )
        )
        app = self._app(fake)
        with TestClient(app) as client:
            callback = self._login_callback(client)

        self.assertEqual(callback.status_code, 403)
        self.assertEqual(callback.json()["error"]["code"], "oidc_user_not_registered")

    def test_callback_can_auto_provision_unknown_identity_when_explicitly_enabled(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="auto-subject",
                display_name="Utilisateur auto",
                email=None,
            )
        )
        app = self._app(fake, auto_provision=True)
        with TestClient(app) as client:
            callback = self._login_callback(client)
            self.assertEqual(callback.status_code, 303)
            me = client.get("/api/v1/auth/me")
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["roles"], [ROLE_TECHNICIAN])
            self.assertEqual(me.json()["permissions"], ["read"])
            self.assertIsNone(me.json()["employee_external_id"])

        with app.state.session_factory() as session:
            stored = SqlUserIdentityRepository(session).get_by_external_identity(
                ISSUER,
                "auto-subject",
            )
            assert stored is not None
            self.assertEqual(stored.roles, (ROLE_TECHNICIAN,))

    def test_login_state_is_one_time_use(self) -> None:
        fake = FakeOidcClient(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-1",
                display_name="Technicien OIDC",
                email=None,
            )
        )
        app = self._app(fake)
        with TestClient(app) as client:
            login = client.get("/api/v1/auth/login", follow_redirects=False)
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
            first = client.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )
            second = client.get(
                f"/api/v1/auth/callback?code=valid-code&state={state}",
                follow_redirects=False,
            )

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"]["code"], "oidc_browser_binding_missing")

    def test_expired_and_revoked_sessions_do_not_resolve(self) -> None:
        now = datetime.now(timezone.utc)
        with self.factory.begin() as session:
            repository = SqlAuthSessionRepository(session)
            repository.create_session(
                raw_token="expired-token",
                csrf_token="expired-csrf",
                user_id=self.user_id,
                expires_at=now - timedelta(seconds=1),
            )
            repository.create_session(
                raw_token="revoked-token",
                csrf_token="revoked-csrf",
                user_id=self.user_id,
                expires_at=now + timedelta(hours=1),
            )
            repository.revoke_session("revoked-token")

        with self.factory.begin() as session:
            repository = SqlAuthSessionRepository(session)
            self.assertIsNone(repository.resolve_principal("expired-token"))
            self.assertIsNone(repository.resolve_principal("revoked-token"))


if __name__ == "__main__":
    unittest.main()
