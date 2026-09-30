import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";
import type { OidcState } from "./identityAdmin";

export type UserAdminReadModel = {
  user_id: string;
  app_user_id: string;
  issuer: string | null;
  subject: string | null;
  display_name: string;
  email: string | null;
  employee_external_id: string | null;
  erp_user_id: string | null;
  business_contact_id: string | null;
  phone: string | null;
  roles: string[];
  active: boolean;
  oidc_state: OidcState;
};

export type UserRoleDefinition = {
  role: string;
  label: string;
  permissions: string[];
};

export type UserAdminCreate = {
  issuer: string;
  subject: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  roles: string[];
  active: boolean;
};

export type UserAdminUpdate = {
  display_name: string;
  email: string | null;
  phone: string | null;
  roles: string[];
  active: boolean;
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

export function getAdminUsers(signal?: AbortSignal) {
  return request<UserAdminReadModel[]>("/api/v1/admin/users", { signal });
}

export function getAdminRoleCatalog(signal?: AbortSignal) {
  return request<UserRoleDefinition[]>("/api/v1/admin/users/roles", { signal });
}

export function createAdminUser(payload: UserAdminCreate) {
  return request<UserAdminReadModel>("/api/v1/admin/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAdminUser(userId: string, payload: UserAdminUpdate) {
  return request<UserAdminReadModel>(`/api/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
