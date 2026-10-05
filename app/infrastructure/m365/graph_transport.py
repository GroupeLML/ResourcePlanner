from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import quote

import httpx

from ...application.communications import (
    CommunicationTransportMessage,
    CommunicationTransportPort,
    CommunicationTransportResult,
)
from ...application.errors import ApplicationUnavailableError


@dataclass(frozen=True, slots=True)
class MicrosoftGraphCommunicationSettings:
    tenant_id: str
    client_id: str
    client_credential: str = field(repr=False)
    mailbox: str
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    authority_host: str = "https://login.microsoftonline.com"
    timeout_seconds: float = 20.0

    def safe_summary(self) -> dict[str, object]:
        return {
            "provider": "microsoft_graph",
            "mailbox": self.mailbox,
            "graph_base_url": self.graph_base_url,
            "configured": True,
        }


class MicrosoftGraphCommunicationTransport(CommunicationTransportPort):
    """Create M365 draft messages through Microsoft Graph; never sends mail."""

    def __init__(
        self,
        settings: MicrosoftGraphCommunicationSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http_transport = transport

    def create_drafts(
        self,
        messages: Sequence[CommunicationTransportMessage],
    ) -> CommunicationTransportResult:
        rows = tuple(messages)
        if not rows:
            return CommunicationTransportResult(provider="microsoft_graph", created_count=0)

        with httpx.Client(
            timeout=self._settings.timeout_seconds,
            transport=self._http_transport,
        ) as client:
            token = self._access_token(client)
            created_ids: list[str] = []
            try:
                for message in rows:
                    response = client.post(
                        self._messages_url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json=self._message_payload(message),
                    )
                    self._raise_for_graph_error(response, operation="création du brouillon")
                    created_ids.append(self._draft_id(response))
            except httpx.HTTPError as exc:
                self._rollback_created_drafts(client, token=token, draft_ids=created_ids)
                raise ApplicationUnavailableError(
                    "Impossible de joindre Microsoft Graph pour créer les brouillons M365.",
                    code="communication_graph_unavailable",
                ) from exc
            except Exception:
                self._rollback_created_drafts(client, token=token, draft_ids=created_ids)
                raise

        return CommunicationTransportResult(
            provider="microsoft_graph",
            created_count=len(created_ids),
        )

    @property
    def _messages_url(self) -> str:
        mailbox = quote(self._settings.mailbox, safe="")
        return f"{self._settings.graph_base_url.rstrip('/')}/users/{mailbox}/messages"

    def _access_token(self, client: httpx.Client) -> str:
        tenant = quote(self._settings.tenant_id, safe="")
        url = (
            f"{self._settings.authority_host.rstrip('/')}/{tenant}/oauth2/v2.0/token"
        )
        try:
            response = client.post(
                url,
                data={
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_credential,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApplicationUnavailableError(
                "Impossible de joindre Microsoft Entra pour obtenir un jeton M365.",
                code="communication_graph_auth_unavailable",
            ) from exc

        if response.is_error:
            raise ApplicationUnavailableError(
                f"Microsoft Entra a refusé l'authentification M365 (HTTP {response.status_code}).",
                code="communication_graph_auth_failed",
                context={"status_code": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApplicationUnavailableError(
                "La réponse d'authentification Microsoft Entra est invalide.",
                code="communication_graph_auth_invalid_response",
            ) from exc
        token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
        if not token:
            raise ApplicationUnavailableError(
                "Microsoft Entra n'a pas retourné de jeton d'accès M365.",
                code="communication_graph_auth_invalid_response",
            )
        return token

    @staticmethod
    def _message_payload(message: CommunicationTransportMessage) -> dict[str, object]:
        return {
            "subject": message.subject,
            "body": {
                "contentType": "Text",
                "content": message.body,
            },
            "toRecipients": [
                {"emailAddress": {"address": address}}
                for address in (
                    message.to_emails
                    or (message.recipient_email,)
                )
            ],
            "ccRecipients": [
                {"emailAddress": {"address": address}}
                for address in message.cc_emails
            ],
        }

    @staticmethod
    def _draft_id(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApplicationUnavailableError(
                "La réponse de création du brouillon Microsoft Graph est invalide.",
                code="communication_graph_invalid_response",
            ) from exc
        draft_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        if not draft_id:
            raise ApplicationUnavailableError(
                "Microsoft Graph n'a pas retourné l'identifiant du brouillon créé.",
                code="communication_graph_invalid_response",
            )
        return draft_id

    @staticmethod
    def _graph_error_code(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None
        code = str(error.get("code") or "").strip()
        return code or None

    def _raise_for_graph_error(self, response: httpx.Response, *, operation: str) -> None:
        if not response.is_error:
            return
        graph_code = self._graph_error_code(response)
        context: dict[str, object] = {"status_code": response.status_code}
        if graph_code:
            context["graph_code"] = graph_code
        request_id = str(response.headers.get("request-id") or "").strip()
        if request_id:
            context["request_id"] = request_id
        raise ApplicationUnavailableError(
            f"Microsoft Graph a refusé la {operation} (HTTP {response.status_code}).",
            code="communication_graph_request_failed",
            context=context,
        )

    def _rollback_created_drafts(
        self,
        client: httpx.Client,
        *,
        token: str,
        draft_ids: Sequence[str],
    ) -> None:
        mailbox = quote(self._settings.mailbox, safe="")
        base = self._settings.graph_base_url.rstrip("/")
        for draft_id in reversed(tuple(draft_ids)):
            try:
                client.delete(
                    f"{base}/users/{mailbox}/messages/{quote(draft_id, safe='')}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                # Best effort only. The explicit SQL audit is written only after
                # the full transport call succeeds, so the operator can reconcile
                # a rare partial failure in M365 before retrying.
                pass
