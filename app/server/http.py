from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..application import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationError,
    ApplicationFacade,
    BusinessContactAdminService,
    CompetencyCatalogService,
    DemandRequesterService,
    ApplicationNotFoundError,
    ApplicationOperationError,
    ApplicationValidationError,
    EmployeeSourcePort,
    ErpUserSourcePort,
    IdempotentCommandExecutor,
    OperationalContactService,
    PlannerQueryPort,
    ProjectSourcePort,
    ProjectTaskCatalogSourcePort,
)
from ..application.approval_progress import ApprovalProgressService
from ..application.approval_scopes import ApprovalScopeService
from ..application.communications import CommunicationService, CommunicationTransportPort
from ..application.project_communications import ProjectCommunicationService
from ..application.project_manager_admin import ProjectManagerAdminService
from ..application.smtp_settings import (
    SecretCipherPort,
    SmtpClientPort,
    SmtpConfigurationService,
)
from ..application.errors import ApplicationUnavailableError
from ..application.security import AuthPrincipal
from ..application.user_view_context import UserViewContextRepositoryPort
from ..infrastructure.smtp import SmtpClient
from ..infrastructure.sql import (
    SqlSessionFactory,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from .acumatica_project_task_sync import AcumaticaProjectTaskSyncRunner
from .composition import (
    build_approval_progress_service,
    build_approval_scope_service,
    build_business_contact_admin_service,
    build_communication_service,
    build_project_communication_service,
    build_smtp_configuration_service,
    build_competency_catalog_service,
    build_demand_requester_service,
    build_operational_contact_service,
    build_project_manager_admin_service,
    build_sql_facade,
    build_sql_idempotency_executor,
    build_sql_query_port,
    build_user_admin_service,
    build_user_view_context_repository,
)
from .break_glass import BreakGlassRuntime
from .dev_user_switcher import DevUserSwitcherRuntime
from .oidc import OidcRuntime, oidc_csrf_guard
from .performance import (
    InstrumentedJSONResponse,
    install_performance_middleware,
    install_sql_performance_instrumentation,
)
from .readiness import DatabaseReadinessError, check_database_readiness
from .routes_admin_settings import build_admin_settings_router
from .routes_approval_scopes import build_approval_scope_router
from .routes_assets import build_asset_router
from .routes_auth import build_auth_router
from .routes_break_glass import build_break_glass_router
from .routes_business_contacts import build_business_contact_router
from .routes_commands import build_command_router
from .routes_competencies import build_competency_router
from .routes_communications import build_communication_router
from .routes_project_communications import build_project_communication_router
from .routes_project_managers import build_project_manager_admin_router
from .routes_dev_user_switcher import build_dev_user_switcher_router
from .routes_delivery import build_delivery_router
from .routes_erp_users import build_erp_user_admin_router
from .routes_integrations import build_integration_router
from .routes_me import build_me_router
from .routes_operational_responsibility import build_operational_responsibility_router
from .routes_reads import build_read_router
from .routes_resource_classes import (
    build_operational_resource_class_router,
    build_resource_class_router,
)
from .routes_task_catalog import build_task_catalog_router
from .routes_user_admin import build_user_admin_router
from .security import AuthResolver, install_authorization_middleware


SessionDependency = Callable[[], Iterator[Session]]
FacadeDependency = Callable[[], Iterator[ApplicationFacade]]
IdempotencyDependency = Callable[[], Iterator[IdempotentCommandExecutor]]
QueryDependency = Callable[[], Iterator[PlannerQueryPort]]
UserAdminDependency = Callable[..., Any]
DemandRequesterDependency = Callable[[], Iterator[DemandRequesterService]]
OperationalContactDependency = Callable[[], Iterator[OperationalContactService]]
CommunicationDependency = Callable[..., Any]
ProjectCommunicationDependency = Callable[..., Any]
SmtpSettingsDependency = Callable[..., Any]
CompetencyDependency = Callable[[], Iterator[CompetencyCatalogService]]
BusinessContactDependency = Callable[[], Iterator[BusinessContactAdminService]]
ProjectManagerAdminDependency = Callable[[], Iterator[ProjectManagerAdminService]]
ApprovalScopeDependency = Callable[[], Iterator[ApprovalScopeService]]
ApprovalProgressDependency = Callable[[], Iterator[ApprovalProgressService]]
UserViewContextDependency = Callable[[], Iterator[UserViewContextRepositoryPort]]


def application_error_status(exc: ApplicationError) -> int:
    if isinstance(exc, ApplicationAuthorizationError):
        return 403
    if isinstance(exc, ApplicationValidationError):
        return 422
    if isinstance(exc, ApplicationNotFoundError):
        return 404
    if isinstance(exc, ApplicationConflictError):
        return 409
    if isinstance(exc, ApplicationUnavailableError):
        return 503
    if isinstance(exc, ApplicationOperationError):
        return 500
    return 400


def application_error_response(exc: ApplicationError) -> JSONResponse:
    return JSONResponse(
        status_code=application_error_status(exc),
        content={"error": exc.as_dict()},
    )


def make_session_dependency(factory: SqlSessionFactory) -> SessionDependency:
    def dependency() -> Iterator[Session]:
        with transactional_session(factory) as session:
            yield session

    return dependency


def _request_actor(request: Request, fallback: str) -> str:
    principal: AuthPrincipal | None = getattr(request.state, "auth_principal", None)
    return principal.display_name if principal is not None else fallback


def _request_actor_user_id(request: Request) -> str | None:
    principal: AuthPrincipal | None = getattr(request.state, "auth_principal", None)
    if principal is None:
        return None
    return str(principal.local_user_id or "").strip() or None


def _request_permissions(request: Request) -> tuple[str, ...]:
    principal: AuthPrincipal | None = getattr(request.state, "auth_principal", None)
    return tuple(principal.permissions) if principal is not None else ()


def _request_roles(request: Request) -> tuple[str, ...]:
    principal: AuthPrincipal | None = getattr(request.state, "auth_principal", None)
    return tuple(principal.roles) if principal is not None else ()


def make_facade_dependency(
    factory: SqlSessionFactory,
    *,
    actor_name: str = "api",
    session_dependency: SessionDependency | None = None,
) -> FacadeDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        request: Request,
        session: Session = Depends(request_session),
    ) -> Iterator[ApplicationFacade]:
        yield build_sql_facade(
            session,
            actor_name=_request_actor(request, actor_name),
            actor_user_id=_request_actor_user_id(request),
            permissions=_request_permissions(request),
            roles=_request_roles(request),
        )

    return dependency


