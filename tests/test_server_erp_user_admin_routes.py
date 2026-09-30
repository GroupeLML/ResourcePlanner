from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.erp_user_directory import ExternalErpUserRecord
from app.application.security import (
    ROLE_ADMIN,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    ErpUserDirectoryEntry,
    IdentityAdminAudit,
    SqlUserIdentityRepository,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


def erp_user(
    user_id: str,
    employee_id: str,
    *,
    active: bool = True,
    employee_status: str = "Actif",
) -> ExternalErpUserRecord:
    return ExternalErpUserRecord(
        user_id=user_id,
        employee_external_id=employee_id,
        display_name=f"Utilisateur {user_id}",
        email=f"{user_id.casefold()}@example.invalid",
        erp_user_active=active,
        employee_status=employee_status,
    )


class StubUserSource:
    def __init__(self, rows: tuple[ExternalErpUserRecord, ...] | None = None) -> None:
        self.rows = rows or (erp_user("ERP-ADMIN-CANDIDATE", "EMP-100"),)

    def list_users(self):
        return self.rows


def principal(role: str, *, auth_mode: str = "test", user_id: str | None = None) -> AuthPrincipal:
    local_user_id = user_id or ("admin-test" if role == ROLE_ADMIN else "pm-test")
    return AuthPrincipal.from_roles(
        local_user_id=local_user_id,
        issuer="urn:test",
        subject=f"subject-{local_user_id}",
        display_name="Test",
        email=None,
        roles=(role,),
        auth_mode=auth_mode,
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

    def _app(
        self,
        *,
        role: str = ROLE_ADMIN,
        auth_mode: str = "test",
        source: StubUserSource | None = None,
        seed_actor: bool = True,
    ):
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal(role, auth_mode=auth_mode)),
            user_source=source or StubUserSource(),
        )
        factory = app.state.session_factory
        Base.metadata.create_all(factory.kw["bind"])
        if seed_actor and role == ROLE_ADMIN:
            with factory.begin() as session:
                if session.get(AppUser, "admin-test") is None:
                    session.add(
                        AppUser(
                            id="admin-test",
                            issuer="urn:test",
                            subject="subject-admin-test",
                            display_name="Administrateur test",
                            roles_json=json.dumps([ROLE_ADMIN]),
                            active=True,
                        )
                    )
        return app

    def _sync_with_auth_mode(self, auth_mode: str):
        app = self._app(auth_mode=auth_mode, seed_actor=False)
        with TestClient(app) as client:
            response = client.post("/api/v1/integrations/acumatica/users/sync")
        return response

    def test_rp_users_sync_is_available_in_local_auth_mode(self) -> None:
        response = self._sync_with_auth_mode("local")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created"], 1)

    def test_rp_users_sync_is_available_in_oidc_auth_mode(self) -> None:
        response = self._sync_with_auth_mode("oidc")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created"], 1)

    def test_admin_activation_preprovisions_app_user_and_replay_is_idempotent(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            synced = client.post("/api/v1/integrations/acumatica/users/sync")
            before = client.get("/api/v1/admin/erp-users")
            first = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )
            replay = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        self.assertEqual(synced.status_code, 200, synced.text)
        self.assertIsNone(before.json()[0]["app_user_id"])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(first.json()["app_user_id"], replay.json()["app_user_id"])
        self.assertEqual(first.json()["oidc_state"], "pending")
        self.assertTrue(first.json()["local_active"])
        self.assertEqual(first.json()["roles"], [ROLE_TECHNICIAN])

        factory = app.state.session_factory
        with factory() as session:
            row = session.scalar(
                select(AppUser).where(
                    AppUser.erp_user_id == "ERP-ADMIN-CANDIDATE"
                )
            )
            assert row is not None
            self.assertEqual(row.id, first.json()["app_user_id"])
            self.assertEqual(row.employee_external_id, "EMP-100")
            self.assertIsNone(row.issuer)
            self.assertIsNone(row.subject)
            self.assertTrue(row.active)
            self.assertIsNotNone(row.business_contact_id)
            self.assertEqual(
                session.scalars(
                    select(AppUser).where(
                        AppUser.erp_user_id == "ERP-ADMIN-CANDIDATE"
                    )
                ).all().__len__(),
                1,
            )
            actions = session.scalars(
                select(IdentityAdminAudit.action).where(
                    IdentityAdminAudit.target_user_id == row.id
                )
            ).all()
            self.assertIn("APP_USER_PREPROVISIONED", actions)
            self.assertIn("APP_USER_ACTIVATED", actions)
            self.assertIn("APP_USER_ROLES_CHANGED", actions)

        app_user_id = first.json()["app_user_id"]
        with factory.begin() as session:
            bound = SqlUserIdentityRepository(session).bind_external_identity(
                app_user_id,
                "urn:test:oidc",
                "subject-erp-candidate",
            )
            self.assertEqual(bound.user_id, app_user_id)
            self.assertEqual(bound.erp_user_id, "ERP-ADMIN-CANDIDATE")

        with TestClient(app) as client:
            erp_projection = client.get("/api/v1/admin/erp-users")
            app_projection = client.get("/api/v1/admin/users")

        erp_row = next(
            item
            for item in erp_projection.json()
            if item["user_id"] == "ERP-ADMIN-CANDIDATE"
        )
        app_row = next(
            item for item in app_projection.json() if item["user_id"] == app_user_id
        )
        self.assertEqual(erp_row["oidc_state"], "linked")
        self.assertEqual(app_row["oidc_state"], "linked")
        self.assertEqual(erp_row["app_user_id"], app_row["app_user_id"])
        self.assertEqual(erp_row["erp_user_id"], app_row["erp_user_id"])
        self.assertEqual(erp_row["employee_external_id"], app_row["employee_external_id"])
        self.assertEqual(erp_row["active"], app_row["active"])
        self.assertEqual(erp_row["roles"], app_row["roles"])

    def test_roles_deactivation_and_reactivation_use_same_app_user(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            client.post("/api/v1/integrations/acumatica/users/sync")
            created = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )
            changed = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_PROJECT_MANAGER]},
            )
            disabled = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": False, "roles": [ROLE_PROJECT_MANAGER]},
            )
            reenabled = client.patch(
                "/api/v1/admin/erp-users/ERP-ADMIN-CANDIDATE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        user_id = created.json()["app_user_id"]
        self.assertEqual(changed.json()["app_user_id"], user_id)
        self.assertEqual(changed.json()["roles"], [ROLE_PROJECT_MANAGER])
        self.assertFalse(disabled.json()["local_active"])
        self.assertEqual(disabled.json()["app_user_id"], user_id)
        self.assertTrue(reenabled.json()["local_active"])
        self.assertEqual(reenabled.json()["app_user_id"], user_id)

    def test_ineligible_source_cannot_be_activated(self) -> None:
        source = StubUserSource(
            (erp_user("ERP-INACTIVE", "EMP-INACTIVE", employee_status="Inactif"),)
        )
        app = self._app(source=source)
        with TestClient(app) as client:
            client.post("/api/v1/integrations/acumatica/users/sync")
            response = client.patch(
                "/api/v1/admin/erp-users/ERP-INACTIVE",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "erp_user_source_ineligible")
        with app.state.session_factory() as session:
            self.assertIsNone(
                session.scalar(
                    select(AppUser).where(AppUser.erp_user_id == "ERP-INACTIVE")
                )
            )
            directory = session.get(ErpUserDirectoryEntry, "ERP-INACTIVE")
            assert directory is not None
            self.assertFalse(directory.local_active)

    def test_second_user_id_for_same_employee_is_explicit_conflict(self) -> None:
        source = StubUserSource(
            (
                erp_user("ERP-A", "EMP-SHARED"),
                erp_user("ERP-B", "EMP-SHARED"),
            )
        )
        app = self._app(source=source)
        with TestClient(app) as client:
            client.post("/api/v1/integrations/acumatica/users/sync")
            first = client.patch(
                "/api/v1/admin/erp-users/ERP-A",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )
            second = client.patch(
                "/api/v1/admin/erp-users/ERP-B",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(
            second.json()["error"]["code"],
            "erp_user_employee_identity_conflict",
        )

    def test_self_protection_applies_through_erp_surface(self) -> None:
        app = self._app(seed_actor=False)
        factory = app.state.session_factory
        with factory.begin() as session:
            session.add(
                ErpUserDirectoryEntry(
                    user_id="ERP-SELF",
                    employee_external_id="EMP-SELF",
                    display_name="Administrateur test",
                    erp_user_active=True,
                    employee_status="Actif",
                    local_active=True,
                    roles_json=json.dumps([ROLE_ADMIN]),
                )
            )
            session.add(
                AppUser(
                    id="admin-test",
                    issuer="urn:test",
                    subject="subject-admin-test",
                    display_name="Administrateur test",
                    employee_external_id="EMP-SELF",
                    erp_user_id="ERP-SELF",
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )

        with TestClient(app) as client:
            deactivation = client.patch(
                "/api/v1/admin/erp-users/ERP-SELF",
                json={"active": False, "roles": [ROLE_ADMIN]},
            )
            role_removal = client.patch(
                "/api/v1/admin/erp-users/ERP-SELF",
                json={"active": True, "roles": [ROLE_TECHNICIAN]},
            )

        self.assertEqual(deactivation.status_code, 409)
        self.assertEqual(
            deactivation.json()["error"]["code"],
            "user_admin_self_deactivation",
        )
        self.assertEqual(role_removal.status_code, 409)
        self.assertEqual(
            role_removal.json()["error"]["code"],
            "user_admin_self_admin_removal",
        )

    def test_non_admin_cannot_read_or_mutate_erp_user_directory(self) -> None:
        app = self._app(role=ROLE_PROJECT_MANAGER, seed_actor=False)
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
