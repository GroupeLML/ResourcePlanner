from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import secrets
from threading import Lock

from fastapi import Request
from sqlalchemy import inspect as inspect_database

from ..application.security import AuthPrincipal
from ..infrastructure.sql import (
    SqlAuthSessionRepository,
    SqlSessionFactory,
    SqlUserIdentityRepository,
)
from ..infrastructure.sql.base import utc_now
from .security import AuthResolver


@dataclass(slots=True)
class DevUserSwitcherRuntime:
    bootstrap_principal: AuthPrincipal
    cookie_name: str = "resourceplanner_dev_session"
    session_hours: int = 24
    _bootstrap_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    def new_secret(self, length: int = 64) -> str:
        return secrets.token_urlsafe(length)

    def resolve_bootstrap_principal(self, request: Request) -> AuthPrincipal:
        """Materialize the local bootstrap identity as the canonical AppUser actor."""

        principal = self.bootstrap_principal
        if principal.local_user_id is not None:
            return principal
        if principal.auth_mode != "local":
            raise RuntimeError(
                "Le bootstrap d'identité de développement exige auth_mode=local."
            )

        with self._bootstrap_lock:
            principal = self.bootstrap_principal
            if principal.local_user_id is not None:
                return principal

            factory: SqlSessionFactory = request.app.state.session_factory
            with factory.begin() as session:
                if not inspect_database(session.get_bind()).has_table("app_users"):
                    return principal
                record = SqlUserIdentityRepository(session).upsert(
                    issuer=principal.issuer,
                    subject=principal.subject,
                    display_name=principal.display_name,
                    email=principal.email,
                    employee_external_id=principal.employee_external_id,
                    roles=principal.roles,
                    active=True,
                )

            if record.issuer is None or record.subject is None:
                raise RuntimeError(
                    "L'identité locale de développement matérialisée est incomplète."
                )

            resolved = AuthPrincipal.from_roles(
                local_user_id=record.user_id,
                issuer=record.issuer,
                subject=record.subject,
                display_name=record.display_name,
                email=record.email,
                employee_external_id=record.employee_external_id,
                roles=record.roles,
                auth_mode="local",
            )
            self.bootstrap_principal = resolved
            return resolved


def local_dev_auth_resolver(runtime: DevUserSwitcherRuntime) -> AuthResolver:
    """Resolve the canonical persisted bootstrap identity for local development."""

    def resolve(request: Request) -> AuthPrincipal:
        return runtime.resolve_bootstrap_principal(request)

    return resolve


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
        return runtime.resolve_bootstrap_principal(request)

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
