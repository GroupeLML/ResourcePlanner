from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.erp_user_directory import ExternalErpUserRecord
from app.application.security import ROLE_ADMIN, ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN, AuthPrincipal
from app.infrastructure.sql import AppUser, Base, ErpUserDirectoryEntry
from app.server import create_api_app
from app.server.security import static_auth_resolver


class StubUserSource:
    def list_users(self):
        return (
            ExternalErpUserRecord(
                user_id="ERP-ADMIN-CANDIDATE",
                employee_external_id="EMP-100",
                display_name="Utilisateur ERP candidat",
                email="candidate" + chr(64) + "example.invalid",
                erp_user_active=True,
                employee_status="Actif",
            ),
        )


def principal(role: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id="admin-test" if role == ROLE_ADMIN else "pm-test",
        issuer="urn:test",
        subject="subject-test",
        display_name="Test",
        email=None,
        roles=(role,),
        auth_mode="test",
    )


class ServerErpUserAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_url = (
            "sqlite+pysqlite:///"
            + (Path(self.temp.name) / "erp-users.db").as_posix()
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_admin_syncs_then_configures_local_access_without_app_user_provisioning(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal(ROLE_ADMIN)),
            user_source=StubUserSource(),
        )
        Base.metadata.create_all(app.state.session_factory.kw["bind"])
        with TestClient(app) as client:
            synced = client.post("/api/v1/integrations/acumatica/users/sync")
            listing = client.get("/api/v1/admin/erp-users")
            updated = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        self.assertEqual(synced.status_code, 200, synced.text)
        self.assertEqual(
            synced.json(),
            {
                "received": 1,
                "created": 1,
                "updated": 0,
                "unchanged": 0,
                "errors": 0,
            },
        )
        self.assertEqual(listing.status_code, 200)
        row = listing.json()[0]
        self.assertEqual(row["user_id"], "ERP-ADMIN-CANDIDATE")
        self.assertEqual(row["employee_external_id"], "EMP-100")
        self.assertTrue(row["erp_user_active"])
        self.assertTrue(row["source_admissible"])
        self.assertFalse(row["local_active"])
        self.assertEqual(row["roles"], [])
        self.assertEqual(row["oidc_state"], "pending")
        self.assertFalse(row["access_ready"])

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(updated.json()["local_active"])
        self.assertEqual(updated.json()["roles"], [ROLE_TECHNICIAN])
        self.assertEqual(updated.json()["oidc_state"], "pending")
        self.assertTrue(updated.json()["access_ready"])

        factory = app.state.session_factory
        with factory() as session:
            self.assertIsNotNone(
                session.scalar(
                    select(ErpUserDirectoryEntry).where(
                        ErpUserDirectoryEntry.user_id == "ERP-ADMIN-CANDIDATE"
                    )
                )
            )
            self.assertIsNone(session.scalar(select(AppUser)))

    def test_non_admin_cannot_read_or_mutate_erp_user_directory(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal(ROLE_PROJECT_MANAGER)),
            user_source=StubUserSource(),
        )
        Base.metadata.create_all(app.state.session_factory.kw["bind"])
        with TestClient(app) as client:
            listing = client.get("/api/v1/admin/erp-users")
            update = client.patch(
                "/api/v1/admin/erp-users/ERP-X",
                json={"active": False, "roles": []},
            )

        self.assertEqual(listing.status_code, 403)
        self.assertEqual(update.status_code, 403)
        self.assertEqual(
            listing.json()["error"]["context"]["required_permission"],
            "admin_users",
        )


if __name__ == "__main__":
    unittest.main()
