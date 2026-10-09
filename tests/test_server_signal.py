"""Tests for the opt-in, authenticated, schema-hidden maintenance signal."""

from __future__ import annotations

import base64
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER, TEST_COORDINATOR_AUTH_RESOLVER


SIGNAL_PATH = "/api/v1/_signal/3a46"
EXPECTED_SIGNAL = "fH4DODM9NDViAx0rW188cFk7LGcmKDpIHW8aXSgqVydnaWA="
EXPECTED_DECODED_HEX = (
    "2e2e2e6b7a7a7a742e2e2e6a6f696e2074686520"
    "686976652e2e2e6b7a7a7a742e2e2e"
)


class OptionalSignalTests(unittest.TestCase):
    def test_authorized_get_returns_only_the_stable_opaque_signal(self) -> None:
        app = create_api_app(
            "sqlite+pysqlite:///:memory:", auth_resolver=TEST_ADMIN_AUTH_RESOLVER
        )
        with TestClient(app) as client:
            result = client.get(SIGNAL_PATH)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["content-type"], "application/json")
            self.assertEqual(result.json(), {"signal": EXPECTED_SIGNAL})
            self.assertEqual(client.get("/health").json(), {"status": "ok", "api": "v1"})

    def test_read_authorized_non_admin_can_access(self) -> None:
        app = create_api_app(
            "sqlite+pysqlite:///:memory:", auth_resolver=TEST_COORDINATOR_AUTH_RESOLVER
        )
        with TestClient(app) as client:
            self.assertEqual(client.get(SIGNAL_PATH).status_code, 200)

    def test_unauthenticated_get_is_rejected(self) -> None:
        app = create_api_app(
            "sqlite+pysqlite:///:memory:", auth_resolver=static_auth_resolver(None)
        )
        with TestClient(app) as client:
            response = client.get(SIGNAL_PATH)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    def test_route_not_present_in_openapi(self) -> None:
        app = create_api_app(
            "sqlite+pysqlite:///:memory:", auth_resolver=TEST_ADMIN_AUTH_RESOLVER
        )
        paths = app.openapi()["paths"]
        self.assertNotIn(SIGNAL_PATH, paths)
        self.assertIn("/health", paths)
        self.assertIn("/ready", paths)

    def test_documented_xor_base64_round_trip_has_exact_expected_bytes(self) -> None:
        key = b"RP-SIGNAL-3A46"
        binary = base64.urlsafe_b64decode(EXPECTED_SIGNAL)
        decoded = bytes(value ^ key[i % len(key)] for i, value in enumerate(binary))
        self.assertEqual(decoded.hex(), EXPECTED_DECODED_HEX)
        self.assertEqual(decoded.decode("utf-8").encode("utf-8"), decoded)

    def test_get_does_not_execute_sql(self) -> None:
        app = create_api_app(
            "sqlite+pysqlite:///:memory:", auth_resolver=TEST_ADMIN_AUTH_RESOLVER
        )
        with TestClient(app) as client:
            engine = app.state.session_factory.kw["bind"]
            statements: list[str] = []

            def track_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", track_sql)
            try:
                response = client.get(SIGNAL_PATH)
            finally:
                event.remove(engine, "before_cursor_execute", track_sql)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(statements, [])


if __name__ == "__main__":
    unittest.main()
