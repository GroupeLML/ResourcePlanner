from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os

from fastapi import FastAPI
import uvicorn

from ..application.identity_provisioning import AutoProvisioningPolicy
from ..application.security import AuthPrincipal, ROLE_ADMIN, normalize_roles
from ..infrastructure.acumatica import (
    ODataEmployeeSource,
    ODataEmployeeSourceSettings,
    ODataProjectSource,
    ODataProjectSourceSettings,
    ODataProjectTaskSource,
    ODataProjectTaskSourceSettings,
    ODataUserSource,
    ODataUserSourceSettings,
)
from ..infrastructure.acumatica.oidc import OidcClient, OidcClientSettings
from ..infrastructure.smtp import FernetSecretCipher, SmtpClient
from ..infrastructure.m365 import (
    MicrosoftGraphCommunicationSettings,
    MicrosoftGraphCommunicationTransport,
)
from .break_glass import BreakGlassRuntime
from .dev_user_switcher import DevUserSwitcherRuntime, dev_user_switcher_auth_resolver
from .embedding import EmbeddingSettings, install_embedding_headers
from .frontend import FrontendBuildError, attach_frontend
from .http import create_api_app
from .oidc import OidcRuntime, oidc_session_auth_resolver
from .security import static_auth_resolver


DATABASE_URL_ENV = "RESOURCEPLANNER_DATABASE_URL"
HOST_ENV = "RESOURCEPLANNER_HOST"
PORT_ENV = "RESOURCEPLANNER_PORT"
LOG_LEVEL_ENV = "RESOURCEPLANNER_LOG_LEVEL"
ACTOR_NAME_ENV = "RESOURCEPLANNER_ACTOR_NAME"
FRONTEND_DIST_ENV = "RESOURCEPLANNER_FRONTEND_DIST"
AUTH_MODE_ENV = "RESOURCEPLANNER_AUTH_MODE"
LOCAL_AUTH_NAME_ENV = "RESOURCEPLANNER_LOCAL_AUTH_NAME"
LOCAL_AUTH_EMAIL_ENV = "RESOURCEPLANNER_LOCAL_AUTH_EMAIL"
LOCAL_AUTH_ROLES_ENV = "RESOURCEPLANNER_LOCAL_AUTH_ROLES"
ALLOW_LOCAL_AUTH_NETWORK_ENV = "RESOURCEPLANNER_ALLOW_LOCAL_AUTH_NETWORK"
DEV_USER_SWITCHER_ENV = "RESOURCEPLANNER_DEV_USER_SWITCHER"
BREAK_GLASS_ENABLED_ENV = "RESOURCEPLANNER_BREAK_GLASS_ENABLED"
OIDC_DISCOVERY_URL_ENV = "RESOURCEPLANNER_OIDC_DISCOVERY_URL"
OIDC_CLIENT_ID_ENV = "RESOURCEPLANNER_OIDC_CLIENT_ID"
OIDC_CLIENT_SECRET_ENV = "RESOURCEPLANNER_OIDC_CLIENT_SECRET"
OIDC_REDIRECT_URI_ENV = "RESOURCEPLANNER_OIDC_REDIRECT_URI"
OIDC_SCOPES_ENV = "RESOURCEPLANNER_OIDC_SCOPES"
OIDC_COOKIE_NAME_ENV = "RESOURCEPLANNER_OIDC_COOKIE_NAME"
OIDC_SESSION_HOURS_ENV = "RESOURCEPLANNER_OIDC_SESSION_HOURS"
OIDC_SECURE_COOKIE_ENV = "RESOURCEPLANNER_OIDC_SECURE_COOKIE"
OIDC_AUTO_PROVISION_ENV = "RESOURCEPLANNER_OIDC_AUTO_PROVISION"
OIDC_CLAIM_DIAGNOSTICS_ENV = "RESOURCEPLANNER_OIDC_CLAIM_DIAGNOSTICS"
API_DOCS_ENABLED_ENV = "RESOURCEPLANNER_API_DOCS_ENABLED"
ACUMATICA_BASE_URL_ENV = "RESOURCEPLANNER_ACUMATICA_BASE_URL"
ACUMATICA_USERNAME_ENV = "RESOURCEPLANNER_ACUMATICA_USERNAME"
ACUMATICA_PASSWORD_ENV = "RESOURCEPLANNER_ACUMATICA_PASSWORD"
ACUMATICA_ACCESS_TOKEN_ENV = "RESOURCEPLANNER_ACUMATICA_ACCESS_TOKEN"
ACUMATICA_ENDPOINT_ENV = "RESOURCEPLANNER_ACUMATICA_ENDPOINT"
ACUMATICA_VERSION_ENV = "RESOURCEPLANNER_ACUMATICA_VERSION"
ACUMATICA_ENTITY_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_ENTITY"
ACUMATICA_NUMBER_FIELD_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_NUMBER_FIELD"
ACUMATICA_NAME_FIELD_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_NAME_FIELD"
ACUMATICA_CLIENT_FIELD_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_CLIENT_FIELD"
ACUMATICA_MANAGER_FIELD_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_MANAGER_FIELD"
ACUMATICA_STATUS_FIELD_ENV = "RESOURCEPLANNER_ACUMATICA_PROJECT_STATUS_FIELD"
ACUMATICA_PAGE_SIZE_ENV = "RESOURCEPLANNER_ACUMATICA_PAGE_SIZE"
ACUMATICA_TIMEOUT_SECONDS_ENV = "RESOURCEPLANNER_ACUMATICA_TIMEOUT_SECONDS"
M365_TENANT_ID_ENV = "RESOURCEPLANNER_M365_TENANT_ID"
M365_CLIENT_ID_ENV = "RESOURCEPLANNER_M365_CLIENT_ID"
M365_CLIENT_SECRET_ENV = "RESOURCEPLANNER_M365_CLIENT_SECRET"
M365_MAILBOX_ENV = "RESOURCEPLANNER_M365_MAILBOX"
M365_GRAPH_BASE_URL_ENV = "RESOURCEPLANNER_M365_GRAPH_BASE_URL"
M365_AUTHORITY_HOST_ENV = "RESOURCEPLANNER_M365_AUTHORITY_HOST"
M365_TIMEOUT_SECONDS_ENV = "RESOURCEPLANNER_M365_TIMEOUT_SECONDS"
CONFIG_ENCRYPTION_KEY_ENV = "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY"

