import { csrfHeaders } from "./csrf";

export type ViewScope = "mine" | "global";

export type ProjectReadModel = {
  id: string;
  number: string;
  name: string;
  client: string | null;
  project_manager: string | null;
  status: string;
  active: boolean;
  erp_external_id: string | null;
};

export type AcumaticaIntegrationStatus = {
  configured: boolean;
  project_tasks_configured?: boolean;
  endpoint?: string;
  version?: string;
  entity?: string;
  page_size?: number;
};

export type ProjectSyncResult = {
  received: number;
  created: number;
  updated: number;
  unchanged: number;
};

export type ProjectTaskSyncResult = {
  project_number: string;
  source_rows: number;
  task_count: number;
  rejected_rows: number;
  created: number;
  updated: number;
  unchanged: number;
  deactivated: number;
  ignored: number;
  duration_ms: number | null;
};

export type GlobalProjectTaskSyncStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "COMPLETED_WITH_ERRORS"
  | "FAILED"
  | "INTERRUPTED";

export type GlobalProjectTaskSyncResult = {
  run_id: string;
  status: GlobalProjectTaskSyncStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  projects_total: number;
  projects_inspected: number;
  projects_processed: number;
  projects_synchronized: number;
  projects_ignored: number;
  projects_rejected: number;
  source_rows_received: number;
  source_rows_rejected: number;
  tasks_received: number;
  tasks_created: number;
  tasks_updated: number;
  tasks_unchanged: number;
  tasks_deactivated: number;
  tasks_rejected: number;
  source_requests: number | null;
  source_rows_scanned: number | null;
  source_read_duration_ms: number | null;
  duration_ms: number | null;
  error_code: string | null;
  diagnostic: string | null;
  project_results: Array<{
    project_id: string;
    project_number: string;
    status: "synchronized" | "ignored" | "rejected";
    error_code: string | null;
    reason_code: string | null;
    source_rows: number;
    source_rows_rejected: number;
    tasks_received: number;
    created: number;
    updated: number;
    unchanged: number;
    deactivated: number;
    rejected: number;
    duration_ms: number | null;
  }>;
};

export type ProjectTaskSyncMetadata = {
  project_number: string;
  last_success_at: string;
  source_rows: number;
  task_count: number;
  rejected_rows: number;
  duration_ms: number | null;
  last_error_code: string | null;
};

export type BusinessContactReadModel = {
  id: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  active: boolean;
  source: string;
  external_system: string | null;
  external_entity: string | null;
  external_id: string | null;
  version: number;
};

export type ContactLinkReadModel = {
  entity_type: "PROJECT" | "TASK" | "RESOURCE" | "DEMAND";
  entity_id: string;
  entity_label: string;
  project_manager_contact_id: string | null;
  operational_responsible_contact_id: string | null;
  coordinator_contact_id: string | null;
  operational_responsible_override_contact_id: string | null;
  aggregate_version: number | null;
  status: string | null;
};

export type ProjectManagerResolutionStatus =
  | "RESOLVED"
  | "UNRESOLVED_USER"
  | "UNRESOLVED_CONTACT"
  | "INVALID_REFERENCE"
  | "IDENTITY_CONFLICT";

export type EffectiveProjectManagerReadModel = {
  sources: Array<"ERP" | "RP">;
  employee_external_id: string | null;
  erp_display_name: string | null;
  app_user_id: string | null;
  business_contact_id: string | null;
  display_name: string;
  user_active: boolean | null;
  contact_active: boolean | null;
  resolution_status: ProjectManagerResolutionStatus;
  diagnostics: string[];
};

export type ProjectManagersReadModel = {
  project_id: string;
  project_number: string;
  co_managers_version: number;
  primary: EffectiveProjectManagerReadModel | null;
  co_managers: EffectiveProjectManagerReadModel[];
  diagnostics: string[];
};

export type ProjectCoManagerMutationResult = {
  project_id: string;
  business_contact_id: string;
  version: number;
  action: "PROJECT_CO_MANAGER_ADDED" | "PROJECT_CO_MANAGER_REMOVED";
};

export type ContactResolutionReadModel = {
  status: "RESOLVED" | "UNRESOLVED" | "INVALID_REFERENCE" | "INACTIVE";
  contact_id: string | null;
  display_name: string | null;
  email: string | null;
  phone: string | null;
  source_type: string;
  source_entity_id: string | null;
  source_label: string | null;
  diagnostics: string[];
};

export type RequestLineContactResolutionReadModel = {
  line_id: string;
  demand_number: string | null;
  project_number: string;
  task_id: string | null;
  task_code: string | null;
  task_label: string | null;
  proposed_resource_id: string | null;
  proposed_resource_name: string | null;
  operational_responsible: ContactResolutionReadModel;
  coordinator: ContactResolutionReadModel;
  diagnostics: string[];
};

export type DemandOverrideMutationResult = {
  demand_number: string;
  contact_id: string | null;
  version: number;
  status: string;
  reapproval_required: boolean;
  changed: boolean;
};

export type MaterializedContactResolutionReadModel = {
  subject_type: "RESOURCE_REQUIREMENT" | "SHIFT";
  subject_id: string;
  requirement_id: string;
  shift_id: string | null;
  request_line_id: string | null;
  demand_number: string | null;
  project_number: string;
  approved_request_version: number | null;
  approved_contact_context_status: string;
  task_id: string | null;
  task_code: string | null;
  task_label: string | null;
  resource_id: string | null;
  resource_name: string | null;
  operational_responsible: ContactResolutionReadModel;
  coordinator: ContactResolutionReadModel;
  diagnostics: string[];
};

export type ProjectOperationalResponsibilityReadModel = {
  project_id: string;
  project_number: string;
  override_contact_id: string | null;
  override_version: number;
  operational_responsible: ContactResolutionReadModel;
};

export type OperationalResponsibilityMutationResult = {
  entity_type: "PROJECT" | "SEGMENT" | "SHIFT";
  entity_id: string;
  override_contact_id: string | null;
  project_override_version: number | null;
  planning_version: number | null;
  auto_source_converted: boolean;
};

export type TaskCatalogItemReadModel = {
  id: string | null;
  project_number: string;
  code: string;
  label: string;
  status: string;
  active: boolean;
  billing_rule: string | null;
  allocation_rule: string | null;
  completion_percent: number | null;
  erp_created_at: string | null;
  branch: string | null;
  approver_name: string | null;
  cv_enabled: boolean | null;
  time_entry_enabled: boolean | null;
  expenses_enabled: boolean | null;
  operational_responsible_contact_id: string | null;
  coordinator_contact_id: string | null;
  resource_class_code: string | null;
};

export type CompetencyReadModel = {
  id: string;
  name: string;
  description: string | null;
  active: boolean;
  sort_order: number;
  resource_class_code: string | null;
  resource_class_label: string | null;
  resource_class_active: boolean | null;
  resource_class_version: number;
};

export type CompetencyResourceClassMutationResult = {
  competency_id: string;
  resource_class_code: string | null;
  version: number;
  action: string;
};

export type CompetencyWrite = {
  name: string;
  description: string | null;
  active: boolean;
  sort_order: number;
};

export type CompetencyMutationResult = {
  competency_id: string;
  action: string;
};

export type WorkPackageReadModel = {
  id: string;
  reference: string;
  project_number: string;
  code: string | null;
  name: string;
  description: string | null;
  start_date: string | null;
  end_date: string | null;
  planned_hours: number | null;
  status: string;
  status_diagnostic: string | null;
  task_catalog_item_id: string | null;
  task_code: string | null;
  task_label: string | null;
  resource_class_code: string | null;
  resource_class_label: string | null;
  resource_class_active: boolean | null;
  task_resource_class_code: string | null;
  resource_class_diagnostic: string | null;
  version: number;
};

