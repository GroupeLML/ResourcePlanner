from __future__ import annotations

import json
import unittest

import httpx

from app.application.communications import CommunicationTransportMessage
from app.application.errors import ApplicationUnavailableError
from app.infrastructure.m365 import (
    MicrosoftGraphCommunicationSettings,
    MicrosoftGraphCommunicationTransport,
)


TEST_DOMAIN = "example.test"
TEST_MAILBOX = "planning" + chr(64) + TEST_DOMAIN


class MicrosoftGraphCommunicationTransportTests(unittest.TestCase):
    def _settings(self) -> MicrosoftGraphCommunicationSettings:
        return MicrosoftGraphCommunicationSettings(
            tenant_id="tenant-id",
            client_id="client-id",
            client_credential="test",
            mailbox=TEST_MAILBOX,
        )

    @staticmethod
    def _message(local_part: str) -> CommunicationTransportMessage:
        address = local_part + chr(64) + TEST_DOMAIN
        return CommunicationTransportMessage(
            audience="technician",
            recipient_id=f"resource:{local_part}",
            recipient_email=address,
            subject="Planning semaine prochaine",
            body="Bonjour\nVoici votre planning.",
            cc_emails=(
                "cc-" + local_part + chr(64) + TEST_DOMAIN,
            ),
            to_emails=(
                address,
                "co-" + local_part + chr(64) + TEST_DOMAIN,
            ),
        )

    def test_create_drafts_uses_app_only_token_and_never_calls_send(self) -> None:
        requests: list[httpx.Request] = []
        draft_number = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal draft_number
            requests.append(request)
            if request.url.path.endswith("/oauth2/v2.0/token"):
                body = request.content.decode("utf-8")
                self.assertIn("grant_type=client_credentials", body)
                self.assertIn("scope=https%3A%2F%2Fgraph.microsoft.com%2F.default", body)
                return httpx.Response(200, json={"access_token": "test-token"})
            if request.method == "POST" and request.url.path.endswith(f"/users/{TEST_MAILBOX}/messages"):
                draft_number += 1
                payload = json.loads(request.content)
                self.assertEqual(payload["body"]["contentType"], "Text")
                self.assertEqual(len(payload["toRecipients"]), 2)
                self.assertTrue(
                    all(
                        row["emailAddress"]["address"].endswith(TEST_DOMAIN)
                        for row in payload["toRecipients"]
                    )
                )
                self.assertEqual(len(payload["ccRecipients"]), 1)
                self.assertTrue(
                    payload["ccRecipients"][0]["emailAddress"]["address"].endswith(TEST_DOMAIN)
                )
                self.assertEqual(request.headers["Authorization"], "Bearer test-token")
                return httpx.Response(201, json={"id": f"draft-{draft_number}"})
            self.fail(f"Requête Graph inattendue: {request.method} {request.url}")

        adapter = MicrosoftGraphCommunicationTransport(
            self._settings(),
            transport=httpx.MockTransport(handler),
        )
        result = adapter.create_drafts((self._message("tech1"), self._message("tech2")))

        self.assertEqual(result.provider, "microsoft_graph")
        self.assertEqual(result.created_count, 2)
        self.assertEqual(sum("/messages" in request.url.path for request in requests), 2)
        self.assertFalse(any(request.url.path.endswith("/send") for request in requests))

    def test_partial_failure_attempts_to_delete_drafts_already_created(self) -> None:
        draft_posts = 0
        deleted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal draft_posts
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "test-token"})
            if request.method == "POST" and request.url.path.endswith("/messages"):
                draft_posts += 1
                if draft_posts == 1:
                    return httpx.Response(201, json={"id": "draft-1"})
                return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
            if request.method == "DELETE":
                deleted.append(request.url.path)
                return httpx.Response(204)
            self.fail(f"Requête Graph inattendue: {request.method} {request.url}")

        adapter = MicrosoftGraphCommunicationTransport(
            self._settings(),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ApplicationUnavailableError) as caught:
            adapter.create_drafts((self._message("tech1"), self._message("tech2")))

        self.assertEqual(caught.exception.code, "communication_graph_request_failed")
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0].endswith("/messages/draft-1"))

    def test_network_failure_is_translated_to_application_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "test-token"})
            raise httpx.ConnectError("graph unavailable", request=request)

        adapter = MicrosoftGraphCommunicationTransport(
            self._settings(),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ApplicationUnavailableError) as caught:
            adapter.create_drafts((self._message("tech"),))

        self.assertEqual(caught.exception.code, "communication_graph_unavailable")

    def test_invalid_success_payload_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "test-token"})
            return httpx.Response(201, text="not-json")

        adapter = MicrosoftGraphCommunicationTransport(
            self._settings(),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ApplicationUnavailableError) as caught:
            adapter.create_drafts((self._message("tech"),))

        self.assertEqual(caught.exception.code, "communication_graph_invalid_response")

    def test_settings_repr_never_exposes_client_credential(self) -> None:
        settings = self._settings()
        self.assertNotIn("client_credential='test'", repr(settings))


if __name__ == "__main__":
    unittest.main()
