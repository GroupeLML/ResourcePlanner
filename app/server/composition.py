from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from ..application import (
    AllocationService,
    ApplicationFacade,
    CompositeAllocationService,
    BusinessContactAdminService,
    CompetencyCatalogService,
    DemandRequesterService,
    EmergencyApplicationFacade,
    EmergencyDemandService,
    IdempotentCommandExecutor,
    PlannerQueryPort,
    PlanningService,
    ResourceAdminService,
    WorkPackageService,
)
from ..application.approval_cycles import ApprovalCycleService
from ..application.approval_progress import ApprovalProgressService
from ..application.approval_scopes import ApprovalScopeService
from ..application.approval_voting import ApprovalVoteService
from ..application.communications import CommunicationService, CommunicationTransportPort
from ..application.operational_contacts import OperationalContactService
from ..application.project_manager_admin import ProjectManagerAdminService
from ..application.project_managers import ProjectManagerResolutionService
from ..application.project_communications import ProjectCommunicationService
from ..application.smtp_settings import (
    SecretCipherPort,
    SmtpClientPort,
    SmtpConfigurationService,
)
from ..application.quick_shift_service import QuickShiftService
from ..application.segment_service import SegmentService
from ..application.user_admin import UserAdminService
from ..application.user_view_context import UserViewContextRepositoryPort
from ..infrastructure.sql import (
    LoadProfileAuditedSegmentRepository,
    OverallocationAuditedAllocationCommandAdapter,
    OverallocationAuditedSegmentRepository,
    SqlApprovalCycleRepository,
    SqlApprovalScopeRepository,
    SqlBreakGlassRepository,
    SqlBusinessContactAdminRepository,
    SqlCommandIdempotencyAdapter,
    SqlCompositeAllocationCommandAdapter,
    SqlCompetencyCatalogRepository,
    SqlDemandApprovalEnvelopePolicyRepository,
    SqlDemandPeriodRepository,
    SqlEmergencyDemandRepository,
    SqlErpUserDirectoryRepository,
    SqlIdentityAdminAuditRepository,
    SqlOverallocationAllocationCommandAdapter,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    SqlOperationalContactRepository,
    SqlRequestOperationalChoiceRepository,
    SqlProjectCommunicationRepository,
    SqlProjectCoManagerRepository,
    SqlProjectManagerResolutionRepository,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    SqlPlanningCommandAdapter,
    SqlPlanningMutationVersionRepository,
    SqlRequestPlanningAuthorizationRepository,
    SqlResourceAdminRepository,
    SqlSmtpConfigurationRepository,
    SqlSegmentRepositoryWithActiveDayMetrics,
    SqlUserIdentityRepository,
    SqlWorkPackageRepository,
)
from ..infrastructure.sql.communication_repository import SqlCommunicationRepository
from ..infrastructure.sql.project_manager_admin_repository import (
    SqlProjectManagerAdminProjectRepository,
)
from ..infrastructure.sql.user_view_context_repository import SqlUserViewContextRepository
from ..infrastructure.sql.emergency_planning_audit import (
    EmergencyAwareApprovedDemandSyncAdapter,
)
from ..infrastructure.sql.planning_audit import (
    AuditedSegmentRepository,
    SqlPlanningAuditJournal,
)