export type WorkPackageWrite = {
  project_number: string;
  task_catalog_item_id: string | null;
  code?: string | null;
  name: string;
  description: string | null;
  start_date: string | null;
  end_date: string | null;
  planned_hours: number | null;
  resource_class_code?: string | null;
  load_intervals?: WorkPackageLoadIntervalWrite[];
};

export type WorkPackageMutationResult = {
  reference: string;
  action: string;
  version: number;
};

export type MediumTermWeeklyLoadReadModel = {
  week_start: string;
  hours: number;
  explicit_hours: number;
  automatic_hours: number;
};

export type WorkPackageLoadIntervalReadModel = {
  id: string;
  start_date: string;
  end_date: string;
  hours: number;
  origin: "MANUAL" | "LEGACY_AUTO" | "LEGACY_MANUAL";
};

export type WorkPackageLoadIntervalWrite = {
  id?: string | null;
  start_date: string;
  end_date: string;
  hours: number;
};

export type MediumTermDemandPeriodReadModel = {
  demand_number: string;
  line_id: string;
  period_id: string;
  work_package_ref: string;
  start_date: string | null;
  end_date: string | null;
  hours: number | null;
  status: string;
  provenance: "CANDIDATE" | "APPROVED";
  line_kind: string;
  period_kind: "BASE" | "CUMULATIVE" | "ALTERNATIVE" | string;
  alternative_group: string | null;
  selected: boolean;
  confirmation: string | null;
  outside_work_package: boolean;
  outside_position: "NONE" | "BEFORE" | "AFTER" | "BOTH" | "UNAVAILABLE";
  diagnostics: string[];
};

export type MediumTermBudgetWorkPackageReadModel = {
  id: string;
  reference: string;
  code: string | null;
  name: string;
  planned_hours: number | null;
  status: string;
  status_diagnostic: string | null;
  budget_included: boolean;
  project_id: string;
  project_number: string;
  project_name: string;
  start_date: string | null;
  end_date: string | null;
  version: number;
  current_load_included: boolean;
  weekly_load_origin: "AUTO" | "MANUAL" | null;
  weekly_loads: MediumTermWeeklyLoadReadModel[];
  weekly_load_diagnostic: string | null;
  load_intervals: WorkPackageLoadIntervalReadModel[];
  explicit_hours: number | null;
  automatic_hours: number | null;
  resource_class_code: string | null;
  resource_class_label: string | null;
  resource_class_active: boolean | null;
  task_resource_class_code: string | null;
  resource_class_diagnostic: string | null;
  requested_hours: number | null;
  requested_hours_diagnostics: string[];
  demand_periods: MediumTermDemandPeriodReadModel[];
};

export type MediumTermBudgetTaskReadModel = {
  task_catalog_item_id: string;
  task_code: string;
  task_label: string;
  erp_task_id: string | null;
  account_group: string;
  budget_amount_cad: number | null;
  budget_actual_cad: number | null;
  remaining_budget_cad: number | null;
  financial_diagnostic: string | null;
  average_hourly_cost_cad: number | null;
  remaining_budget_hours_from_actual: number | null;
  actual_hours_diagnostic: string | null;
  future_work_package_hours: number | null;
  future_work_package_diagnostic: string | null;
  remaining_after_work_packages_hours: number | null;
  budget_hours: number | null;
  planned_wp_hours: number | null;
  remaining_budget_hours: number | null;
  remaining_reference_date: string | null;
  remaining_reference_basis: string;
  remaining_work_package_hours: number | null;
  remaining_structured_balance_hours: number | null;
  remaining_mode_diagnostics: string[];
  associated_work_package_count: number;
  budget_included_work_package_count: number;
  diagnostic_state: string;
  budget_source_diagnostic: string | null;
  work_packages: MediumTermBudgetWorkPackageReadModel[];
  active: boolean;
  workforce_eligible: boolean | null;
  project_id: string;
  project_number: string;
  project_name: string;
  project_manager_contact_id: string | null;
  project_manager_display_name: string | null;
  manager_group_key: string;
  manager_display_name: string | null;
  manager_resolution_status: string;
  manager_diagnostics: string[];
  erp_budget_last_success_at: string | null;
};

export type MediumTermTaskOptionReadModel = {
  task_catalog_item_id: string;
  project_id: string;
  project_number: string;
  project_name: string;
  task_code: string;
  task_label: string;
};

export type MediumTermResourceClassOptionReadModel = {
  code: string;
  label: string;
  active: boolean;
};

export type MediumTermClassWeekReadModel = {
  resource_class_code: string | null;
  resource_class_label: string;
  capacity_hours: number;
  work_package_hours: number | null;
  utilization: number | null;
  state: "available" | "warning" | "overloaded" | "unavailable";
  diagnostics: string[];
};

export type MediumTermWeekReadModel = {
  week_start: string;
  work_package_hours: number | null;
  capacity_hours: number;
  utilization: number | null;
  state: "available" | "warning" | "overloaded" | "unavailable";
  diagnostics: string[];
  classes: MediumTermClassWeekReadModel[];
};

export type MediumTermBudgetReadModel = {
  project_id: string | null;
  project_number: string | null;
  project_name: string | null;
  tasks: MediumTermBudgetTaskReadModel[];
  reference_week_start: string | null;
  actual_through_date: string | null;
  reference_basis: string;
  erp_budget_last_success_at: string | null;
  unclassified_work_packages: MediumTermBudgetWorkPackageReadModel[];
  diagnostics: string[];
  weekly_diagnostics: string[];
  window_start: string | null;
  window_end: string | null;
  weeks: MediumTermWeekReadModel[];
  project_count: number;
  task_options: MediumTermTaskOptionReadModel[];
  resource_classes: MediumTermResourceClassOptionReadModel[];
};

export type MediumTermBudgetFilters = {
  taskCatalogItemId?: string;
  taskCode?: string;
  resourceClassCode?: string;
  includeInactiveProjects?: boolean;
};

export type WorkPackageWeeklyLoadProposalReadModel = {
  reference: string;
  version: number;
  planned_hours: number;
  origin: "AUTO";
  loads: MediumTermWeeklyLoadReadModel[];
};

export type WorkPackageWeeklyLoadWrite = {
  week_start: string;
  hours: number;
};


export type ResourceReadModel = {
  id: string;
  name: string;
  email: string | null;
  resource_class: string | null;
  competencies: string | null;
  competency_ids: string[];
  note: string | null;
  active: boolean;
  sort_order: number;
  external_id: string | null;
  erp_status: string | null;
  erp_active: boolean;
  erp_department_description: string | null;
  erp_department_code: string | null;
  erp_employee_class: string | null;
  erp_supervisor_external_id: string | null;
  erp_phone: string | null;
  erp_branch_code: string | null;
  erp_contact_id: number | null;
};

export type ResourceWrite = {
  name: string;
  email: string | null;
  resource_class: string | null;
  competencies: string | null;
  competency_ids: string[];
  note: string | null;
  active: boolean;
  sort_order: number;
  external_id: string | null;
};

export type ResourceMutationResult = {
  resource_id: string;
  action: string;
};

export type PlanningResourceOrderReadModel = {
  positions: Record<string, number>;
};

export type AvailabilityType = "Horaire standard" | "Vacances" | "Jour férié";

export type ResourceAvailabilityRuleReadModel = {
  id: string;
  availability_type: AvailabilityType;
  resource_id: string | null;
  resource_name: string | null;
  resource_class_codes: string[];
  start_date: string | null;
  end_date: string | null;
  weekdays: string | null;
  start_time: string | null;
  end_time: string | null;
  note: string | null;
  active: boolean;
};

