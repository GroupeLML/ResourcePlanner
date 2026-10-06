import { ApiError } from "./api";
import { createClientId } from "./clientId";
import { csrfHeaders } from "./csrf";

export type VerificationAction =
  | "VIEW_SCOPE"
  | "PILOT_SCOPE"
  | "RECORD_STORY_DECISION"
  | "DEFINE_STORY_REQUIREMENTS"
  | "REVISE_REQUIREMENTS"
  | "ASSIGN_EXECUTORS"
  | "WITHDRAW_REQUIREMENTS"
  | "REQUEST_RETEST"
  | "RECORD_RESULT"
  | "ADD_EVIDENCE";

export type VerificationDecisionKind = "NO_TEST_REQUIRED" | "TESTS_DEFINED";
export type VerificationPhase = "FAT" | "SAT" | "COMMISSIONING";
export type VerificationRequirementState = "ACTIVE" | "WITHDRAWN";
export type VerificationResult = "PASS" | "FAIL" | "BLOCKED";
export type VerificationProjectedStatus = VerificationResult | "NOT_RUN";
export type VerificationMeasure = string | number | boolean | null;

export type VerificationRequirementDefinition = {
  phase: VerificationPhase;
  objective: string;
  method: string;
  expected_result: string;
  prerequisites: string[];
  criticality?: string | null;
};

export type StoryVerificationDecisionPayload = {
  kind: VerificationDecisionKind;
  justification?: string | null;
  existing_requirement_ids?: string[];
  new_requirements?: VerificationRequirementDefinition[];
};

export type StoryVerificationClosureResult = {
  story_id: string;
  delivery_plan_id: string;
  work_package_id: string;
  story_status: "DONE";
  delivery_version: number;
  verification_scope_id: string;
  verification_version: number;
  decision_id: string;
  decision_kind: VerificationDecisionKind;
  requirement_ids: string[];
  historical: boolean;
};

export type VerificationEvidenceLinkReadModel = {
  id: string;
  url: string;
  label: string | null;
  provenance: string;
  added_by_user_id: string;
  created_at: string | null;
};

export type VerificationExecutionReadModel = {
  id: string;
  revision_id: string;
  sequence: number;
  result: VerificationResult;
  executor_user_id: string;
  executed_at: string | null;
  recorded_at: string | null;
  measurements: Record<string, VerificationMeasure>;
  comments: string | null;
  evidence_links: VerificationEvidenceLinkReadModel[];
};

export type VerificationRequirementReadModel = {
  id: string;
  story_id: string;
  phase: VerificationPhase;
  state: VerificationRequirementState;
  withdrawal_reason: string | null;
  current_revision: {
    id: string;
    revision_number: number;
    objective: string;
    method: string;
    expected_result: string;
    prerequisites: string[];
    criticality: string | null;
  } | null;
  assigned_executor_ids: string[];
  projected_status: VerificationProjectedStatus | null;
  retest_after_sequence: number;
  latest_execution_id: string | null;
  executions: VerificationExecutionReadModel[];
};

export type VerificationPackageReadModel = {
  work_package_id: string;
  verification_scope_id: string | null;
  verification_version: number | null;
  lead_user_id: string | null;
  allowed_actions: VerificationAction[];
  story_decision_coverage: {
    done_story_count: number;
    documented_done_story_count: number;
    undocumented_done_story_ids: string[];
    complete: boolean;
    coverage_rate: number | null;
  };
  phases: Record<
    VerificationPhase,
    {
      active: number;
      PASS: number;
      FAIL: number;
      BLOCKED: number;
      NOT_RUN: number;
    }
  >;
  active_requirement_count: number;
  withdrawn_requirement_count: number;
  pass_rate: number | null;
  no_tests_defined: boolean;
  requirements: VerificationRequirementReadModel[];
};

