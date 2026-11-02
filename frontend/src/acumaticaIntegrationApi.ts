import { createClientId } from "./clientId";
import { csrfHeaders } from "./csrf";

export type AcumaticaIntegrationStatus = {
  configured: boolean;
  source?: string;
  feed?: string;
  page_size?: number;
  timeout_seconds?: number;
};

export type ProjectSyncResult = {
  received: number;
  created: number;
  updated: number;
  unchanged: number;
};

export type EmployeeSyncResult = {
  received: number;
  created: number;
  updated: number;
  unchanged: number;
  errors: number;
};

type ErrorPayload = { error?: { code?: string; message?: string } };
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
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
    const detail = payload?.error?.code ? ` (${payload.error.code})` : "";
    throw new Error(payload?.error?.message
      ? `${payload.error.message}${detail}`
      : `Erreur HTTP ${response.status}${detail}`);
  }
  return response.json() as Promise<T>;
}

export function getAcumaticaIntegrationStatus(signal?: AbortSignal) {
  return request<AcumaticaIntegrationStatus>("/api/v1/integrations/acumatica", { signal });
}

type ErpProjectSyncRun = {
  run_id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "COMPLETED_WITH_ERRORS" | "FAILED" | "INTERRUPTED";
  project_received?: number;
  projects_created?: number;
  projects_updated?: number;
  projects_unchanged?: number;
  error_code?: string | null;
  diagnostic?: string | null;
};

const ERP_RUN_ACTIVE = new Set<ErpProjectSyncRun["status"]>(["PENDING", "RUNNING"]);

export async function syncAcumaticaProjects() {
  const accepted = await request<ErpProjectSyncRun>(
    "/api/v1/integrations/acumatica/erp-sync/projects/run-now",
    {
      method: "POST",
      headers: { "Idempotency-Key": createClientId() },
    },
  );
  let run = accepted;
  while (ERP_RUN_ACTIVE.has(run.status)) {
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    run = await request<ErpProjectSyncRun>(
      `/api/v1/integrations/acumatica/erp-sync/runs/${encodeURIComponent(run.run_id)}`,
    );
  }
  if (run.status !== "COMPLETED" && run.status !== "COMPLETED_WITH_ERRORS") {
    throw new Error(
      run.diagnostic
        ? `${run.diagnostic}${run.error_code ? ` (${run.error_code})` : ""}`
        : `La synchronisation des projets a échoué${run.error_code ? ` (${run.error_code})` : ""}.`,
    );
  }
  return {
    received: run.project_received ?? 0,
    created: run.projects_created ?? 0,
    updated: run.projects_updated ?? 0,
    unchanged: run.projects_unchanged ?? 0,
  } satisfies ProjectSyncResult;
}

export function syncAcumaticaEmployees() {
  return request<EmployeeSyncResult>("/api/v1/integrations/acumatica/employees/sync", {
    method: "POST",
  });
}