export type AvailabilityRuleWrite = {
  availability_type: AvailabilityType;
  resource_id: string | null;
  start_date: string | null;
  end_date: string | null;
  weekdays: string | null;
  start_time: string | null;
  end_time: string | null;
  note: string | null;
  active: boolean;
  resource_class_codes?: string[];
};

export type AvailabilityRuleMutationResult = {
  rule_id: string;
  action: string;
};

export type DemandLineReadModel = {
  line_id: string;
  position: number;
  kind: "WORKFORCE" | "ASSET";
  slot_count: number;
  required_resource_class: string | null;
  required_resource_class_label: string | null;
  required_competencies: string | null;
  required_competency_ids: string[];
  desired_start: string | null;
  desired_end: string | null;
  desired_active_days: number | null;
  estimated_hours: number | null;
  estimated_hours_source: "EXPLICIT" | "DEFAULT_8H" | "LEGACY" | null;
  default_hours_per_day: number | null;
  confirmation: "Tentative" | "Confirmée";
  work_package_ref: string | null;
  work_package_name: string | null;
  task_code: string | null;
  task_label: string | null;
  proposed_resource_id: string | null;
  proposed_resource: string | null;
  description: string | null;
  active: boolean;
  asset_type_id: string | null;
  asset_type_code: string | null;
  asset_type_label: string | null;
  proposed_asset_id: string | null;
  proposed_asset_code: string | null;
  proposed_asset_label: string | null;
};

export type DemandRequesterReadModel = {
  user_id: string;
  display_name: string;
  roles: string[];
};

export type DemandCancellationPolicyReadModel = {
  has_operational_decisions: boolean;
  direct_cancel: boolean;
  request_cancellation: boolean;
  cancellation_pending: boolean;
  resolve_cancellation: boolean;
  reason_code: string | null;
  reason: string | null;
};

export type DemandReadModel = {
  number: string;
  status: string;
  approved_by_name?: string | null;
  approved_at?: string | null;
  approval_comment?: string | null;
  effective_status?: string | null;
  terminal?: boolean;
  created_at?: string | null;
  cancellation_request_id?: string | null;
  cancellation_state?: "PENDING" | "REJECTED" | "ACCEPTED" | null;
  cancellation_requested_by_user_id?: string | null;
  cancellation_requested_at?: string | null;
  cancellation_reason?: string | null;
  cancellation_resolved_by_user_id?: string | null;
  cancellation_resolved_at?: string | null;
  cancellation_resolution_comment?: string | null;
  cancellation_policy?: DemandCancellationPolicyReadModel | null;
  available_quick_actions?: Array<"submit" | "approve" | "cancel" | "request-cancellation">;
  project_number: string | null;
  project_name: string | null;
  client: string | null;
  project_manager: string | null;
  requester_user_id: string | null;
  requester: string | null;
  request_type: string | null;
  priority: string | null;
  confirmation: string | null;
  desired_start: string | null;
  desired_end: string | null;
  description: string | null;
  site_client: string | null;
  location: string | null;
  work_package_ref: string | null;
  work_package_name: string | null;
  task_code: string | null;
  task_label: string | null;
  resource_count: number;
  required_competencies: string | null;
  required_competency_ids: string[];
  estimated_hours: number | null;
  estimated_days: number | null;
  proposed_resource: string | null;
  version: number;
  line_mode: boolean;
  lines: DemandLineReadModel[];
};

export type DemandDetailWorkflowActionReadModel = {
  action: string;
  allowed: boolean;
  required_permission: string;
  required_permissions?: string[];
  reason_code: string | null;
  reason: string | null;
};

export type DemandDetailAlternativeGroupReadModel = {
  line_id: string;
  group_key: string;
  period_ids: string[];
  selected_period_id: string | null;
};

export type DemandDetailLineReadModel = {
  line: DemandLineReadModel;
  periods: DemandPeriodReadModel[];
  alternative_groups: DemandDetailAlternativeGroupReadModel[];
  contacts: RequestLineContactResolutionReadModel | null;
};

export type DemandDetailMaterializedResourceReadModel = {
  resource_id: string;
  resource_name: string;
  allocated_hours: number;
  locked_hours: number;
};

export type DemandDetailMaterializedRequirementReadModel = {
  requirement_id: string;
  segment_id: string;
  source_request_line_id: string | null;
  status: string;
  start_date: string;
  end_date: string;
  planned_hours: number;
  covered_hours: number;
  locked_hours: number;
  remaining_hours: number;
  excess_hours: number;
  automatic_target_resource_id: string | null;
  automatic_target_resource_name: string | null;
  mobilized_resources: DemandDetailMaterializedResourceReadModel[];
  approval_revision_id: string | null;
  approved_entry_key: string | null;
  approval_reference_status: string | null;
};

export type AssetRequirementOrigin =
  | "REQUEST"
  | "SHIFT_AD_HOC"
  | "PROJECT_DIRECT"
  | "SEGMENT"
  | "RESOURCE_PERIOD";

export type DemandDetailAssetRequirementReadModel = {
  requirement_id: string;
  origin: AssetRequirementOrigin;
  request_id: string | null;
  demand_number: string | null;
  project_id: string | null;
  project_number: string | null;
  source_request_line_id: string | null;
  source_period_id: string | null;
  approval_revision_id: string | null;
  approved_entry_key: string | null;
  resource_requirement_id: string | null;
  segment_reference: string | null;
  shift_id: string | null;
  context_resource_id: string | null;
  slot_index: number;
  asset_type_id: string;
  asset_type_code: string;
  asset_type_label: string;
  start_date: string;
  end_date: string;
  usage_hours: number | null;
  status: string;
  allocation_id: string | null;
  asset_id: string | null;
  asset_code: string | null;
  asset_label: string | null;
  allocation_start_date: string | null;
  allocation_end_date: string | null;
  allocation_locked: boolean;
  operator_resource_id: string | null;
  operator_resource_name: string | null;
  qualification_state: "SATISFIED" | "MISSING_OPERATOR" | "SKILL_MISMATCH" | "NO_OVERLAP";
  required_competency_ids: string[];
  required_competency_names: string[];
};

export type ApprovalCycleApproverReadModel = {
  app_user_id: string;
  display_name: string;
  active: boolean;
  sources: string[];
};

export type ApprovalCycleDecisionReadModel = {
  app_user_id: string;
  display_name: string;
  decision: string;
  action_id: string;
  decided_at: string | null;
  comment: string | null;
};

export type ApprovalCycleRequirementReadModel = {
  requirement_id: string;
  request_line_id: string;
  task_catalog_item_id: string | null;
  approval_scope_id: string | null;
  proposed_resource_id: string | null;
  routing_sources: string[];
  satisfied: boolean;
  actor_can_approve: boolean;
  approvers: ApprovalCycleApproverReadModel[];
  decisions: ApprovalCycleDecisionReadModel[];
};

export type ApprovalCycleProgressReadModel = {
  approval_cycle_id: string;
  state: "OPEN" | "INVALIDATED" | "COMPLETED";
  submitted_request_version: number;
  submitted_at: string;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  completed_at: string | null;
  approval_revision_id: string | null;
  total_requirements: number;
  satisfied_requirements: number;
  quorum_complete: boolean;
  actor_approvable_requirement_ids: string[];
  actor_approvable_request_line_ids: string[];
  requirements: ApprovalCycleRequirementReadModel[];
};

export type DemandDetailTechnicalContextReadModel = {
  request_version: number;
  workflow_version: number;
  expected_request_version: number;
  expected_operational_version: number | null;
  active_revision_id: string | null;
  approved_request_version: number | null;
  operational_version: number | null;
  envelope_decision: string | null;
  envelope_reason: string | null;
  diagnostics: string[];
};