def make_idempotency_dependency(
    factory: SqlSessionFactory,
    *,
    actor_name: str = "api",
    session_dependency: SessionDependency | None = None,
) -> IdempotencyDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        request: Request,
        session: Session = Depends(request_session),
    ) -> Iterator[IdempotentCommandExecutor]:
        yield build_sql_idempotency_executor(
            session,
            actor_name=_request_actor(request, actor_name),
        )

    return dependency


def make_stable_idempotency_dependency(
    factory: SqlSessionFactory,
    *,
    actor_name: str = "api",
    session_dependency: SessionDependency | None = None,
) -> IdempotencyDependency:
    """Use the authenticated stable local user id for new durable command keys."""

    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        request: Request,
        session: Session = Depends(request_session),
    ) -> Iterator[IdempotentCommandExecutor]:
        stable_actor = _request_actor_user_id(request) or _request_actor(request, actor_name)
        yield build_sql_idempotency_executor(
            session,
            actor_name=stable_actor,
        )

    return dependency


def make_query_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> QueryDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[PlannerQueryPort]:
        yield build_sql_query_port(session)

    return dependency


def make_user_view_context_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> UserViewContextDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[UserViewContextRepositoryPort]:
        yield build_user_view_context_repository(session)

    return dependency


def make_demand_requester_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> DemandRequesterDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[DemandRequesterService]:
        yield build_demand_requester_service(session)

    return dependency


