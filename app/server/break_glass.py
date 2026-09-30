from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import secrets

from ..application.break_glass import BreakGlassPolicy


@dataclass(frozen=True, slots=True)
class BreakGlassRuntime:
    cookie_name: str = "resourceplanner_session"
    session_hours: int = 8
    secure_cookie: bool = True
    cookie_samesite: str = "lax"
    csrf_cookie_name: str = "resourceplanner_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    policy: BreakGlassPolicy = BreakGlassPolicy()

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    def new_secret(self, length: int = 48) -> str:
        return secrets.token_urlsafe(length)
