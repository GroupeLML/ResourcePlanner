from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class ApiTests(unittest.TestCase):
    def test_health_and_config_never_expose_token_value(self):
        secret = "test"
        client = TestClient(create_app(Settings(github_token=secret)))
        health = client.get("/api/health")
        config = client.get("/api/config")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(config.status_code, 200)
        combined = health.text + config.text
        self.assertNotIn(secret, combined)
        payload = config.json()
        self.assertTrue(payload["token_configured"])
        self.assertEqual(payload["roadmap_issue"], 55)
        self.assertEqual(payload["canonical_pipeline_issue"], 851)
        self.assertEqual(payload["canonical_pipeline_source_mode"], "DEDICATED_STRICT")

    def test_dashboard_without_token_returns_useful_configuration_error(self):
        client = TestClient(create_app(Settings(github_token=None)))
        response = client.get("/api/dashboard")
        self.assertEqual(response.status_code, 503)
        self.assertIn("DEV_COCKPIT_GITHUB_TOKEN", response.json()["detail"])
        self.assertEqual(client.get("/api/health").json()["status"], "configuration_error")

    def test_flow_analytics_without_token_returns_configuration_error(self):
        client = TestClient(create_app(Settings(github_token=None)))
        response = client.get("/api/flow-analytics")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Flow Analytics", response.json()["detail"])

    def test_writeback_without_token_returns_configuration_error(self):
        client = TestClient(create_app(Settings(github_token=None)))

        preview = client.post("/api/roadmap-writeback/preview")
        self.assertEqual(preview.status_code, 503)
        self.assertIn("Issues en écriture", preview.json()["detail"])

        apply = client.post(
            "/api/roadmap-writeback/apply",
            json={
                "expected_updated_at": "2026-09-24T19:30:00Z",
                "expected_body_sha256": "a" * 64,
                "expected_proposal_sha256": "b" * 64,
                "confirm": True,
            },
        )
        self.assertEqual(apply.status_code, 503)
        self.assertIn("Issues en écriture", apply.json()["detail"])

    def test_roles_can_be_managed_without_github_token(self):
        with TemporaryDirectory() as temp_dir:
            client = TestClient(
                create_app(Settings(github_token=None, data_dir=temp_dir))
            )
            defaults = client.get("/api/roles")
            self.assertEqual(defaults.status_code, 200)
            self.assertEqual(defaults.json()["roles"][1]["id"], "developer")

            updated = client.put(
                "/api/roles",
                json={
                    "roles": [
                        {
                            "id": "developer",
                            "name": "DEV #13",
                            "avatar": "developer",
                            "chat_url": "https://chatgpt.com/c/example",
                            "enabled": True,
                            "order": 10,
                        }
                    ]
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["roles"][0]["name"], "DEV #13")

            second_client = TestClient(
                create_app(Settings(github_token=None, data_dir=temp_dir))
            )
            persisted = second_client.get("/api/roles")
            self.assertEqual(
                persisted.json()["roles"][0]["chat_url"],
                "https://chatgpt.com/c/example",
            )

    def test_chat_status_heartbeat_does_not_require_github_token(self):
        client = TestClient(create_app(Settings(github_token=None)))
        heartbeat = client.post(
            "/api/chat-status/heartbeat",
            json={
                "conversation_url": "https://chatgpt.com/c/example?model=test",
                "state": "working",
                "page_visible": True,
                "page_focused": False,
                "ui_signal": "stop-control",
            },
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(
            heartbeat.json()["conversation_url"],
            "https://chatgpt.com/c/example",
        )
        self.assertEqual(heartbeat.json()["effective_state"], "working")

        status = client.get("/api/chat-status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(
            status.json()["conversations"][0]["effective_state"],
            "working",
        )


if __name__ == "__main__":
    unittest.main()
