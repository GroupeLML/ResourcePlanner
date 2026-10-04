import { useEffect, useRef, useState } from "react";

import { useAuth } from "./AuthContext";
import { ContactSelect } from "./BusinessContactUi";
import { createClientId } from "./clientId";
import {
  ApiError,
  BusinessContactReadModel,
  ContactResolutionReadModel,
  getAllocationOperationalResponsibility,
  getBusinessContacts,
  getProjectOperationalResponsibility,
  getSegmentOperationalResponsibility,
  setAllocationOperationalResponsible,
  setProjectOperationalResponsible,
  setSegmentOperationalResponsible,
} from "./api";

type ResponsibilityTarget =
  | { kind: "project"; reference: string }
  | { kind: "segment"; reference: string; expectedPlanningVersion: number }
  | {
      kind: "allocation";
      reference: string;
      expectedPlanningVersion: number;
      source: string;
      locked: boolean;
    };

function errorMessage(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error
    ? reason.message
    : "Impossible de mettre à jour le responsable opérationnel.";
}

function sourceLabel(sourceType: string | null | undefined) {
  switch ((sourceType ?? "").toUpperCase()) {
    case "SHIFT_OVERRIDE":
      return "Override du quart";
    case "RESOURCE_REQUIREMENT_OVERRIDE":
      return "Override du besoin";
    case "REQUEST_OVERRIDE":
      return "Override de la demande";
    case "TASK_RESPONSIBLE":
      return "Responsable de la tâche";
    case "PROJECT_OVERRIDE":
      return "Override du projet";
    case "PROJECT_MANAGER":
      return "Chargé principal ERP";
    case "NONE":
      return "Non résolu";
    default:
      return sourceType || "Non résolu";
  }
}

function ownOverrideSource(kind: ResponsibilityTarget["kind"]) {
  if (kind === "project") return "PROJECT_OVERRIDE";
  if (kind === "segment") return "RESOURCE_REQUIREMENT_OVERRIDE";
  return "SHIFT_OVERRIDE";
}

export default function OperationalResponsibilityControl({
  target,
  onSaved,
}: {
  target: ResponsibilityTarget;
  onSaved?: () => void;
}) {
  const { can } = useAuth();
  const canManage = target.kind === "project"
    ? can("manage_resources")
    : can("manage_planning");
  const [contacts, setContacts] = useState<BusinessContactReadModel[]>([]);
  const [resolution, setResolution] = useState<ContactResolutionReadModel | null>(null);
  const [overrideContactId, setOverrideContactId] = useState<string | null>(null);
  const [projectVersion, setProjectVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const intentKeys = useRef(new Map<string, string>());

  const targetKey = target.kind === "project"
    ? `project:${target.reference}`
    : target.kind === "segment"
      ? `segment:${target.reference}:${target.expectedPlanningVersion}`
      : `allocation:${target.reference}:${target.expectedPlanningVersion}:${target.source}:${target.locked}`;

  async function reload(signal?: AbortSignal) {
    const [contactRows, responsibility] = await Promise.all([
      getBusinessContacts(true, signal),
      target.kind === "project"
        ? getProjectOperationalResponsibility(target.reference, signal)
        : target.kind === "segment"
          ? getSegmentOperationalResponsibility(target.reference, signal)
          : getAllocationOperationalResponsibility(target.reference, signal),
    ]);
    setContacts(contactRows);
    setResolution(responsibility.operational_responsible);
    if (target.kind === "project") {
      setOverrideContactId(responsibility.override_contact_id);
      setProjectVersion(responsibility.override_version);
    } else {
      const ownSource = ownOverrideSource(target.kind);
      setOverrideContactId(
        responsibility.operational_responsible.source_type === ownSource
          ? responsibility.operational_responsible.contact_id
          : null,
      );
      setProjectVersion(null);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setMessage(null);
    void reload(controller.signal)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [targetKey]);

  function intentKey(contactId: string | null) {
    const version = target.kind === "project"
      ? projectVersion
      : target.expectedPlanningVersion;
    return `${targetKey}:${contactId ?? "inherit"}:${version ?? "unknown"}`;
  }

  async function change(contactId: string | null) {
    if (!canManage || saving || loading || contactId === overrideContactId) return;
    if (
      target.kind === "allocation"
      && contactId
      && target.source.toUpperCase() === "AUTO"
      && !target.locked
      && !window.confirm(
        "Choisir un responsable explicite transforme ce quart AUTO en décision MANUAL verrouillée. Continuer?",
      )
    ) {
      return;
    }

    const intent = intentKey(contactId);
    const existingKey = intentKeys.current.get(intent);
    const idempotencyKey = existingKey ?? createClientId();
    if (!existingKey) intentKeys.current.set(intent, idempotencyKey);

    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const result = target.kind === "project"
        ? await setProjectOperationalResponsible(
            target.reference,
            contactId,
            projectVersion ?? 0,
            idempotencyKey,
          )
        : target.kind === "segment"
          ? await setSegmentOperationalResponsible(
              target.reference,
              contactId,
              target.expectedPlanningVersion,
              idempotencyKey,
            )
          : await setAllocationOperationalResponsible(
              target.reference,
              contactId,
              target.expectedPlanningVersion,
              idempotencyKey,
            );
      intentKeys.current.delete(intent);
      setOverrideContactId(result.override_contact_id);
      if (target.kind === "project" && result.project_override_version != null) {
        setProjectVersion(result.project_override_version);
      }
      setMessage(
        result.auto_source_converted
          ? "Responsable enregistré. Le quart est maintenant MANUAL et verrouillé."
          : contactId
            ? "Override du responsable opérationnel enregistré."
            : "Override retiré; l’héritage canonique s’applique de nouveau.",
      );
      await reload();
      onSaved?.();
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        intentKeys.current.delete(intent);
      }
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="segment-help operational-responsibility-control" data-testid={`operational-responsibility-${target.kind}`}>
      <strong>Responsable opérationnel effectif</strong>
      {loading ? (
        <span>Chargement de la résolution…</span>
      ) : resolution ? (
        <>
          <span>
            {resolution.display_name || "Non résolu"} · source : {sourceLabel(resolution.source_type)}
          </span>
          {resolution.diagnostics.length > 0 && (
            <small>{resolution.diagnostics.join(" · ")}</small>
          )}
          <ContactSelect
            contacts={contacts}
            value={overrideContactId}
            onChange={(value) => void change(value)}
            disabled={!canManage || saving}
            inheritLabel="Aucun override — utiliser l’héritage canonique"
          />
          {target.kind === "allocation" && target.source.toUpperCase() === "AUTO" && !target.locked && (
            <small>
              Un override explicite sur ce quart convertit AUTO → MANUAL et verrouille le quart.
            </small>
          )}
          {message && <small role="status">{message}</small>}
        </>
      ) : null}
      {error && <small className="dialog-error" role="alert">{error}</small>}
    </div>
  );
}
