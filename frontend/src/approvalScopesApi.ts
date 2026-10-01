import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";

export type ApprovalScopeReadModel = {
  id: string;
  code: string;
  label: string;
  active: boolean;
  version: number;
  approver_user_ids: string[];
  resource_class_codes: string[];
  task_catalog_item_ids: string[];
};

type ErrorPayload = { error?: { code?: string; message?: string } };
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

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
  if (!response.ok) {
    let payload: ErrorPayload | null = null;
    try {
      payload = (await response.json()) as ErrorPayload;
    } catch {
      // Stable fallback below.
    }
    throw new ApiError(
      payload?.error?.message || `Erreur HTTP ${response.status}`,
      response.status,
      payload?.error?.code ?? null,
    );
  }
  return response.json() as Promise<T>;
}

export function getApprovalScopes(signal?: AbortSignal) {
  return request<ApprovalScopeReadModel[]>("/api/v1/admin/approval-scopes", { signal });
}

export function createApprovalScope(code: string, label: string) {
  return request<ApprovalScopeReadModel>("/api/v1/admin/approval-scopes", {
    method: "POST",
    body: JSON.stringify({ code, label, active: true }),
  });
}

export function updateApprovalScope(
  scopeId: string,
  expectedVersion: number,
  changes: { label?: string; active?: boolean },
) {
  return request<ApprovalScopeReadModel>(
    `/api/v1/admin/approval-scopes/${encodeURIComponent(scopeId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
    },
  );
}

export function setApprovalScopeApprover(
  scopeId: string,
  userId: string,
  assigned: boolean,
  expectedVersion: number,
) {
  return request<ApprovalScopeReadModel>(
    `/api/v1/admin/approval-scopes/${encodeURIComponent(scopeId)}/approvers/${encodeURIComponent(userId)}`,
    {
      method: assigned ? "PUT" : "DELETE",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}


export function setApprovalScopeResourceClass(
  scopeId: string,
  classCode: string,
  assigned: boolean,
  expectedVersion: number,
) {
  return request<ApprovalScopeReadModel>(
    `/api/v1/admin/approval-scopes/${encodeURIComponent(scopeId)}/resource-classes/${encodeURIComponent(classCode)}`,
    {
      method: assigned ? "PUT" : "DELETE",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}

export function setApprovalScopeAssetType(
  scopeId: string,
  assetTypeId: string,
  assigned: boolean,
  expectedVersion: number,
) {
  return request<ApprovalScopeReadModel>(
    `/api/v1/admin/approval-scopes/${encodeURIComponent(scopeId)}/asset-types/${encodeURIComponent(assetTypeId)}`,
    {
      method: assigned ? "PUT" : "DELETE",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}
