import { ApiError } from "./api";
import { csrfHeaders } from "./csrf";

export type ResourceClassConfigReadModel = {
  code: string;
  label: string;
  average_hourly_cost_cad: string | null;
  active: boolean;
  version: number;
};

export type ResourceClassOptionReadModel = {
  code: string;
  label: string;
  active: boolean;
};

export type TaskClassStandardReadModel = {
  task_code: string;
  resource_class_code: string;
  active: boolean;
  version: number;
};

export type ProjectTaskClassOverrideReadModel = {
  project_id: string;
  task_code: string;
  resource_class_code: string | null;
  excluded: boolean;
  version: number;
};

export type TaskClassResolutionReadModel = {
  project_id: string;
  task_code: string;
  status: "CLASS" | "EXCLUDED" | "UNCLASSIFIED";
  resource_class_code: string | null;
  configured_resource_class_code: string | null;
  source: "PROJECT_OVERRIDE" | "STANDARD" | null;
  diagnostics: string[];
};

export type ProjectTaskClassRuleReadModel = {
  task_code: string;
  standard: TaskClassStandardReadModel | null;
  override: ProjectTaskClassOverrideReadModel | null;
  resolution: TaskClassResolutionReadModel;
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

export function getResourceClassOptions(signal?: AbortSignal) {
  return request<ResourceClassOptionReadModel[]>(
    "/api/v1/resource-classes",
    { signal },
  );
}

export function getResourceClasses(signal?: AbortSignal) {
  return request<ResourceClassConfigReadModel[]>(
    "/api/v1/admin/resource-classes",
    { signal },
  );
}

export function createResourceClass(payload: {
  code: string;
  label: string;
  average_hourly_cost_cad: string | null;
  active: boolean;
}) {
  return request<ResourceClassConfigReadModel>(
    "/api/v1/admin/resource-classes",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function updateResourceClass(
  code: string,
  expectedVersion: number,
  payload: {
    label?: string;
    average_hourly_cost_cad?: string | null;
    active?: boolean;
  },
) {
  return request<ResourceClassConfigReadModel>(
    `/api/v1/admin/resource-classes/${encodeURIComponent(code)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        expected_version: expectedVersion,
        ...payload,
      }),
    },
  );
}

export function getTaskClassStandards(signal?: AbortSignal) {
  return request<TaskClassStandardReadModel[]>(
    "/api/v1/admin/resource-classes/task-standards",
    { signal },
  );
}

export function createTaskClassStandard(payload: {
  task_code: string;
  resource_class_code: string;
  active: boolean;
}) {
  return request<TaskClassStandardReadModel>(
    "/api/v1/admin/resource-classes/task-standards",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function updateTaskClassStandard(
  taskCode: string,
  expectedVersion: number,
  payload: {
    resource_class_code?: string;
    active?: boolean;
  },
) {
  return request<TaskClassStandardReadModel>(
    `/api/v1/admin/resource-classes/task-standards/${encodeURIComponent(taskCode)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        expected_version: expectedVersion,
        ...payload,
      }),
    },
  );
}

export function getProjectTaskClassRules(
  projectId: string,
  signal?: AbortSignal,
) {
  return request<ProjectTaskClassRuleReadModel[]>(
    `/api/v1/admin/resource-classes/projects/${encodeURIComponent(projectId)}/task-rules`,
    { signal },
  );
}

export function setProjectTaskClassOverride(
  projectId: string,
  taskCode: string,
  payload: {
    expected_version?: number;
    resource_class_code?: string | null;
    excluded: boolean;
  },
) {
  return request<ProjectTaskClassOverrideReadModel>(
    `/api/v1/admin/resource-classes/projects/${encodeURIComponent(projectId)}/task-overrides/${encodeURIComponent(taskCode)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export function removeProjectTaskClassOverride(
  projectId: string,
  taskCode: string,
  expectedVersion: number,
) {
  return request<TaskClassResolutionReadModel>(
    `/api/v1/admin/resource-classes/projects/${encodeURIComponent(projectId)}/task-overrides/${encodeURIComponent(taskCode)}`,
    {
      method: "DELETE",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}
