import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";
import type { OidcState } from "./identityAdmin";

export type ErpUserDirectoryReadModel = {
  user_id: string;
  erp_user_id: string;
  employee_external_id: string;
  display_name: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  erp_user_active: boolean;
  employee_status: string | null;
  source_admissible: boolean;
  local_active: boolean;
  active: boolean;
  roles: string[];
  resource_id: string | null;
  resource_name: string | null;
  resource_erp_active: boolean | null;
  app_user_id: string | null;
  issuer: string | null;
  subject: string | null;
  oidc_state: OidcState;
  access_ready: boolean;
};

type ErrorPayload = {
  error?: { code?: string; message?: string };
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

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

export function getErpUsers(signal?: AbortSignal) {
  return request<ErpUserDirectoryReadModel[]>("/api/v1/admin/erp-users", { signal });
}

export function updateErpUser(
  userId: string,
  payload: { active: boolean; roles: string[] },
) {
  return request<ErpUserDirectoryReadModel[] | ErpUserDirectoryReadModel>(
    `/api/v1/admin/erp-users/${encodeURIComponent(userId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  ) as Promise<ErpUserDirectoryReadModel>;
}

export function syncErpUsers() {
  return request<{
    received: number;
    created: number;
    updated: number;
    unchanged: number;
    errors: number;
  }>("/api/v1/integrations/acumatica/users/sync", { method: "POST" });
}
