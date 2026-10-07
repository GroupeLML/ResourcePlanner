import { ApiError, type ViewScope } from "./api";

export type PlanDeltaChange = "ADD" | "MODIFY" | "MOVE" | "CANCEL";

export type DemandPlanDeltaItem = {
  change: PlanDeltaChange;
  segment_id: string;
  current_resource_name: string | null;
  proposed_resource_name: string | null;
  current_resource_id: string | null;
  proposed_resource_id: string | null;
  current_date: string | null;
  proposed_date: string | null;
  current_hours: number;
  proposed_hours: number;
  current_allocation_type: string | null;
  proposed_allocation_type: string | null;
  current_outside_standard_hours: boolean;
  proposed_outside_standard_hours: boolean;
  locked: boolean;
};

export type DemandPlanDeltaDiagnostic = {
  code: string;
  message: string;
  requirement_id: string | null;
  spec_key: string[] | null;
  shift_ids: string[];
};

export type EnvelopeChange = {
  code: string;
  entry_key?: string | null;
  detail?: string | null;
  reference_hours?: string | null;
  candidate_hours?: string | null;
  delta_hours?: string | null;
  delta_percent?: string | null;
};

export type DemandApprovalState = {
  demand_number: string;
  candidate_request_version: number;
  approval_reference_status: string | null;
  active_revision_id: string | null;
  previous_revision_id: string | null;
  approved_request_version: number | null;
  approved_at: string | null;
  approved_by_name: string | null;
  authorization_fingerprint: string | null;
  candidate_authorization_fingerprint: string | null;
  candidate_matches_approved: boolean | null;
  payload_format_version: number | null;
  operational_version: number | null;
  envelope_decision: string | null;
  envelope_reason: string | null;
  envelope_changes: EnvelopeChange[];
  active_selections: Record<string, string>;
  active_confirmations: Record<string, string>;
  active_budget_overrides: Record<string, number>;
  active_requirement_count: number;
  active_planned_hours: number;
  active_approved_entry_keys: string[];
  active_matches_approved_revision: boolean | null;
  diagnostics: string[];
};

export type DemandPlanDelta = {
  demand_number: string;
  available: boolean;
  reason: string | null;
  has_changes: boolean;
  add_count: number;
  modify_count: number;
  move_count: number;
  cancel_count: number;
  current_hours: number;
  proposed_hours: number;
  net_hours: number;
  items: DemandPlanDeltaItem[];
  approval_reference_status: string | null;
  active_revision_id: string | null;
  approved_request_version: number | null;
  authorization_fingerprint: string | null;
  candidate_authorization_fingerprint: string | null;
  operational_version: number | null;
  envelope_decision: string | null;
  envelope_reason: string | null;
  diagnostics: DemandPlanDeltaDiagnostic[];
};

type ApiErrorPayload = {
  error?: { code?: string; message?: string };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function getReadJson<T>(path: string, scope?: ViewScope): Promise<T> {
  const params = new URLSearchParams();
  if (scope) params.set("scope", scope);
  const query = params.toString();
  const response = await fetch(
    `${API_BASE}${path}${query ? `?${query}` : ""}`,
    { headers: { Accept: "application/json" } },
  );
  if (!response.ok) {
    let payload: ApiErrorPayload | null = null;
    try {
      payload = (await response.json()) as ApiErrorPayload;
    } catch {
      // Keep the stable fallback for non-JSON intermediaries.
    }
    throw new ApiError(
      payload?.error?.message || `Erreur HTTP ${response.status}`,
      response.status,
      payload?.error?.code ?? null,
    );
  }
  return response.json() as Promise<T>;
}

export function getDemandApprovalState(
  number: string,
  scope?: ViewScope,
): Promise<DemandApprovalState> {
  return getReadJson<DemandApprovalState>(
    `/api/v1/demands/${encodeURIComponent(number)}/approval-state`,
    scope,
  );
}

export function getDemandPlanDelta(
  number: string,
  scope?: ViewScope,
): Promise<DemandPlanDelta> {
  return getReadJson<DemandPlanDelta>(
    `/api/v1/demands/${encodeURIComponent(number)}/plan-delta`,
    scope,
  );
}
