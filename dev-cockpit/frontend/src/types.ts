export type PipelineKind =
  | 'WORK'
  | 'ARCHITECTURE_GATE'
  | 'ENVIRONMENT_GATE'

export type PipelineStep = {
  key: string
  title: string
  kind: PipelineKind
  done: boolean
  lane: 'MAIN' | 'PARALLEL'
  marker: string | null
  issue_number: number | null
  status: string | null
  rationale: string | null
}

export type Job = {
  id: number | null
  name: string
  status: string | null
  conclusion: string | null
  url: string | null
  started_at?: string | null
  completed_at?: string | null
}

export type Run = {
  id: number
  name: string
  status: string
  conclusion: string | null
  url: string | null
  run_number: number | null
  created_at: string | null
  updated_at: string | null
  jobs: Job[]
}

export type PullRequest = {
  number: number
  title: string
  body: string
  state: string
  merged: boolean
  draft: boolean
  mergeable: boolean | null
  mergeable_state: string | null
  url: string
  head: string
  head_sha: string
  base: string
  created_at: string | null
  updated_at: string
  merged_at: string | null
  runs: Run[]
}

export type RoadmapItem = {
  key: string
  title: string
  done: boolean
  marker: string | null
  issue_number: number | null
}

export type Issue = {
  number: number
  title: string
  state: string
  url: string
  updated_at: string
}

export type Branch = {
  name: string
  sha: string | null
  url: string
}

export type Commit = {
  sha: string
  short_sha: string
  message: string
  date: string
  url: string
}

export type StalledDetails = {
  level: 'confirmed' | 'stalled' | 'possible' | null
  threshold_minutes: number
  ci_failed_at: string | null
  ci_failed_minutes: number | null
  last_commit_at: string | null
  last_commit_minutes: number | null
  last_activity_at: string | null
  last_activity_minutes: number | null
  last_activity_source: string | null
  no_new_commit: boolean
  no_active_workflow: boolean
  branch_present: boolean
  pr_present: boolean
  explicit_in_progress: boolean
}

export type ReconciliationEvidence = {
  kind: 'pull_request' | 'issue'
  label: string
  url: string | null
  state: string | null
}

export type ReconciliationFinding = {
  code: string
  severity: 'stale' | 'attention'
  key: string
  message: string
  evidence: ReconciliationEvidence[]
}

export type PipelineReconciliation = {
  status: 'coherent' | 'stale' | 'attention' | 'invalid' | 'legacy'
  summary: string
  findings: ReconciliationFinding[]
  proposal: {
    changes: Array<{
      key: string
      from: string
      to: string
    }>
    pipeline_block: string
  } | null
}

export type ExecutionPhase =
  | 'PIPELINE_INVALID'
  | 'ROADMAP_UPDATE_REQUIRED'
  | 'ARCHITECTURE_GATE'
  | 'ENVIRONMENT_GATE'
  | 'READY'
  | 'DEVELOPING'
  | 'PR_OPEN'
  | 'CI_RUNNING'
  | 'CI_RED'
  | 'STALLED'
  | 'POSSIBLE_STALL'
  | 'READY_TO_MERGE'
  | 'DELIVERY_UNVERIFIED'
  | 'NO_ACTIVE_WORK'

export type ExecutionLink = {
  label: string
  url: string
}

export type ExecutionTimelineEvent = {
  kind: 'roadmap' | 'commit' | 'pull_request' | 'ci'
  label: string
  detail: string
  at: string
  url: string | null
  status: string | null
}

export type RoleMission = {
  state: 'action' | 'waiting' | 'clear' | 'blocked'
  title: string
  detail: string
  prompt: string
  primary_link: ExecutionLink | null
}

export type HandoffConfidence = 'COMPLETE' | 'PARTIAL' | 'BLOCKED'

export type HandoffSource = {
  label: string
  url: string
  kind: 'roadmap' | 'issue' | 'pull_request' | 'branch' | 'commit' | 'adr'
}

export type HandoffPack = {
  role: AvatarPreset
  role_label: string
  confidence: HandoffConfidence
  missing: string[]
  title: string
  prompt: string
  sources: HandoffSource[]
  section: {
    title: string
    content: string
  } | null
}

export type HandoffProjection = {
  active_key: string | null
  packs: Record<AvatarPreset, HandoffPack>
}

