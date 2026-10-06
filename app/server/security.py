from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..application.security import (
    AuthPrincipal,
    PERMISSION_ADMIN_SETTINGS,
    PERMISSION_ADMIN_USERS,
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_EXECUTE_VERIFICATION,
    PERMISSION_MANAGE_VERIFICATION,
    PERMISSION_MANAGE_COMMUNICATIONS,
    PERMISSION_MANAGE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_MANAGE_RESOURCES,
    PERMISSION_MANAGE_WORK_PACKAGES,
    PERMISSION_READ,
    PERMISSION_SYNC_PROJECTS,
)
from .performance import performance_phase


AuthResolverResult = AuthPrincipal | None | Awaitable[AuthPrincipal | None]
AuthResolver = Callable[[Request], AuthResolverResult]
CsrfGuardResult = bool | Awaitable[bool]
CsrfGuard = Callable[[Request], CsrfGuardResult]
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_PUBLIC_PREFIXES = ("/assets/",)
_PUBLIC_PATHS = {
    "/",
    "/health",
    "/ready",
    "/docs",
    "/docs/",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
    "/api/v1/auth/login",
    "/api/v1/auth/callback",
    "/api/v1/auth/break-glass",
}


def required_permission(method: str, path: str) -> str | None:
    verb = str(method).upper()
    if not path.startswith("/api/v1/"):
        return None
    if path in {"/api/v1/auth/me", "/api/v1/auth/logout"}:
        return None
    if path.startswith("/api/v1/dev/user-switcher"):
        return None
    if (
        path.startswith("/api/v1/admin/settings")
        or path.startswith("/api/v1/admin/approval-scopes")
        or path.startswith("/api/v1/admin/resource-classes")
    ):
        return PERMISSION_ADMIN_SETTINGS
    if path.startswith("/api/v1/admin/users") or path.startswith("/api/v1/admin/erp-users"):
        return PERMISSION_ADMIN_USERS
    if path.startswith("/api/v1/communications"):
        return PERMISSION_MANAGE_COMMUNICATIONS
    if path == "/api/v1/demand-requesters":
        return PERMISSION_MANAGE_DEMANDS
    if path.startswith("/api/v1/integrations/acumatica/projects/tasks/sync"):
        return PERMISSION_SYNC_PROJECTS
    if verb == "GET":
        return PERMISSION_READ
    if (
        path.startswith("/api/v1/assets/requirements/")
        and (path.endswith("/reservation") or path.endswith("/operator"))
    ):
        return PERMISSION_MANAGE_PLANNING
    if (
        path.startswith("/api/v1/assets/project-reservations")
        or path.startswith("/api/v1/assets/resource-period-reservations")
        or path.startswith("/api/v1/assets/segment-reservations")
    ):
        return PERMISSION_MANAGE_PLANNING
    if (
        path.startswith("/api/v1/assets/shifts/")
        and path.endswith("/assignment")
    ):
        return PERMISSION_MANAGE_PLANNING
    if path.startswith("/api/v1/assets/") and "/unavailability" in path:
        return PERMISSION_MANAGE_PLANNING
    if path.startswith("/api/v1/assets"):
        return PERMISSION_MANAGE_RESOURCES
    if (
        path.startswith("/api/v1/resources")
        or path.startswith("/api/v1/availability-rules")
        or path.startswith("/api/v1/competencies")
        or path.startswith("/api/v1/business-contacts")
        or path.startswith("/api/v1/task-catalog")
        or (
            path.startswith("/api/v1/projects/")
            and (
                path.endswith("/project-manager-contact")
                or path.endswith("/operational-responsible")
                or "/co-managers/" in path
            )
        )
    ):
        return PERMISSION_MANAGE_RESOURCES
    if path.startswith("/api/v1/work-packages"):
        return PERMISSION_MANAGE_WORK_PACKAGES
    if path.startswith("/api/v1/delivery"):
        return PERMISSION_CONTRIBUTE_DELIVERY
    if path.startswith("/api/v1/verification"):
        if "/executions" in path:
            return PERMISSION_EXECUTE_VERIFICATION
        return PERMISSION_MANAGE_VERIFICATION
    if path.startswith("/api/v1/demands"):
        if (
            path.endswith("/approve")
            or path.endswith("/approval-votes")
            or path.endswith("/correction")
            or path.endswith("/accept-cancellation")
            or path.endswith("/reject-cancellation")
            or path.endswith("/emergency-plan")
        ):
            return PERMISSION_APPROVE_DEMANDS
        return PERMISSION_MANAGE_DEMANDS
    if (
        path.startswith("/api/v1/segments")
        or path.startswith("/api/v1/allocations")
        or path.startswith("/api/v1/quick-shifts")
        or path.startswith("/api/v1/planning/")
    ):
        return PERMISSION_MANAGE_PLANNING
    if (
        path in {
            "/api/v1/integrations/acumatica/projects/sync",
            "/api/v1/integrations/acumatica/employees/sync",
            "/api/v1/integrations/acumatica/users/sync",
        }
        or (
            path.startswith("/api/v1/integrations/acumatica/projects/")
            and path.endswith("/tasks/sync")
        )
    ):
        return PERMISSION_SYNC_PROJECTS
    return "__unassigned_mutation__"


def _error(status_code: int, code: str, message: str, *, context: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "context": dict(context or {}),
            }
        },
    )


def static_auth_resolver(principal: AuthPrincipal | None) -> AuthResolver:
    def resolve(_request: Request) -> AuthPrincipal | None:
        return principal

    return resolve


async def resolve_principal(resolver: AuthResolver, request: Request) -> AuthPrincipal | None:
    resolved = resolver(request)
    if inspect.isawaitable(resolved):
        return await resolved
    return resolved


async def _csrf_allowed(guard: CsrfGuard, request: Request) -> bool:
    result = guard(request)
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


def install_authorization_middleware(
    app: Any,
    resolver: AuthResolver,
    *,
    csrf_guard: CsrfGuard | None = None,
) -> None:
    @app.middleware("http")
    async def authorize(request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)
        if not path.startswith("/api/v1/"):
            return await call_next(request)

        with performance_phase("auth"):
            principal = await resolve_principal(resolver, request)
            if principal is None:
                return _error(
                    401,
                    "authentication_required",
                    "Une authentification est requise pour accéder à RessourcePlanner.",
                )
            request.state.auth_principal = principal
            permission = required_permission(request.method, path)
            if permission is not None and not principal.has_permission(permission):
                return _error(
                    403,
                    "permission_denied",
                    "Vous n'avez pas la permission requise pour cette opération.",
                    context={"required_permission": permission},
                )
            if (
                csrf_guard is not None
                and principal.auth_mode in {"oidc", "break_glass"}
                and request.method.upper() in _UNSAFE_METHODS
            ):
                if not await _csrf_allowed(csrf_guard, request):
                    return _error(
                        403,
                        "csrf_validation_failed",
                        "La requête de modification ne possède pas une preuve CSRF valide.",
                    )
        return await call_next(request)