def make_operational_contact_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> OperationalContactDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[OperationalContactService]:
        yield build_operational_contact_service(session)

    return dependency


def make_user_admin_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> UserAdminDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[Any]:
        yield build_user_admin_service(session)

    return dependency


def make_smtp_settings_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
    cipher: SecretCipherPort | None = None,
    client: SmtpClientPort | None = None,
) -> SmtpSettingsDependency:
    request_session = session_dependency or make_session_dependency(factory)
    resolved_client = client or SmtpClient()

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[SmtpConfigurationService]:
        yield build_smtp_configuration_service(
            session,
            cipher=cipher,
            client=resolved_client,
        )

    return dependency


def make_project_communication_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
    transport: CommunicationTransportPort | None = None,
    smtp_cipher: SecretCipherPort | None = None,
    smtp_client: SmtpClientPort | None = None,
) -> ProjectCommunicationDependency:
    request_session = session_dependency or make_session_dependency(factory)
    resolved_smtp_client = smtp_client or SmtpClient()

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[ProjectCommunicationService]:
        smtp_service = build_smtp_configuration_service(
            session,
            cipher=smtp_cipher,
            client=resolved_smtp_client,
        )
        yield build_project_communication_service(
            session,
            transport=transport,
            smtp_service=smtp_service,
        )

    return dependency


def make_competency_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> CompetencyDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[CompetencyCatalogService]:
        yield build_competency_catalog_service(session)

    return dependency


def make_business_contact_dependency(
    factory: SqlSessionFactory,
    *,
    actor_name: str = "api",
    session_dependency: SessionDependency | None = None,
) -> BusinessContactDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        request: Request,
        session: Session = Depends(request_session),
    ) -> Iterator[BusinessContactAdminService]:
        yield build_business_contact_admin_service(
            session,
            actor_name=_request_actor(request, actor_name),
            actor_user_id=_request_actor_user_id(request),
        )

    return dependency


def make_project_manager_admin_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> ProjectManagerAdminDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[ProjectManagerAdminService]:
        yield build_project_manager_admin_service(session)

    return dependency


def make_approval_progress_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> ApprovalProgressDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[ApprovalProgressService]:
        yield build_approval_progress_service(session)

    return dependency


def make_approval_scope_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
) -> ApprovalScopeDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[ApprovalScopeService]:
        yield build_approval_scope_service(session)

    return dependency


def make_communication_dependency(
    factory: SqlSessionFactory,
    *,
    session_dependency: SessionDependency | None = None,
    transport: CommunicationTransportPort | None = None,
) -> CommunicationDependency:
    request_session = session_dependency or make_session_dependency(factory)

    def dependency(
        session: Session = Depends(request_session),
    ) -> Iterator[CommunicationService]:
        yield build_communication_service(session, transport=transport)

    return dependency


def _request_validation_response(exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "location": [str(item) for item in error.get("loc", ())],
            "message": str(error.get("msg") or "Valeur invalide"),
            "type": str(error.get("type") or "validation_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "request_validation_error",
                "message": "La requête HTTP est invalide.",
                "context": {"errors": details},
            }
        },
    )


