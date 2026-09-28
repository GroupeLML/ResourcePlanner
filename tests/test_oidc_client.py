from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
import unittest

import httpx
from authlib.jose import JsonWebKey, JsonWebToken

from app.infrastructure.acumatica.oidc import (
    OidcClient,
    OidcClientSettings,
    OidcProtocolError,
    pkce_s256,
)


ISSUER = "https://identity.example.invalid"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
CLIENT_ID = "resourceplanner-test"
REDIRECT_URI = "https://planner.example.invalid/api/v1/auth/callback"


class OidcClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = JsonWebKey.generate_key(
            "RSA",
            2048,
            options={"kid": "test-key", "use": "sig"},
            is_private=True,
        )
        self.public_jwk = self.key.as_dict(is_private=False)

    def _id_token(
        self,
        *,
        nonce: str,
        extra_claims: dict[str, object] | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        payload: dict[str, object] = {
            "iss": ISSUER,
            "sub": "subject-123",
            "aud": CLIENT_ID,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "nonce": nonce,
            "name": "Utilisateur OIDC",
            "email": "person" + chr(64) + "example.invalid",
        }
        if extra_claims:
            payload.update(extra_claims)
        encoded = JsonWebToken(["RS256"]).encode(
            {"alg": "RS256", "kid": "test-key", "typ": "JWT"},
            payload,
            self.key,
        )
        return encoded.decode("ascii") if isinstance(encoded, bytes) else str(encoded)

    def _transport(
        self,
        *,
        nonce: str,
        algorithms: list[str] | None = None,
        extra_claims: dict[str, object] | None = None,
        token_response_extra: dict[str, object] | None = None,
    ) -> httpx.MockTransport:
        token = self._id_token(nonce=nonce, extra_claims=extra_claims)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == DISCOVERY:
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/jwks",
                        "id_token_signing_alg_values_supported": algorithms or ["RS256"],
                    },
                )
            if str(request.url) == f"{ISSUER}/token":
                body = request.content.decode("utf-8")
                self.assertIn("grant_type=authorization_code", body)
                self.assertIn("code_verifier=verifier-123", body)
                token_response: dict[str, object] = {"id_token": token}
                token_response.update(token_response_extra or {})
                return httpx.Response(200, json=token_response)
            if str(request.url) == f"{ISSUER}/jwks":
                return httpx.Response(200, json={"keys": [self.public_jwk]})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def _client(
        self,
        *,
        nonce: str,
        algorithms: list[str] | None = None,
        claim_diagnostics: bool = False,
        extra_claims: dict[str, object] | None = None,
        token_response_extra: dict[str, object] | None = None,
    ) -> OidcClient:
        return OidcClient(
            OidcClientSettings(
                discovery_url=DISCOVERY,
                client_id=CLIENT_ID,
                client_credential=None,
                redirect_uri=REDIRECT_URI,
            ),
            transport=self._transport(
                nonce=nonce,
                algorithms=algorithms,
                extra_claims=extra_claims,
                token_response_extra=token_response_extra,
            ),
            claim_diagnostics=claim_diagnostics,
        )

    def test_authorization_url_uses_code_flow_nonce_and_pkce_s256(self) -> None:
        client = self._client(nonce="nonce-123")
        url = asyncio.run(
            client.authorization_url(
                state="state-123",
                nonce="nonce-123",
                code_verifier="verifier-123",
            )
        )
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["state"], ["state-123"])
        self.assertEqual(query["nonce"], ["nonce-123"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [pkce_s256("verifier-123")])

    def test_exchange_validates_signed_id_token_and_returns_identity(self) -> None:
        client = self._client(nonce="nonce-123")
        identity = asyncio.run(
            client.exchange_code(
                code="code-123",
                code_verifier="verifier-123",
                nonce="nonce-123",
            )
        )

        self.assertEqual(identity.issuer, ISSUER)
        self.assertEqual(identity.subject, "subject-123")
        self.assertEqual(identity.display_name, "Utilisateur OIDC")
        self.assertEqual(identity.email, "person" + chr(64) + "example.invalid")
        self.assertIsNone(identity.diagnostic_claims)

    def test_claim_diagnostics_exposes_filtered_validated_claims_when_enabled(self) -> None:
        client = self._client(
            nonce="nonce-123",
            claim_diagnostics=True,
            extra_claims={
                "preferred_username": "ERPUSER42",
                "unique_name": "unique-user-42",
                "username": "erp.username42",
                "user_id": "erp-user-42",
                "userid": "legacy-user-42",
                "employee_id": "EMPLOYEE42",
                "access_token": "test",
                "refresh_token": "test",
                "id_token": "test",
                "client_secret": "test",
                "authorization_code": "test",
                "code": "test",
                "session_cookie": "test",
                "csrf_token": "test",
                "pkce_verifier": "test",
                "state": "test",
            },
            token_response_extra={
                "access_token": "test",
                "refresh_token": "test",
            },
        )
        identity = asyncio.run(
            client.exchange_code(
                code="code-123",
                code_verifier="verifier-123",
                nonce="nonce-123",
            )
        )

        diagnostics = identity.diagnostic_claims
        self.assertIsNotNone(diagnostics)
        assert diagnostics is not None
        self.assertEqual(
            diagnostics["identity_candidates"],
            {
                "iss": ISSUER,
                "sub": "subject-123",
                "preferred_username": "ERPUSER42",
                "name": "Utilisateur OIDC",
                "email": "person" + chr(64) + "example.invalid",
                "unique_name": "unique-user-42",
                "username": "erp.username42",
                "user_id": "erp-user-42",
                "userid": "legacy-user-42",
                "employee_id": "EMPLOYEE42",
            },
        )
        self.assertEqual(diagnostics["claim_names"], sorted(diagnostics["claim_names"]))
        self.assertIn("aud", diagnostics["claim_names"])
        self.assertIn("nonce", diagnostics["claim_names"])
        for forbidden_name in (
            "access_token",
            "refresh_token",
            "id_token",
            "client_secret",
            "authorization_code",
            "code",
            "session_cookie",
            "csrf_token",
            "pkce_verifier",
            "state",
        ):
            self.assertNotIn(forbidden_name, diagnostics["claim_names"])

        self.assertEqual(
            set(diagnostics),
            {"claim_names", "identity_candidates"},
        )
        self.assertTrue(
            set(diagnostics["identity_candidates"]).issubset(
                {
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
                }
            )
        )

    def test_claim_diagnostics_ignores_absent_optional_identity_claims(self) -> None:
        client = self._client(nonce="nonce-123", claim_diagnostics=True)
        identity = asyncio.run(
            client.exchange_code(
                code="code-123",
                code_verifier="verifier-123",
                nonce="nonce-123",
            )
        )

        diagnostics = identity.diagnostic_claims
        self.assertIsNotNone(diagnostics)
        assert diagnostics is not None
        candidates = diagnostics["identity_candidates"]
        self.assertNotIn("preferred_username", candidates)
        self.assertNotIn("unique_name", candidates)
        self.assertNotIn("username", candidates)
        self.assertNotIn("user_id", candidates)
        self.assertNotIn("userid", candidates)
        self.assertNotIn("employee_id", candidates)

    def test_exchange_rejects_wrong_nonce(self) -> None:
        client = self._client(
            nonce="nonce-from-provider",
            claim_diagnostics=True,
        )
        with self.assertRaises(OidcProtocolError):
            asyncio.run(
                client.exchange_code(
                    code="code-123",
                    code_verifier="verifier-123",
                    nonce="different-nonce",
                )
            )

    def test_exchange_refuses_none_signing_algorithm(self) -> None:
        client = self._client(nonce="nonce-123", algorithms=["none"])
        with self.assertRaises(OidcProtocolError):
            asyncio.run(
                client.exchange_code(
                    code="code-123",
                    code_verifier="verifier-123",
                    nonce="nonce-123",
                )
            )


if __name__ == "__main__":
    unittest.main()
