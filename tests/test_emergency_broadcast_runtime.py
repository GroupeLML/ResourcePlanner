from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.security import AuthPrincipal, ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.sql import AppUser, Base, create_sql_engine
from app.server.emergency_broadcast_runtime import PresenceRegistry
from app.server.http import create_api_app
from app.server.security import install_authorization_middleware, static_auth_resolver


class FakeClock:
    def __init__(self) -> None:
        self.mono = 100.0
        self.now = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.mono

    def utc_now(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


def principal(user_id: str, role: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=user_id,
        display_name=user_id,
        email=None,
        roles=(role,),
        auth_mode="local",
    )


class EmergencyPresenceTests(unittest.TestCase):
    def test_multi_tab_observation_aggregates_by_user_and_uses_60_second_activity(self) -> None:
        clock = FakeClock()
        presence = PresenceRegistry(monotonic=clock.monotonic, utc_now=clock.utc_now)

        self.assertTrue(presence.observe("MARY"))
        clock.advance(40)
        self.assertTrue(presence.observe("MARY"))
        self.assertEqual(presence.snapshot().tracked_count, 1)

        clock.advance(59)
        snapshot = presence.snapshot()
        self.assertEqual(snapshot.active_count, 1)
        self.assertTrue(snapshot.observations[0].active)

        clock.advance(2)
        snapshot = presence.snapshot()
        self.assertEqual(snapshot.active_count, 0)
        self.assertFalse(snapshot.observations[0].active)

    def test_presence_expires_after_fifteen_minutes_and_warmup_is_explicit(self) -> None:
        clock = FakeClock()
        presence = PresenceRegistry(monotonic=clock.monotonic, utc_now=clock.utc_now)
        presence.observe("MARY")

        self.assertTrue(presence.snapshot().warming)
        self.assertFalse(presence.snapshot().complete)

        clock.advance(61)
        self.assertFalse(presence.snapshot().warming)
        self.assertTrue(presence.snapshot().complete)

        clock.advance(15 * 60)
        snapshot = presence.snapshot()
        self.assertEqual(snapshot.tracked_count, 0)

    def test_capacity_overflow_never_evicts_an_active_user_as_complete(self) -> None:
        clock = FakeClock()
        presence = PresenceRegistry(monotonic=clock.monotonic, utc_now=clock.utc_now)
        presence.CAPACITY = 2

        self.assertTrue(presence.observe("A"))
        self.assertTrue(presence.observe("B"))
        self.assertFalse(presence.observe("C"))

        snapshot = presence.snapshot()
        self.assertEqual(snapshot.tracked_count, 2)
        self.assertFalse(snapshot.complete)


class EmergencyRuntimeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "runtime.db"
        self.database_url = f"sqlite+pysqlite:///{self.database_path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                AppUser.__table__.insert(),
                [
                    {
                        "id": "ADMIN-894",
                        "display_name": "Admin 894",
                        "roles_json": '["ADMIN"]',
                        "active": True,
                    },
                    {
                        "id": "USER-894",
                        "display_name": "User 894",
                        "roles_json": '["TECHNICIAN"]',
                        "active": True,
                    },
                ],
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(self, who: AuthPrincipal):
        return create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(who),
            api_docs_enabled=False,
        )

    def test_authenticated_status_records_presence_and_returns_active_broadcast(self) -> None:
        admin_app = self._app(principal("ADMIN-894", ROLE_ADMIN))
        with TestClient(admin_app) as admin:
            created = admin.post(
                "/api/v1/admin/runtime/broadcasts",
                json={
                    "creation_command_id": str(uuid4()),
                    "title": "AVIS IMPORTANT",
                    "message": "Sauvegardez votre travail.",
                    "confirmed": True,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            broadcast_id = created.json()["broadcast"]["id"]

        user_app = self._app(principal("USER-894", ROLE_TECHNICIAN))
        with TestClient(user_app) as user:
            status = user.get("/api/v1/runtime/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["user_id"], "USER-894")
            self.assertEqual(status.json()["broadcast"]["id"], broadcast_id)
            self.assertIsNone(status.json()["acknowledgment"])
            self.assertEqual(status.headers["cache-control"], "no-store, private")
            self.assertEqual(status.headers["vary"], "Cookie")

    def test_normal_user_cannot_use_admin_operations_but_can_ack_only_self(self) -> None:
        admin_app = self._app(principal("ADMIN-894", ROLE_ADMIN))
        with TestClient(admin_app) as admin:
            created = admin.post(
                "/api/v1/admin/runtime/broadcasts",
                json={
                    "creation_command_id": str(uuid4()),
                    "title": "Avis",
                    "message": "Message",
                    "confirmed": True,
                },
            )
            broadcast_id = created.json()["broadcast"]["id"]

        user_app = self._app(principal("USER-894", ROLE_TECHNICIAN))
        with TestClient(user_app) as user:
            denied = user.get("/api/v1/admin/runtime/status")
            self.assertEqual(denied.status_code, 403)

            ack = user.post(
                f"/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment",
                json={},
            )
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertNotIn("user_id", ack.json()["acknowledgment"])

            injected = user.post(
                f"/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment",
                json={"user_id": "ADMIN-894"},
            )
            self.assertEqual(injected.status_code, 422)
            self.assertEqual(
                injected.json()["error"]["code"],
                "request_validation_error",
            )

    def test_new_broadcast_does_not_reuse_old_positive_receipt(self) -> None:
        admin_app = self._app(principal("ADMIN-894", ROLE_ADMIN))
        user_app = self._app(principal("USER-894", ROLE_TECHNICIAN))
        with TestClient(admin_app) as admin, TestClient(user_app) as user:
            first = admin.post(
                "/api/v1/admin/runtime/broadcasts",
                json={
                    "creation_command_id": str(uuid4()),
                    "title": "A",
                    "message": "A",
                    "confirmed": True,
                },
            ).json()["broadcast"]["id"]
            user.post(f"/api/v1/runtime/broadcasts/{first}/acknowledgment", json={})
            admin.post(f"/api/v1/admin/runtime/broadcasts/{first}/end", json={})
            second = admin.post(
                "/api/v1/admin/runtime/broadcasts",
                json={
                    "creation_command_id": str(uuid4()),
                    "title": "B",
                    "message": "B",
                    "confirmed": True,
                },
            ).json()["broadcast"]["id"]

            status = user.get("/api/v1/runtime/status")
            self.assertEqual(status.json()["broadcast"]["id"], second)
            self.assertIsNone(status.json()["acknowledgment"])

    def test_ended_broadcast_acknowledgment_is_still_idempotent(self) -> None:
        admin_app = self._app(principal("ADMIN-894", ROLE_ADMIN))
        user_app = self._app(principal("USER-894", ROLE_TECHNICIAN))
        with TestClient(admin_app) as admin, TestClient(user_app) as user:
            broadcast_id = admin.post(
                "/api/v1/admin/runtime/broadcasts",
                json={
                    "creation_command_id": str(uuid4()),
                    "title": "A",
                    "message": "A",
                    "confirmed": True,
                },
            ).json()["broadcast"]["id"]
            admin.post(f"/api/v1/admin/runtime/broadcasts/{broadcast_id}/end", json={})

            first = user.post(
                f"/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment",
                json={},
            )
            second = user.post(
                f"/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment",
                json={},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(
                first.json()["acknowledgment"]["acknowledged_at"],
                second.json()["acknowledgment"]["acknowledged_at"],
            )


class EmergencyCsrfBoundaryTests(unittest.TestCase):
    def test_runtime_mutation_keeps_csrf_guard_for_session_auth(self) -> None:
        app = FastAPI()

        @app.post("/api/v1/runtime/broadcasts/B/acknowledgment")
        def command() -> dict[str, bool]:
            return {"ok": True}

        oidc = AuthPrincipal.from_roles(
            local_user_id="U",
            issuer="issuer",
            subject="subject",
            display_name="User",
            email=None,
            roles=(ROLE_TECHNICIAN,),
            auth_mode="oidc",
        )
        install_authorization_middleware(
            app,
            static_auth_resolver(oidc),
            csrf_guard=lambda _request: False,
        )

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/runtime/broadcasts/B/acknowledgment",
                json={},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_validation_failed")


if __name__ == "__main__":
    unittest.main()