export type DemandDetailReadModel = {
  demand: DemandReadModel;
  version: number;
  lines: DemandDetailLineReadModel[];
  periods: DemandPeriodReadModel[];
  materialized_plan: {
    requirement_count: number;
    planned_hours: number;
    covered_hours: number;
    locked_hours: number;
    requirements: DemandDetailMaterializedRequirementReadModel[];
    asset_requirement_count: number;
    asset_assigned_count: number;
    asset_usage_hours: number;
    asset_unbudgeted_requirement_count: number;
    asset_requirements: DemandDetailAssetRequirementReadModel[];
  };
  workflow: {
    demand_number: string;
    status: string;
    version: number;
    available_actions: string[];
    actions: DemandDetailWorkflowActionReadModel[];
    cancellation?: DemandCancellationPolicyReadModel | null;
  };
  approval_cycle: ApprovalCycleProgressReadModel | null;
  approval_state: {
    active_revision_id: string | null;
    approved_request_version: number | null;
    operational_version: number | null;
    envelope_decision: string | null;
    envelope_reason: string | null;
    approval_reference_status: string | null;
  } | null;
  policy: {
    can_modify_candidate: boolean;
    can_edit_periods: boolean;
    can_change_operational_choices: boolean;
    editable_candidate_fields: string[];
    expected_request_version: number;
    expected_operational_version: number | null;
    envelope_decision: string | null;
    reapproval_required: boolean;
  };
  diagnostics: string[];
  technical_context: DemandDetailTechnicalContextReadModel | null;
};

export type SegmentMobilizedResourceReadModel = {
  resource_id: string;
  resource_name: string;
  allocated_hours: number;
  locked_hours: number;
  replaceable_hours: number;
};

export type SegmentReadModel = {
  segment_id: string;
  demand_number: string | null;
  project_number: string | null;
  project_name: string | null;
  resource_name: string | null;
  requirement_id: string | null;
  automatic_target_resource_id: string | null;
  automatic_target_resource_name: string | null;
  mobilized_resources: SegmentMobilizedResourceReadModel[];
  start_date: string | null;
  end_date: string | null;
  planned_hours: number;
  status: string;
  description: string | null;
  origin: string | null;
  required_competency: string | null;
  required_competency_id: string | null;
  planning_type: string | null;
  priority: string | null;
  outside_standard_hours: boolean;
  confirmation: string | null;
  confirmation_overridden: boolean;
  project_manager: string | null;
  requester: string | null;
  locked_hours: number;
  replaceable_hours: number;
  covered_hours: number;
  automatic_rebuild_hours: number;
  remaining_hours: number;
  excess_hours: number;
  overallocated_hours: number;
  overallocated: boolean;
  desired_active_days: number | null;
  planned_active_days: number;
  active_day_target_met: boolean | null;
  active_day_diagnostic: string | null;
  load_profile: string;
};

export type ShiftAssetReservationReadModel = {
  requirement_id: string;
  allocation_id: string;
  origin: "REQUEST" | "SHIFT_AD_HOC" | "PROJECT_DIRECT" | "RESOURCE_PERIOD" | "SEGMENT" | string;
  association_kind:
    | "OWNED_SHIFT"
    | "RELATED_REQUEST"
    | "INHERITED_RESOURCE_PERIOD"
    | "INHERITED_PROJECT_DIRECT"
    | "INHERITED_SEGMENT"
    | "SEGMENT_CONTEXT"
    | string;
  asset_id: string;
  asset_code: string;
  asset_label: string;
  asset_active: boolean;
  operator_resource_id: string | null;
  qualification_state: "SATISFIED" | "MISSING_OPERATOR" | "SKILL_MISMATCH" | "NO_OVERLAP" | string;
  project_id: string | null;
  resource_requirement_id: string | null;
  context_resource_id: string | null;
  start_date: string;
  end_date: string;
};

export type ShiftAssetActionReadModel = {
  allowed: boolean;
  reason: string | null;
};

export type ShiftAssetActionsReadModel = {
  assign: ShiftAssetActionReadModel;
  change: ShiftAssetActionReadModel;
  release: ShiftAssetActionReadModel;
};

export type ShiftReadModel = {
  allocation_id: string;
  segment_id: string;
  requirement_id: string | null;
  resource_id: string;
  resource_name: string;
  work_date: string;
  hours: number;
  allocation_type: string | null;
  source: string;
  locked: boolean;
  outside_standard_hours: boolean;
  confirmation: string | null;
  confirmation_override: string | null;
  load_kind: string;
  note: string | null;
  demand_number: string | null;
  project_number: string | null;
  project_name: string | null;
  project_manager: string | null;
  requester: string | null;
  asset_assignment: ShiftAssetReservationReadModel | null;
  related_asset_reservations: ShiftAssetReservationReadModel[];
  asset_actions: ShiftAssetActionsReadModel | null;
  asset_diagnostics: string[];
};

export type DemandPeriodReadModel = {
  period_id: string;
  demand_number: string;
  request_line_id: string | null;
  sequence: number;
  kind: "CUMULATIVE" | "ALTERNATIVE";
  start_date: string;
  end_date: string;
  hours: number;
  confirmation: "Tentative" | "Confirmée";
  alternative_group: string | null;
  proposed_resource: string | null;
  resource_count: number;
  desired_active_days: number | null;
  note: string | null;
  selected: boolean;
};

export type DemandPeriodWrite = {
  period_id: string;
  start_date: string;
  end_date: string;
  hours: number;
  kind: "CUMULATIVE" | "ALTERNATIVE";
  alternative_group: string | null;
  confirmation: "Tentative" | "Confirmée";
  proposed_resource: string | null;
  resource_count: number;
  note: string;
};

export type DemandPeriodsMutationResult = {
  demand_number: string;
  period_count: number;
  status: string | null;
  reapproval_required: boolean;
};

export type DemandAlternativeSelectionResult = {
  demand_number: string;
  alternative_group: string;
  period_id: string;
};

export type PendingDemandLoadReadModel = {
  demand_number: string;
  project_number: string | null;
  project_name: string | null;
  start_date: string;
  end_date: string;
  projected_hours: number | null;
  window_hours: number;
  mode: string;
  load_kind: string;
  current_plan_hours: number;
  delta_hours: number | null;
  resource_count: number;
  required_competencies: string | null;
  proposed_resource: string | null;
  work_package_ref: string | null;
  confirmation: string | null;
  periods: DemandPeriodReadModel[];
};

export type PlanningActionReadModel = {
  kind: "APPROVAL" | "ASSIGNMENT";
  reference: string;
  demand_number: string | null;
  segment_id: string | null;
  project_number: string | null;
  project_name: string | null;
  task_code: string | null;
  task_label: string | null;
  start_date: string;
  end_date: string;
  planned_hours: number;
  required_competency: string | null;
  required_competency_id: string | null;
  priority: string | null;
  status: string | null;
  confirmation: string | null;
  project_manager: string | null;
  requester: string | null;
  emergency_override_active: boolean;
};

export type CoordinatorDashboardKpiReadModel = {
  personal_demands: number;
  total_actions: number;
  assignments: number;
  cancellations: number;
  approvals: number;
  coverage_issues: number;
  partial_coverages: number;
  conflicts: number;
  attention_items: number;
};

export type CoordinatorDashboardDemandReadModel = {
  demand_number: string;
  project_number: string | null;
  project_name: string | null;
  effective_status: string;
  priority: string | null;
  desired_start: string | null;
  desired_end: string | null;
  cancellation_pending: boolean;
  attention: "URGENT" | "OVERDUE" | "SOON" | "NORMAL";
  days_until_start: number | null;
};

