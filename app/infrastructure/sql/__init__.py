"""SQL-backed persistence foundation for RessourcePlanner.

This package contains database infrastructure only. Application services and the pure
planning engine must continue to depend on ports/read models rather than SQLAlchemy.
"""

from .approval_envelope_policy_repository import SqlDemandApprovalEnvelopePolicyRepository
from .approval_cycle_models import (
    ApprovalDecision,
    ApprovalRequirement,
    ApprovalRequirementApprover,
    RequestApprovalCycle,
)
from .approval_cycle_repository import SqlApprovalCycleRepository
from .approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    ResourceClassApprovalScopeMapping,
    TaskApprovalScopeMapping,
)
from .approval_scope_repository import SqlApprovalScopeRepository
from .resource_class_models import (
    ProjectTaskClassOverride,
    ResourceClassConfig,
    TaskClassStandard,
)
from .resource_class_repository import SqlResourceClassRepository
from .asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
    AssetUnavailability,
)
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    APPROVAL_REFERENCE_LEGACY_UNKNOWN,
    APPROVAL_REFERENCE_NOT_APPLICABLE,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .approval_revision_repository import SqlRequestApprovalRevisionRepository
from .active_days_query_repository import (
    SqlPlannerQueryRepositoryWithEstimatedDays,
    SqlSegmentRepositoryWithActiveDayMetrics,
)
from .auth_session_repository import LoginTransactionRecord, SqlAuthSessionRepository
from .base import Base, NAMING_CONVENTION, new_id
from .business_contact_models import BusinessContact
from .business_contact_admin_repository import SqlBusinessContactAdminRepository
from .command_adapters import (
    SqlAllocationCommandAdapter,
    SqlApprovedDemandSyncAdapter,
    SqlPlanningCommandAdapter,
)
from .communication_models import (
    CommunicationBatchRow,
    CommunicationContact,
    CommunicationDeliveryRow,
    CommunicationMessageRow,
    CommunicationSnapshotLine,
    SmtpConfigurationAuditRow,
    SmtpConfigurationRow,
)
from .communication_repository import SqlCommunicationRepository
from .composite_allocation import SqlCompositeAllocationCommandAdapter
from .smtp_settings_repository import SqlSmtpConfigurationRepository
from .competency_catalog_repository import SqlCompetencyCatalogRepository
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
)
from .demand_period_repository import SqlDemandPeriodRepository
from .demand_repository import SqlDemandRepository
from .emergency_demand_repository import SqlEmergencyDemandRepository
from .emergency_query_repository import SqlPlannerQueryRepositoryWithEmergencyOverride
from .employee_sync_repository import SqlEmployeeSyncRepository
from .erp_user_models import ErpUserDirectoryEntry
from .erp_user_directory_repository import SqlErpUserDirectoryRepository
from .idempotency import CommandIdempotencyReceipt, SqlCommandIdempotencyAdapter
from .identity_models import AppUser, AuthLoginTransaction, AuthSession
from .identity_repository import SqlUserIdentityRepository
from .identity_resource_link_repository import SqlIdentityResourceLinkRepository
from .load_profile_audit import LoadProfileAuditedSegmentRepository
from .load_profile_query_repository import SqlPlannerQueryRepositoryWithLoadProfiles
from .models import (
    ORIGIN_AD_HOC,
    ORIGIN_QUICK_SHIFT,
    ORIGIN_REQUEST,
    Competency,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceCompetency,
    ResourceRequirementCompetency,
    TaskCatalogEntry,
    TaskCatalogProjectSyncState,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestCompetency,
    WorkforceRequestHistory,
    WorkPackage,
)
from .identity_constraints import (
    RESOURCE_REQUIREMENT_NUMBER_INDEX,
    WORKFORCE_REQUEST_NUMBER_INDEX,
)
from .resource_identity_constraints import RESOURCE_EXTERNAL_ID_INDEX
from .operational_choice_models import RequestOperationalState
from .operational_choice_repository import SqlRequestOperationalChoiceRepository
from .operational_contact_repository import SqlOperationalContactRepository
from .project_communication_repository import SqlProjectCommunicationRepository
from .period_approved_sync import SqlPeriodAwareApprovedDemandSyncAdapter
from .planning_audit import PlanningChangeHistory
from .planning_authorization_repository import SqlRequestPlanningAuthorizationRepository
from .planning_repository import SqlPlanningReadRepository
from .planning_version import (
    PlanningMutationState,
    SqlPlanningMutationVersionRepository,
)
from .capacity_query_repository import SqlPlannerQueryRepository
from .project_sync_repository import SqlProjectSyncRepository
from .task_catalog_repository import SqlTaskCatalogRepository
from .task_catalog_workforce_policy import SqlTaskCatalogWorkforcePolicy
from .resource_admin_repository import SqlResourceAdminRepository
from .segment_repository import SqlSegmentRepository
from .overallocation import (
    OverallocationAuditedAllocationCommandAdapter,
    OverallocationAuditedSegmentRepository,
    SqlOverallocationAllocationCommandAdapter,
    SqlPlannerQueryRepositoryWithOverallocation,
    SqlSegmentRepositoryWithAllocationMetrics,
)
from .session import (
    SqlSessionFactory,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from .work_package_repository import SqlWorkPackageRepository

__all__ = [
    "APPROVAL_REFERENCE_CAPTURED",
    "APPROVAL_REFERENCE_LEGACY_UNKNOWN",
    "APPROVAL_REFERENCE_NOT_APPLICABLE",
    "AppUser",
    "ApprovalDecision",
    "ApprovalRequirement",
    "ApprovalRequirementApprover",
    "ApprovalScope",
    "ApprovalScopeApprover",
    "AuthLoginTransaction",
    "AuthSession",
    "Asset",
    "AssetAllocation",
    "AssetRequirement",
    "AssetType",
    "AssetTypeCompetency",
    "AssetUnavailability",
    "Base",
    "BusinessContact",
    "CommandIdempotencyReceipt",
    "Competency",
    "CommunicationBatchRow",
    "CommunicationContact",
    "CommunicationDeliveryRow",
    "CommunicationMessageRow",
    "CommunicationSnapshotLine",
    "SmtpConfigurationAuditRow",
    "SmtpConfigurationRow",
    "LoadProfileAuditedSegmentRepository",
    "LoginTransactionRecord",
    "NAMING_CONVENTION",
    "ORIGIN_AD_HOC",
    "ORIGIN_QUICK_SHIFT",
    "ORIGIN_REQUEST",
    "OverallocationAuditedAllocationCommandAdapter",
    "OverallocationAuditedSegmentRepository",
    "PlanningChangeHistory",
    "PlanningMutationState",
    "Project",
    "RequestLine",
    "RequestApprovalCycle",
    "RequestApprovalReference",
    "RequestApprovalRevision",
    "RequestOperationalState",
    "RequestLineCompetency",
    "RESOURCE_EXTERNAL_ID_INDEX",
    "RESOURCE_REQUIREMENT_NUMBER_INDEX",
    "Resource",
    "ResourceClassApprovalScopeMapping",
    "ResourceClassConfig",
    "ResourceAvailabilityRule",
    "ResourceCompetency",
    "ResourceRequirement",
    "ResourceRequirementCompetency",
    "Shift",
    "ProjectTaskClassOverride",
    "TaskClassStandard",
    "TaskApprovalScopeMapping",
    "TaskCatalogEntry",
    "TaskCatalogProjectSyncState",
    "SqlAllocationCommandAdapter",
    "SqlApprovalCycleRepository",
    "SqlApprovalScopeRepository",
    "SqlResourceClassRepository",
    "SqlApprovedDemandSyncAdapter",
    "SqlAuthSessionRepository",
    "SqlBusinessContactAdminRepository",
    "SqlCommandIdempotencyAdapter",
    "SqlCommunicationRepository",
    "SqlCompositeAllocationCommandAdapter",
    "SqlSmtpConfigurationRepository",
    "SqlCompetencyCatalogRepository",
    "SqlDemandApprovalEnvelopePolicyRepository",
    "SqlDemandPeriodRepository",
    "SqlDemandRepository",
    "SqlEmergencyDemandRepository",
    "SqlEmployeeSyncRepository",
    "SqlErpUserDirectoryRepository",
    "ErpUserDirectoryEntry",
    "SqlIdentityResourceLinkRepository",
    "SqlOperationalContactRepository",
    "SqlRequestOperationalChoiceRepository",
    "SqlProjectCommunicationRepository",
    "SqlOverallocationAllocationCommandAdapter",
    "SqlPeriodAwareApprovedDemandSyncAdapter",
    "SqlPlannerQueryRepository",
    "SqlPlannerQueryRepositoryWithEmergencyOverride",
    "SqlPlannerQueryRepositoryWithEstimatedDays",
    "SqlPlannerQueryRepositoryWithLoadProfiles",
    "SqlPlannerQueryRepositoryWithOverallocation",
    "SqlPlanningCommandAdapter",
    "SqlRequestPlanningAuthorizationRepository",
    "SqlPlanningReadRepository",
    "SqlPlanningMutationVersionRepository",
    "SqlProjectSyncRepository",
    "SqlTaskCatalogRepository",
    "SqlTaskCatalogWorkforcePolicy",
    "SqlRequestApprovalRevisionRepository",
    "SqlResourceAdminRepository",
    "SqlSegmentRepository",
    "SqlSegmentRepositoryWithActiveDayMetrics",
    "SqlSegmentRepositoryWithAllocationMetrics",
    "SqlSessionFactory",
    "SqlUserIdentityRepository",
    "SqlWorkPackageRepository",
    "WORKFORCE_REQUEST_NUMBER_INDEX",
    "WorkPackage",
    "WorkforceRequest",
    "WorkforceRequestCompetency",
    "WorkforceRequestHistory",
    "WorkforceRequestPeriod",
    "WorkforceRequestPeriodRequirement",
    "WorkforceRequestPeriodSelection",
    "create_session_factory",
    "create_sql_engine",
    "new_id",
    "transactional_session",
]