type ApiErrorPayload = {
  error?: { code?: string; message?: string };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function verificationDocumentUrl(path: string) {
  return `${API_BASE}${path}`;
}

export function verificationTestPlanUrl(workPackageId: string) {
  return verificationDocumentUrl(
    `/api/v1/verification/work-packages/${encodeURIComponent(workPackageId)}/documents/test-plan`,
  );
}

export function verificationPhaseReportUrl(
  workPackageId: string,
  phase: VerificationPhase,
) {
  return verificationDocumentUrl(
    `/api/v1/verification/work-packages/${encodeURIComponent(workPackageId)}/documents/reports/${phase}`,
  );
}

export function verificationTraceabilityUrl(workPackageId: string) {
  return verificationDocumentUrl(
    `/api/v1/verification/work-packages/${encodeURIComponent(workPackageId)}/documents/traceability.csv`,
  );
}

async function responseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload | null = null;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    // Preserve a stable fallback for proxy/non-JSON failures.
  }
  return new ApiError(
    payload?.error?.message || `Erreur HTTP ${response.status}`,
    response.status,
    payload?.error?.code ?? null,
  );
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    credentials: "include",
    signal,
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

async function sendJson<T>(
  path: string,
  body: unknown,
  idempotencyKey = createClientId(),
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...csrfHeaders(),
    },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export function getVerificationPackage(
  workPackageId: string,
  signal?: AbortSignal,
) {
  return getJson<VerificationPackageReadModel>(
    `/api/v1/verification/work-packages/${encodeURIComponent(workPackageId)}/package`,
    signal,
  );
}

export function completeStoryWithVerification(
  storyId: string,
  payload: {
    expected_delivery_version: number;
    expected_verification_version: number | null;
    decision: StoryVerificationDecisionPayload;
  },
  idempotencyKey?: string,
) {
  return sendJson<StoryVerificationClosureResult>(
    `/api/v1/delivery/items/${encodeURIComponent(storyId)}/complete-with-verification`,
    payload,
    idempotencyKey,
  );
}

export function recordHistoricalStoryVerificationDecision(
  storyId: string,
  payload: {
    expected_delivery_version: number;
    expected_verification_version: number | null;
    decision: StoryVerificationDecisionPayload;
  },
  idempotencyKey?: string,
) {
  return sendJson<StoryVerificationClosureResult>(
    `/api/v1/delivery/items/${encodeURIComponent(storyId)}/historical-verification-decision`,
    payload,
    idempotencyKey,
  );
}

export function assignVerificationExecutor(
  requirementId: string,
  executorUserId: string,
  expectedVerificationVersion: number,
) {
  return sendJson<{ verification_version: number }>(
    `/api/v1/verification/requirements/${encodeURIComponent(requirementId)}/assignments`,
    {
      executor_user_id: executorUserId,
      expected_verification_version: expectedVerificationVersion,
    },
  );
}

export function unassignVerificationExecutor(
  requirementId: string,
  executorUserId: string,
  reason: string,
  expectedVerificationVersion: number,
) {
  return sendJson<{ verification_version: number }>(
    `/api/v1/verification/requirements/${encodeURIComponent(requirementId)}/unassignments`,
    {
      executor_user_id: executorUserId,
      reason,
      expected_verification_version: expectedVerificationVersion,
    },
  );
}

export function recordVerificationExecution(
  requirementId: string,
  payload: {
    result: VerificationResult;
    executed_at?: string | null;
    measurements?: Record<string, VerificationMeasure>;
    comments?: string | null;
    expected_verification_version: number;
  },
) {
  return sendJson<{
    execution_id: string;
    sequence: number;
    verification_version: number;
  }>(
    `/api/v1/verification/requirements/${encodeURIComponent(requirementId)}/executions`,
    payload,
  );
}

export function addVerificationEvidenceLink(
  executionId: string,
  payload: {
    url: string;
    label?: string | null;
    provenance?: string | null;
    expected_verification_version: number;
  },
) {
  return sendJson<{ verification_version: number }>(
    `/api/v1/verification/executions/${encodeURIComponent(executionId)}/evidence-links`,
    payload,
  );
}

export function requestVerificationRetest(
  requirementId: string,
  reason: string,
  expectedVerificationVersion: number,
) {
  return sendJson<{
    after_execution_sequence: number;
    verification_version: number;
  }>(
    `/api/v1/verification/requirements/${encodeURIComponent(requirementId)}/retests`,
    {
      reason,
      expected_verification_version: expectedVerificationVersion,
    },
  );
}
