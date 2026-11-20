import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export type EmergencyBroadcastContent = {
  title: string;
  message: string;
};

export type EmergencyActiveBroadcast = {
  id: string;
  version: number;
  activated_at: string;
  acknowledged_at: string | null;
  content: EmergencyBroadcastContent | null;
};

export type EmergencyRuntimeStatus = {
  server_time_utc: string;
  observation_epoch: string;
  presence: {
    tracked: boolean;
    warming: boolean;
    complete: boolean;
  };
  active_broadcast: EmergencyActiveBroadcast | null;
};

export type EmergencyAcknowledgment = {
  broadcast_id: string;
  acknowledged_at: string;
};

export type EmergencyAcknowledgmentResponse = {
  status: "acknowledged" | "already_acknowledged";
  acknowledgment: EmergencyAcknowledgment;
};

export type EmergencyBroadcastAdminRecord = {
  id: string;
  version: number;
  title: string;
  message: string;
  created_at: string;
  activated_at: string;
  state: "ACTIVE" | "ENDED";
  ended_at: string | null;
};

export type EmergencyAdminUser = {
  user_id: string;
  display_name: string;
  last_seen_at: string | null;
  active: boolean;
  acknowledged_at: string | null;
  pending: boolean;
  presence_state: "active" | "recent_inactive" | "unknown";
};

export type EmergencyAdminStatus = {
  generated_at: string;
  observation_epoch: string;
  presence_warming: boolean;
  presence_complete: boolean;
  stale: boolean;
  error: string | null;
  tracked_user_count: number;
  active_user_count: number;
  active_pending_count: number;
  active_acknowledged_count: number;
  broadcast: EmergencyBroadcastAdminRecord | null;
  users: EmergencyAdminUser[];
  diagnostics: Record<string, unknown>;
};

export type EmergencyBroadcastHistoryPage = {
  items: EmergencyBroadcastAdminRecord[];
  next_cursor: number | null;
};

export type EmergencyAcknowledgmentHistoryItem = {
  user_id: string;
  display_name: string;
  acknowledged_at: string;
  late: boolean;
};

export type EmergencyAcknowledgmentHistoryPage = {
  items: EmergencyAcknowledgmentHistoryItem[];
  next_cursor: number | null;
};

async function apiError(response: Response): Promise<ApiError> {
  let message = `Erreur HTTP ${response.status}`;
  let code: string | null = null;
  try {
    const payload = await response.json() as {
      error?: { message?: string; code?: string };
    };
    message = payload.error?.message || message;
    code = payload.error?.code ?? null;
  } catch {
    // Stable fallback for proxy/non-JSON failures.
  }
  return new ApiError(message, response.status, code);
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.method && init.method !== "GET" ? csrfHeaders() : {}),
      ...(init?.headers ?? {}),
    },
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw await apiError(response);
  return response.json() as Promise<T>;
}

export async function getEmergencyRuntimeStatus(
  signal?: AbortSignal,
  known?: { id: string; version: number } | null,
): Promise<EmergencyRuntimeStatus> {
  const query = new URLSearchParams();
  if (known) {
    query.set("known_broadcast_id", known.id);
    query.set("known_broadcast_version", String(known.version));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return requestJson<EmergencyRuntimeStatus>(
    `/api/v1/runtime/status${suffix}`,
    { method: "GET", signal },
  );
}

export function acknowledgeEmergencyBroadcast(
  broadcastId: string,
  signal?: AbortSignal,
): Promise<EmergencyAcknowledgmentResponse> {
  return requestJson<EmergencyAcknowledgmentResponse>(
    `/api/v1/runtime/broadcasts/${encodeURIComponent(broadcastId)}/acknowledgment`,
    { method: "POST", body: JSON.stringify({}), signal },
  );
}

export function createEmergencyBroadcast(
  input: {
    creation_command_id: string;
    title: string;
    message: string;
    confirmed: true;
  },
): Promise<{ broadcast: EmergencyBroadcastAdminRecord }> {
  return requestJson<{ broadcast: EmergencyBroadcastAdminRecord }>("/api/v1/admin/runtime/broadcasts", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function endEmergencyBroadcast(
  broadcastId: string,
): Promise<{ broadcast: EmergencyBroadcastAdminRecord }> {
  return requestJson<{ broadcast: EmergencyBroadcastAdminRecord }>(
    `/api/v1/admin/runtime/broadcasts/${encodeURIComponent(broadcastId)}/end`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function getEmergencyAdminStatus(
  signal?: AbortSignal,
): Promise<EmergencyAdminStatus> {
  return requestJson<EmergencyAdminStatus>("/api/v1/admin/runtime/status", { method: "GET", signal });
}

export function getEmergencyBroadcastHistory(
  cursor = 0,
  limit = 25,
  signal?: AbortSignal,
): Promise<EmergencyBroadcastHistoryPage> {
  const query = new URLSearchParams({
    cursor: String(cursor),
    limit: String(limit),
  });
  return requestJson<EmergencyBroadcastHistoryPage>(
    `/api/v1/admin/runtime/broadcasts?${query.toString()}`,
    { method: "GET", signal },
  );
}

export function getEmergencyAcknowledgmentHistory(
  broadcastId: string,
  cursor = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<EmergencyAcknowledgmentHistoryPage> {
  const query = new URLSearchParams({
    cursor: String(cursor),
    limit: String(limit),
  });
  return requestJson<EmergencyAcknowledgmentHistoryPage>(
    `/api/v1/admin/runtime/broadcasts/${encodeURIComponent(broadcastId)}/acknowledgments?${query.toString()}`,
    { method: "GET", signal },
  );
}
