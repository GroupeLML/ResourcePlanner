from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import secrets
from typing import Any

from fastapi import Request

from ..application.identity_provisioning import AutoProvisioningPolicy
from ..application.security import AuthPrincipal
from ..infrastructure.acumatica.oidc import OidcClient
from ..infrastructure.sql import SqlAuthSessionRepository, SqlSessionFactory
from ..infrastructure.sql.base import utc_now
from .security import AuthResolver


@dataclass(frozen=True, slots=True)
class OidcRuntime:
    client: OidcClient
    cookie_name: str = "resourceplanner_session"
    session_hours: int = 8
    secure_cookie: bool = True
    cookie_samesite: str = "lax"
    auto_provisioning: AutoProvisioningPolicy = field(default_factory=AutoProvisioningPolicy)
    claim_diagnostics_enabled: bool = False
    login_cookie_name: str = "resourceplanner_oidc_login"
    csrf_cookie_name: str = "resourceplanner_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    _diagnostics_by_session: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @staticmethod
    def _diagnostic_session_key(raw_session_token: str) -> str:
        return hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()

    def remember_claim_diagnostics(
        self,
        raw_session_token: str,
        diagnostics: dict[str, Any] | None,
    ) -> None:
        if not self.claim_diagnostics_enabled or not raw_session_token or diagnostics is None:
            return
        self._diagnostics_by_session[self._diagnostic_session_key(raw_session_token)] = {
            "claim_names": list(diagnostics.get("claim_names") or ()),
            "identity_candidates": dict(diagnostics.get("identity_candidates") or {}),
        }

    def claim_diagnostics_for_session(
        self,
        raw_session_token: str,
    ) -> dict[str, Any] | None:
        if not self.claim_diagnostics_enabled or not raw_session_token:
            return None
        diagnostics = self._diagnostics_by_session.get(
            self._diagnostic_session_key(raw_session_token)
        )
        if diagnostics is None:
            return None
        return {
            "claim_names": list(diagnostics.get("claim_names") or ()),
            "identity_candidates": dict(diagnostics.get("identity_candidates") or {}),
        }

    def forget_claim_diagnostics(self, raw_session_token: str) -> None:
        if not raw_session_token:
            return
        self._diagnostics_by_session.pop(
            self._diagnostic_session_key(raw_session_token),
            None,
        )

    @property
    def login_ttl(self) -> timedelta:
        return timedelta(minutes=10)

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    def new_secret(self, length: int = 48) -> str:
        return secrets.token_urlsafe(length)


def oidc_session_auth_resolver(cookie_name: str) -> AuthResolver:
    def resolve(request: Request) -> AuthPrincipal | None:
        raw_token = str(request.cookies.get(cookie_name) or "").strip()
        if not raw_token:
            return None
        factory: SqlSessionFactory = request.app.state.session_factory
        with factory() as session:
            return SqlAuthSessionRepository(session).resolve_principal(raw_token)

    return resolve


def create_login_transaction(
    factory: SqlSessionFactory,
    runtime: OidcRuntime,
) -> tuple[str, str, str, str]:
    state = runtime.new_secret()
    nonce = runtime.new_secret()
    code_verifier = runtime.new_secret(64)
    browser_binding = runtime.new_secret(48)
    with factory.begin() as session:
        SqlAuthSessionRepository(session).create_login_transaction(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            browser_binding=browser_binding,
            expires_at=utc_now() + runtime.login_ttl,
        )
    return state, nonce, code_verifier, browser_binding


def consume_login_transaction(
    factory: SqlSessionFactory,
    state: str,
    *,
    browser_binding: str,
) -> Any:
    with factory.begin() as session:
        return SqlAuthSessionRepository(session).consume_login_transaction(
            state,
            browser_binding=browser_binding,
        )


def create_server_session(
    factory: SqlSessionFactory,
    runtime: Any,
    *,
    user_id: str,
    auth_mode: str = "oidc",
) -> tuple[str, str]:
    raw_token = runtime.new_secret(64)
    csrf_token = runtime.new_secret(48)
    with factory.begin() as session:
        SqlAuthSessionRepository(session).create_session(
            raw_token=raw_token,
            csrf_token=csrf_token,
            user_id=user_id,
            expires_at=utc_now() + runtime.session_ttl,
            auth_mode=auth_mode,
        )
    return raw_token, csrf_token



def oidc_csrf_guard(runtime: OidcRuntime):
    def validate(request: Request) -> bool:
        raw_session = str(request.cookies.get(runtime.cookie_name) or "").strip()
        cookie_token = str(request.cookies.get(runtime.csrf_cookie_name) or "").strip()
        header_token = str(request.headers.get(runtime.csrf_header_name) or "").strip()
        if not raw_session or not cookie_token or not header_token:
            return False
        if not secrets.compare_digest(cookie_token, header_token):
            return False

        origin = str(request.headers.get("origin") or "").strip()
        if origin:
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin.rstrip("/").casefold() != expected_origin.rstrip("/").casefold():
                return False

        factory: SqlSessionFactory = request.app.state.session_factory
        with factory() as session:
            return SqlAuthSessionRepository(session).validate_csrf(
                raw_session,
                header_token,
            )

    return validate

def revoke_server_session(
    factory: SqlSessionFactory,
    *,
    cookie_name: str,
    request: Request,
) -> None:
    raw_token = str(request.cookies.get(cookie_name) or "").strip()
    if not raw_token:
        return
    with factory.begin() as session:
        SqlAuthSessionRepository(session).revoke_session(raw_token)