def create_api_app(
    database_url: str,
    *,
    actor_name: str = "api",
    project_source: ProjectSourcePort | None = None,
    employee_source: EmployeeSourcePort | None = None,
    user_source: ErpUserSourcePort | None = None,
    project_task_source: ProjectTaskCatalogSourcePort | None = None,
    acumatica_info: dict[str, Any] | None = None,
    auth_resolver: AuthResolver | None = None,
    api_docs_enabled: bool = True,
    oidc_runtime: OidcRuntime | None = None,
    break_glass_runtime: BreakGlassRuntime | None = None,
    dev_user_switcher_runtime: DevUserSwitcherRuntime | None = None,
    communication_transport: CommunicationTransportPort | None = None,
    smtp_cipher: SecretCipherPort | None = None,
    smtp_client: SmtpClientPort | None = None,
    performance_log_path: Path | None = None,
    runtime_dependencies: dict[str, Any] | None = None,
) -> FastAPI:
    if auth_resolver is None:
        raise ValueError(
            "create_api_app exige un auth_resolver explicite; "
            "aucune identité privilégiée implicite n'est autorisée."
        )
    if oidc_runtime is not None and dev_user_switcher_runtime is not None:
        raise ValueError("OIDC et le sélecteur d’utilisateur de développement sont mutuellement exclusifs.")

    engine = create_sql_engine(database_url)
    install_sql_performance_instrumentation(engine)
    factory = create_session_factory(engine)
    project_task_sync_runner = AcumaticaProjectTaskSyncRunner(factory, project_task_source)
    session_dependency = make_session_dependency(factory)
    facade_dependency = make_facade_dependency(
        factory,
        actor_name=actor_name,
        session_dependency=session_dependency,
    )
    idempotency_dependency = make_idempotency_dependency(
        factory,
        actor_name=actor_name,
        session_dependency=session_dependency,
    )
    stable_idempotency_dependency = make_stable_idempotency_dependency(
        factory,
        actor_name=actor_name,
        session_dependency=session_dependency,
    )
    query_dependency = make_query_dependency(
        factory,
        session_dependency=session_dependency,
    )
    user_view_context_dependency = make_user_view_context_dependency(
        factory,
        session_dependency=session_dependency,
    )
    user_admin_dependency = make_user_admin_dependency(
        factory,
        session_dependency=session_dependency,
    )
    demand_requester_dependency = make_demand_requester_dependency(
        factory,
        session_dependency=session_dependency,
    )
    operational_contact_dependency = make_operational_contact_dependency(
        factory,
        session_dependency=session_dependency,
    )
    communication_dependency = make_communication_dependency(
        factory,
        session_dependency=session_dependency,
        transport=communication_transport,
    )
    smtp_settings_dependency = make_smtp_settings_dependency(
        factory,
        session_dependency=session_dependency,
        cipher=smtp_cipher,
        client=smtp_client,
    )
    project_communication_dependency = make_project_communication_dependency(
        factory,
        session_dependency=session_dependency,
        transport=communication_transport,
        smtp_cipher=smtp_cipher,
        smtp_client=smtp_client,
    )
    competency_dependency = make_competency_dependency(
        factory,
        session_dependency=session_dependency,
    )
    business_contact_dependency = make_business_contact_dependency(
        factory,
        actor_name=actor_name,
        session_dependency=session_dependency,
    )
    project_manager_admin_dependency = make_project_manager_admin_dependency(
        factory,
        session_dependency=session_dependency,
    )
    approval_scope_dependency = make_approval_scope_dependency(
        factory,
        session_dependency=session_dependency,
    )
    approval_progress_dependency = make_approval_progress_dependency(
        factory,
        session_dependency=session_dependency,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        project_task_sync_runner.reconcile_interrupted_runs()
        try:
            yield
        finally:
            project_task_sync_runner.shutdown()
            engine.dispose()

    app = FastAPI(
        title="RessourcePlanner API",
        version="1.0.0-dev",
        lifespan=lifespan,
        docs_url="/docs" if api_docs_enabled else None,
        redoc_url="/redoc" if api_docs_enabled else None,
        openapi_url="/openapi.json" if api_docs_enabled else None,
        default_response_class=InstrumentedJSONResponse,
    )
    app.state.database_dialect = engine.dialect.name
    app.state.session_factory = factory
    app.state.facade_dependency = facade_dependency
    app.state.idempotency_dependency = idempotency_dependency
    app.state.stable_idempotency_dependency = stable_idempotency_dependency
    app.state.query_dependency = query_dependency
    app.state.operational_contact_dependency = operational_contact_dependency
    app.state.user_admin_dependency = user_admin_dependency
    app.state.user_view_context_dependency = user_view_context_dependency
    app.state.communication_dependency = communication_dependency
    app.state.smtp_settings_dependency = smtp_settings_dependency
    app.state.project_communication_dependency = project_communication_dependency
    app.state.competency_dependency = competency_dependency
    app.state.business_contact_dependency = business_contact_dependency
    app.state.project_manager_admin_dependency = project_manager_admin_dependency
    app.state.approval_scope_dependency = approval_scope_dependency
    app.state.approval_progress_dependency = approval_progress_dependency
    app.state.runtime_dependencies = dict(runtime_dependencies or {})
    app.state.project_task_sync_runner = project_task_sync_runner
    app.state.dev_user_switcher_enabled = dev_user_switcher_runtime is not None

    install_authorization_middleware(
        app,
        auth_resolver,
        csrf_guard=(
            oidc_csrf_guard(oidc_runtime or break_glass_runtime)
            if (oidc_runtime is not None or break_glass_runtime is not None)
            else None
        ),
    )
    # Registered after authorization so Starlette wraps it outside auth and the
    # request performance context is already active while credentials are resolved.
    install_performance_middleware(app, log_path=performance_log_path)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return _request_validation_response(exc)

    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        _request: Request,
        exc: ApplicationError,
    ) -> JSONResponse:
        return application_error_response(exc)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        _request: Request,
        _exc: Exception,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Une erreur interne est survenue.",
                    "context": {},
                }
            },
        )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        """Process liveness only; intentionally does not touch SQL or external systems."""
        return {
            "status": "ok",
            "api": "v1",
        }

    @app.get("/ready", tags=["system"], response_model=None)
    def readiness() -> Any:
        """Required local dependencies only; external integrations are informational."""
        dependencies = dict(app.state.runtime_dependencies)
        try:
            database = check_database_readiness(engine)
        except DatabaseReadinessError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "api": "v1",
                    "database": {
                        "status": "error",
                        "reason": exc.code,
                    },
                    "external_dependencies": dependencies,
                },
            )
        return {
            "status": "ready",
            "api": "v1",
            "database": database,
            "external_dependencies": dependencies,
        }

    app.include_router(build_auth_router(oidc_runtime))
    app.include_router(build_break_glass_router(break_glass_runtime))
    if dev_user_switcher_runtime is not None:
        app.include_router(build_dev_user_switcher_router(dev_user_switcher_runtime))
    app.include_router(build_user_admin_router(user_admin_dependency))
    app.include_router(build_erp_user_admin_router(user_admin_dependency))
    app.include_router(build_admin_settings_router(smtp_settings_dependency))
    app.include_router(build_approval_scope_router(approval_scope_dependency))
    app.include_router(build_resource_class_router(session_dependency))
    app.include_router(
        build_operational_resource_class_router(session_dependency)
    )
    app.include_router(
        build_command_router(
            facade_dependency,
            idempotency_dependency,
            competency_dependency,
            stable_idempotency_dependency,
        )
    )
    app.include_router(
        build_read_router(
            query_dependency,
            user_view_context_dependency,
            demand_requester_dependency,
            operational_contact_dependency,
            approval_progress_dependency,
        )
    )
    app.include_router(build_competency_router(competency_dependency))
    app.include_router(
        build_business_contact_router(
            business_contact_dependency,
            session_dependency,
        )
    )
    app.include_router(build_operational_responsibility_router(session_dependency))
    app.include_router(
        build_project_manager_admin_router(
            project_manager_admin_dependency,
            user_view_context_dependency,
        )
    )
    app.include_router(build_task_catalog_router(session_dependency))
    app.include_router(build_asset_router(session_dependency))
    app.include_router(build_delivery_router(session_dependency))
    app.include_router(
        build_me_router(
            query_dependency,
            user_view_context_dependency,
        )
    )
    app.include_router(build_communication_router(communication_dependency))
    app.include_router(
        build_project_communication_router(project_communication_dependency)
    )
    app.include_router(
        build_integration_router(
            session_dependency,
            session_factory=factory,
            project_source=project_source,
            employee_source=employee_source,
            user_source=user_source,
            project_task_source=project_task_source,
            project_task_sync_runner=project_task_sync_runner,
            acumatica_info=acumatica_info,
        )
    )
    return app
