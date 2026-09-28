from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
import httpx

from ..application.identity_provisioning import (
    ErpControlledIdentityProvisioningService,
    ErpIdentityProvisioningConflict,
    ErpIdentityProvisioningDenied,
    IdentityProvisioningService,
)
from ..application.security import AuthPrincipal
from ..infrastructure.acumatica.oidc import OidcProtocolError
from ..infrastructure.sql import (
    SqlErpUserDirectoryRepository,
    SqlUserIdentityRepository,
)
from .oidc import (
    OidcRuntime,
    consume_login_transaction,
    create_login_transaction,
    create_server_session,
    revoke_server_session,
)


def _auth_error(
    status: int,
    code: str,
    message: str,
    *,
    context: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "context": dict(context or {})}},
    )


def _clear_login_cookie(response: Response, runtime: OidcRuntime) -> Response:
    response.delete_cookie(
        runtime.login_cookie_name,
        path="/api/v1/auth/callback",
        secure=runtime.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    return response


def build_auth_router(oidc_runtime: OidcRuntime | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.get("/me")
    def current_user(request: Request) -> dict[str, Any]:
        principal: AuthPrincipal = request.state.auth_principal
        return principal.to_dict()

    if oidc_runtime is None:
        return router

    if oidc_runtime.claim_diagnostics_enabled:
        @router.get("/oidc-claims")
        def oidc_claim_diagnostics(request: Request) -> Any:
            raw_session = str(
                request.cookies.get(oidc_runtime.cookie_name) or ""
            ).strip()
            diagnostics = oidc_runtime.claim_diagnostics_for_session(raw_session)
            if diagnostics is None:
                return _auth_error(
                    404,
                    "oidc_claim_diagnostics_unavailable",
                    "Aucun diagnostic OIDC n'est disponible pour cette session.",
                )
            return diagnostics

    @router.get("/login")
    async def login(request: Request) -> Response:
        factory = request.app.state.session_factory
        state, nonce, code_verifier, browser_binding = create_login_transaction(factory, oidc_runtime)
        try:
            target = await oidc_runtime.client.authorization_url(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
            )
        except (httpx.HTTPError, OidcProtocolError):
            return _auth_error(
                503,
                "oidc_provider_unavailable",
                "Le fournisseur OIDC n'est pas disponible ou sa configuration est invalide.",
            )
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(
            oidc_runtime.login_cookie_name,
            browser_binding,
            max_age=int(oidc_runtime.login_ttl.total_seconds()),
            httponly=True,
            secure=oidc_runtime.secure_cookie,
            samesite="lax",
            path="/api/v1/auth/callback",
        )
        return response

    @router.get("/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        if error:
            return _auth_error(401, "oidc_authorization_denied", "L'authentification OIDC a été refusée.")
        if not code or not state:
            return _auth_error(400, "oidc_callback_invalid", "Le callback OIDC est incomplet.")

        factory = request.app.state.session_factory
        browser_binding = str(request.cookies.get(oidc_runtime.login_cookie_name) or "").strip()
        if not browser_binding:
            return _clear_login_cookie(
                _auth_error(
                    400,
                    "oidc_browser_binding_missing",
                    "La transaction de connexion OIDC n'appartient pas à ce navigateur.",
                ),
                oidc_runtime,
            )
        transaction = consume_login_transaction(
            factory,
            state,
            browser_binding=browser_binding,
        )
        if transaction is None:
            return _clear_login_cookie(
                _auth_error(
                    400,
                    "oidc_state_invalid",
                    "La transaction de connexion OIDC est expirée, invalide ou liée à un autre navigateur.",
                ),
                oidc_runtime,
            )
        try:
            identity = await oidc_runtime.client.exchange_code(
                code=code,
                code_verifier=transaction.code_verifier,
                nonce=transaction.nonce,
            )
        except (httpx.HTTPError, OidcProtocolError, ValueError) as exc:
            diagnostic_context = None
            if oidc_runtime.claim_diagnostics_enabled:
                if isinstance(exc, OidcProtocolError):
                    diagnostic_context = exc.diagnostic_context()
                else:
                    diagnostic_context = {"oidc_failure_stage": "unexpected"}
            return _clear_login_cookie(
                _auth_error(
                    401,
                    "oidc_token_invalid",
                    "La réponse OIDC n'a pas pu être validée.",
                    context=diagnostic_context,
                ),
                oidc_runtime,
            )

        principal = None
        try:
            with factory.begin() as session:
                identities = SqlUserIdentityRepository(session)
                principal = ErpControlledIdentityProvisioningService(
                    identities,
                    SqlErpUserDirectoryRepository(session),
                ).resolve_or_provision(
                    issuer=identity.issuer,
                    subject=identity.subject,
                    preferred_username=identity.preferred_username,
                    display_name=identity.display_name,
                    email=identity.email,
                    auth_mode="oidc",
                )
                if principal is None:
                    # Preserve the historical explicit generic policy for
                    # providers/fixtures without preferred_username. A real
                    # preferred_username never bypasses RP_Users authorization.
                    principal = IdentityProvisioningService(
                        identities,
                        oidc_runtime.auto_provisioning,
                    ).resolve_or_provision(
                        issuer=identity.issuer,
                        subject=identity.subject,
                        display_name=identity.display_name,
                        email=identity.email,
                        auth_mode="oidc",
                    )
        except ErpIdentityProvisioningConflict:
            return _clear_login_cookie(
                _auth_error(
                    403,
                    "oidc_identity_conflict",
                    "Cette identité Acumatica entre en conflit avec une liaison RessourcePlanner existante.",
                ),
                oidc_runtime,
            )
        except ErpIdentityProvisioningDenied:
            principal = None

        if principal is None or principal.local_user_id is None:
            diagnostic_context = None
            if (
                oidc_runtime.claim_diagnostics_enabled
                and identity.diagnostic_claims is not None
            ):
                diagnostic_context = {
                    "oidc_claim_diagnostics": identity.diagnostic_claims,
                }
            return _clear_login_cookie(
                _auth_error(
                    403,
                    "oidc_user_not_registered",
                    "Cette identité Acumatica n'est pas autorisée dans RessourcePlanner.",
                    context=diagnostic_context,
                ),
                oidc_runtime,
            )

        raw_session, csrf_token = create_server_session(
            factory,
            oidc_runtime,
            user_id=principal.local_user_id,
        )
        oidc_runtime.remember_claim_diagnostics(
            raw_session,
            identity.diagnostic_claims,
        )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            oidc_runtime.cookie_name,
            raw_session,
            max_age=int(oidc_runtime.session_ttl.total_seconds()),
            httponly=True,
            secure=oidc_runtime.secure_cookie,
            samesite=oidc_runtime.cookie_samesite,
            path="/",
        )
        response.set_cookie(
            oidc_runtime.csrf_cookie_name,
            csrf_token,
            max_age=int(oidc_runtime.session_ttl.total_seconds()),
            httponly=False,
            secure=oidc_runtime.secure_cookie,
            samesite=oidc_runtime.cookie_samesite,
            path="/",
        )
        return _clear_login_cookie(response, oidc_runtime)

    @router.post("/logout", status_code=204)
    def logout(request: Request) -> Response:
        factory = request.app.state.session_factory
        raw_session = str(request.cookies.get(oidc_runtime.cookie_name) or "").strip()
        oidc_runtime.forget_claim_diagnostics(raw_session)
        revoke_server_session(
            factory,
            cookie_name=oidc_runtime.cookie_name,
            request=request,
        )
        response = Response(status_code=204)
        response.delete_cookie(
            oidc_runtime.cookie_name,
            path="/",
            secure=oidc_runtime.secure_cookie,
            httponly=True,
            samesite=oidc_runtime.cookie_samesite,
        )
        response.delete_cookie(
            oidc_runtime.csrf_cookie_name,
            path="/",
            secure=oidc_runtime.secure_cookie,
            httponly=False,
            samesite=oidc_runtime.cookie_samesite,
        )
        return response

    return router
