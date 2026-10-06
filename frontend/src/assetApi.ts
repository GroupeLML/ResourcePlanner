import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";

export type AssetTypeCatalogItem = {
  id: string;
  code: string;
  label: string;
  category: string;
  active: boolean;
  occupancy_policy: string;
  qualification_policy: string;
  required_competencies: { id: string; name: string }[];
  metadata: Record<string, unknown>;
};

export type AssetCatalogItem = {
  id: string;
  code: string;
  label: string;
  asset_type_id: string;
  active: boolean;
  approver_user_ids: string[];
  metadata: Record<string, unknown>;
};

export type AssetApproverCandidate = {
  id: string;
  display_name: string;
};

export type AssetCatalog = {
  types: AssetTypeCatalogItem[];
  assets: AssetCatalogItem[];
  approver_candidates: AssetApproverCandidate[];
  planning_version: number;
};

type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
  };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function responseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload | null = null;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    // Preserve the stable fallback for non-JSON proxy/server errors.
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
    signal,
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

export function getAssetCatalog(signal?: AbortSignal) {
  return getJson<AssetCatalog>("/api/v1/assets/catalog", signal);
}


export type AssetUnavailabilityCatalogItem = {
  id: string;
  asset_id: string;
  start_date: string;
  end_date: string;
  reason: string | null;
};

export type AssetPlanningState = {
  unavailability: AssetUnavailabilityCatalogItem[];
  planning_version: number;
};

export function getAssetPlanningState(signal?: AbortSignal) {
  return getJson<AssetPlanningState>("/api/v1/assets/requirements", signal);
}

export function createAssetType(payload: {
  code: string;
  label: string;
  category: string;
  metadata?: Record<string, unknown> | null;
}) {
  return sendJson<{ id: string; code: string }>(
    "/api/v1/assets/types",
    "POST",
    payload,
  );
}

export function updateAssetType(
  assetTypeId: string,
  payload: {
    code: string;
    label: string;
    category: string;
    expected_planning_version: number;
  },
) {
  return sendJson<{ id: string; planning_version: number }>(
    "/api/v1/assets/types/" + encodeURIComponent(assetTypeId),
    "PATCH",
    payload,
  );
}

export function setAssetTypeActive(
  assetTypeId: string,
  active: boolean,
  expectedPlanningVersion: number,
) {
  return sendJson<{ id: string; active: boolean; planning_version: number }>(
    "/api/v1/assets/types/" + encodeURIComponent(assetTypeId) + "/active",
    "PATCH",
    {
      active,
      expected_planning_version: expectedPlanningVersion,
    },
  );
}

export function setAssetTypeQualification(
  assetTypeId: string,
  payload: {
    competency_ids: string[];
    qualification_policy: string;
    expected_planning_version: number;
  },
) {
  return sendJson<{ id: string; planning_version: number }>(
    "/api/v1/assets/types/" + encodeURIComponent(assetTypeId) + "/qualification",
    "PUT",
    payload,
  );
}

export function createAsset(payload: {
  code: string;
  label: string;
  asset_type_id: string;
  metadata?: Record<string, unknown> | null;
}) {
  return sendJson<{ id: string; code: string }>(
    "/api/v1/assets",
    "POST",
    payload,
  );
}

export function updateAsset(
  assetId: string,
  payload: {
    code: string;
    label: string;
    asset_type_id: string;
    expected_planning_version: number;
  },
) {
  return sendJson<{ id: string; planning_version: number }>(
    "/api/v1/assets/" + encodeURIComponent(assetId),
    "PATCH",
    payload,
  );
}

export function setAssetActive(
  assetId: string,
  active: boolean,
  expectedPlanningVersion: number,
) {
  return sendJson<{ id: string; active: boolean; planning_version: number }>(
    "/api/v1/assets/" + encodeURIComponent(assetId) + "/active",
    "PATCH",
    {
      active,
      expected_planning_version: expectedPlanningVersion,
    },
  );
}