export type ExecutionControl = {
  phase: ExecutionPhase
  label: string
  summary: string
  next_action: string
  prompt: string
  responsible_role: 'product-owner' | 'developer' | 'architect' | 'reviewer'
  primary_link: ExecutionLink | null
  timeline: ExecutionTimelineEvent[]
  missions: Record<AvatarPreset, RoleMission>
}

export type AttentionLevel = 'ACTION' | 'WATCH' | 'CLEAR'

export type AttentionItem = {
  id: string
  code: string
  level: 'ACTION' | 'WATCH'
  role: AvatarPreset
  role_label: string
  key: string
  title: string
  detail: string
  action: string
  primary_link: ExecutionLink | null
  handoff_confidence: HandoffConfidence | null
}

export type AttentionCenter = {
  status: AttentionLevel
  action_count: number
  watch_count: number
  summary: string
  roles: Record<
    'product-owner' | 'developer' | 'architect' | 'reviewer',
    {
      label: string
      actions: number
      watches: number
    }
  >
  items: AttentionItem[]
}

export type RoadmapWritebackPreview = {
  status: 'ready'
  repo: string
  human_roadmap_issue: number
  pipeline_issue: number
  pipeline_url: string | null
  expected_updated_at: string
  expected_body_sha256: string
  proposal_sha256: string
  proposed_body_sha256: string
  changes: Array<{
    key: string
    from: string
    to: string
  }>
  current_block: string
  proposed_block: string
  diff: string
  safety: {
    only_canonical_block: boolean
    reconciler_status: 'stale'
    explicit_confirmation_required: boolean
    optimistic_lock: string
  }
}

export type RoadmapWritebackResult = {
  status: 'applied'
  repo: string
  human_roadmap_issue: number
  pipeline_issue: number
  pipeline_url: string | null
  previous_body_sha256: string
  body_sha256: string
  updated_at: string | null
  changes: Array<{
    key: string
    from: string
    to: string
  }>
  pipeline_block: string
}

export type FlowAnalyticsAttempt = {
  sha: string
  short_sha: string
  state: 'green' | 'red' | 'pending' | 'unknown'
  started_at: string | null
  completed_at: string | null
  workflow_count: number
  workflows: Array<{
    name: string | null
    status: string | null
    conclusion: string | null
    url: string | null
    created_at: string | null
    updated_at: string | null
  }>
  failed_jobs: string[]
  url: string | null
}

export type FlowAnalyticsDelivery = {
  key: string
  title: string
  parent_issue: number | null
  lane: 'MAIN' | 'PARALLEL'
  status: 'complete' | 'partial' | 'unavailable'
  reason: string | null
  pr: {
    number: number
    title: string
    url: string
    head?: string | null
    created_at: string | null
    merged_at: string | null
  } | null
  first_commit_at?: string | null
  first_green_at?: string | null
  metrics: {
    commit_to_pr_minutes?: number | null
    pr_to_green_minutes?: number | null
    green_to_merge_minutes?: number | null
    total_observed_minutes?: number | null
    pr_to_merge_minutes?: number | null
    validation_attempts?: number | null
    red_attempts?: number | null
    red_recovery_minutes?: number | null
    recovered_red_attempts?: number | null
  }
  bottleneck?: {
    segment: string
    label: string
    minutes: number
  } | null
  attempts: FlowAnalyticsAttempt[]
  timeline: Array<{
    kind: 'commit' | 'pull_request' | 'ci'
    label: string
    at: string | null
    status: string
    url: string | null
  }>
  diagnostics: string[]
}

export type FlowAnalyticsReport = {
  status: 'complete' | 'partial' | 'unavailable'
  repo: string
  pipeline_issue: number
  generated_at: string
  reason: string | null
  summary: {
    requested_limit: number
    delivery_count: number
    analyzable_count: number
    unavailable_count: number
    complete_count: number
    partial_count: number
    median_total_observed_minutes: number | null
    median_commit_to_pr_minutes: number | null
    median_pr_to_green_minutes: number | null
    median_green_to_merge_minutes: number | null
    average_validation_attempts: number | null
    average_red_attempts: number | null
    trend: {
      metric: 'pr_to_green_minutes'
      older_median: number
      recent_median: number
      delta_minutes: number
      sample_size: number
      description: string
    } | null
  } | null
  deliveries: FlowAnalyticsDelivery[]
  notes: string[]
}