export type CoordinatorDashboardActionReadModel = {
  action_id: string;
  category: "ASSIGNMENT" | "CANCELLATION" | "APPROVAL" | "COVERAGE";
  kind:
    | "WORKFORCE_ASSIGNMENT"
    | "ASSET_ASSIGNMENT"
    | "CANCELLATION"
    | "APPROVAL"
    | "PARTIAL_COVERAGE"
    | "CONFLICT";
  label: string;
  detail: string | null;
  demand_number: string;
  source_id: string;
  project_number: string | null;
  project_name: string | null;
  status: string | null;
  priority: string | null;
  start_date: string | null;
  end_date: string | null;
  planned_hours: number | null;
  covered_hours: number | null;
  remaining_hours: number | null;
  attention: "URGENT" | "OVERDUE" | "SOON" | "NORMAL";
  days_until_start: number | null;
  target: "DEMANDS" | "PLANNING";
  resource_kind: "WORKFORCE" | "ASSET" | null;
  related_ids: string[];
  operational_responsible_contact_id: string | null;
  operational_responsible_display_name: string | null;
  operational_responsible_status: string | null;
  operational_responsible_source_type: string | null;
  operational_responsible_source_label: string | null;
};

export type CoordinatorDashboardReadModel = {
  as_of: string;
  attention_horizon_days: number;
  kpis: CoordinatorDashboardKpiReadModel;
  personal_demands: CoordinatorDashboardDemandReadModel[];
  actions: CoordinatorDashboardActionReadModel[];
};

export type ResourceRecommendationReadModel = {
  resource_id: string;
  resource_name: string;
  resource_class: string | null;
  required_competency: string | null;
  required_class: string | null;
  competency_match: boolean;
  class_match: boolean;
  capacity_hours: number;
  confirmed_hours: number;
  tentative_hours: number;
  outside_standard_hours: number;
  free_after_confirmed: number;
  prudent_free: number;
  overtime_needed: number;
  enough_after_confirmed: boolean;
  enough_prudent: boolean;
  score: number;
  rank: number;
  recommended: boolean;
  preferred: boolean;
  recommendation_category: number;
  competency_state: "NOT_REQUIRED" | "SATISFIED" | "MISSING" | "UNRESOLVED";
  missing_competency_ids: string[];
  capacity_state: "PRUDENT_FULL" | "PRUDENT_PARTIAL" | "TENTATIVE_ONLY" | "NONE";
  fallback_requires_confirmation: boolean;
  preferred_resource_id: string | null;
  preferred_resource_name: string | null;
  preferred_resource_status:
    | "NONE"
    | "ELIGIBLE"
    | "INACTIVE_LOCAL"
    | "INACTIVE_ERP"
    | "NO_SCHEDULE_IN_WINDOW"
    | "NOT_FOUND"
    | "INVALID_TASK_CONTEXT"
    | "UNRESOLVED_CONTEXT";
};

export type MediumTermUnlinkedSegmentReadModel = {
  segment_id: string;
  demand_number: string | null;
  project_number: string;
  project_name: string;
  task_code: string | null;
  task_label: string | null;
  start_date: string;
  end_date: string;
  planned_hours: number;
  resource_name: string | null;
  status: string;
  origin: string;
  classification: "REQUEST_UNLINKED" | "AD_HOC_ALLOWED" | "BROKEN_REFERENCE" | "ORPHAN_SEGMENT";
  anomaly: boolean;
  link_target: "DEMAND" | "SEGMENT";
  current_work_package_ref: string | null;
  reapproval_on_link: boolean;
  description: string | null;
};

export type MediumTermCapacityBucketReadModel = {
  week_start: string;
  week_end: string;
  resource_class: string | null;
  resource_count: number;
  capacity_hours: number;
  firm_hours: number;
  current_potential_hours: number;
  submitted_hours: number;
  replacement_proposal_hours: number;
  replacement_delta_hours: number;
  exposure_hours: number;
  firm_residual_hours: number;
  residual_hours: number;
  utilization_pct: number | null;
  state: "available" | "warning" | "overloaded" | "unavailable";
};

export type PlanningDayCapacityReadModel = {
  day: string;
  capacity_hours: number;
  confirmed_hours: number;
  tentative_hours: number;
  outside_standard_hours: number;
  total_hours: number;
  prudent_free: number;
  available: boolean;
  overloaded: boolean;
  reason: string | null;
};

export type PlanningResourceCapacityReadModel = {
  resource_id: string;
  resource_name: string;
  resource_class: string | null;
  capacity_hours: number;
  confirmed_hours: number;
  tentative_hours: number;
  outside_standard_hours: number;
  prudent_free: number;
  overloaded: boolean;
  days: PlanningDayCapacityReadModel[];
};

export type PlanningSegmentCapacityDiagnosticReadModel = {
  segment_id: string;
  resource_id: string | null;
  resource_name: string | null;
  planned_hours: number;
  allocated_hours: number;
  outside_standard_hours: number;
  unplaced_hours: number;
  requires_outside_standard_hours: boolean;
  automatic_target_resource_id: string | null;
  automatic_target_resource_name: string | null;
};

export type PlanningCapacityGridReadModel = {
  start: string;
  end: string;
  resources: PlanningResourceCapacityReadModel[];
  segment_diagnostics: PlanningSegmentCapacityDiagnosticReadModel[];
};

export type AssetTypePlanningReadModel = {
  id: string;
  code: string;
  label: string;
  category: string;
  occupancy_policy: string;
  active: boolean;
  qualification_policy: string;
  required_competency_ids: string[];
  required_competency_names: string[];
};

export type AssetPlanningReadModel = {
  id: string;
  code: string;
  label: string;
  asset_type_id: string;
  active: boolean;
};

export type AssetRequirementPlanningReadModel = DemandDetailAssetRequirementReadModel;

export type AssetAllocationPlanningReadModel = {
  allocation_id: string;
  requirement_id: string;
  asset_id: string;
  asset_code: string;
  asset_label: string;
  project_number: string | null;
  start_date: string;
  end_date: string;
  locked: boolean;
  source: string;
  operator_resource_id: string | null;
  operator_resource_name: string | null;
  qualification_state: "SATISFIED" | "MISSING_OPERATOR" | "SKILL_MISMATCH" | "NO_OVERLAP";
  required_competency_ids: string[];
  required_competency_names: string[];
};

export type AssetUnavailabilityPlanningReadModel = {
  id: string;
  asset_id: string;
  start_date: string;
  end_date: string;
  reason: string | null;
};

export type AssetDayCapacityPlanningReadModel = {
  asset_id: string;
  day: string;
  capacity_units: number;
  occupied_units: number;
  remaining_units: number;
  unavailable: boolean;
  available: boolean;
  visible_occupations: AssetAllocationPlanningReadModel[];
  has_hidden_occupancy: boolean;
};

export type AssetPlanningDiagnosticReadModel = {
  code: string;
  message: string;
  requirement_id: string | null;
  allocation_id: string | null;
  asset_id: string | null;
  day: string | null;
};

export type PlanningSnapshotReadModel = {
  start: string;
  end: string;
  planning_version: number;
  resources: ResourceReadModel[];
  demands: DemandReadModel[];
  segments: SegmentReadModel[];
  shifts: ShiftReadModel[];
  pending_loads: PendingDemandLoadReadModel[];
  capacity_buckets: MediumTermCapacityBucketReadModel[];
  asset_types: AssetTypePlanningReadModel[];
  assets: AssetPlanningReadModel[];
  asset_requirements: AssetRequirementPlanningReadModel[];
  asset_allocations: AssetAllocationPlanningReadModel[];
  asset_unavailability: AssetUnavailabilityPlanningReadModel[];
  asset_capacity: AssetDayCapacityPlanningReadModel[];
  asset_diagnostics: AssetPlanningDiagnosticReadModel[];
  firm_hours: number;
  potential_hours: number;
  replacement_proposal_hours: number;
};