_ALLOWED_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ServerConfigurationError(RuntimeError):
    """Raised when the server environment is incomplete or invalid."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _bool(value: object, *, default: bool = False) -> bool:
    text = _text(value).casefold()
    if not text:
        return default
    if text in {"1", "true", "yes", "oui", "on"}:
        return True
    if text in {"0", "false", "no", "non", "off"}:
        return False
    raise ServerConfigurationError("Une valeur booléenne attend true/false ou 1/0.")


def _positive_int(value: object, *, default: int, label: str, maximum: int) -> int:
    text = _text(value)
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ServerConfigurationError(f"{label} doit être un entier positif.") from exc
    if not 1 <= parsed <= maximum:
        raise ServerConfigurationError(f"{label} doit être compris entre 1 et {maximum}.")
    return parsed


def _port(value: object) -> int:
    return _positive_int(value, default=8000, label=PORT_ENV, maximum=65535)


def _positive_float(
    value: object,
    *,
    default: float,
    label: str,
    maximum: float,
) -> float:
    text = _text(value)
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ServerConfigurationError(f"{label} doit être un nombre positif.") from exc
    if not 0 < parsed <= maximum:
        raise ServerConfigurationError(
            f"{label} doit être supérieur à 0 et inférieur ou égal à {maximum:g}."
        )
    return parsed


def _acumatica_settings(values: Mapping[str, str]) -> ODataProjectSourceSettings | None:
    base_url = _text(values.get(ACUMATICA_BASE_URL_ENV))
    if not base_url:
        return None

    username = _text(values.get(ACUMATICA_USERNAME_ENV))
    credential = _text(values.get(ACUMATICA_PASSWORD_ENV))
    missing = [
        name
        for name, value in (
            (ACUMATICA_USERNAME_ENV, username),
            (ACUMATICA_PASSWORD_ENV, credential),
        )
        if not value
    ]
    if missing:
        raise ServerConfigurationError(
            "Configuration OData Acumatica incomplète; variables requises: "
            + ", ".join(missing)
        )

    return ODataProjectSourceSettings(
        base_url=base_url,
        username=username,
        credential=credential,
        page_size=_positive_int(
            values.get(ACUMATICA_PAGE_SIZE_ENV),
            default=100,
            label=ACUMATICA_PAGE_SIZE_ENV,
            maximum=10000,
        ),
        timeout_seconds=_positive_float(
            values.get(ACUMATICA_TIMEOUT_SECONDS_ENV),
            default=30.0,
            label=ACUMATICA_TIMEOUT_SECONDS_ENV,
            maximum=120.0,
        ),
    )


def _m365_settings(values: Mapping[str, str]) -> MicrosoftGraphCommunicationSettings | None:
    tenant_id = _text(values.get(M365_TENANT_ID_ENV))
    client_id = _text(values.get(M365_CLIENT_ID_ENV))
    credential_value = _text(values.get(M365_CLIENT_SECRET_ENV))
    mailbox = _text(values.get(M365_MAILBOX_ENV))

    if not any((tenant_id, client_id, credential_value, mailbox)):
        return None

    missing = [
        name
        for name, value in (
            (M365_TENANT_ID_ENV, tenant_id),
            (M365_CLIENT_ID_ENV, client_id),
            (M365_CLIENT_SECRET_ENV, credential_value),
            (M365_MAILBOX_ENV, mailbox),
        )
        if not value
    ]
    if missing:
        raise ServerConfigurationError(
            "Configuration Microsoft 365 incomplète; variables requises: " + ", ".join(missing)
        )

    timeout_seconds = _positive_int(
        values.get(M365_TIMEOUT_SECONDS_ENV),
        default=20,
        label=M365_TIMEOUT_SECONDS_ENV,
        maximum=120,
    )
    return MicrosoftGraphCommunicationSettings(
        tenant_id=tenant_id,
        client_id=client_id,
        client_credential=credential_value,
        mailbox=mailbox,
        graph_base_url=(
            _text(values.get(M365_GRAPH_BASE_URL_ENV)) or "https://graph.microsoft.com/v1.0"
        ),
        authority_host=(
            _text(values.get(M365_AUTHORITY_HOST_ENV)) or "https://login.microsoftonline.com"
        ),
        timeout_seconds=float(timeout_seconds),
    )


def _oidc_settings(values: Mapping[str, str]) -> OidcClientSettings:
    discovery_url = _text(values.get(OIDC_DISCOVERY_URL_ENV))
    client_id = _text(values.get(OIDC_CLIENT_ID_ENV))
    redirect_uri = _text(values.get(OIDC_REDIRECT_URI_ENV))
    missing = [
        name
        for name, value in (
            (OIDC_DISCOVERY_URL_ENV, discovery_url),
            (OIDC_CLIENT_ID_ENV, client_id),
            (OIDC_REDIRECT_URI_ENV, redirect_uri),
        )
        if not value
    ]
    if missing:
        raise ServerConfigurationError(
            "Configuration OIDC incomplète; variables requises: " + ", ".join(missing)
        )
    scopes = tuple(
        part for part in _text(values.get(OIDC_SCOPES_ENV) or "openid profile email").split() if part
    )
    if "openid" not in scopes:
        raise ServerConfigurationError(f"{OIDC_SCOPES_ENV} doit contenir le scope 'openid'.")
    return OidcClientSettings(
        discovery_url=discovery_url,
        client_id=client_id,
        client_credential=_text(values.get(OIDC_CLIENT_SECRET_ENV)) or None,
        redirect_uri=redirect_uri,
        scopes=scopes,
    )


def _default_local_principal() -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=None,
        issuer="urn:resourceplanner:local",
        subject="local-user",
        display_name="Administrateur local",
        email=None,
        roles=(ROLE_ADMIN,),
        auth_mode="local",
    )


def _local_principal(values: Mapping[str, str], *, actor_name: str) -> AuthPrincipal:
    roles_text = _text(values.get(LOCAL_AUTH_ROLES_ENV)) or ROLE_ADMIN
    try:
        roles = normalize_roles(tuple(part.strip() for part in roles_text.split(",") if part.strip()))
    except ValueError as exc:
        raise ServerConfigurationError(str(exc)) from exc
    if not roles:
        raise ServerConfigurationError(f"{LOCAL_AUTH_ROLES_ENV} doit contenir au moins un rôle.")
    return AuthPrincipal.from_roles(
        local_user_id=None,
        issuer="urn:resourceplanner:local",
        subject="local-user",
        display_name=_text(values.get(LOCAL_AUTH_NAME_ENV)) or actor_name or "Administrateur local",
        email=_text(values.get(LOCAL_AUTH_EMAIL_ENV)) or None,
        roles=roles,
        auth_mode="local",
    )


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Environment-driven configuration for the standalone FastAPI server."""

    database_url: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    actor_name: str = "api"
    frontend_dist: str | None = None
    auth_mode: str = "local"
    auth_principal: AuthPrincipal | None = field(default_factory=_default_local_principal, repr=False)
    dev_user_switcher: bool = False
    break_glass_enabled: bool = False
    oidc: OidcClientSettings | None = field(default=None, repr=False)
    oidc_cookie_name: str = "resourceplanner_session"
    oidc_session_hours: int = 8
    oidc_secure_cookie: bool = True
    oidc_auto_provision: bool = False
    oidc_claim_diagnostics: bool = False
    api_docs_enabled: bool = True
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    acumatica: ODataProjectSourceSettings | None = field(default=None, repr=False)
    m365: MicrosoftGraphCommunicationSettings | None = field(default=None, repr=False)
    config_encryption_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "ServerSettings":
        values = os.environ if environ is None else environ
        database_url = _text(values.get(DATABASE_URL_ENV))
        if not database_url:
            raise ServerConfigurationError(f"{DATABASE_URL_ENV} est requis pour démarrer le serveur.")

        host = _text(values.get(HOST_ENV)) or "127.0.0.1"
        port = _port(values.get(PORT_ENV))
        log_level = (_text(values.get(LOG_LEVEL_ENV)) or "info").casefold()
        if log_level not in _ALLOWED_LOG_LEVELS:
            allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
            raise ServerConfigurationError(
                f"{LOG_LEVEL_ENV} doit être l'une des valeurs suivantes: {allowed}."
            )
        actor_name = _text(values.get(ACTOR_NAME_ENV)) or "api"
        frontend_dist = _text(values.get(FRONTEND_DIST_ENV)) or None
        auth_mode = (_text(values.get(AUTH_MODE_ENV)) or "local").casefold()
        if auth_mode not in {"local", "oidc"}:
            raise ServerConfigurationError(
                f"{AUTH_MODE_ENV} doit être 'local' ou 'oidc'."
            )

        auth_principal: AuthPrincipal | None = None
        oidc: OidcClientSettings | None = None
        oidc_cookie_name = _text(values.get(OIDC_COOKIE_NAME_ENV)) or "resourceplanner_session"
        oidc_session_hours = _positive_int(
            values.get(OIDC_SESSION_HOURS_ENV),
            default=8,
            label=OIDC_SESSION_HOURS_ENV,
            maximum=168,
        )
        oidc_secure_cookie = True
        oidc_auto_provision = False
        oidc_claim_diagnostics = False
        dev_user_switcher = _bool(values.get(DEV_USER_SWITCHER_ENV), default=False)
        break_glass_enabled = _bool(values.get(BREAK_GLASS_ENABLED_ENV), default=False)
        if break_glass_enabled and auth_mode != "oidc":
            raise ServerConfigurationError(
                f"{BREAK_GLASS_ENABLED_ENV}=true est réservé à {AUTH_MODE_ENV}=oidc."
            )
        if dev_user_switcher and auth_mode != "local":
            raise ServerConfigurationError(
                f"{DEV_USER_SWITCHER_ENV}=true est réservé à {AUTH_MODE_ENV}=local."
            )

        if auth_mode == "local":
            allow_network = _bool(values.get(ALLOW_LOCAL_AUTH_NETWORK_ENV), default=False)
            if host.casefold() not in _LOOPBACK_HOSTS and not allow_network:
                raise ServerConfigurationError(
                    "Le mode d'authentification local ne peut pas écouter sur le réseau sans "
                    f"{ALLOW_LOCAL_AUTH_NETWORK_ENV}=true."
                )
            auth_principal = _local_principal(values, actor_name=actor_name)
        else:
            oidc = _oidc_settings(values)
            oidc_secure_cookie = _bool(
                values.get(OIDC_SECURE_COOKIE_ENV),
                default=oidc.redirect_uri.casefold().startswith("https://"),
            )
            oidc_auto_provision = _bool(
                values.get(OIDC_AUTO_PROVISION_ENV),
                default=False,
            )
            oidc_claim_diagnostics = _bool(
                values.get(OIDC_CLAIM_DIAGNOSTICS_ENV),
                default=False,
            )

        api_docs_enabled = _bool(
            values.get(API_DOCS_ENABLED_ENV),
            default=auth_mode == "local",
        )

        try:
            embedding = EmbeddingSettings.from_environment(
                values,
                secure_cookie=oidc_secure_cookie,
            )
        except ValueError as exc:
            raise ServerConfigurationError(str(exc)) from exc

        return cls(
            database_url=database_url,
            host=host,
            port=port,
            log_level=log_level,
            actor_name=actor_name,
            frontend_dist=frontend_dist,
            auth_mode=auth_mode,
            auth_principal=auth_principal,
            dev_user_switcher=dev_user_switcher,
            break_glass_enabled=break_glass_enabled,
            oidc=oidc,
            oidc_cookie_name=oidc_cookie_name,
            oidc_session_hours=oidc_session_hours,
            oidc_secure_cookie=oidc_secure_cookie,
            oidc_auto_provision=oidc_auto_provision,
            oidc_claim_diagnostics=oidc_claim_diagnostics,
            api_docs_enabled=api_docs_enabled,
            embedding=embedding,
            acumatica=_acumatica_settings(values),
            m365=_m365_settings(values),
            config_encryption_key=_text(values.get(CONFIG_ENCRYPTION_KEY_ENV)) or None,
        )


