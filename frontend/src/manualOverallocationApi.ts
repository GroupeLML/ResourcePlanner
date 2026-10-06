import { ApiError, DemandMutationResult, ManualAllocationUpdate } from "./api";
import { csrfHeaders } from "./csrf";
import { SegmentMutationResult, SegmentUpdateWrite } from "./segments-api";

export type OverallocationPolicy = "KEEP_EXCEPTION" | "INCREASE_PLANNED";

export type AtomicAllocationResult = {
  operation: "SPLIT" | "DUPLICATE" | "EXTEND_AND_MOVE";
  source_allocation_id: string;
  target_allocation_id: string;
  source_shift_id: string;
  target_shift_id: string;
  segment_id: string;
  requirement_id: string;
  source_hours: number;
  target_hours: number;
  planned_hours: number;
  locked_hours: number;
  excess_hours: number;
  planning_version: number;
  approval_revision_id: string | null;
  operational_version: number | null;
  auto_source_converted: boolean;
};

export type AtomicAllocationWrite = {
  resource_id: string;
  day: string;
  expected_planning_version: number;
  outside_standard_hours: boolean | null;
  expected_approval_revision_id: string | null;
  expected_operational_version: number | null;
  overallocation_policy: OverallocationPolicy | null;
};

export type AtomicAllocationSplitWrite = AtomicAllocationWrite & {
  transfer_hours: number;
};

export type PlanningDropAction = {
  code: "MOVE" | "SPLIT" | "DUPLICATE" | "EXTEND_AND_MOVE" | "OVERRIDE_WINDOW_AND_MOVE" | "PROPOSE_WINDOW_EXTENSION" | "CANCEL" | string;
  label: string;
  enabled: boolean;
  required_parameters: string[];
  required_permission?: string | null;
  reason_code?: string | null;
  reason?: string | null;
};

export type PlanningDropWarning = {
  code: string;
  message: string;
  excess_hours?: number;
};

export type PlanningDropEvaluation = {
  allocation_id: string;
  source_shift_id: string;
  segment_id: string;
  requirement_id: string;
  origin: string;
  source_resource_id: string;
  target_resource_id: string;
  target_day: string;
  current_window: { start: string; end: string };
  proposed_window: { start: string; end: string };
  requested_window: { start: string; end: string } | null;
  planning_version: number;
  approval_revision_id: string | null;
  approved_entry_key: string | null;
  request_line_id: string | null;
  period_key: string | null;
  approved_window: { start: string; end: string } | null;
  request_number: string | null;
  request_version: number | null;
  operational_version: number | null;
  authorization_decision: string;
  availability_hours: number;
  planned_hours: number;
  current_locked_hours: number;
  projected_locked_hours: number;
  projected_excess_hours: number;
  actions: PlanningDropAction[];
  warnings: PlanningDropWarning[];
};

export type AllocationDropEvaluateWrite = {
  resource_id: string;
  day: string;
  outside_standard_hours: boolean;
  include_planning_window_override_options?: boolean;
};

export type PlanningWindowOverrideResult = {
  operation: "OVERRIDE_WINDOW" | "OVERRIDE_WINDOW_AND_MOVE" | string;
  segment_id: string;
  requirement_id: string;
  override_id: string;
  approved_window: { start: string; end: string };
  effective_window: { start: string; end: string };
  planning_version: number;
  approval_revision_id: string;
  operational_version: number | null;
  allocation_id: string | null;
  shift_id: string | null;
  resource_id: string | null;
  work_date: string | null;
  auto_source_converted: boolean;
};

export type PlanningWindowOverrideExtendWrite = {
  start_date: string;
  end_date: string;
  reason: string;
  expected_planning_version: number;
  expected_approval_revision_id: string;
  expected_operational_version: number | null;
};

export type AllocationWindowOverrideMoveWrite = {
  resource_id: string;
  day: string;
  reason: string;
  expected_planning_version: number;
  expected_approval_revision_id: string;
  expected_operational_version: number | null;
  outside_standard_hours: boolean;
  overallocation_policy: OverallocationPolicy | null;
};

export type AllocationExtendMoveWrite = Omit<AtomicAllocationWrite, "outside_standard_hours"> & {
  outside_standard_hours: boolean;
  confirm_window_extension: boolean;
};

export type AllocationWindowExtensionProposalWrite = {
  resource_id: string;
  day: string;
  outside_standard_hours: boolean;
  expected_request_version: number;
  expected_approval_revision_id: string;
};

export type OverallocationContext = {
  segment_id: string;
  planned_hours: number;
  current_planned_hours?: number;
  current_locked_hours?: number;
  projected_locked_hours?: number;
  locked_hours?: number;
  current_excess_hours?: number;
  excess_hours: number;
};

export class OverallocationApiError extends ApiError {
  readonly context: unknown;

  constructor(message: string, status: number, code: string | null, context: unknown) {
    super(message, status, code);
    this.name = "OverallocationApiError";
    this.context = context;
  }
}

type ErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    context?: unknown;
  };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function errorFromResponse(response: Response) {
  let payload: ErrorPayload | null = null;
  try {
    payload = (await response.json()) as ErrorPayload;
  } catch {
    // Preserve a stable fallback if a proxy returns non-JSON content.
  }
  return new OverallocationApiError(
    payload?.error?.message || `Erreur HTTP ${response.status}`,
    response.status,
    payload?.error?.code ?? null,
    payload?.error?.context ?? null,
  );
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
  if (!response.ok) throw await errorFromResponse(response);
  return response.json() as Promise<T>;
}

export function overallocationContext(reason: unknown): OverallocationContext | null {
  if (!(reason instanceof OverallocationApiError)) return null;
  if (!reason.context || typeof reason.context !== "object") return null;
  const value = reason.context as Record<string, unknown>;
  if (typeof value.segment_id !== "string") return null;
  if (typeof value.planned_hours !== "number") return null;
  if (typeof value.excess_hours !== "number") return null;
  return value as unknown as OverallocationContext;
}

export function createManualAllocationWithOverallocation(
  segmentId: string,
  payload: ManualAllocationUpdate,
  idempotencyKey: string,
  policy: OverallocationPolicy | null = null,
) {
  return sendJson<Record<string, unknown>>(
    `/api/v1/segments/${encodeURIComponent(segmentId)}/allocations`,
    "POST",
    {
      ...payload,
      overallocation_policy: policy,
    },
    { "Idempotency-Key": idempotencyKey },
  );
}

export function splitAllocationAtomic(
  allocationId: string,
  payload: AtomicAllocationSplitWrite,
  idempotencyKey: string,
) {
  return sendJson<AtomicAllocationResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/split`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function duplicateAllocationAtomic(
  allocationId: string,
  payload: AtomicAllocationWrite,
  idempotencyKey: string,
) {
  return sendJson<AtomicAllocationResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/duplicate`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function evaluateAllocationDrop(
  allocationId: string,
  payload: AllocationDropEvaluateWrite,
) {
  return sendJson<PlanningDropEvaluation>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/evaluate-drop`,
    "POST",
    payload,
  );
}

export function extendPlanningWindowOverride(
  segmentId: string,
  payload: PlanningWindowOverrideExtendWrite,
  idempotencyKey: string,
) {
  return sendJson<PlanningWindowOverrideResult>(
    `/api/v1/segments/${encodeURIComponent(segmentId)}/planning-window-override`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function overrideAndMovePlanningWindow(
  allocationId: string,
  payload: AllocationWindowOverrideMoveWrite,
  idempotencyKey: string,
) {
  return sendJson<PlanningWindowOverrideResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/planning-window-override-move`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function extendAndMoveAllocationAtomic(
  allocationId: string,
  payload: AllocationExtendMoveWrite,
  idempotencyKey: string,
) {
  return sendJson<AtomicAllocationResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/extend-and-move`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function proposeAllocationWindowExtension(
  allocationId: string,
  payload: AllocationWindowExtensionProposalWrite,
  idempotencyKey: string,
) {
  return sendJson<DemandMutationResult>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}/propose-window-extension`,
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export async function releaseManualAllocation(allocationId: string) {
  const response = await fetch(
    `${API_BASE}/api/v1/allocations/${encodeURIComponent(allocationId)}/release`,
    {
      method: "POST",
      headers: { Accept: "application/json", ...csrfHeaders() },
      credentials: "include",
    },
  );
  if (!response.ok) throw await errorFromResponse(response);
  return response.json() as Promise<Record<string, unknown>>;
}

export async function deleteManualAllocation(allocationId: string) {
  const response = await fetch(
    `${API_BASE}/api/v1/allocations/${encodeURIComponent(allocationId)}`,
    {
      method: "DELETE",
      headers: { Accept: "application/json", ...csrfHeaders() },
      credentials: "include",
    },
  );
  if (!response.ok) throw await errorFromResponse(response);
  return response.json() as Promise<Record<string, unknown>>;
}

export function updateAllocationWithOverallocation(
  allocationId: string,
  payload: ManualAllocationUpdate,
  policy: OverallocationPolicy | null = null,
) {
  return sendJson<Record<string, unknown>>(
    `/api/v1/allocations/${encodeURIComponent(allocationId)}`,
    "PUT",
    {
      ...payload,
      overallocation_policy: policy,
    },
  );
}

export function updateSegmentWithOverallocation(
  segmentId: string,
  payload: SegmentUpdateWrite,
  allowLockedOverallocation: boolean,
) {
  return sendJson<SegmentMutationResult>(
    `/api/v1/segments/${encodeURIComponent(segmentId)}`,
    "PATCH",
    {
      ...payload,
      allow_locked_overallocation: allowLockedOverallocation,
    },
  );
}