export function setAssetApprover(
  assetId: string,
  userId: string,
  assigned: boolean,
) {
  return sendJson<{ id: string; approver_user_ids: string[] }>(
    `/api/v1/assets/${encodeURIComponent(assetId)}/approvers/${encodeURIComponent(userId)}`,
    assigned ? "PUT" : "DELETE",
    {},
  );
}

export type ShiftAssetCandidate = {
  id: string;
  code: string;
  label: string;
  asset_type_id: string;
  asset_type_code: string;
  asset_type_label: string;
  active: boolean;
  compatible: boolean;
  available: boolean;
  qualification_state: string;
  allowed: boolean;
  reason: string | null;
  diagnostics: string[];
  currently_assigned: boolean;
  selection_mode:
    | "ASSIGN"
    | "DESIGNATE_PROJECT_OPERATOR"
    | "ALREADY_INHERITED"
    | "RESERVED_OTHER_OPERATOR";
  existing_allocation_id: string | null;
  existing_requirement_id: string | null;
  existing_operator_resource_id: string | null;
  existing_start_date: string | null;
  existing_end_date: string | null;
};

export type ShiftAssetCandidates = {
  shift_id: string;
  work_date: string;
  operator_resource_id: string;
  current_requirement_id: string | null;
  current_allocation_id: string | null;
  candidates: ShiftAssetCandidate[];
  planning_version: number;
};

export type ShiftAssetAssignmentResult = {
  operation: "ASSIGN" | "CHANGE" | "RELEASE" | "DESIGNATE_PROJECT_OPERATOR";
  shift_id: string;
  requirement_id: string;
  requirement_origin: string;
  allocation_id: string | null;
  asset_id: string | null;
  operator_resource_id: string | null;
  qualification_state: string | null;
  shift_source: string;
  shift_locked: boolean;
  planning_version: number;
};

export function getShiftAssetCandidates(
  shiftId: string,
  signal?: AbortSignal,
) {
  return getJson<ShiftAssetCandidates>(
    `/api/v1/assets/shifts/${encodeURIComponent(shiftId)}/assignment/candidates`,
    signal,
  );
}

