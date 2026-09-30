from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import secrets

from fastapi import Request

from ..application.security import AuthPrincipal
from ..infrastructure.sql import SqlAuthSessionRepository, SqlSessionFactory
from ..infrastructure.sql.base import utc_now
from .security import AuthResolver


@dataclass(frozen=True, slots=True)
class DevUserSwitcherRuntime:
    bootstrap_principal: AuthPrincipal
    cookie_name: str = "resourceplanner_dev_session"
    session_hours: int = 24

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    def new_secret(self, length: int = 64) -> str:
        return secrets.token_urlsafe(length)


def dev_user_switcher_auth_resolver(runtime: DevUserSwitcherRuntime) -> AuthResolver:
    """Resolve a selected AppUser, falling back to the local bootstrap administrator."""

    def resolve(request: Request) -> AuthPrincipal:
        raw_token = str(request.cookies.get(runtime.cookie_name) or "").strip()
        if raw_token:
            factory: SqlSessionFactory = request.app.state.session_factory
            with factory() as session:
                principal = SqlAuthSessionRepository(session).resolve_principal(
                    raw_token,
                    auth_mode="local",
                )
            if principal is not None:
                return principal
        return runtime.bootstrap_principal

    return resolve


def create_dev_user_session(
    factory: SqlSessionFactory,
    runtime: DevUserSwitcherRuntime,
    *,
    user_id: str,
) -> str:
    raw_token = runtime.new_secret()
    with factory.begin() as session:
        SqlAuthSessionRepository(session).create_session(
            raw_token=raw_token,
            user_id=user_id,
            expires_at=utc_now() + runtime.session_ttl,
            auth_mode="local",
        )
    return raw_token


def revoke_dev_user_session(
    factory: SqlSessionFactory,
    runtime: DevUserSwitcherRuntime,
    request: Request,
) -> None:
    raw_token = str(request.cookies.get(runtime.cookie_name) or "").strip()
    if not raw_token:
        return
    with factory.begin() as session:
        SqlAuthSessionRepository(session).revoke_session(raw_token)
