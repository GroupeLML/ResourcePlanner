import type { ViewScope } from "./api";

export type DemandHistoryReadModel = {
  demand_number: string;
  action: string;
  occurred_at: string;
  previous_status: string | null;
  status: string | null;
  comment: string | null;
  details: string | null;
  actor_user_id: string | null;
  actor_name: string | null;
};

type ApiErrorPayload = {
  error?: {
    message?: string;
  };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export async function getDemandHistory(
  number: string,
  signal?: AbortSignal,
  scope?: ViewScope,
) {
  const params = new URLSearchParams();
  if (scope) params.set("scope", scope);
  const query = params.toString();
  const response = await fetch(
    `${API_BASE}/api/v1/demands/${encodeURIComponent(number)}/history${query ? `?${query}` : ""}`,
    {
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) {
    let payload: ApiErrorPayload | null = null;
    try {
      payload = (await response.json()) as ApiErrorPayload;
    } catch {
      // Keep the stable fallback below for non-JSON proxy/server errors.
    }
    throw new Error(payload?.error?.message || `Erreur HTTP ${response.status}`);
  }
  return response.json() as Promise<DemandHistoryReadModel[]>;
}