def create_configured_app(settings: ServerSettings | None = None) -> FastAPI:
    """Create the configured SQL API and optionally attach the React build."""

    resolved = settings or ServerSettings.from_environment()
    project_source = (
        ODataProjectSource(resolved.acumatica)
        if resolved.acumatica is not None
        else None
    )
    project_task_source = None
    if resolved.acumatica is not None:
        project_task_source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url=resolved.acumatica.base_url,
                username=resolved.acumatica.username,
                credential=resolved.acumatica.credential,
                page_size=resolved.acumatica.page_size,
                timeout_seconds=resolved.acumatica.timeout_seconds,
            )
        )
    employee_source = None
    if resolved.acumatica is not None:
        employee_source = ODataEmployeeSource(
            ODataEmployeeSourceSettings(
                base_url=resolved.acumatica.base_url,
                username=resolved.acumatica.username,
                credential=resolved.acumatica.credential,
                page_size=resolved.acumatica.page_size,
                timeout_seconds=resolved.acumatica.timeout_seconds,
            )
        )
    user_source = None
    if resolved.acumatica is not None:
        user_source = ODataUserSource(
            ODataUserSourceSettings(
                base_url=resolved.acumatica.base_url,
                username=resolved.acumatica.username,
                credential=resolved.acumatica.credential,
                page_size=resolved.acumatica.page_size,
                timeout_seconds=resolved.acumatica.timeout_seconds,
            )
        )
    communication_transport = (
        MicrosoftGraphCommunicationTransport(resolved.m365)
        if resolved.m365 is not None
        else None
    )
    smtp_cipher = None
    if resolved.config_encryption_key:
        try:
            smtp_cipher = FernetSecretCipher(resolved.config_encryption_key)
        except Exception as exc:
            raise ServerConfigurationError(
                f"{CONFIG_ENCRYPTION_KEY_ENV} n'est pas une clé Fernet valide."
            ) from exc
    smtp_client = SmtpClient()
    oidc_runtime = None
    break_glass_runtime = None
    dev_user_switcher_runtime = None
    if resolved.dev_user_switcher and resolved.auth_mode != "local":
        raise ServerConfigurationError(
            "Le sélecteur d’utilisateur de développement ne peut être activé qu’en mode local."
        )

    if resolved.auth_mode == "oidc":
        if resolved.oidc is None:
            raise ServerConfigurationError("La configuration OIDC est absente.")
        oidc_runtime = OidcRuntime(
            client=OidcClient(
                resolved.oidc,
                claim_diagnostics=resolved.oidc_claim_diagnostics,
            ),
            cookie_name=resolved.oidc_cookie_name,
            session_hours=resolved.oidc_session_hours,
            secure_cookie=resolved.oidc_secure_cookie,
            cookie_samesite=resolved.embedding.oidc_cookie_samesite,
            auto_provisioning=AutoProvisioningPolicy(enabled=resolved.oidc_auto_provision),
            claim_diagnostics_enabled=resolved.oidc_claim_diagnostics,
        )
        auth_resolver = oidc_session_auth_resolver(resolved.oidc_cookie_name)
        if resolved.break_glass_enabled:
            break_glass_runtime = BreakGlassRuntime(
                cookie_name=resolved.oidc_cookie_name,
                session_hours=resolved.oidc_session_hours,
                secure_cookie=resolved.oidc_secure_cookie,
                cookie_samesite=resolved.embedding.oidc_cookie_samesite,
            )
    else:
        if resolved.dev_user_switcher:
            if resolved.auth_principal is None:
                raise ServerConfigurationError("L’identité administrateur locale de bootstrap est absente.")
            dev_user_switcher_runtime = DevUserSwitcherRuntime(
                bootstrap_principal=resolved.auth_principal,
            )
            auth_resolver = dev_user_switcher_auth_resolver(dev_user_switcher_runtime)
        else:
            auth_resolver = static_auth_resolver(resolved.auth_principal)

    app = create_api_app(
        resolved.database_url,
        actor_name=resolved.actor_name,
        project_source=project_source,
        employee_source=employee_source,
        user_source=user_source,
        project_task_source=project_task_source,
        acumatica_info=(
            resolved.acumatica.safe_summary() if resolved.acumatica is not None else None
        ),
        auth_resolver=auth_resolver,
        api_docs_enabled=resolved.api_docs_enabled,
        oidc_runtime=oidc_runtime,
        break_glass_runtime=break_glass_runtime,
        dev_user_switcher_runtime=dev_user_switcher_runtime,
        communication_transport=communication_transport,
        smtp_cipher=smtp_cipher,
        smtp_client=smtp_client,
        runtime_dependencies={
            "oidc": {
                "required": resolved.auth_mode == "oidc",
                "configured": resolved.oidc is not None,
                "check": "configuration_only",
            },
            "break_glass": {
                "required": False,
                "configured": resolved.break_glass_enabled,
                "check": "database_credential",
            },
            "acumatica": {
                "required": False,
                "configured": resolved.acumatica is not None,
                "check": "configuration_only",
            },
            "m365": {
                "required": False,
                "configured": resolved.m365 is not None,
                "check": "configuration_only",
            },
            "smtp": {
                "required": False,
                "secret_key_configured": smtp_cipher is not None,
                "check": "configuration_only",
            },
        },
    )
    app.state.auth_mode = resolved.auth_mode
    app.state.embedding = resolved.embedding
    app.state.m365 = resolved.m365.safe_summary() if resolved.m365 is not None else {"configured": False}
    install_embedding_headers(app, resolved.embedding)
    if resolved.frontend_dist is not None:
        try:
            attach_frontend(app, resolved.frontend_dist, required=True)
        except FrontendBuildError as exc:
            raise ServerConfigurationError(str(exc)) from exc
    return app


def run_server(settings: ServerSettings | None = None) -> None:
    """Run one Uvicorn process using the explicit server configuration."""

    resolved = settings or ServerSettings.from_environment()
    app = create_configured_app(resolved)
    uvicorn.run(
        app,
        host=resolved.host,
        port=resolved.port,
        log_level=resolved.log_level,
        reload=False,
    )


def main() -> None:
    try:
        run_server()
    except ServerConfigurationError as exc:
        raise SystemExit(f"Configuration serveur invalide: {exc}")
