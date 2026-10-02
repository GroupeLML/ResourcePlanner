import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  ShiftReadModel,
} from "./api";
import {
  ShiftAssetCandidate,
  ShiftAssetCandidates,
  getShiftAssetCandidates,
  setShiftAssetAssignment,
} from "./assetApi";
import SearchableCombobox, { ComboboxOption } from "./SearchableCombobox";

export type ShiftAssetDialogAction = "assign" | "change" | "release";

const STALE_CODES = new Set([
  "planning_version_conflict",
  "planning_version_stale",
]);

function mutationKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `shift-asset-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Action impossible.";
}

function isStale(reason: unknown) {
  return reason instanceof ApiError && Boolean(reason.code && STALE_CODES.has(reason.code));
}

function backendReasonLabel(reason: string | null | undefined) {
  const labels: Record<string, string> = {
    permission_denied: "Permission insuffisante pour cette action.",
    shift_state_invalid: "L’état actuel du quart ne permet pas cette action.",
    asset_assignment_missing: "Aucun actif ad hoc n’est actuellement associé au quart.",
    asset_assignment_attached: "Un actif ad hoc est déjà associé au quart.",
    asset_assignment_incomplete: "L’affectation d’actif du quart est incomplète.",
    asset_inactive: "Actif inactif.",
    operator_not_qualified: "La ressource du quart n’est pas qualifiée pour cet actif.",
    asset_unavailable: "Actif indisponible pour cette date.",
  };
  return reason ? labels[reason] ?? reason : null;
}

function qualificationLabel(state: string) {
  if (state === "SATISFIED") return "Qualifié";
  if (state === "SKILL_MISMATCH") return "Non qualifié";
  if (state === "MISSING_OPERATOR") return "Opérateur manquant";
  if (state === "NO_OVERLAP") return "Hors fenêtre";
  return state;
}

function candidateLabel(candidate: ShiftAssetCandidate) {
  const state = [
    candidate.available ? "Disponible" : "Indisponible",
    qualificationLabel(candidate.qualification_state),
    candidate.active ? null : "Inactif",
    !candidate.allowed ? backendReasonLabel(candidate.reason) : null,
  ].filter(Boolean).join(" · ");
  return `${candidate.code} — ${candidate.label} · ${candidate.asset_type_code} — ${candidate.asset_type_label} · ${state}`;
}

function dialogTitle(action: ShiftAssetDialogAction) {
  if (action === "change") return "Changer l’actif";
  if (action === "release") return "Libérer l’actif";
  return "Assigner un actif";
}

export default function ShiftAssetAssignmentDialog({
  shift,
  action,
  planningVersion,
  onClose,
  onRefresh,
}: {
  shift: ShiftReadModel;
  action: ShiftAssetDialogAction;
  planningVersion: number;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [candidateState, setCandidateState] = useState<ShiftAssetCandidates | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState(shift.asset_assignment?.asset_id ?? "");
  const [loading, setLoading] = useState(action !== "release");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{
    tone: "info" | "success" | "error";
    message: string;
  } | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  const projectedAction = shift.asset_actions?.[action] ?? null;
  const actionAllowed = projectedAction?.allowed === true;

  useEffect(() => {
    setLoadError(null);
    setSelectedAssetId(shift.asset_assignment?.asset_id ?? "");

    if (action === "release") {
      setCandidateState(null);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    getShiftAssetCandidates(shift.allocation_id, controller.signal)
      .then((state) => {
        if (controller.signal.aborted) return;
        setCandidateState(state);
        const current = state.candidates.find((candidate) => candidate.currently_assigned);
        setSelectedAssetId(current?.id ?? shift.asset_assignment?.asset_id ?? "");
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setCandidateState(null);
        setLoadError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [action, planningVersion, shift.allocation_id, shift.asset_assignment?.asset_id]);

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [busy, onClose]);

  const selectedCandidate = candidateState?.candidates.find(
    (candidate) => candidate.id === selectedAssetId,
  ) ?? null;

  const candidateOptions = useMemo<ComboboxOption[]>(
    () => (candidateState?.candidates ?? []).map((candidate) => ({
      value: candidate.id,
      label: candidateLabel(candidate),
      searchText: [
        candidate.code,
        candidate.label,
        candidate.asset_type_code,
        candidate.asset_type_label,
        backendReasonLabel(candidate.reason),
      ].filter(Boolean).join(" "),
      disabled: !candidate.allowed,
    })),
    [candidateState],
  );

  const currentHistoricalOption = useMemo<ComboboxOption | null>(() => {
    const assignment = shift.asset_assignment;
    if (!assignment) return null;
    return {
      value: assignment.asset_id,
      label: `${assignment.asset_code} — ${assignment.asset_label}${assignment.asset_active ? "" : " · Inactif"}`,
      disabled: !assignment.asset_active,
    };
  }, [shift.asset_assignment]);

  function keyFor(fingerprint: string) {
    const existing = retryKeys.current.get(fingerprint);
    if (existing) return existing;
    const key = mutationKey();
    retryKeys.current.set(fingerprint, key);
    return key;
  }

  async function save() {
    if (busy || !actionAllowed) return;

    const current = shift.asset_assignment;
    const targetAssetId = action === "release" ? null : selectedAssetId || null;
    if (action !== "release") {
      if (!candidateState || !targetAssetId || !selectedCandidate?.allowed) return;
      if (action === "change" && targetAssetId === current?.asset_id) return;
    }
    if (action === "release" && !current) return;

    const expectedVersion = action === "release"
      ? planningVersion
      : candidateState?.planning_version ?? planningVersion;
    const requirementId = current?.requirement_id
      ?? candidateState?.current_requirement_id
      ?? null;
    const fingerprint = [
      action,
      shift.allocation_id,
      targetAssetId ?? "release",
      requirementId ?? "auto",
      expectedVersion,
    ].join("|");
    const idempotencyKey = keyFor(fingerprint);

    setBusy(true);
    setFeedback(null);
    try {
      await setShiftAssetAssignment(
        shift.allocation_id,
        {
          asset_id: targetAssetId,
          asset_requirement_id: requirementId,
          expected_planning_version: expectedVersion,
        },
        idempotencyKey,
      );
      retryKeys.current.delete(fingerprint);
      setFeedback({
        tone: "success",
        message: action === "release"
          ? "Actif libéré."
          : action === "change"
            ? "Actif changé."
            : "Actif assigné.",
      });
      onRefresh();
      onClose();
    } catch (reason: unknown) {
      if (reason instanceof TypeError) {
        setFeedback({
          tone: "info",
          message: "La réponse réseau est incertaine. Réessaie : la même clé d’idempotence sera réutilisée.",
        });
        return;
      }

      retryKeys.current.delete(fingerprint);
      if (isStale(reason)) {
        setFeedback({
          tone: "info",
          message: "Le planning a changé. Le snapshot est rafraîchi; vérifie l’état du quart avant de réessayer.",
        });
        onRefresh();
      } else {
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(false);
    }
  }

  const noChange = action === "change"
    && Boolean(shift.asset_assignment)
    && selectedAssetId === shift.asset_assignment?.asset_id;
  const mutationDisabled = busy
    || !actionAllowed
    || (
      action === "release"
        ? !shift.asset_assignment
        : loading || Boolean(loadError) || !selectedAssetId || !selectedCandidate?.allowed || noChange
    );

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (!busy && event.currentTarget === event.target) onClose();
      }}
    >
      <section
        className="shift-dialog shift-asset-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shift-asset-title"
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Actif du quart</span>
            <h2 id="shift-asset-title">{dialogTitle(action)}</h2>
            <p>La disponibilité, la qualification et les permissions proviennent du backend.</p>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Fermer"
          >
            ×
          </button>
        </div>

        <div className="shift-asset-dialog-body">
          <div className="dialog-context-grid">
            <div><span>Projet / demande</span><strong>{shift.project_number || "—"} · {shift.demand_number || "—"}</strong></div>
            <div><span>Ressource</span><strong>{shift.resource_name}</strong></div>
            <div><span>Date du quart</span><strong>{shift.work_date}</strong></div>
            <div>
              <span>Actif actuel</span>
              <strong>{shift.asset_assignment?.asset_code || "Aucun actif"}</strong>
            </div>
          </div>

          {feedback && (
            <div
              className={feedback.tone === "error" ? "dialog-error" : "info-banner"}
              role={feedback.tone === "error" ? "alert" : "status"}
            >
              {feedback.message}
            </div>
          )}

          {!actionAllowed && (
            <div className="dialog-error" role="alert">
              {backendReasonLabel(projectedAction?.reason) || "Cette action n’est pas disponible."}
            </div>
          )}

          {action === "release" ? (
            <div className="shift-asset-release-summary">
              <strong>
                {shift.asset_assignment
                  ? `${shift.asset_assignment.asset_code} — ${shift.asset_assignment.asset_label}`
                  : "Aucun actif associé"}
              </strong>
              <span>
                La libération retire l’affectation ad hoc. Le backend conserve le quart selon la politique Planning existante.
              </span>
            </div>
          ) : (
            <>
              {loadError && <div className="dialog-error" role="alert">{loadError}</div>}
              <label className="shift-asset-combobox-field">
                <span>{action === "change" ? "Nouvel actif" : "Actif"}</span>
                <SearchableCombobox
                  value={selectedAssetId || null}
                  options={candidateOptions}
                  selectedOption={currentHistoricalOption}
                  onChange={(value) => {
                    setSelectedAssetId(value ?? "");
                    setFeedback(null);
                  }}
                  label={action === "change" ? "Nouvel actif" : "Actif"}
                  placeholder="Rechercher par code, libellé ou type…"
                  loading={loading}
                  error={loadError}
                  required
                  autoFocus
                />
              </label>

              {selectedCandidate && (
                <div className="shift-asset-candidate-summary" data-testid="shift-asset-candidate-summary">
                  <div><span>Code</span><strong>{selectedCandidate.code}</strong></div>
                  <div><span>Type</span><strong>{selectedCandidate.asset_type_code} — {selectedCandidate.asset_type_label}</strong></div>
                  <div><span>Disponibilité</span><strong>{selectedCandidate.available ? "Disponible" : "Indisponible"}</strong></div>
                  <div><span>Qualification</span><strong>{qualificationLabel(selectedCandidate.qualification_state)}</strong></div>
                  <div><span>Admissibilité</span><strong>{selectedCandidate.allowed ? "Admissible" : "Non admissible"}</strong></div>
                  {!selectedCandidate.allowed && (
                    <div className="span-2">
                      <span>Motif</span>
                      <strong>{backendReasonLabel(selectedCandidate.reason) || "Candidat refusé par le backend."}</strong>
                    </div>
                  )}
                </div>
              )}

              <div className="info-banner">
                Les candidats sont chargés uniquement pour ce quart à l’ouverture de ce parcours. React ne recalcule ni conflit, ni qualification, ni occupation hors scope.
              </div>
            </>
          )}

          {shift.related_asset_reservations.length > 0 && (
            <div className="shift-related-assets">
              <strong>Réservations liées à la demande</strong>
              {shift.related_asset_reservations.map((reservation) => (
                <span key={reservation.allocation_id}>
                  {reservation.asset_code} — {reservation.asset_label}
                </span>
              ))}
            </div>
          )}

          <div className="dialog-actions">
            <button className="secondary-button" type="button" onClick={onClose} disabled={busy}>
              Annuler
            </button>
            <button
              className={action === "release" ? "danger-button" : "primary-button"}
              type="button"
              onClick={() => void save()}
              disabled={mutationDisabled}
            >
              {busy
                ? "Enregistrement…"
                : action === "release"
                  ? "Libérer l’actif"
                  : action === "change"
                    ? "Changer l’actif"
                    : "Assigner l’actif"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