def build_sql_facade(
    session: Session,
    *,
    actor_name: str = "api",
    actor_user_id: str | None = None,
    permissions: Sequence[str] | None = None,
    roles: Sequence[str] | None = None,
) -> ApplicationFacade:
    """Compose one application facade inside the caller-owned SQL transaction."""

    actor = str(actor_name or "api").strip() or "api"
    identities = SqlUserIdentityRepository(session)
    journal = SqlPlanningAuditJournal(session, actor_name=actor)
    demands = SqlEmergencyDemandRepository(
        session,
        actor_name=actor,
        actor_user_id=actor_user_id,
    )
    periods = SqlDemandPeriodRepository(session, actor_name=actor)
    base_segments = SqlSegmentRepositoryWithActiveDayMetrics(session, actor_name=actor)
    segments = OverallocationAuditedSegmentRepository(
        LoadProfileAuditedSegmentRepository(
            AuditedSegmentRepository(base_segments, journal),
            journal,
        ),
        journal,
    )
    work_packages = SqlWorkPackageRepository(session, actor_user_id=actor_user_id)
    resources = SqlResourceAdminRepository(session)
    planning_versions = SqlPlanningMutationVersionRepository(session)
    planning_commands = SqlPlanningCommandAdapter(
        session,
        versioning=planning_versions,
    )
    planning_authorization = SqlRequestPlanningAuthorizationRepository(
        session,
        actor_name=actor,
        roles=roles,
    )
    allocation_commands = OverallocationAuditedAllocationCommandAdapter(
        SqlOverallocationAllocationCommandAdapter(
            session,
            planning=planning_commands,
            authorization=planning_authorization,
            versioning=planning_versions,
        ),
        journal,
        session,
    )
    composite_allocation_commands = SqlCompositeAllocationCommandAdapter(
        session,
        planning=planning_commands,
        authorization=planning_authorization,
        versioning=planning_versions,
        journal=journal,
        actor_user_id=actor_user_id,
        permissions=permissions,
    )
    query_port = SqlPlannerQueryRepositoryWithLoadProfiles(session)
    approved_sync = EmergencyAwareApprovedDemandSyncAdapter(
        SqlPeriodAwareApprovedDemandSyncAdapter(session),
        journal,
        session,
        versioning=planning_versions,
    )
    approval_scope_service = ApprovalScopeService(SqlApprovalScopeRepository(session))
    approval_cycle_repository = SqlApprovalCycleRepository(
        session,
        actor_user_id=actor_user_id,
        actor_name=actor,
    )
    approval_cycle_service = ApprovalCycleService(
        approval_cycle_repository,
        approval_scope_service,
    )
    approval_vote_service = ApprovalVoteService(
        cycles=approval_cycle_service,
        repository=approval_cycle_repository,
        planning_versions=planning_versions,
        approved_sync=approved_sync,
        planning=planning_commands,
        current_user_id=actor_user_id,
        current_user_name=actor,
        permissions=(
            tuple(permissions)
            if permissions is not None
            else ("manage_demands", "approve_demands")
        ),
    )

    return EmergencyApplicationFacade(
        demands=EmergencyDemandService(
            demands,
            planning_commands,
            approved_sync,
            periods=periods,
            operational_choices=SqlRequestOperationalChoiceRepository(
                session,
                actor_name=actor,
            ),
            approval_envelope_policy=SqlDemandApprovalEnvelopePolicyRepository(
                session,
                actor_name=actor,
            ),
            requester_directory=identities,
            current_user=actor,
            current_user_id=actor_user_id,
            permissions=permissions,
            roles=roles,
            planning_versions=planning_versions,
            queries=query_port,
            approval_cycles=approval_cycle_service,
            approval_votes=approval_vote_service,
        ),
        segments=SegmentService(segments, planning_commands, planning_authorization),
        allocations=AllocationService(allocation_commands),
        quick_shifts=QuickShiftService(segments, allocation_commands),
        composite_allocations=CompositeAllocationService(composite_allocation_commands),
        planning=PlanningService(planning_commands),
        work_packages=WorkPackageService(work_packages),
        resource_admin=ResourceAdminService(resources),
        planning_versions=planning_versions,
    )


def build_sql_idempotency_executor(
    session: Session,
    *,
    actor_name: str = "api",
) -> IdempotentCommandExecutor:
    """Compose durable command replay inside the same transaction as the facade."""

    actor = str(actor_name or "api").strip() or "api"
    return IdempotentCommandExecutor(
        SqlCommandIdempotencyAdapter(session, actor_name=actor)
    )


def build_sql_query_port(session: Session) -> PlannerQueryPort:
    """Compose the canonical read-only query port for one request transaction."""

    return SqlPlannerQueryRepositoryWithLoadProfiles(session)