export type DemandLineWrite = {
  id?: string;
  position?: number;
  kind?: "WORKFORCE" | "ASSET";
  required_resource_class: string | null;
  required_competency_ids: string[];
  desired_start: string | null;
  desired_end: string | null;
  desired_active_days: number | null;
  estimated_hours: number | null;
  work_package_ref: string | null;
  task_code: string | null;
  proposed_resource_id: string | null;
  asset_type_id?: string | null;
  proposed_asset_id?: string | null;
  confirmation: "Tentative" | "Confirmée";
  description: string | null;
};

export type DemandWrite = {
  project_number: string;
  project_name?: string;
  client?: string;
  requester_user_id?: string | null;
  request_type?: string;
  priority: string;
  description: string;
  work_package_ref?: string | null;
  task_code?: string | null;
  confirmation?: "Tentative" | "Confirmée";
  desired_start?: string | null;
  desired_end?: string | null;
  resource_count?: number;
  required_competencies?: string | null;
  required_competency_ids?: string[];
  required_resource_class?: string | null;
  estimated_hours?: number | null;
  estimated_days?: number | null;
  proposed_technician?: string | null;
  lines?: DemandLineWrite[];
  expected_version?: number;
};

export type DemandMutationResult = {
  demand_number: string;
  status: string | null;
  reapproval_required: boolean;
};

export type AllocationMoveWrite = {
  resource_id: string;
  day: string;
  technician?: string | null;
  outside_standard_hours: boolean;
  expected_planning_version: number;
};

export type ManualAllocationUpdate = {
  resource_id: string;
  technician?: string | null;
  day: string;
  hours: number;
  outside_standard_hours: boolean;
  note: string;
  confirmation: string | null;
};

export type QuickShiftCreate = {
  project_number: string;
  technician: string;
  day: string;
  hours: number;
  project_name: string;
  outside_standard_hours: boolean;
  note: string;
  description: string;
  confirmation: "Tentative" | "Confirmée";
};

export type QuickShiftCreated = {
  segment_id: string;
  allocation_id: string;
};

type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    context?: unknown;
  };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly context: unknown;

  constructor(
    message: string,
    status: number,
    code: string | null = null,
    context: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.context = context;
  }
}

async function responseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload | null = null;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    // Keep the stable fallback below when a proxy/server returns non-JSON content.
  }
  return new ApiError(
    payload?.error?.message || `Erreur HTTP ${response.status}`,
    response.status,
    payload?.error?.code ?? null,
    payload?.error?.context ?? null,
  );
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

async function postJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { Accept: "application/json", ...csrfHeaders() },
    credentials: "include",
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

async function sendJson<T>(
  path: string,
  method: string,
  body: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...csrfHeaders(),
      ...headers,
    },
    body: JSON.stringify(body),
    credentials: "include",
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export function getMediumTermBudget(
  projectNumber: string,
  start: string,
  end: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
  filters: MediumTermBudgetFilters = {},
) {
  const params = new URLSearchParams({
    start,
    end,
    scope,
    include_inactive_projects: String(Boolean(filters.includeInactiveProjects)),
  });
  const project = projectNumber.trim();
  if (project) params.set("project_number", project);
  if (filters.taskCatalogItemId) {
    params.set("task_catalog_item_id", filters.taskCatalogItemId);
  }
  if (filters.taskCode) {
    params.set("task_code", filters.taskCode);
  }
  if (filters.resourceClassCode) {
    params.set("resource_class_code", filters.resourceClassCode);
  }
  return getJson<MediumTermBudgetReadModel>(
    `/api/v1/medium-term/budget?${params.toString()}`,
    signal,
  );
}

export function getMediumTermBudgetSummary(
  projectNumber: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({
    project_number: projectNumber,
    scope,
    include_inactive_projects: "true",
  });
  return getJson<MediumTermBudgetReadModel>(
    `/api/v1/medium-term/budget?${params.toString()}`,
    signal,
  );
}

export function getMediumTermUnlinkedSegments(
  start: string,
  end: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ start, end, scope });
  return getJson<MediumTermUnlinkedSegmentReadModel[]>(
    `/api/v1/medium-term/unlinked-segments?${params.toString()}`,
    signal,
  );
}

export function linkDemandToWorkPackage(number: string, workPackageRef: string) {
  return sendJson<DemandMutationResult>(
    `/api/v1/demands/${encodeURIComponent(number)}`,
    "PATCH",
    {
      work_package_ref: workPackageRef,
      comment: "Rattachement au WorkPackage depuis la vue Moyen terme",
    },
  );
}

export function getPlanningSnapshot(
  start: string,
  end: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ start, end, scope });
  return getJson<PlanningSnapshotReadModel>(`/api/v1/planning/snapshot?${params.toString()}`, signal);
}

export function getPlanningCapacityGrid(
  start: string,
  end: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ start, end, scope });
  return getJson<PlanningCapacityGridReadModel>(
    `/api/v1/planning/capacity-grid?${params.toString()}`,
    signal,
  );
}

export function getPlanningActions(
  start: string,
  end: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ start, end, scope });
  return getJson<PlanningActionReadModel[]>(`/api/v1/planning/actions?${params.toString()}`, signal);
}

export function getCoordinatorDashboard(signal?: AbortSignal) {
  return getJson<CoordinatorDashboardReadModel>(
    "/api/v1/coordinator-dashboard",
    signal,
  );
}

export function getResourceRecommendations(segmentId: string, signal?: AbortSignal) {
  return getJson<ResourceRecommendationReadModel[]>(
    `/api/v1/segments/${encodeURIComponent(segmentId)}/resource-recommendations`,
    signal,
  );
}

export function getProjects(
  activeOnly = true,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({
    active_only: String(activeOnly),
    scope,
  });
  return getJson<ProjectReadModel[]>(`/api/v1/projects?${params.toString()}`, signal);
}

export function getAcumaticaIntegrationStatus(signal?: AbortSignal) {
  return getJson<AcumaticaIntegrationStatus>("/api/v1/integrations/acumatica", signal);
}

export function syncAcumaticaProjects() {
  return postJson<ProjectSyncResult>("/api/v1/integrations/acumatica/projects/sync");
}

export function syncAcumaticaProjectTasks(projectId: string) {
  return postJson<ProjectTaskSyncResult>(
    `/api/v1/integrations/acumatica/projects/${encodeURIComponent(projectId)}/tasks/sync`,
  );
}

export function syncAcumaticaActiveProjectTasks() {
  return postJson<GlobalProjectTaskSyncResult>(
    "/api/v1/integrations/acumatica/projects/tasks/sync",
  );
}

export function getAcumaticaProjectTaskSyncRun(
  runId: string,
  signal?: AbortSignal,
) {
  return getJson<GlobalProjectTaskSyncResult>(
    `/api/v1/integrations/acumatica/projects/tasks/sync/${encodeURIComponent(runId)}`,
    signal,
  );
}

export function getCurrentAcumaticaProjectTaskSyncRun(signal?: AbortSignal) {
  return getJson<GlobalProjectTaskSyncResult | null>(
    "/api/v1/integrations/acumatica/projects/tasks/sync/current",
    signal,
  );
}

export function getAcumaticaProjectTaskSyncMetadata(
  projectId: string,
  signal?: AbortSignal,
) {
  return getJson<ProjectTaskSyncMetadata | null>(
    `/api/v1/integrations/acumatica/projects/${encodeURIComponent(projectId)}/tasks/sync-metadata`,
    signal,
  );
}

