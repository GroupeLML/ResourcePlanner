from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    ROLE_ADMIN,
    ROLE_PROJECT_MANAGER,
    AuthPrincipal,
)
from app.infrastructure.sql import AppUser, Base, ErpUserDirectoryEntry
from app.server import create_api_app
from app.server.security import static_auth_resolver


def principal(role: str, *, user_id: str = "admin-1") -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name="Administrateur test" if role == ROLE_ADMIN else "Utilisateur test",
        email=None,
        roles=(role,),
        auth_mode="test",
    )


class ServerUserAdminRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "user-admin.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, role: str = ROLE_ADMIN, *, seed_self: bool = True):
        app = create_api_app(
            f"sqlite+pysqlite:///{self.database_path.as_posix()}",
            auth_resolver=static_auth_resolver(principal(role)),
        )
        factory = app.state.session_factory
        Base.metadata.create_all(factory.kw["bind"])
        if seed_self:
            with factory() as session, session.begin():
                session.add(
                    AppUser(
                        id="admin-1",
                        issuer="urn:test",
                        subject="subject-admin-1",
                        display_name="Administrateur test",
                        email=None,
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    )
                )
        return app

    def test_admin_can_read_role_catalog_create_update_and_list_users(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            roles = client.get("/api/v1/admin/users/roles")
            created = client.post(
                "/api/v1/admin/users",
                json={
                    "issuer": "https://issuer.example.invalid",
                    "subject": "subject-42",
                    "display_name": "Utilisateur 42",
                    "email": None,
                    "phone": "514" + "-" + "555" + "-" + "0042",
                    "roles": [ROLE_PROJECT_MANAGER],
                    "active": True,
                },
            )
            user_id = created.json()["user_id"]
            updated = client.patch(
                f"/api/v1/admin/users/{user_id}",
                json={
                    "display_name": "Utilisateur modifié",
                    "email": None,
                    "phone": "450" + "-" + "555" + "-" + "0042",
                    "roles": [ROLE_PROJECT_MANAGER],
                    "active": False,
                },
            )
            listing = client.get("/api/v1/admin/users")

        self.assertEqual(roles.status_code, 200)
        admin_role = next(item for item in roles.json() if item["role"] == ROLE_ADMIN)
        self.assertIn("admin_users", admin_role["permissions"])
        self.assertIn("admin_settings", admin_role["permissions"])
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["issuer"], "https://issuer.example.invalid")
        self.assertIsNotNone(created.json()["business_contact_id"])
        self.assertEqual(created.json()["phone"], "514" + "-" + "555" + "-" + "0042")
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["active"])
        self.assertEqual(updated.json()["display_name"], "Utilisateur modifié")
        self.assertEqual(updated.json()["phone"], "450" + "-" + "555" + "-" + "0042")
        self.assertTrue(any(item["user_id"] == user_id for item in listing.json()))

    def test_pending_oidc_account_can_be_updated_by_app_user_id(self) -> None:
        app = self._app()
        factory = app.state.session_factory
        with factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERP-PENDING",
                    employee_external_id="EMP-PENDING",
                    display_name="Utilisateur pending",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json=json.dumps([ROLE_PROJECT_MANAGER]),
                )
            )
            session.add(
                AppUser(
                    id="pending-1",
                    issuer=None,
                    subject=None,
                    display_name="Utilisateur pending",
                    employee_external_id="EMP-PENDING",
                    erp_user_id="ERP-PENDING",
                    roles_json=json.dumps([ROLE_PROJECT_MANAGER]),
                    active=True,
                )
            )

        with TestClient(app) as client:
            response = client.patch(
                "/api/v1/admin/users/pending-1",
                json={
                    "display_name": "Utilisateur pending modifié",
                    "email": None,
                    "roles": [ROLE_PROJECT_MANAGER],
                    "active": True,
                },
            )
            listing = client.get("/api/v1/admin/users")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user_id"], "pending-1")
        self.assertIsNone(response.json()["issuer"])
        self.assertIsNone(response.json()["subject"])
        self.assertEqual(response.json()["erp_user_id"], "ERP-PENDING")
        row = next(item for item in listing.json() if item["user_id"] == "pending-1")
        self.assertIsNone(row["issuer"])
        self.assertIsNone(row["subject"])
        self.assertEqual(row["erp_user_id"], "ERP-PENDING")

    def test_non_admin_cannot_even_read_user_admin_surface(self) -> None:
        app = self._app(ROLE_PROJECT_MANAGER, seed_self=False)
        with TestClient(app) as client:
            response = client.get("/api/v1/admin/users")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(
            response.json()["error"]["context"]["required_permission"],
            "admin_users",
        )

    def test_admin_cannot_deactivate_or_remove_own_admin_role(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            deactivation = client.patch(
                "/api/v1/admin/users/admin-1",
                json={
                    "display_name": "Administrateur test",
                    "email": None,
                    "roles": [ROLE_ADMIN],
                    "active": False,
                },
            )
            role_removal = client.patch(
                "/api/v1/admin/users/admin-1",
                json={
                    "display_name": "Administrateur test",
                    "email": None,
                    "roles": [ROLE_PROJECT_MANAGER],
                    "active": True,
                },
            )

        self.assertEqual(deactivation.status_code, 409)
        self.assertEqual(deactivation.json()["error"]["code"], "user_admin_self_deactivation")
        self.assertEqual(role_removal.status_code, 409)
        self.assertEqual(role_removal.json()["error"]["code"], "user_admin_self_admin_removal")

    def test_duplicate_identity_and_invalid_role_are_structured_errors(self) -> None:
        app = self._app()
        payload = {
            "issuer": "https://issuer.example.invalid",
            "subject": "duplicate",
            "display_name": "Utilisateur",
            "email": None,
            "roles": [ROLE_PROJECT_MANAGER],
            "active": True,
        }
        with TestClient(app) as client:
            first = client.post("/api/v1/admin/users", json=payload)
            duplicate = client.post("/api/v1/admin/users", json=payload)
            invalid = client.post(
                "/api/v1/admin/users",
                json={**payload, "subject": "invalid-role", "roles": ["SUPERUSER"]},
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "user_admin_identity_exists")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "user_admin_invalid_roles")


if __name__ == "__main__":
    unittest.main()