def build_user_admin_service(session: Session) -> UserAdminService:
    """Compose local identity administration inside the request transaction."""

    return UserAdminService(
        SqlUserIdentityRepository(session),
        erp_directory=SqlErpUserDirectoryRepository(session),
        audit=SqlIdentityAdminAuditRepository(session),
        break_glass_protection=SqlBreakGlassRepository(session),
    )


def build_approval_scope_service(session: Session) -> ApprovalScopeService:
    """Compose #276 approval-scope administration and pure resolution."""

    return ApprovalScopeService(SqlApprovalScopeRepository(session))


def build_approval_progress_service(session: Session) -> ApprovalProgressService:
    """Compose the common #276D approval-cycle read projection."""

    repository = SqlApprovalCycleRepository(session)
    cycles = ApprovalCycleService(
        repository,
        ApprovalScopeService(SqlApprovalScopeRepository(session)),
    )
    return ApprovalProgressService(cycles, repository)


def build_demand_requester_service(session: Session) -> DemandRequesterService:
    """Compose the read-only directory used by demand requester selectors."""

    return DemandRequesterService(SqlUserIdentityRepository(session))


def build_operational_contact_service(
    session: Session,
) -> OperationalContactService:
    """Compose batched operational-contact resolution for demand-detail reads."""

    return OperationalContactService(SqlOperationalContactRepository(session))


def build_user_view_context_repository(
    session: Session,
) -> UserViewContextRepositoryPort:
    """Compose stable current-user relationship reads for one request transaction."""

    return SqlUserViewContextRepository(session)


def build_competency_catalog_service(session: Session) -> CompetencyCatalogService:
    """Compose competency catalogue operations inside the request transaction."""

    return CompetencyCatalogService(SqlCompetencyCatalogRepository(session))


def build_communication_service(
    session: Session,
    *,
    transport: CommunicationTransportPort | None = None,
) -> CommunicationService:
    """Compose controlled communication preparation and explicit external draft creation."""

    return CommunicationService(SqlCommunicationRepository(session), transport=transport)


def build_smtp_configuration_service(
    session: Session,
    *,
    cipher: SecretCipherPort | None,
    client: SmtpClientPort,
) -> SmtpConfigurationService:
    """Compose SMTP administration and delivery configuration."""

    return SmtpConfigurationService(
        SqlSmtpConfigurationRepository(session),
        cipher=cipher,
        client=client,
    )


def build_project_communication_service(
    session: Session,
    *,
    transport: CommunicationTransportPort | None = None,
    smtp_service: SmtpConfigurationService | None = None,
) -> ProjectCommunicationService:
    """Compose the #290 project-centric communication projection."""

    operational = OperationalContactService(
        SqlOperationalContactRepository(session)
    )
    return ProjectCommunicationService(
        SqlProjectCommunicationRepository(
            session,
            operational_contacts=operational,
        ),
        workflow_repository=SqlCommunicationRepository(session),
        transport=transport,
        smtp_service=smtp_service,
    )


def build_business_contact_admin_service(
    session: Session,
    *,
    actor_name: str = "api",
    actor_user_id: str | None = None,
) -> BusinessContactAdminService:
    """Compose business-contact administration inside the request transaction."""

    actor = str(actor_name or "api").strip() or "api"
    approval_cycle_repository = SqlApprovalCycleRepository(
        session,
        actor_user_id=actor_user_id,
        actor_name=actor,
    )
    approval_cycles = ApprovalCycleService(
        approval_cycle_repository,
        ApprovalScopeService(SqlApprovalScopeRepository(session)),
    )
    return BusinessContactAdminService(
        SqlBusinessContactAdminRepository(session),
        approval_cycles=approval_cycles,
    )


def build_project_manager_admin_service(
    session: Session,
) -> ProjectManagerAdminService:
    """Compose canonical project-manager reads and RP co-manager mutations."""

    return ProjectManagerAdminService(
        SqlProjectManagerAdminProjectRepository(session),
        ProjectManagerResolutionService(
            SqlProjectManagerResolutionRepository(session)
        ),
        SqlProjectCoManagerRepository(session),
    )
