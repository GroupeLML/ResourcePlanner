import { csrfHeaders } from "./csrf";

export type CommunicationParticipant = {
  contact_id: string | null;
  user_id: string | null;
  display_name: string;
  email: string | null;
  phone: string | null;
  active: boolean;
  diagnostics: string[];
};

export type ProjectMessageDiagnostic = {
  code: string;
  severity: "BLOCKING" | "WARNING";
  entity_type: string;
  entity_id: string;
  message: string;
};

export type ProjectCommunicationDraft = {
  message_key: string;
  audience: "project";
  project_id: string;
  project_number: string;
  to_recipient: CommunicationParticipant;
  to_recipients: CommunicationParticipant[];
  cc_recipients: CommunicationParticipant[];
  subject: string;
  body: string;
  message_kind: "project_confirmation" | "project_planning_change";
  week_start: string;
  content_fingerprint: string;
  approvable: boolean;
  diagnostics: ProjectMessageDiagnostic[];
};

export type ProjectCommunicationPreview = {
  week_start: string;
  mode: "project_confirmation" | "project_planning_change";
  snapshot_fingerprint: string;
  drafts: ProjectCommunicationDraft[];
  diagnostics: ProjectMessageDiagnostic[];
  has_communicated_baseline: boolean;
  baseline_fingerprint: string | null;
};

export type CommunicationDelivery = {
  id: string;
  message_id: string;
  provider: string;
  status: "PENDING" | "SENDING" | "SENT" | "FAILED";
  attempt_count: number;
  attempted_at: string | null;
  sent_at: string | null;
  provider_message_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  last_actor: string | null;
};

export type CommunicationMessage = {
  id: string;
  audience: string;
  recipient_id: string;
  recipient_email: string | null;
  subject: string;
  body: string;
  included: boolean;
  message_key: string | null;
  project_id: string | null;
  cc_emails: string[];
  to_emails: string[];
  content_fingerprint: string | null;
  approvable: boolean;
  diagnostics_json: string | null;
  deliveries: CommunicationDelivery[];
};

export type CommunicationBatch = {
  id: string;
  week_start: string;
  kind: string;
  snapshot_fingerprint: string;
  status: "PREPARED" | "APPROVED" | "CANCELLED" | "COMMUNICATED";
  prepared_by: string | null;
  prepared_at: string;
  approved_by: string | null;
  approved_at: string | null;
  communicated_by: string | null;
  communicated_at: string | null;
  cancelled_by: string | null;
  cancelled_at: string | null;
  drafts_provider: string | null;
  drafts_created_count: number;
  drafts_created_by: string | null;
  drafts_created_at: string | null;
  messages: CommunicationMessage[];
  stale: boolean;
  model_version: string;
};

export type ProjectCommunicationReview = {
  message_key: string;
  include: boolean;
  subject: string;
  body: string;
};

export type CommunicationDraftDownload = {
  blob: Blob;
  filename: string;
};

type ApiErrorPayload = { error?: { message?: string } };
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
    let payload: ApiErrorPayload | null = null;
    try {
      payload = (await response.json()) as ApiErrorPayload;
    } catch {
      // Stable fallback for non-JSON server/proxy errors.
    }
    throw new Error(payload?.error?.message || `Erreur HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function getProjectCommunicationPreview(weekStart: string) {
  return request<ProjectCommunicationPreview>(
    `/api/v1/communications/project-preview?week_start=${encodeURIComponent(weekStart)}`,
  );
}

export function prepareProjectCommunicationBatch(
  weekStart: string,
  expectedFingerprint: string,
  reviews: ProjectCommunicationReview[],
) {
  return request<CommunicationBatch>("/api/v1/communications/project-batches", {
    method: "POST",
    body: JSON.stringify({
      week_start: weekStart,
      expected_fingerprint: expectedFingerprint,
      reviews,
    }),
  });
}

export function listProjectCommunicationBatches(weekStart: string) {
  return request<CommunicationBatch[]>(
    `/api/v1/communications/project-batches?week_start=${encodeURIComponent(weekStart)}`,
  );
}

export function projectCommunicationBatchAction(
  batchId: string,
  action: "approve" | "create-drafts" | "send-smtp" | "cancel" | "mark-communicated",
) {
  return request<CommunicationBatch>(
    `/api/v1/communications/project-batches/${encodeURIComponent(batchId)}/${action}`,
    { method: "POST" },
  );
}

export async function downloadProjectCommunicationDrafts(
  batchId: string,
): Promise<CommunicationDraftDownload> {
  const response = await fetch(
    `${API_BASE}/api/v1/communications/project-batches/${encodeURIComponent(batchId)}/draft-download`,
    {
      headers: {
        Accept: "message/rfc822, application/zip",
        ...csrfHeaders(),
      },
      credentials: "include",
    },
  );
  if (!response.ok) {
    let payload: ApiErrorPayload | null = null;
    try {
      payload = (await response.json()) as ApiErrorPayload;
    } catch {
      // Stable fallback for non-JSON server/proxy errors.
    }
    throw new Error(payload?.error?.message || `Erreur HTTP ${response.status}`);
  }

  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filenameMatch = /filename="?([^";]+)"?/i.exec(disposition);
  const contentType = response.headers.get("Content-Type") ?? "";
  const fallback = contentType.includes("message/rfc822")
    ? `brouillon-m365-${batchId}.eml`
    : `brouillons-m365-${batchId}.zip`;
  return {
    blob: await response.blob(),
    filename: filenameMatch?.[1] || fallback,
  };
}