export function getBusinessContacts(activeOnly = false, signal?: AbortSignal) {
  const params = new URLSearchParams({
    active_only: String(activeOnly),
    user_backed_only: "true",
  });
  return getJson<BusinessContactReadModel[]>(
    `/api/v1/business-contacts?${params.toString()}`,
    signal,
  );
}

export function getOperationalResponsibilityContacts(signal?: AbortSignal) {
  const params = new URLSearchParams({
    active_only: "true",
    user_backed_only: "false",
  });
  return getJson<BusinessContactReadModel[]>(
    `/api/v1/business-contacts?${params.toString()}`,
    signal,
  );
}

export function createBusinessContact(payload: {
  display_name: string;
  email?: string | null;
  phone?: string | null;
  active?: boolean;
  source?: string;
  external_system?: string | null;
  external_entity?: string | null;
  external_id?: string | null;
}) {
  return sendJson<BusinessContactReadModel>("/api/v1/business-contacts", "POST", payload);
}

export function updateBusinessContact(
  contactId: string,
  payload: Partial<Omit<BusinessContactReadModel, "id" | "version">> & { expected_version?: number },
) {
  return sendJson<BusinessContactReadModel>(
    `/api/v1/business-contacts/${encodeURIComponent(contactId)}`,
    "PATCH",
    payload,
  );
}

export function getProjectBusinessContacts(projectNumber: string, signal?: AbortSignal) {
  return getJson<ContactLinkReadModel>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/business-contacts`,
    signal,
  );
}

export function getProjectManagerCandidateContacts(signal?: AbortSignal) {
  const params = new URLSearchParams({
    active_only: "true",
    user_backed_only: "false",
  });
  return getJson<BusinessContactReadModel[]>(
    `/api/v1/business-contacts?${params.toString()}`,
    signal,
  );
}

export function getProjectManagers(
  projectNumber: string,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ scope });
  return getJson<ProjectManagersReadModel>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/managers?${params.toString()}`,
    signal,
  );
}

export function addProjectCoManager(
  projectNumber: string,
  businessContactId: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<ProjectCoManagerMutationResult>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/co-managers/${encodeURIComponent(businessContactId)}`,
    "PUT",
    { expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function removeProjectCoManager(
  projectNumber: string,
  businessContactId: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<ProjectCoManagerMutationResult>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/co-managers/${encodeURIComponent(businessContactId)}`,
    "DELETE",
    { expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function setTaskBusinessContacts(
  taskId: string,
  payload: {
    operational_responsible_contact_id?: string | null;
    coordinator_contact_id?: string | null;
  },
) {
  return sendJson<ContactLinkReadModel>(
    `/api/v1/task-catalog/${encodeURIComponent(taskId)}/business-contacts`,
    "PATCH",
    payload,
  );
}

export function getResourceBusinessContacts(resourceId: string, signal?: AbortSignal) {
  return getJson<ContactLinkReadModel>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/business-contacts`,
    signal,
  );
}

export function setResourceCoordinatorContact(resourceId: string, contactId: string | null) {
  return sendJson<ContactLinkReadModel>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/coordinator-contact`,
    "PATCH",
    { contact_id: contactId },
  );
}

export function getDemandBusinessContacts(demandNumber: string, signal?: AbortSignal) {
  return getJson<ContactLinkReadModel>(
    `/api/v1/demands/${encodeURIComponent(demandNumber)}/business-contacts`,
    signal,
  );
}

export function setDemandOperationalResponsible(
  demandNumber: string,
  contactId: string | null,
  expectedVersion: number,
) {
  return sendJson<DemandOverrideMutationResult>(
    `/api/v1/demands/${encodeURIComponent(demandNumber)}/operational-responsible`,
    "PATCH",
    { contact_id: contactId, expected_version: expectedVersion },
  );
}

export function getRequestLineContactResolution(lineId: string, signal?: AbortSignal) {
  return getJson<RequestLineContactResolutionReadModel>(
    `/api/v1/request-lines/${encodeURIComponent(lineId)}/contact-resolution`,
    signal,
  );
}

export function getProjectOperationalResponsibility(projectNumber: string, signal?: AbortSignal) {
  return getJson<ProjectOperationalResponsibilityReadModel>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/operational-responsibility`,
    signal,
  );
}

export function setProjectOperationalResponsible(
  projectNumber: string,
  contactId: string | null,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<OperationalResponsibilityMutationResult>(
    `/api/v1/projects/${encodeURIComponent(projectNumber)}/operational-responsible`,
    "PATCH",
    { contact_id: contactId, expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function getSegmentOperationalResponsibility(segmentReference: string, signal?: AbortSignal) {
  return getJson<MaterializedContactResolutionReadModel>(
    `/api/v1/segments/${encodeURIComponent(segmentReference)}/operational-responsibility`,
    signal,
  );
}

export function setSegmentOperationalResponsible(
  segmentReference: string,
  contactId: string | null,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  return sendJson<OperationalResponsibilityMutationResult>(
    `/api/v1/segments/${encodeURIComponent(segmentReference)}/operational-responsible`,
    "PATCH",
    { contact_id: contactId, expected_planning_version: expectedPlanningVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function getAllocationOperationalResponsibility(allocationReference: string, signal?: AbortSignal) {
  return getJson<MaterializedContactResolutionReadModel>(
    `/api/v1/allocations/${encodeURIComponent(allocationReference)}/operational-responsibility`,
    signal,
  );
}

export function setAllocationOperationalResponsible(
  allocationReference: string,
  contactId: string | null,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  return sendJson<OperationalResponsibilityMutationResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationReference)}/operational-responsible`,
    "PATCH",
    { contact_id: contactId, expected_planning_version: expectedPlanningVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function getTaskCatalog(
  projectNumber: string,
  query = "",
  activeOnly = true,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    project_number: projectNumber,
    active_only: String(activeOnly),
  });
  if (query.trim()) params.set("q", query.trim());
  return getJson<TaskCatalogItemReadModel[]>(
    `/api/v1/task-catalog?${params.toString()}`,
    signal,
  );
}

export function getCompetencies(
  query = "",
  activeOnly = true,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ active_only: String(activeOnly) });
  if (query.trim()) params.set("q", query.trim());
  return getJson<CompetencyReadModel[]>(`/api/v1/competencies?${params.toString()}`, signal);
}

export function createCompetency(payload: CompetencyWrite) {
  return sendJson<CompetencyMutationResult>("/api/v1/competencies", "POST", payload);
}

export function updateCompetency(competencyId: string, payload: Partial<CompetencyWrite>) {
  return sendJson<CompetencyMutationResult>(
    `/api/v1/competencies/${encodeURIComponent(competencyId)}`,
    "PATCH",
    payload,
  );
}

export function setCompetencyResourceClass(
  competencyId: string,
  resourceClassCode: string | null,
  expectedVersion: number,
) {
  return sendJson<CompetencyResourceClassMutationResult>(
    `/api/v1/competencies/${encodeURIComponent(competencyId)}/resource-class`,
    "PATCH",
    {
      resource_class_code: resourceClassCode,
      expected_version: expectedVersion,
    },
  );
}

export function deactivateCompetency(competencyId: string) {
  return postJson<CompetencyMutationResult>(
    `/api/v1/competencies/${encodeURIComponent(competencyId)}/deactivate`,
  );
}

export function getWorkPackages(
  projectNumber: string,
  activeOnly = true,
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({
    project_number: projectNumber,
    active_only: String(activeOnly),
    scope,
  });
  return getJson<WorkPackageReadModel[]>(`/api/v1/work-packages?${params.toString()}`, signal);
}

