from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import base64
import hashlib
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError as AuthlibJoseError
from authlib.oidc.core import CodeIDToken
from joserfc.errors import JoseError as JoseRfcError


class OidcProtocolError(RuntimeError):
    _DIAGNOSTIC_FAILURE_STAGES = frozenset(
        {
            "discovery",
            "token_endpoint",
            "token_response_invalid",
            "id_token_missing",
            "jwks_fetch",
            "id_token_decode",
            "id_token_validation",
            "unexpected",
        }
    )
    _SAFE_PROVIDER_ERROR_CHARS = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    )

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str | None = None,
        http_status: int | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message)
        normalized_stage = str(failure_stage or "").strip()
        self.failure_stage = (
            normalized_stage
            if normalized_stage in self._DIAGNOSTIC_FAILURE_STAGES
            else ("unexpected" if normalized_stage else None)
        )
        self.http_status = int(http_status) if http_status is not None else None

        normalized_provider_error = str(provider_error or "").strip()
        self.provider_error = (
            normalized_provider_error
            if normalized_provider_error
            and len(normalized_provider_error) <= 64
            and all(
                character in self._SAFE_PROVIDER_ERROR_CHARS
                for character in normalized_provider_error
            )
            else None
        )

    def diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {}
        if self.failure_stage:
            context["oidc_failure_stage"] = self.failure_stage
        if self.http_status is not None:
            context["http_status"] = self.http_status
        if self.provider_error:
            context["provider_error"] = self.provider_error
        return context


_DIAGNOSTIC_IDENTITY_CLAIMS = (
    "iss",
    "sub",
    "preferred_username",
    "name",
    "email",
    "unique_name",
    "username",
    "user_id",
    "userid",
    "employee_id",
)
_DIAGNOSTIC_FORBIDDEN_CLAIM_NAMES = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "authorization_code",
        "code",
        "session_cookie",
        "cookie",
        "csrf_token",
        "code_verifier",
        "pkce_verifier",
        "state",
    }
)

_SAFE_OAUTH_ERROR_CODE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


