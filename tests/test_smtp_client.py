from __future__ import annotations

import smtplib
import unittest
from unittest.mock import patch

from app.application.communications import CommunicationTransportMessage
from app.application.smtp_settings import (
    SMTP_SECURITY_SSL_TLS,
    SMTP_SECURITY_STARTTLS,
    SmtpRuntimeConfiguration,
)
from app.infrastructure.smtp import SmtpClient


TEST_DOMAIN = "example.test"


class FakeSmtpConnection:
    def __init__(self) -> None:
        self.ehlo_count = 0
        self.starttls_count = 0
        self.login_calls = []
        self.sent_messages = []
        self.noop_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        self.ehlo_count += 1
        return 250, b"ok"

    def starttls(self, *, context):
        self.starttls_count += 1
        return 220, b"ready"

    def login(self, username, credential):
        self.login_calls.append((username, credential))
        return 235, b"authenticated"

    def noop(self):
        self.noop_count += 1
        return 250, b"ok"

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None

    def send_message(self, message):
        self.sent_messages.append(message)


class SmtpClientTests(unittest.TestCase):
    @staticmethod
    def _configuration(security: str) -> SmtpRuntimeConfiguration:
        return SmtpRuntimeConfiguration(
            host="smtp.example.invalid",
            port=587 if security == SMTP_SECURITY_STARTTLS else 465,
            security=security,
            username="mailer",
            credential="fixture-" + "credential",
            from_email="planning" + chr(64) + TEST_DOMAIN,
            from_name="RessourcePlanner",
            reply_to="reply" + chr(64) + TEST_DOMAIN,
            timeout_seconds=20,
        )

    @staticmethod
    def _message() -> CommunicationTransportMessage:
        return CommunicationTransportMessage(
            audience="project",
            recipient_id="project:P1",
            recipient_email="pm" + chr(64) + TEST_DOMAIN,
            subject="Confirmation projet",
            body="Bonjour\nPlanning confirmé.",
            cc_emails=(
                "tech-a" + chr(64) + TEST_DOMAIN,
                "tech-b" + chr(64) + TEST_DOMAIN,
            ),
            to_emails=(
                "pm" + chr(64) + TEST_DOMAIN,
                "co-manager" + chr(64) + TEST_DOMAIN,
            ),
        )

    def test_starttls_send_builds_expected_headers_and_authenticates(self) -> None:
        connection = FakeSmtpConnection()
        with patch("app.infrastructure.smtp.smtplib.SMTP", return_value=connection):
            result = SmtpClient().send_message(
                self._configuration(SMTP_SECURITY_STARTTLS),
                self._message(),
                message_id="<fixture-message" + chr(64) + "local.invalid>",
            )

        self.assertEqual(result, "<fixture-message" + chr(64) + "local.invalid>")
        self.assertEqual(connection.starttls_count, 1)
        self.assertEqual(connection.ehlo_count, 2)
        self.assertEqual(
            connection.login_calls,
            [("mailer", "fixture-" + "credential")],
        )
        self.assertEqual(len(connection.sent_messages), 1)
        sent = connection.sent_messages[0]
        self.assertIn("pm" + chr(64) + TEST_DOMAIN, sent["To"])
        self.assertIn("co-manager" + chr(64) + TEST_DOMAIN, sent["To"])
        self.assertIn("tech-a" + chr(64) + TEST_DOMAIN, sent["Cc"])
        self.assertIn("tech-b" + chr(64) + TEST_DOMAIN, sent["Cc"])
        self.assertEqual(sent["Reply-To"], "reply" + chr(64) + TEST_DOMAIN)
        self.assertEqual(
            sent["Message-ID"],
            "<fixture-message" + chr(64) + "local.invalid>",
        )

    def test_ssl_tls_uses_smtp_ssl_and_connection_test_does_not_send(self) -> None:
        connection = FakeSmtpConnection()
        with patch(
            "app.infrastructure.smtp.smtplib.SMTP_SSL",
            return_value=connection,
        ) as smtp_ssl:
            result = SmtpClient().test_connection(
                self._configuration(SMTP_SECURITY_SSL_TLS)
            )

        smtp_ssl.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Connexion SMTP réussie.")
        self.assertTrue(any(entry.step == "tls" for entry in result.log))
        self.assertTrue(any(entry.step == "authentification" for entry in result.log))
        self.assertEqual(connection.starttls_count, 0)
        self.assertEqual(connection.noop_count, 1)
        self.assertEqual(connection.sent_messages, [])

    def test_connection_failure_returns_sanitized_authentication_log(self) -> None:
        connection = FakeSmtpConnection()

        def reject_login(username, credential):
            connection.login_calls.append((username, credential))
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        connection.login = reject_login
        with patch(
            "app.infrastructure.smtp.smtplib.SMTP",
            return_value=connection,
        ):
            result = SmtpClient().test_connection(
                self._configuration(SMTP_SECURITY_STARTTLS)
            )

        self.assertFalse(result.ok)
        self.assertIn("authentification", result.message)
        failure = result.log[-1]
        self.assertEqual(failure.level, "ERROR")
        self.assertEqual(failure.step, "authentification")
        self.assertIn("SMTP 535", failure.message)
        self.assertNotIn("fixture-" + "credential", failure.message)


if __name__ == "__main__":
    unittest.main()
