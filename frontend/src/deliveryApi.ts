import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";

export type DeliveryAction =
  | "MANAGE_PLAN_LIFECYCLE"
  | "ASSIGN_TEAM_LEAD"
  | "SET_PLAN_PRIORITY_DUE_DATE"
  | "MANAGE_STRUCTURE"
  | "ORDER_ITEMS"
  | "ASSIGN_STORIES"
  | "ESTIMATE_STORIES"
  | "MANAGE_STORY_STATUS_BLOCKING"
  | "MANAGE_SPRINT"
  | "UPDATE_OWN_STORY_STATUS"
  | "UPDATE_OWN_REMAINING_HOURS"
  | "DOCUMENT_OWN_BLOCKAGE";

export type DeliveryPlanStatus = "DRAFT" | "ACTIVE" | "ARCHIVED";
export type DeliveryItemType = "EPIC" | "STORY";
export type DeliveryItemStatus =
  | "BACKLOG"
  | "TODO"
  | "IN_PROGRESS"
  | "BLOCKED"
  | "DONE"
  | "CANCELLED";

export type DeliveryPlanReadModel = {
  id: string;
  work_package_id: string;
  status: DeliveryPlanStatus;
  lead_user_id: string | null;
  delivery_version: number;
  created_at: string | null;
  updated_at: string | null;
  activated_at: string | null;
  archived_at: string | null;
};

export type DeliveryItemReadModel = {
  id: string;
  delivery_plan_id: string;
  item_type: DeliveryItemType;
  title: string;
  parent_id: string | null;
  description: string | null;
  priority: string | null;
  status: DeliveryItemStatus;
  assignee_user_id: string | null;
  current_estimate_hours: number | null;
  reference_estimate_hours: number | null;
  remaining_hours: number | null;
  due_date: string | null;
  position: number;
  sprint: string | null;
  actions: DeliveryAction[];
};

export type DeliveryBoardReadModel = {
  plan: DeliveryPlanReadModel;
  actions: DeliveryAction[];
  items: DeliveryItemReadModel[];
};

export type DeliverySummary = {
  work_package: {
    id: string;
    reference: string;
    name: string;
    status: string;
    reference_hours: number | null;
  };
  delivery_plan: {
    id: string;
    status: DeliveryPlanStatus;
    delivery_version: number;
    lead_user_id: string | null;
  } | null;
  progress: {
    state: string;
    progress_ratio: number | null;
    coverage_ratio: number;
    completed_reference_hours: number;
    total_reference_hours: number;
    included_story_count: number;
    estimated_story_count: number;
    unestimated_story_ids: string[];
    cancelled_story_count: number;
  };
  forecast: {
    state: string;
    total_remaining_hours: number | null;
    coverage_ratio: number;
    open_story_count: number;
    covered_story_count: number;
    missing_remaining_story_ids: string[];
    cancelled_story_count: number;
  };
  planning_capacity: {
    provenance: "ACTIVE_APPROVED_PLAN";
    window_start: string | null;
    window_end: string | null;
    human_reserved_hours: number;
    total_reserved_hours: number;
    observed_planning_version: number;
    approved_sources: {
      demand_reference: string;
      approval_revision_id: string;
      approved_request_version: number;
      approved_entry_keys: string[];
    }[];
    asset_reserved_capacity: {
      asset_type_id: string;
      reserved_days: number;
    }[];
  };
  forecast_capacity_balance_hours: number | null;
  diagnostics: Array<Record<string, unknown> & { code: string }>;
};

export type DeliveryItemCreate = {
  expected_delivery_version: number;
  item_type: DeliveryItemType;
  title: string;
  parent_id?: string | null;
  description?: string | null;
  priority?: string | null;
  status?: DeliveryItemStatus;
  assignee_user_id?: string | null;
  current_estimate_hours?: number | null;
  remaining_hours?: number | null;
  due_date?: string | null;
  position?: number | null;
  sprint?: string | null;
};

export type DeliveryItemUpdate = {
  expected_delivery_version: number;
  title?: string;
  parent_id?: string | null;
  description?: string | null;
  priority?: string | null;
  status?: DeliveryItemStatus;
  assignee_user_id?: string | null;
  current_estimate_hours?: number | null;
  remaining_hours?: number | null;
  due_date?: string | null;
  position?: number;
  sprint?: string | null;
};

type ApiErrorPayload = {
  error?: { code?: string; message?: string };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function responseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload | null = null;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    // Keep the stable HTTP fallback for proxy/non-JSON errors.
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
  method: string,
  body: unknown,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...csrfHeaders(),
    },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export function getDeliveryBoardForWorkPackage(
  workPackageId: string,
  signal?: AbortSignal,
) {
  return getJson<DeliveryBoardReadModel | null>(
    `/api/v1/delivery/work-packages/${encodeURIComponent(workPackageId)}/plan`,
    signal,
  );
}

export function getDeliverySummary(
  workPackageId: string,
  signal?: AbortSignal,
) {
  return getJson<DeliverySummary>(
    `/api/v1/delivery/work-packages/${encodeURIComponent(workPackageId)}/summary`,
    signal,
  );
}

export function createDeliveryPlan(
  workPackageId: string,
  leadUserId: string | null = null,
) {
  return sendJson<DeliveryBoardReadModel>("/api/v1/delivery/plans", "POST", {
    work_package_id: workPackageId,
    lead_user_id: leadUserId,
  });
}

export function activateDeliveryPlan(
  planId: string,
  expectedDeliveryVersion: number,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/plans/${encodeURIComponent(planId)}/activate`,
    "POST",
    { expected_delivery_version: expectedDeliveryVersion },
  );
}

export function archiveDeliveryPlan(
  planId: string,
  expectedDeliveryVersion: number,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/plans/${encodeURIComponent(planId)}/archive`,
    "POST",
    { expected_delivery_version: expectedDeliveryVersion },
  );
}

export function setDeliveryLead(
  planId: string,
  leadUserId: string | null,
  expectedDeliveryVersion: number,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/plans/${encodeURIComponent(planId)}/lead`,
    "PATCH",
    {
      expected_delivery_version: expectedDeliveryVersion,
      lead_user_id: leadUserId,
    },
  );
}

export function createDeliveryItem(
  planId: string,
  payload: DeliveryItemCreate,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/plans/${encodeURIComponent(planId)}/items`,
    "POST",
    payload,
  );
}

export function updateDeliveryItem(
  itemId: string,
  payload: DeliveryItemUpdate,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/items/${encodeURIComponent(itemId)}`,
    "PATCH",
    payload,
  );
}

export function documentDeliveryBlockage(
  itemId: string,
  note: string,
  expectedDeliveryVersion: number,
) {
  return sendJson<DeliveryBoardReadModel>(
    `/api/v1/delivery/items/${encodeURIComponent(itemId)}/blockage-note`,
    "POST",
    {
      expected_delivery_version: expectedDeliveryVersion,
      note,
    },
  );
}