export function createWorkPackage(payload: WorkPackageWrite, idempotencyKey: string) {
  return sendJson<WorkPackageMutationResult>(
    "/api/v1/work-packages",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateWorkPackage(
  reference: string,
  payload: WorkPackageWrite,
  expectedVersion: number,
) {
  return sendJson<WorkPackageMutationResult>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}`,
    "PATCH",
    { ...payload, expected_version: expectedVersion },
  );
}

export function closeWorkPackage(
  reference: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<WorkPackageMutationResult>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}/close`,
    "POST",
    { expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function cancelWorkPackage(
  reference: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<WorkPackageMutationResult>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}/cancel`,
    "POST",
    { expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function reopenWorkPackage(
  reference: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return sendJson<WorkPackageMutationResult>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}/reopen`,
    "POST",
    { expected_version: expectedVersion },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function proposeWorkPackageWeeklyLoads(reference: string) {
  return postJson<WorkPackageWeeklyLoadProposalReadModel>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}/weekly-loads/proposal`,
  );
}

export function replaceWorkPackageWeeklyLoads(
  reference: string,
  expectedVersion: number,
  origin: "AUTO" | "MANUAL",
  loads: WorkPackageWeeklyLoadWrite[],
  idempotencyKey: string,
) {
  return sendJson<WorkPackageMutationResult>(
    `/api/v1/work-packages/${encodeURIComponent(reference)}/weekly-loads`,
    "PUT",
    {
      expected_version: expectedVersion,
      origin,
      loads,
    },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function getResources(activeOnly = true, signal?: AbortSignal) {
  const params = new URLSearchParams({ active_only: String(activeOnly) });
  return getJson<ResourceReadModel[]>(`/api/v1/resources?${params.toString()}`, signal);
}

export function createResource(payload: ResourceWrite, idempotencyKey: string) {
  return sendJson<ResourceMutationResult>(
    "/api/v1/resources",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateResource(resourceId: string, payload: ResourceWrite) {
  return sendJson<ResourceMutationResult>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}`,
    "PATCH",
    payload,
  );
}

export function getPlanningResourceOrder(signal?: AbortSignal) {
  return getJson<PlanningResourceOrderReadModel>("/api/v1/planning/resource-order", signal);
}

export function reorderPlanningResource(
  resourceId: string,
  direction: "up" | "down",
  idempotencyKey: string,
) {
  return sendJson<ResourceMutationResult>(
    `/api/v1/planning/resources/${encodeURIComponent(resourceId)}/reorder`,
    "POST",
    { direction },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function deactivateResource(resourceId: string) {
  return postJson<ResourceMutationResult>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
  );
}

export function reactivateResource(resourceId: string) {
  return sendJson<ResourceMutationResult>(
    `/api/v1/resources/${encodeURIComponent(resourceId)}`,
    "PATCH",
    { active: true },
  );
}

export function getAvailabilityRules(
  resourceId: string | null = null,
  includeGlobal = true,
  activeOnly = true,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    include_global: String(includeGlobal),
    active_only: String(activeOnly),
  });
  if (resourceId) params.set("resource_id", resourceId);
  return getJson<ResourceAvailabilityRuleReadModel[]>(
    `/api/v1/availability-rules?${params.toString()}`,
    signal,
  );
}

export function createAvailabilityRule(payload: AvailabilityRuleWrite, idempotencyKey: string) {
  return sendJson<AvailabilityRuleMutationResult>(
    "/api/v1/availability-rules",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateAvailabilityRule(ruleId: string, payload: AvailabilityRuleWrite) {
  return sendJson<AvailabilityRuleMutationResult>(
    `/api/v1/availability-rules/${encodeURIComponent(ruleId)}`,
    "PATCH",
    payload,
  );
}

export function deactivateAvailabilityRule(ruleId: string) {
  return postJson<AvailabilityRuleMutationResult>(
    `/api/v1/availability-rules/${encodeURIComponent(ruleId)}/deactivate`,
  );
}

export function getDemandRequesters(signal?: AbortSignal) {
  return getJson<DemandRequesterReadModel[]>("/api/v1/demand-requesters", signal);
}

export function getDemands(
  signal?: AbortSignal,
  scope: ViewScope = "global",
) {
  const params = new URLSearchParams({ scope });
  return getJson<DemandReadModel[]>(`/api/v1/demands?${params.toString()}`, signal);
}

export function getDemand(number: string, signal?: AbortSignal) {
  return getJson<DemandReadModel>(`/api/v1/demands/${encodeURIComponent(number)}`, signal);
}

export function getDemandDetail(number: string, signal?: AbortSignal) {
  return getJson<DemandDetailReadModel>(
    `/api/v1/demands/${encodeURIComponent(number)}/detail`,
    signal,
  );
}

export function getDemandPeriods(number: string, signal?: AbortSignal) {
  return getJson<DemandPeriodReadModel[]>(
    `/api/v1/demands/${encodeURIComponent(number)}/periods`,
    signal,
  );
}

export function getDemandLinePeriods(
  number: string,
  lineId: string,
  signal?: AbortSignal,
) {
  return getJson<DemandPeriodReadModel[]>(
    `/api/v1/demands/${encodeURIComponent(number)}/lines/${encodeURIComponent(lineId)}/periods`,
    signal,
  );
}

export function createDemand(payload: DemandWrite, idempotencyKey: string) {
  return sendJson<DemandMutationResult>(
    "/api/v1/demands",
    "POST",
    { ...payload, submit: false },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateDemand(number: string, payload: DemandWrite, comment: string) {
  // request_type is not edited here. Do not write a fallback value back over
  // historical requests until the field has an explicit UI and canonical SQL read.
  const { request_type: _requestType, ...editablePayload } = payload;
  return sendJson<DemandMutationResult>(
    `/api/v1/demands/${encodeURIComponent(number)}`,
    "PATCH",
    { ...editablePayload, comment },
  );
}

export function replaceDemandPeriods(
  number: string,
  periods: DemandPeriodWrite[],
  expectedRequestVersion: number,
) {
  return sendJson<DemandPeriodsMutationResult>(
    `/api/v1/demands/${encodeURIComponent(number)}/periods`,
    "PUT",
    { periods, expected_request_version: expectedRequestVersion },
  );
}

export function replaceDemandLinePeriods(
  number: string,
  lineId: string,
  periods: DemandPeriodWrite[],
  expectedRequestVersion: number,
) {
  return sendJson<DemandPeriodsMutationResult>(
    `/api/v1/demands/${encodeURIComponent(number)}/lines/${encodeURIComponent(lineId)}/periods`,
    "PUT",
    { periods, expected_request_version: expectedRequestVersion },
  );
}

export function selectDemandAlternative(
  number: string,
  alternativeGroup: string,
  periodId: string,
) {
  return sendJson<DemandAlternativeSelectionResult>(
    `/api/v1/demands/${encodeURIComponent(number)}/alternative-groups/${encodeURIComponent(alternativeGroup)}/selection`,
    "PUT",
    { period_id: periodId },
  );
}

export function selectDemandLineAlternative(
  number: string,
  lineId: string,
  alternativeGroup: string,
  periodId: string,
) {
  return sendJson<DemandAlternativeSelectionResult>(
    `/api/v1/demands/${encodeURIComponent(number)}/lines/${encodeURIComponent(lineId)}/alternative-groups/${encodeURIComponent(alternativeGroup)}/selection`,
    "PUT",
    { period_id: periodId },
  );
}

export function moveAllocation(allocationId: string, payload: AllocationMoveWrite) {
  return sendJson<Record<string, unknown>>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/move`,
    "POST",
    payload,
  );
}

export function updateAllocation(allocationId: string, payload: ManualAllocationUpdate) {
  return sendJson<Record<string, unknown>>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}`,
    "PUT",
    payload,
  );
}

export function createQuickShift(payload: QuickShiftCreate, idempotencyKey: string) {
  return sendJson<QuickShiftCreated>(
    "/api/v1/quick-shifts",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}
