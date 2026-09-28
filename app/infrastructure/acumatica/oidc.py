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
    pass


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
        metadata = await self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        if self.settings.client_credential:
            form["client_secret"] = self.settings.client_credential

        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            response = await client.post(
                str(metadata["token_endpoint"]),
                data=form,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            token = response.json()
        if not isinstance(token, dict):
            raise OidcProtocolError("La réponse token OIDC doit être un objet JSON.")
        id_token = str(token.get("id_token") or "").strip()
        if not id_token:
            raise OidcProtocolError("Acumatica n'a retourné aucun id_token OIDC.")

        jwks = await self._get_json(str(metadata["jwks_uri"]))
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
                "Le fournisseur OIDC ne publie aucun algorithme de signature sûr pour l'id_token."
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
            claims.validate(leeway=120)
        except (AuthlibJoseError, JoseRfcError) as exc:
            raise OidcProtocolError("L'id_token OIDC est invalide.") from exc

        issuer = str(claims.get("iss") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        if not issuer or not subject:
            raise OidcProtocolError("L'id_token OIDC ne contient pas iss/sub.")
        display_name = str(
            claims.get("name")
            or claims.get("preferred_username")
            or claims.get("email")
            or subject
        ).strip()
        email = str(claims.get("email") or "").strip() or None
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
            diagnostic_claims=diagnostic_claims,
        )
