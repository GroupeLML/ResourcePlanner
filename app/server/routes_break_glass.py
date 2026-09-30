from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..application.break_glass import (
    BREAK_GLASS_AUTH_MODE,
    BreakGlassAuthenticationDenied,
    BreakGlassAuthenticationService,
    BreakGlassRateLimited,
)
from ..infrastructure.sql.secret_hashing import ScryptSecretHasher
from ..infrastructure.sql import SqlBreakGlassRepository
from ..infrastructure.sql.base import utc_now
from .break_glass import BreakGlassRuntime
from .oidc import create_server_session


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BreakGlassLoginRequest(StrictRequest):
    login_name: str = Field(min_length=1, max_length=128)
    credential_value: str = Field(alias="secret", min_length=1, max_length=4096)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "context": {},
            }
        },
    )


def build_break_glass_router(runtime: BreakGlassRuntime | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
    if runtime is None:
        return router

    @router.post("/break-glass")
    def break_glass_login(
        body: BreakGlassLoginRequest,
        request: Request,
    ) -> Response:
        factory = request.app.state.session_factory
        outcome = "authenticated"
        user_id: str | None = None
        with factory.begin() as session:
            try:
                user_id = BreakGlassAuthenticationService(
                    SqlBreakGlassRepository(session),
                    ScryptSecretHasher(),
                    policy=runtime.policy,
                ).authenticate(
                    login_name=body.login_name,
                    credential_value=body.credential_value,
                    now=utc_now(),
                )
            except BreakGlassRateLimited:
                outcome = "rate_limited"
            except BreakGlassAuthenticationDenied:
                outcome = "denied"

        if outcome == "rate_limited":
            response = _error(
                429,
                "break_glass_rate_limited",
                "Les tentatives de connexion sont temporairement limitées.",
            )
            response.headers["Retry-After"] = str(
                int(runtime.policy.lock_duration.total_seconds())
            )
            return response
        if outcome == "denied" or user_id is None:
            return _error(
                401,
                "break_glass_authentication_failed",
                "Le credential administrateur de secours est invalide.",
            )

        raw_session, csrf_token = create_server_session(
            factory,
            runtime,
            user_id=user_id,
            auth_mode=BREAK_GLASS_AUTH_MODE,
        )
        response = Response(status_code=204)
        response.set_cookie(
            runtime.cookie_name,
            raw_session,
            max_age=int(runtime.session_ttl.total_seconds()),
            httponly=True,
            secure=runtime.secure_cookie,
            samesite=runtime.cookie_samesite,
            path="/",
        )
        response.set_cookie(
            runtime.csrf_cookie_name,
            csrf_token,
            max_age=int(runtime.session_ttl.total_seconds()),
            httponly=False,
            secure=runtime.secure_cookie,
            samesite=runtime.cookie_samesite,
            path="/",
        )
        return response

    return router