export type Dashboard = {
  repo: string
  generated_at: string
  config: {
    roadmap_issue: number
    canonical_pipeline_issue: number
    canonical_pipeline_source_mode: 'LEGACY_EMBEDDED' | 'DEDICATED_STRICT'
    migration_candidate_pipeline_issue: number
    stalled_after_minutes: number
    token_configured: boolean
  }
  pipeline: {
    steps: PipelineStep[]
    valid: boolean
    source: 'canonical_v1' | 'legacy'
    errors: string[]
    completed_count: number
    now: PipelineStep | null
    parallel: PipelineStep[]
    next: PipelineStep[]
    later: PipelineStep[]
  }
  pipeline_source: {
    number: number
    title: string | null
    url: string | null
    updated_at: string | null
    strict: boolean
    mode: 'LEGACY_EMBEDDED' | 'DEDICATED_STRICT'
  }
  reconciliation: PipelineReconciliation
  execution: ExecutionControl
  handoff: HandoffProjection
  attention: AttentionCenter
  roadmap: {
    number: number
    title: string
    url: string
    updated_at: string
    declared_active: string | null
    active_issue: number | null
    effective_active: string | null
    items: RoadmapItem[]
  }
  active_work: {
    key: string
    issue_number: number
    title: string | null
    issue: Issue
    subitem_key: string | null
    block_done: boolean
    can_chain_block: boolean
    remaining_subitems: string[]
    primary_pr: PullRequest | null
    active_branch: Branch | null
    last_commit: Commit | null
    states: string[]
    stalled: boolean
    stall_level: 'confirmed' | 'stalled' | 'possible' | null
    stalled_details: StalledDetails
    failed_jobs: string[]
    explicit_in_progress: boolean
    active_runs: Run[]
    merged_but_unmarked_pr: {
      number: number
      title: string
      url: string
      merged_at: string
    } | null
  } | null
  architecture: {
    path: string
    url: string
    adrs: Array<{ name: string; url: string }>
    referenced_adrs: string[]
  }
  open_prs: PullRequest[]
  related_issues: Issue[]
  latest_commit: Commit | null
  next_action: string
  dev_prompt: string
  warnings: string[]
}

export type CockpitConfig = {
  repository: string
  roadmap_issue: number
  canonical_pipeline_issue: number
  migration_candidate_pipeline_issue: number
  stalled_after_minutes: number
  token_configured: boolean
}

export type AvatarPreset =
  | 'product-owner'
  | 'developer'
  | 'architect'
  | 'reviewer'
  | 'generic'

export type RoleConfig = {
  id: string
  name: string
  avatar: AvatarPreset
  chat_url: string
  enabled: boolean
  order: number
}

export type RolesConfig = {
  roles: RoleConfig[]
}

export type ChatEffectiveState =
  | 'working'
  | 'idle'
  | 'possible_stall'
  | 'disconnected'

export type ChatConversationStatus = {
  conversation_url: string
  state: 'working' | 'idle'
  effective_state: ChatEffectiveState
  connected: boolean
  page_visible: boolean
  page_focused: boolean
  ui_signal: 'stop-control' | 'none'
  last_seen_at: string
  last_seen_seconds: number
  working_since: string | null
  last_completed_at: string | null
  last_completed_seconds: number | null
}

export type ChatStatusResponse = {
  stale_after_seconds: number
  conversations: ChatConversationStatus[]
}

export type MarkdownSection = {
  title: string
  level: number
  work_key: string | null
  content: string
}

export type DetailDocument = {
  name: string
  path: string
  url: string
  title: string
  status: string | null
  content: string
}

export type IssueDetail = Issue & {
  body: string
  sections: MarkdownSection[]
  referenced_issues: number[]
  documents: DetailDocument[]
}

export type RoadmapDetail = Issue & {
  body: string
  items: RoadmapItem[]
}

export type CommitFileDetail = {
  filename: string
  status: string | null
  additions: number | null
  deletions: number | null
  changes: number | null
  url: string | null
}

export type CommitDetail = Commit & {
  author: string | null
  stats: {
    additions: number | null
    deletions: number | null
    total: number | null
  }
  files: CommitFileDetail[]
  documentation: DetailDocument[]
}

export type ArchitectureDetail = {
  path: string
  url: string
  documents: DetailDocument[]
}
