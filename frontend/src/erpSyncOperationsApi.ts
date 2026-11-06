import { ApiError } from "./api";
import { createClientId } from "./clientId";
import { csrfHeaders } from "./csrf";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const ERP_BASE = "/api/v1/integrations/acumatica/erp-sync";

type ErrorPayload = {
  error?: { code?: string; message?: string; context?: Record<string, unknown> };
};

async function failure(response: Response): Promise<ApiError> {
  let payload: ErrorPayload | null = null;
  try {
    payload = (await response.json()) as ErrorPayload;
  } catch {
    // Stable fallback below.
  }
  return new ApiError(
    payload?.error?.message || `Erreur HTTP ${response.status}`,
    response.status,
    payload?.error?.code ?? null,
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...csrfHeaders(),
      ...(init?.headers ?? {}),
    },
    credentials: "include",
  });
  if (!response.ok) throw await failure(response);
  return response.json() as Promise<T>;
}

export type ErpSyncAttemptOperations = {
  id: string;
  occurrence_id: string;
  status: string;
  phase: string;
  trigger_kind: string;
  generation: number;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  duration_ms: number | null;
  projects_received: number;
  projects_created: number;
  projects_updated: number;
  projects_unchanged: number;
  task_projects_total: number | null;
  task_projects_synchronized: number | null;
  task_projects_rejected: number | null;
  task_rows_received: number | null;
  tasks_received: number | null;
  tasks_created: number | null;
  tasks_updated: number | null;
  tasks_unchanged: number | null;
  tasks_deactivated: number | null;
  tasks_rejected: number | null;
  error_code: string | null;
  diagnostic: string | null;
};

export type ErpSyncProjectOperations = {
  project_number: string;
  result_status: string | null;
  quality_state: string;
  source_observed_at: string | null;
  validated_at: string | null;
  reason_code: string | null;
  error_code: string | null;
  producer_attempt_id: string | null;
  producer_generation: number | null;
};

export type ErpSyncRecoveryOperations = {
  state: string;
  attempt_id: string | null;
  reason_code: string | null;
  pinned: boolean;
};

export type ErpSyncEvidenceOperations = {
  status: string;
  manifests_total: number;
  source_manifests: number;
  checkpoint_manifests: number;
  manifests_ready: number;
  manifests_expired: number;
  manifests_pinned: number;
  active_reservations: number;
  reserved_bytes: number;
  used_bytes: number;
  budget_bytes: number | null;
  pressure_ratio: number | null;
};

export type ErpSyncOperationsSnapshot = {
  automation_enabled: boolean;
  automatic_execution_available: boolean;
  scheduler_status: string;
  scheduler_observation_status: string;
  scheduler_observed_at: string | null;
  scheduler_observation_age_seconds: number | null;
  next_scheduled_run: string | null;
  business_timezone: string;
  current_business_date: string;
  last_attempt: ErpSyncAttemptOperations | null;
  last_complete_success: ErpSyncAttemptOperations | null;
  last_partial_or_failed_attempt: ErpSyncAttemptOperations | null;
  current_phase: string | null;
  current_attempt_id: string | null;
  current_generation: number;
  current_owner_attempt_id: string | null;
  lease_expires_at: string | null;
  owner_deadline_at: string | null;
  recovery_state: ErpSyncRecoveryOperations;
  freshness_status: string;
  freshness_age_seconds: number | null;
  consecutive_failures: number;
  repeated_failure: boolean;
  projects: ErpSyncProjectOperations[];
  evidence: ErpSyncEvidenceOperations;
  compatibility: {
    status: string;
    protocol_compatible: boolean | null;
    configuration_compatible: boolean | null;
  };
  readiness_blockers: string[];
  safe_to_operate: boolean;
};

export type ErpSyncCommandResult = {
  run_id: string;
  attempt_id?: string;
  occurrence_id?: string;
  status: string;
  phase?: string;
  intent?: string;
  trigger_kind?: string;
  error_code?: string | null;
  diagnostic?: string | null;
};

export type ErpSyncEvidenceSource = {
  manifest_id: string;
  attempt_id: string;
  protocol_version: number;
  source_type: string;
  proof_type: string;
  profile_fingerprint: string;
  source_fingerprint: string | null;
  observed_at: string;
  row_count: number;
  chunk_count: number;
  plaintext_bytes: number;
  ciphertext_bytes: number;
  content_sha256: string;
  state: string;
  pinned: boolean;
  expires_at: string | null;
  diagnostic: string | null;
};