def _safe_oauth_error_code(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("error")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        return None
    if any(character not in _SAFE_OAUTH_ERROR_CODE_CHARS for character in normalized):
        return None
    return normalized


def _claim_diagnostics_projection(claims: Mapping[str, Any]) -> dict[str, Any]:
    claim_names = sorted(
        str(name)
        for name in claims.keys()
        if str(name).casefold() not in _DIAGNOSTIC_FORBIDDEN_CLAIM_NAMES
    )
    identity_candidates: dict[str, str] = {}
    for name in _DIAGNOSTIC_IDENTITY_CLAIMS:
        if name not in claims:
            continue
        value = claims.get(name)
        if not isinstance(value, (str, int, float, bool)):
            continue
        normalized = str(value).strip()
        if normalized:
            identity_candidates[name] = normalized
    return {
        "claim_names": claim_names,
        "identity_candidates": identity_candidates,
    }


@dataclass(frozen=True, slots=True)
class OidcClientSettings:
    discovery_url: str
    client_id: str
    client_credential: str | None
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "profile", "email")


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    issuer: str
    subject: str
    display_name: str
    email: str | None
    preferred_username: str | None = None
    diagnostic_claims: dict[str, Any] | None = None


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OidcClient:
    def __init__(
        self,
        settings: OidcClientSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        claim_diagnostics: bool = False,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._timeout = timeout
        self._claim_diagnostics = bool(claim_diagnostics)
        self._metadata: dict[str, Any] | None = None

    async def _get_json(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise OidcProtocolError("La réponse OIDC attendue doit être un objet JSON.")
        return payload

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            metadata = await self._get_json(self.settings.discovery_url)
            required = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
            missing = [key for key in required if not str(metadata.get(key) or "").strip()]
            if missing:
                raise OidcProtocolError(
                    "Le document de découverte OIDC est incomplet: " + ", ".join(missing)
                )
            self._metadata = metadata
        return dict(self._metadata)

    async def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        metadata = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": " ".join(self.settings.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_s256(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{metadata['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> OidcIdentity:
        try:
            metadata = await self.metadata()
        except (httpx.HTTPError, OidcProtocolError, ValueError) as exc:
            raise OidcProtocolError(
                "Le document de découverte OIDC n'a pas pu être chargé.",
                failure_stage="discovery",
            ) from exc

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        if self.settings.client_credential:
            form["client_secret"] = self.settings.client_credential

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                response = await client.post(
                    str(metadata["token_endpoint"]),
                    data=form,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise OidcProtocolError(
                "Le token endpoint OIDC n'a pas pu être joint.",
                failure_stage="token_endpoint",
            ) from exc

        token_http_status = response.status_code
        if not response.is_success:
            provider_payload: object = None
            try:
                provider_payload = response.json()
            except ValueError:
                pass
            raise OidcProtocolError(
                "Le token endpoint OIDC a refusé la requête.",
                failure_stage="token_endpoint",
                http_status=token_http_status,
                provider_error=_safe_oauth_error_code(provider_payload),
            )

        try:
            token = response.json()
        except ValueError as exc:
            raise OidcProtocolError(
                "La réponse token OIDC n'est pas un JSON valide.",
                failure_stage="token_response_invalid",
                http_status=token_http_status,
            ) from exc
        if not isinstance(token, dict):
            raise OidcProtocolError(
                "La réponse token OIDC doit être un objet JSON.",
                failure_stage="token_response_invalid",
                http_status=token_http_status,
            )

        provider_error = _safe_oauth_error_code(token)
        if provider_error:
            raise OidcProtocolError(
                "La réponse token OIDC contient une erreur fournisseur.",
                failure_stage="token_response_invalid",
                http_status=token_http_status,
                provider_error=provider_error,
            )

        id_token = str(token.get("id_token") or "").strip()
        if not id_token:
            raise OidcProtocolError(
                "Acumatica n'a retourné aucun id_token OIDC.",
                failure_stage="id_token_missing",
                http_status=token_http_status,
            )

        try:
            jwks = await self._get_json(str(metadata["jwks_uri"]))
        except (httpx.HTTPError, OidcProtocolError, ValueError) as exc:
            raise OidcProtocolError(
                "Les clés de signature OIDC n'ont pas pu être chargées.",
                failure_stage="jwks_fetch",
                http_status=token_http_status,
            ) from exc

        advertised = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        if not isinstance(advertised, list) or not advertised:
            advertised = ["RS256"]
        algorithms = [
            str(item).strip()
            for item in advertised
            if str(item).strip() and str(item).strip().casefold() != "none"
        ]
        if not algorithms:
            raise OidcProtocolError(
                "Le fournisseur OIDC ne publie aucun algorithme de signature sûr pour l'id_token.",
                failure_stage="id_token_validation",
                http_status=token_http_status,
            )

        try:
            decoder = JsonWebToken(algorithms)
            claims = decoder.decode(
                id_token,
                key=jwks,
                claims_cls=CodeIDToken,
                claims_options={
                    "iss": {"essential": True, "values": [str(metadata["issuer"])]},
                    "sub": {"essential": True},
                    "aud": {"essential": True},
                    "exp": {"essential": True},
                },
                claims_params={
                    "nonce": nonce,
                    "client_id": self.settings.client_id,
                    "access_token": token.get("access_token"),
                },
            )
        except (AuthlibJoseError, JoseRfcError, ValueError) as exc:
            raise OidcProtocolError(
                "L'id_token OIDC est invalide.",
                failure_stage="id_token_decode",
                http_status=token_http_status,
            ) from exc

        try:
            claims.validate(leeway=120)
        except (AuthlibJoseError, JoseRfcError, ValueError) as exc:
            raise OidcProtocolError(
                "L'id_token OIDC est invalide.",
                failure_stage="id_token_validation",
                http_status=token_http_status,
            ) from exc

        issuer = str(claims.get("iss") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        if not issuer or not subject:
            raise OidcProtocolError(
                "L'id_token OIDC ne contient pas iss/sub.",
                failure_stage="id_token_validation",
                http_status=token_http_status,
            )
        display_name = str(
            claims.get("name")
            or claims.get("preferred_username")
            or claims.get("email")
            or subject
        ).strip()
        email = str(claims.get("email") or "").strip() or None
        preferred_username = str(claims.get("preferred_username") or "").strip() or None
        diagnostic_claims = (
            _claim_diagnostics_projection(claims)
            if self._claim_diagnostics
            else None
        )
        return OidcIdentity(
            issuer=issuer,
            subject=subject,
            display_name=display_name,
            email=email,
            preferred_username=preferred_username,
            diagnostic_claims=diagnostic_claims,
        )
