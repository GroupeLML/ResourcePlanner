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
        token_status: int = 200,
        token_response_json: object | None = None,
        token_response_text: str | None = None,
        omit_id_token: bool = False,
        id_token_override: str | None = None,
        jwks_status: int = 200,
    ) -> httpx.MockTransport:
        token = id_token_override or self._id_token(nonce=nonce, extra_claims=extra_claims)

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
                if token_response_text is not None:
                    return httpx.Response(token_status, text=token_response_text)
                if token_response_json is not None:
                    return httpx.Response(token_status, json=token_response_json)
                token_response: dict[str, object] = {}
                if not omit_id_token:
                    token_response["id_token"] = token
                token_response.update(token_response_extra or {})
                return httpx.Response(token_status, json=token_response)
            if str(request.url) == f"{ISSUER}/jwks":
                return httpx.Response(jwks_status, json={"keys": [self.public_jwk]})
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
        token_status: int = 200,
        token_response_json: object | None = None,
        token_response_text: str | None = None,
        omit_id_token: bool = False,
        id_token_override: str | None = None,
        jwks_status: int = 200,
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
                token_status=token_status,
                token_response_json=token_response_json,
                token_response_text=token_response_text,
                omit_id_token=omit_id_token,
                id_token_override=id_token_override,
                jwks_status=jwks_status,
            ),
            claim_diagnostics=claim_diagnostics,
        )

    def _exchange_error(
        self,
        client: OidcClient,
        *,
        nonce: str = "nonce-123",
    ) -> OidcProtocolError:
        with self.assertRaises(OidcProtocolError) as caught:
            asyncio.run(
                client.exchange_code(
                    code="code-123",
                    code_verifier="verifier-123",
                    nonce=nonce,
                )
            )
        return caught.exception

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
        self.assertIsNone(identity.preferred_username)
        self.assertIsNone(identity.diagnostic_claims)

    def test_preferred_username_is_available_when_claim_diagnostics_are_disabled(self) -> None:
        client = self._client(
            nonce="nonce-123",
            extra_claims={"preferred_username": "  ERPUSER42  "},
        )
        identity = asyncio.run(
            client.exchange_code(
                code="code-123",
                code_verifier="verifier-123",
                nonce="nonce-123",
            )
        )

        self.assertEqual(identity.preferred_username, "ERPUSER42")
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

    def test_token_endpoint_failure_diagnostic_is_safe_and_structured(self) -> None:
        client = self._client(
            nonce="nonce-123",
            token_status=400,
            token_response_json={
                "error": "invalid_grant",
                "error_description": "provider detail must stay hidden",
            },
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "token_endpoint",
                "http_status": 400,
                "provider_error": "invalid_grant",
            },
        )
        self.assertNotIn("provider detail", str(error.diagnostic_context()))

    def test_provider_error_is_omitted_when_not_a_safe_oauth_code(self) -> None:
        client = self._client(
            nonce="nonce-123",
            token_status=400,
            token_response_json={"error": "invalid_grant provider detail"},
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "token_endpoint",
                "http_status": 400,
            },
        )

    def test_token_response_invalid_stage_for_malformed_json(self) -> None:
        client = self._client(
            nonce="nonce-123",
            token_response_text="not-json",
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "token_response_invalid",
                "http_status": 200,
            },
        )

    def test_id_token_missing_stage(self) -> None:
        client = self._client(
            nonce="nonce-123",
            omit_id_token=True,
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "id_token_missing",
                "http_status": 200,
            },
        )

    def test_jwks_fetch_stage_preserves_only_token_endpoint_status(self) -> None:
        client = self._client(
            nonce="nonce-123",
            jwks_status=503,
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "jwks_fetch",
                "http_status": 200,
            },
        )

    def test_id_token_decode_stage(self) -> None:
        client = self._client(
            nonce="nonce-123",
            id_token_override="not-a-jwt",
        )

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "id_token_decode",
                "http_status": 200,
            },
        )

    def test_exchange_rejects_wrong_nonce(self) -> None:
        client = self._client(
            nonce="nonce-from-provider",
            claim_diagnostics=True,
        )

        error = self._exchange_error(client, nonce="different-nonce")

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "id_token_validation",
                "http_status": 200,
            },
        )

    def test_exchange_refuses_none_signing_algorithm(self) -> None:
        client = self._client(nonce="nonce-123", algorithms=["none"])

        error = self._exchange_error(client)

        self.assertEqual(
            error.diagnostic_context(),
            {
                "oidc_failure_stage": "id_token_validation",
                "http_status": 200,
            },
        )


if __name__ == "__main__":
    unittest.main()
