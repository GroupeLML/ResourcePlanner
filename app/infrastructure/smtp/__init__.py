from __future__ import annotations

from email.message import EmailMessage
import smtplib
import ssl

from cryptography.fernet import Fernet

from ...application.communications import CommunicationTransportMessage
from ...application.smtp_settings import (
    SMTP_SECURITY_SSL_TLS,
    SmtpConnectionTestResult,
    SmtpRuntimeConfiguration,
    SmtpTestLogEntry,
)


class FernetSecretCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(str(key).strip().encode("ascii"))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(str(value).encode("ascii")).decode("utf-8")


def _smtp_error_message(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        raw = exc.smtp_error
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        text = " ".join(text.split())
        if len(text) > 180:
            text = text[:177] + "..."
        return f"SMTP {exc.smtp_code}: {text}" if text else f"SMTP {exc.smtp_code}"
    text = " ".join(str(exc or "").split())
    if len(text) > 180:
        text = text[:177] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class SmtpClient:
    @staticmethod
    def _connect(configuration: SmtpRuntimeConfiguration):
        timeout = float(configuration.timeout_seconds)
        if configuration.security == SMTP_SECURITY_SSL_TLS:
            return smtplib.SMTP_SSL(
                configuration.host,
                configuration.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        client = smtplib.SMTP(
            configuration.host,
            configuration.port,
            timeout=timeout,
        )
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        return client

    @staticmethod
    def _login(client, configuration: SmtpRuntimeConfiguration) -> None:
        if configuration.username:
            client.login(
                configuration.username,
                configuration.credential or "",
            )

    def test_connection(
        self,
        configuration: SmtpRuntimeConfiguration,
    ) -> SmtpConnectionTestResult:
        log: list[SmtpTestLogEntry] = [
            SmtpTestLogEntry(
                level="INFO",
                step="configuration",
                message=(
                    f"Serveur={configuration.host}, port={configuration.port}, "
                    f"sécurité={configuration.security}, délai={configuration.timeout_seconds}s, "
                    f"authentification={'oui' if configuration.username else 'non'}."
                ),
            )
        ]
        client = None
        step = "connexion"
        try:
            timeout = float(configuration.timeout_seconds)
            if configuration.security == SMTP_SECURITY_SSL_TLS:
                log.append(
                    SmtpTestLogEntry(
                        level="INFO",
                        step="connexion",
                        message="Ouverture de la connexion TCP avec TLS implicite.",
                    )
                )
                client = smtplib.SMTP_SSL(
                    configuration.host,
                    configuration.port,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                )
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="tls",
                        message="Connexion SSL/TLS établie.",
                    )
                )
                step = "ehlo"
                code, _response = client.ehlo()
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="ehlo",
                        message=f"EHLO accepté (code {code}).",
                    )
                )
            else:
                log.append(
                    SmtpTestLogEntry(
                        level="INFO",
                        step="connexion",
                        message="Ouverture de la connexion TCP SMTP.",
                    )
                )
                client = smtplib.SMTP(
                    configuration.host,
                    configuration.port,
                    timeout=timeout,
                )
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="connexion",
                        message="Connexion TCP établie.",
                    )
                )
                step = "ehlo"
                code, _response = client.ehlo()
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="ehlo",
                        message=f"EHLO accepté avant STARTTLS (code {code}).",
                    )
                )
                step = "starttls"
                log.append(
                    SmtpTestLogEntry(
                        level="INFO",
                        step="starttls",
                        message="Négociation STARTTLS.",
                    )
                )
                code, _response = client.starttls(context=ssl.create_default_context())
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="starttls",
                        message=f"STARTTLS accepté (code {code}).",
                    )
                )
                step = "ehlo"
                code, _response = client.ehlo()
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="ehlo",
                        message=f"EHLO accepté après STARTTLS (code {code}).",
                    )
                )

            step = "authentification"
            if configuration.username:
                log.append(
                    SmtpTestLogEntry(
                        level="INFO",
                        step="authentification",
                        message="Authentification SMTP en cours avec le compte configuré.",
                    )
                )
                code, _response = client.login(
                    configuration.username,
                    configuration.credential or "",
                )
                log.append(
                    SmtpTestLogEntry(
                        level="SUCCESS",
                        step="authentification",
                        message=f"Authentification acceptée (code {code}).",
                    )
                )
            else:
                log.append(
                    SmtpTestLogEntry(
                        level="INFO",
                        step="authentification",
                        message="Authentification non configurée; étape ignorée.",
                    )
                )

            step = "noop"
            code, _response = client.noop()
            if not 200 <= int(code) < 400:
                raise smtplib.SMTPResponseException(code, b"NOOP rejected")
            log.append(
                SmtpTestLogEntry(
                    level="SUCCESS",
                    step="noop",
                    message=f"NOOP accepté (code {code}).",
                )
            )
            log.append(
                SmtpTestLogEntry(
                    level="SUCCESS",
                    step="résultat",
                    message="Connexion SMTP validée avec succès.",
                )
            )
            return SmtpConnectionTestResult(
                ok=True,
                message="Connexion SMTP réussie.",
                log=tuple(log),
            )
        except Exception as exc:
            log.append(
                SmtpTestLogEntry(
                    level="ERROR",
                    step=step,
                    message=_smtp_error_message(exc),
                )
            )
            return SmtpConnectionTestResult(
                ok=False,
                message=f"Échec du test SMTP à l'étape « {step} ».",
                log=tuple(log),
            )
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass

    def send_message(
        self,
        configuration: SmtpRuntimeConfiguration,
        message: CommunicationTransportMessage,
        *,
        message_id: str,
    ) -> str:
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = (
            f"{configuration.from_name} <{configuration.from_email}>"
            if configuration.from_name
            else configuration.from_email
        )
        email["To"] = ", ".join(
            message.to_emails or (message.recipient_email,)
        )
        if message.cc_emails:
            email["Cc"] = ", ".join(message.cc_emails)
        if configuration.reply_to:
            email["Reply-To"] = configuration.reply_to
        email["Message-ID"] = message_id
        email.set_content(message.body)

        with self._connect(configuration) as client:
            self._login(client, configuration)
            client.send_message(email)
        return message_id