export type ErpSyncBaselineSummary = {
  baseline_id: string;
  source_type: string;
  profile_fingerprint: string;
  source_fingerprint: string;
  row_count: number;
  approved_by: string;
  approval_reason: string;
  approved_at: string;
};

export type ErpSyncBaselines = {
  available: boolean;
  sources: {
    PROJECTS: ErpSyncBaselineSummary | null;
    TASKS: ErpSyncBaselineSummary | null;
  };
};

export type ErpSyncBootstrapBaselineSource = {
  manifest_id: string;
  attempt_id: string;
  source_type: "PROJECTS" | "TASKS";
  row_count: number;
  observed_at: string;
  state: string;
  diagnostic: string;
};

export type ErpSyncBootstrapBaselineCandidates = {
  attempt_id: string | null;
  sources: ErpSyncBootstrapBaselineSource[];
};

export type ErpSyncReviewSource = {
  manifest_id: string;
  source_type: string;
  source_fingerprint: string;
  row_count: number;
  state: string;
  diagnostics: string[];
  rejected_projects: string[];
};

export type ErpSyncReviewScope = {
  manifest_id: string;
  source_type: string;
  threshold_scope: string;
};

export type ErpSyncPlausibilityReview = {
  attempt_id: string;
  status: string;
  phase: string;
  sources: ErpSyncReviewSource[];
  allowed_exception_scopes: ErpSyncReviewScope[];
};

export type ErpSyncCompensationPreview = {
  attempt_id: string;
  control_generation: number;
  preview_hash: string;
  eligible_records: number;
  conflict_count: number;
  can_confirm: boolean;
  conflicts: string[];
  reason: string;
};

export function getErpSyncOperations(signal?: AbortSignal) {
  return request<ErpSyncOperationsSnapshot>(`${ERP_BASE}/operations`, { signal });
}

export function getErpSyncBaselines(signal?: AbortSignal) {
  return request<ErpSyncBaselines>(`${ERP_BASE}/baselines`, { signal });
}

export function getErpSyncBootstrapBaselineCandidates(signal?: AbortSignal) {
  return request<ErpSyncBootstrapBaselineCandidates>(
    `${ERP_BASE}/bootstrap-baseline-candidates`,
    { signal },
  );
}

export function getErpSyncAttemptEvidence(attemptId: string, signal?: AbortSignal) {
  return request<{ attempt_id: string; sources: ErpSyncEvidenceSource[] }>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/evidence`,
    { signal },
  );
}

export function getErpSyncPlausibilityReview(attemptId: string, signal?: AbortSignal) {
  return request<ErpSyncPlausibilityReview>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/plausibility-review`,
    { signal },
  );
}

export function runErpSyncNow(idempotencyKey = createClientId()) {
  return request<ErpSyncCommandResult>(`${ERP_BASE}/run-now`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

export function retryErpSync(occurrenceId: string, idempotencyKey = createClientId()) {
  return request<ErpSyncCommandResult>(
    `${ERP_BASE}/occurrences/${encodeURIComponent(occurrenceId)}/retry`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
}

export function approveErpBaseline(manifestId: string, reason: string) {
  return request<{ baseline_id: string; manifest_id: string }>(
    `${ERP_BASE}/evidence/${encodeURIComponent(manifestId)}/approve-baseline`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

export function approveErpPlausibilityException(
  attemptId: string,
  payload: { manifest_id: string; threshold_scope: string; reason: string },
) {
  return request<{ exception_id: string; attempt_id: string }>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/plausibility-exceptions`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function continueErpPlausibilityReview(attemptId: string) {
  return request<ErpSyncCommandResult>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/plausibility-continue`,
    { method: "POST" },
  );
}

export function declineErpPlausibilityReview(attemptId: string, reason: string) {
  return request<ErpSyncCommandResult & { review_decision: string }>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/plausibility-decline`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

export function previewErpCompensation(attemptId: string, reason: string) {
  return request<ErpSyncCompensationPreview>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/compensation/preview`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

export function confirmErpCompensation(
  attemptId: string,
  previewHash: string,
  reason: string,
  idempotencyKey = createClientId(),
) {
  return request<{ receipt_id: string; status: string; affected_records: number }>(
    `${ERP_BASE}/runs/${encodeURIComponent(attemptId)}/compensation/confirm`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ preview_hash: previewHash, reason }),
    },
  );
}