export function setShiftAssetAssignment(
  shiftId: string,
  payload: {
    asset_id: string | null;
    asset_requirement_id: string | null;
    asset_allocation_id?: string | null;
    start_date?: string | null;
    end_date?: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<ShiftAssetAssignmentResult>(
    `/api/v1/assets/shifts/${encodeURIComponent(shiftId)}/assignment`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export type AssetOperatorCandidate = {
  resource_id: string;
  resource_name: string;
};

export type AssetOperatorCandidates = {
  requirement_id: string;
  allocation_id: string;
  qualification_state: "SATISFIED" | "MISSING_OPERATOR" | "SKILL_MISMATCH" | "NO_OVERLAP";
  required_competency_ids: string[];
  required_competency_names: string[];
  operator_resource_id: string | null;
  operator_resource_name: string | null;
  candidates: AssetOperatorCandidate[];
  planning_version: number;
};

export function getAssetOperatorCandidates(
  requirementId: string,
  signal?: AbortSignal,
) {
  return getJson<AssetOperatorCandidates>(
    `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/operator-candidates`,
    signal,
  );
}

export function setAssetRequirementOperator(
  requirementId: string,
  payload: {
    operator_resource_id: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<{
    planning_version: number;
    allocation_id: string;
    requirement_id: string;
    operator_resource_id: string | null;
    qualification_state: string;
  }>(
    `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/operator`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function reserveAssetRequirement(
  requirementId: string,
  payload: {
    asset_id: string | null;
    start_date?: string | null;
    end_date?: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<{ planning_version: number; allocation_id?: string | null; changed?: boolean }>(
    `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/reservation`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export type DirectAssetReservationResult = {
  operation: "CREATE" | "UPDATE" | "RELEASE";
  requirement_id: string;
  requirement_origin: "PROJECT_DIRECT" | "RESOURCE_PERIOD" | "SEGMENT";
  allocation_id: string | null;
  asset_id?: string | null;
  project_id?: string | null;
  resource_requirement_id?: string | null;
  context_resource_id?: string | null;
  operator_resource_id?: string | null;
  start_date?: string;
  end_date?: string;
  qualification_state?: string;
  planning_version: number;
};

export function createProjectDirectReservation(
  payload: {
    project_id: string;
    asset_type_id: string;
    asset_id: string;
    start_date: string;
    end_date: string;
    operator_resource_id: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    "/api/v1/assets/project-reservations",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateProjectDirectReservation(
  requirementId: string,
  payload: {
    asset_id: string;
    start_date: string;
    end_date: string;
    operator_resource_id: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    `/api/v1/assets/project-reservations/${encodeURIComponent(requirementId)}`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function createResourcePeriodReservation(
  payload: {
    resource_id: string;
    asset_type_id: string;
    asset_id: string;
    start_date: string;
    end_date: string;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    "/api/v1/assets/resource-period-reservations",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateResourcePeriodReservation(
  requirementId: string,
  payload: {
    asset_id: string;
    start_date: string;
    end_date: string;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    `/api/v1/assets/resource-period-reservations/${encodeURIComponent(requirementId)}`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

function releaseDirectReservation(
  path: string,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  const params = new URLSearchParams({
    expected_planning_version: String(expectedPlanningVersion),
  });
  return sendJson<DirectAssetReservationResult>(
    `${path}?${params.toString()}`,
    "DELETE",
    {},
    { "Idempotency-Key": idempotencyKey },
  );
}

export function releaseProjectDirectReservation(
  requirementId: string,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  return releaseDirectReservation(
    `/api/v1/assets/project-reservations/${encodeURIComponent(requirementId)}`,
    expectedPlanningVersion,
    idempotencyKey,
  );
}

export function releaseResourcePeriodReservation(
  requirementId: string,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  return releaseDirectReservation(
    `/api/v1/assets/resource-period-reservations/${encodeURIComponent(requirementId)}`,
    expectedPlanningVersion,
    idempotencyKey,
  );
}

export function createSegmentReservation(
  payload: {
    segment_id: string;
    asset_type_id: string;
    asset_id: string;
    start_date: string;
    end_date: string;
    operator_resource_id: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    "/api/v1/assets/segment-reservations",
    "POST",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function updateSegmentReservation(
  requirementId: string,
  payload: {
    asset_id: string;
    start_date: string;
    end_date: string;
    operator_resource_id: string | null;
    expected_planning_version: number;
  },
  idempotencyKey: string,
) {
  return sendJson<DirectAssetReservationResult>(
    `/api/v1/assets/segment-reservations/${encodeURIComponent(requirementId)}`,
    "PUT",
    payload,
    { "Idempotency-Key": idempotencyKey },
  );
}

export function releaseSegmentReservation(
  requirementId: string,
  expectedPlanningVersion: number,
  idempotencyKey: string,
) {
  return releaseDirectReservation(
    `/api/v1/assets/segment-reservations/${encodeURIComponent(requirementId)}`,
    expectedPlanningVersion,
    idempotencyKey,
  );
}

export function addAssetUnavailability(
  assetId: string,
  payload: {
    start_date: string;
    end_date: string;
    reason: string | null;
    expected_planning_version: number;
  },
) {
  return sendJson<{ planning_version: number; id?: string }>(
    `/api/v1/assets/${encodeURIComponent(assetId)}/unavailability`,
    "POST",
    payload,
  );
}

export async function removeAssetUnavailability(
  assetId: string,
  unavailabilityId: string,
  expectedPlanningVersion: number,
) {
  const params = new URLSearchParams({
    expected_planning_version: String(expectedPlanningVersion),
  });
  const response = await fetch(
    `${API_BASE}/api/v1/assets/${encodeURIComponent(assetId)}/unavailability/${encodeURIComponent(unavailabilityId)}?${params.toString()}`,
    {
      method: "DELETE",
      headers: { Accept: "application/json", ...csrfHeaders() },
      credentials: "include",
    },
  );
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<{ planning_version: number; changed?: boolean }>;
}
