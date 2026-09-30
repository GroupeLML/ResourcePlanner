from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlparse
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.erp_user_directory import ExternalErpUserRecord
from app.application.identity_provisioning import (
    AUDIT_OIDC_IDENTITY_LINKED,
    AutoProvisioningPolicy,
)
from app.application.security import AuthPrincipal, ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.acumatica.oidc import OidcIdentity
from app.infrastructure.sql import (
    AppUser,
    AuthSession,
    Base,
    ErpUserDirectoryEntry,
    IdentityAdminAudit,
    Resource,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.oidc import OidcRuntime, oidc_session_auth_resolver
from app.server.security import static_auth_resolver


ISSUER = "https://identity.example.invalid"
COOKIE = "rp_identity_f_session"


class MutableUserSource:
    def __init__(self, rows: tuple[ExternalErpUserRecord, ...]) -> None:
        self.rows = rows

    def list_users(self):
        return self.rows


class FakeOidcClient:
    def __init__(self, identity: OidcIdentity) -> None:
        self.identity = identity
        self.last_verifier: str | None = None
        self.last_nonce: str | None = None

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> str:
        self.last_verifier = code_verifier
        self.last_nonce = nonce
        return "https://identity.example.invalid/authorize?" + urlencode(
            {
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
            }
        )

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> OidcIdentity:
        if code != "valid-code":
            raise ValueError("invalid code")
        if code_verifier != self.last_verifier or nonce != self.last_nonce:
            raise ValueError("invalid transaction")
        return self.identity


def erp_user(
    *,
    display_name: str = "Utilisateur ERP initial",
    erp_user_active: bool = True,
    employee_status: str = "Actif",
) -> ExternalErpUserRecord:
    return ExternalErpUserRecord(
        user_id="ERP-USER",
        employee_external_id="EMP-USER",
        display_name=display_name,
        first_name="Utilisateur",
        last_name="ERP",
        email="erp.user" + chr(64) + "example.invalid",
        erp_user_active=erp_user_active,
        employee_status=employee_status,
    )


class IdentityTransversalAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_url = (
            "sqlite+pysqlite:///"
            + (Path(self.temp.name) / "identity-f.db").as_posix()
        )
        engine = create_sql_engine(self.database_url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _admin_app(self, source: MutableUserSource):
        principal = AuthPrincipal.from_roles(
            local_user_id="identity-f-admin",
            issuer="urn:test:admin",
            subject="identity-f-admin",
            display_name="Administrateur IDENTITY-F",
            email=None,
            roles=(ROLE_ADMIN,),
            auth_mode="test",
        )
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal),
            user_source=source,
        )
        with app.state.session_factory.begin() as session:
            if session.get(AppUser, "identity-f-admin") is None:
                session.add(
                    AppUser(
                        id="identity-f-admin",
                        issuer="urn:test:admin",
                        subject="identity-f-admin",
                        display_name="Administrateur IDENTITY-F",
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    )
                )
        return app

    def _oidc_app(self, identity: OidcIdentity):
        fake = FakeOidcClient(identity)
        runtime = OidcRuntime(
            client=fake,  # type: ignore[arg-type]
            cookie_name=COOKIE,
            session_hours=8,
            secure_cookie=False,
            cookie_samesite="lax",
            auto_provisioning=AutoProvisioningPolicy(enabled=False),
            claim_diagnostics_enabled=False,
        )
        app = create_api_app(
            self.database_url,
            auth_resolver=oidc_session_auth_resolver(COOKIE),
            oidc_runtime=runtime,
        )
        return app

    def _login_callback(self, client: TestClient):
        login = client.get("/api/v1/auth/login", follow_redirects=False)
        self.assertEqual(login.status_code, 302, login.text)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        return client.get(
            f"/api/v1/auth/callback?code=valid-code&state={state}",
            follow_redirects=False,
        )

    def test_admin_to_oidc_to_admin_to_erp_resync_preserves_stable_identity(self) -> None:
        source = MutableUserSource((erp_user(),))
        admin_app = self._admin_app(source)

        with TestClient(admin_app) as client:
            sync = client.post("/api/v1/integrations/acumatica/users/sync")
            before = client.get("/api/v1/admin/erp-users")
            provisioned = client.patch(
                "/api/v1/admin/erp-users/ERP-USER",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )
            app_users = client.get("/api/v1/admin/users")

        self.assertEqual(sync.status_code, 200, sync.text)
        self.assertEqual(sync.json()["created"], 1)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertIsNone(before.json()[0]["app_user_id"])
        self.assertEqual(provisioned.status_code, 200, provisioned.text)
        self.assertEqual(provisioned.json()["oidc_state"], "pending")
        self.assertTrue(provisioned.json()["active"])
        self.assertEqual(provisioned.json()["roles"], [ROLE_TECHNICIAN])

        target_id = provisioned.json()["app_user_id"]
        app_row = next(
            row for row in app_users.json() if row["app_user_id"] == target_id
        )
        self.assertEqual(app_row["erp_user_id"], "ERP-USER")
        self.assertEqual(app_row["employee_external_id"], "EMP-USER")
        self.assertTrue(app_row["active"])
        self.assertEqual(app_row["roles"], [ROLE_TECHNICIAN])
        self.assertIsNone(app_row["issuer"])
        self.assertIsNone(app_row["subject"])
        self.assertEqual(app_row["oidc_state"], "pending")
        business_contact_id = app_row["business_contact_id"]
        self.assertIsNotNone(business_contact_id)

        oidc_app = self._oidc_app(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-erp-user",
                display_name="Nom OIDC non autoritaire",
                email="oidc" + chr(64) + "example.invalid",
                preferred_username=" ERP-USER ",
            )
        )
        with TestClient(oidc_app) as client:
            first_callback = self._login_callback(client)
            me = client.get("/api/v1/auth/me")

        self.assertEqual(first_callback.status_code, 303, first_callback.text)
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["local_user_id"], target_id)
        self.assertEqual(me.json()["roles"], [ROLE_TECHNICIAN])
        self.assertEqual(me.json()["employee_external_id"], "EMP-USER")
        self.assertEqual(me.json()["issuer"], ISSUER)
        self.assertEqual(me.json()["subject"], "subject-erp-user")

        with oidc_app.state.session_factory() as session:
            linked = session.get(AppUser, target_id)
            assert linked is not None
            self.assertEqual(linked.id, target_id)
            self.assertEqual(linked.erp_user_id, "ERP-USER")
            self.assertEqual(linked.employee_external_id, "EMP-USER")
            self.assertEqual(linked.business_contact_id, business_contact_id)
            self.assertEqual(json.loads(linked.roles_json), [ROLE_TECHNICIAN])
            self.assertTrue(linked.active)
            self.assertEqual(linked.issuer, ISSUER)
            self.assertEqual(linked.subject, "subject-erp-user")
            sessions = session.scalars(
                select(AuthSession).where(AuthSession.user_id == target_id)
            ).all()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].auth_mode, "oidc")
            audits = session.scalars(
                select(IdentityAdminAudit).where(
                    IdentityAdminAudit.target_user_id == target_id,
                    IdentityAdminAudit.action == AUDIT_OIDC_IDENTITY_LINKED,
                )
            ).all()
            self.assertEqual(len(audits), 1)
            self.assertIsNone(
                session.scalar(
                    select(Resource).where(Resource.external_id == "EMP-USER")
                )
            )

        with TestClient(oidc_app) as client:
            replay_callback = self._login_callback(client)
            replay_me = client.get("/api/v1/auth/me")

        self.assertEqual(replay_callback.status_code, 303, replay_callback.text)
        self.assertEqual(replay_me.status_code, 200, replay_me.text)
        self.assertEqual(replay_me.json()["local_user_id"], target_id)

        with oidc_app.state.session_factory() as session:
            self.assertEqual(
                len(
                    session.scalars(
                        select(AppUser).where(AppUser.erp_user_id == "ERP-USER")
                    ).all()
                ),
                1,
            )
            self.assertEqual(
                len(
                    session.scalars(
                        select(IdentityAdminAudit).where(
                            IdentityAdminAudit.target_user_id == target_id,
                            IdentityAdminAudit.action == AUDIT_OIDC_IDENTITY_LINKED,
                        )
                    ).all()
                ),
                1,
            )
            self.assertEqual(
                len(
                    session.scalars(
                        select(AuthSession).where(AuthSession.user_id == target_id)
                    ).all()
                ),
                2,
            )

        with TestClient(admin_app) as client:
            changed = client.patch(
                "/api/v1/admin/erp-users/ERP-USER",
                json={"active": False, "roles": [ROLE_ADMIN]},
            )
            linked_projection = client.get("/api/v1/admin/users")

        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse(changed.json()["active"])
        self.assertEqual(changed.json()["roles"], [ROLE_ADMIN])
        self.assertEqual(changed.json()["oidc_state"], "linked")
        post_admin = next(
            row
            for row in linked_projection.json()
            if row["app_user_id"] == target_id
        )
        self.assertFalse(post_admin["active"])
        self.assertEqual(post_admin["oidc_state"], "linked")
        self.assertEqual(post_admin["issuer"], ISSUER)
        self.assertEqual(post_admin["subject"], "subject-erp-user")
        self.assertEqual(post_admin["business_contact_id"], business_contact_id)

        source.rows = (
            erp_user(
                display_name="Nom ERP rafraîchi",
                erp_user_active=False,
                employee_status="Inactif",
            ),
        )
        with TestClient(admin_app) as client:
            resync = client.post("/api/v1/integrations/acumatica/users/sync")
            erp_projection = client.get("/api/v1/admin/erp-users")

        self.assertEqual(resync.status_code, 200, resync.text)
        self.assertEqual(resync.json()["updated"], 1)
        projected = next(
            row for row in erp_projection.json() if row["user_id"] == "ERP-USER"
        )
        self.assertFalse(projected["source_admissible"])
        self.assertFalse(projected["active"])
        self.assertEqual(projected["roles"], [ROLE_ADMIN])
        self.assertEqual(projected["oidc_state"], "linked")
        self.assertEqual(projected["app_user_id"], target_id)

        with admin_app.state.session_factory() as session:
            final = session.get(AppUser, target_id)
            assert final is not None
            self.assertEqual(final.id, target_id)
            self.assertEqual(final.erp_user_id, "ERP-USER")
            self.assertEqual(final.employee_external_id, "EMP-USER")
            self.assertEqual(final.business_contact_id, business_contact_id)
            self.assertEqual(json.loads(final.roles_json), [ROLE_ADMIN])
            self.assertFalse(final.active)
            self.assertEqual(final.issuer, ISSUER)
            self.assertEqual(final.subject, "subject-erp-user")
            directory = session.get(ErpUserDirectoryEntry, "ERP-USER")
            assert directory is not None
            self.assertEqual(directory.display_name, "Nom ERP rafraîchi")
            self.assertFalse(directory.erp_user_active)
            self.assertEqual(directory.employee_status, "Inactif")
            self.assertEqual(
                {
                    row.user_id
                    for row in session.scalars(
                        select(AuthSession).where(AuthSession.user_id == target_id)
                    ).all()
                },
                {target_id},
            )

    def test_oidc_callback_refuses_active_account_without_local_role(self) -> None:
        engine = create_sql_engine(self.database_url)
        try:
            with engine.begin() as connection:
                pass
        finally:
            engine.dispose()

        app = self._oidc_app(
            OidcIdentity(
                issuer=ISSUER,
                subject="subject-no-role",
                display_name="Sans rôle",
                email=None,
                preferred_username="ERP-NO-ROLE",
            )
        )
        with app.state.session_factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERP-NO-ROLE",
                    employee_external_id="EMP-NO-ROLE",
                    display_name="Sans rôle",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json="[]",
                )
            )
            session.add(
                AppUser(
                    id="U-NO-ROLE",
                    issuer=None,
                    subject=None,
                    display_name="Sans rôle",
                    employee_external_id="EMP-NO-ROLE",
                    erp_user_id="ERP-NO-ROLE",
                    roles_json="[]",
                    active=True,
                )
            )

        with TestClient(app) as client:
            callback = self._login_callback(client)
            me = client.get("/api/v1/auth/me")

        self.assertEqual(callback.status_code, 403, callback.text)
        self.assertEqual(
            callback.json()["error"]["code"],
            "oidc_user_not_registered",
        )
        self.assertEqual(me.status_code, 401)
        with app.state.session_factory() as session:
            row = session.get(AppUser, "U-NO-ROLE")
            assert row is not None
            self.assertIsNone(row.issuer)
            self.assertIsNone(row.subject)
            self.assertEqual(row.roles_json, "[]")
            self.assertTrue(row.active)
            self.assertIsNone(
                session.scalar(
                    select(AuthSession).where(AuthSession.user_id == "U-NO-ROLE")
                )
            )


if __name__ == "__main__":
    unittest.main()
