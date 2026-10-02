import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, ShiftReadModel } from "./api";
import {
  ShiftAssetCandidate,
  ShiftAssetCandidates,
  getShiftAssetCandidates,
  setShiftAssetAssignment,
} from "./assetApi";
import SearchableCombobox, { ComboboxOption } from "./SearchableCombobox";

export type ShiftAssetAssignmentMode = "assign" | "change" | "release";

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

function reasonLabel(reason: string | null | undefined) {
  switch (reason) {
    case "asset_inactive":
      return "Actif inactif.";
    case "operator_not_qualified":
      return "La ressource du quart n’est pas qualifiée pour cet actif.";
    case "asset_unavailable":
      return "Actif indisponible pour la date du quart.";
    case "permission_denied":
      return "Permission de planification requise.";
    case "shift_state_invalid":
      return "L’état du quart ne permet pas cette action.";
    case "asset_assignment_incomplete":
      return "L’affectation d’actif du quart est incomplète.";
    default:
      return reason || "Action non admissible.";
  }
}

function qualificationLabel(state: string | null | undefined) {
  switch (state) {
    case "SATISFIED":
      return "Conforme";
    case "SKILL_MISMATCH":
      return "Opérateur non qualifié";
    case "MISSING_OPERATOR":
      return "Opérateur manquant";
    case "NO_OVERLAP":
      return "Hors fenêtre";
    default:
      return state || "À vérifier";
  }
}

function actionTitle(mode: ShiftAssetAssignmentMode) {
  if (mode === "change") return "Changer l’actif";
  if (mode === "release") return "Libérer l’actif";
  return "Assigner un actif";
}

export default function ShiftAssetAssignmentDialog({
  shift,
  mode,
  planningVersion,
  onClose,
  onRefresh,
}: {
  shift: ShiftReadModel;
  mode: ShiftAssetAssignmentMode;
  planningVersion: number;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [candidateState, setCandidateState] = useState<ShiftAssetCandidates | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(
    mode === "change" ? shift.asset_assignment?.asset_id ?? null : null,
  );
  const [loadingCandidates, setLoadingCandidates] = useState(mode !== "release");
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [candidateReloadKey, setCandidateReloadKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{
    tone: "info" | "success" | "error";
    message: string;
  } | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  useEffect(() => {
    setSelectedAssetId(mode === "change" ? shift.asset_assignment?.asset_id ?? null : null);
    setFeedback(null);
    retryKeys.current.clear();
  }, [mode, shift.allocation_id, shift.asset_assignment?.asset_id]);

  useEffect(() => {
    if (mode === "release") {
      setCandidateState(null);
      setCandidateError(null);
      setLoadingCandidates(false);
      return;
    }

    const controller = new AbortController();
    setLoadingCandidates(true);
    setCandidateError(null);
    getShiftAssetCandidates(shift.allocation_id, controller.signal)
      .then((payload) => {
        setCandidateState(payload);
        setCandidateError(null);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setCandidateError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingCandidates(false);
      });
    return () => controller.abort();
  }, [candidateReloadKey, mode, shift.allocation_id]);

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [busy, onClose]);

  const selectedCandidate = useMemo(
    () => candidateState?.candidates.find((candidate) => candidate.id === selectedAssetId) ?? null,
    [candidateState, selectedAssetId],
  );

  const options = useMemo<ComboboxOption[]>(
    () => (candidateState?.candidates ?? []).map((candidate) => ({
      value: candidate.id,
      label: `${candidate.code} — ${candidate.label} · ${candidate.asset_type_label} · ${candidate.available ? "Disponible" : "Indisponible"} · ${qualificationLabel(candidate.qualification_state)}`,
      searchText: [
        candidate.code,
        candidate.label,
        candidate.asset_type_code,
        candidate.asset_type_label,
        reasonLabel(candidate.reason),
      ].join(" "),
      disabled: !candidate.allowed,
    })),
    [candidateState],
  );

  function keyFor(fingerprint: string) {
    const existing = retryKeys.current.get(fingerprint);
    if (existing) return existing;
    const key = mutationKey();
    retryKeys.current.set(fingerprint, key);
    return key;
  }

  async function save() {
    if (busy) return;
    if (mode !== "release" && (!selectedAssetId || !selectedCandidate?.allowed)) return;

    const expectedVersion = mode === "release"
      ? planningVersion
      : candidateState?.planning_version;
    if (!expectedVersion) return;

    const assetId = mode === "release" ? null : selectedAssetId;
    const requirementId = shift.asset_assignment?.requirement_id ?? null;
    const fingerprint = [
      "shift-asset",
      shift.allocation_id,
      mode,
      assetId || "release",
      requirementId || "auto",
      expectedVersion,
    ].join("|");

    setBusy(true);
    setFeedback(null);
    try {
      await setShiftAssetAssignment(
        shift.allocation_id,
        {
          asset_id: assetId,
          asset_requirement_id: requirementId,
          expected_planning_version: expectedVersion,
        },
        keyFor(fingerprint),
      );
      retryKeys.current.delete(fingerprint);
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
          message: "Le planning a changé. L’état canonique est resynchronisé; vérifie les candidats avant de réessayer.",
        });
        onRefresh();
        if (mode !== "release") setCandidateReloadKey((value) => value + 1);
      } else {
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(false);
    }
  }

  const current = shift.asset_assignment;
  const noChange = mode === "change" && selectedAssetId === current?.asset_id;
  const saveDisabled = busy
    || (mode !== "release" && (loadingCandidates || !selectedAssetId || !selectedCandidate?.allowed || noChange));

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
            <h2 id="shift-asset-title">{actionTitle(mode)}</h2>
            <p>Disponibilité, qualification et permissions sont déterminées par le backend.</p>
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
            <div><span>Actif actuel</span><strong>{current?.asset_code || "Aucun actif"}</strong></div>
          </div>

          {feedback && (
            <div
              className={feedback.tone === "error" ? "dialog-error" : "info-banner"}
              role={feedback.tone === "error" ? "alert" : "status"}
            >
              {feedback.message}
            </div>
          )}

          {mode === "release" ? (
            <div className="shift-asset-release-confirmation">
              <strong>{current ? `${current.asset_code} — ${current.asset_label}` : "Aucun actif associé"}</strong>
              <span>
                La libération passe par la mutation canonique. Le quart demeure selon la politique backend;
                React ne le remet pas automatiquement en mode AUTO.
              </span>
            </div>
          ) : (
            <>
              <SearchableCombobox
                value={selectedAssetId}
                options={options}
                onChange={(value) => {
                  setSelectedAssetId(value);
                  setFeedback(null);
                }}
                label="Actif"
                placeholder="Rechercher par code, libellé ou type…"
                loading={loadingCandidates}
                error={candidateError}
                emptyLabel="Aucun actif candidat"
                selectedOption={
                  current && selectedAssetId === current.asset_id
                    ? {
                        value: current.asset_id,
                        label: `${current.asset_code} — ${current.asset_label}`,
                        disabled: !current.asset_active,
                      }
                    : null
                }
                autoFocus
              />

              {selectedCandidate && (
                <div className="shift-asset-candidate-detail" data-testid="shift-asset-candidate-detail">
                  <div>
                    <span>Type</span>
                    <strong>{selectedCandidate.asset_type_code} — {selectedCandidate.asset_type_label}</strong>
                  </div>
                  <div>
                    <span>Disponibilité</span>
                    <strong>{selectedCandidate.available ? "Disponible" : "Indisponible"}</strong>
                  </div>
                  <div>
                    <span>Qualification</span>
                    <strong>{qualificationLabel(selectedCandidate.qualification_state)}</strong>
                  </div>
                  <div>
                    <span>Admissibilité</span>
                    <strong>{selectedCandidate.allowed ? "Admissible" : reasonLabel(selectedCandidate.reason)}</strong>
                  </div>
                </div>
              )}

              {selectedCandidate && !selectedCandidate.allowed && (
                <div className="info-banner" role="status">
                  {reasonLabel(selectedCandidate.reason)}
                </div>
              )}
            </>
          )}

          {current && (
            <div className="shift-asset-current-state">
              <strong>État persistant actuel</strong>
              <span>
                {current.asset_code} — {current.asset_label}
                {!current.asset_active ? " · Inactif" : ""}
              </span>
              <span>Qualification : {qualificationLabel(current.qualification_state)}</span>
            </div>
          )}

          <div className="dialog-actions">
            <button className="secondary-button" type="button" onClick={onClose} disabled={busy}>
              Annuler
            </button>
            <button
              className={mode === "release" ? "danger-button" : "primary-button"}
              type="button"
              onClick={() => void save()}
              disabled={saveDisabled || (mode === "release" && !current)}
            >
              {busy
                ? "Enregistrement…"
                : mode === "release"
                  ? "Libérer l’actif"
                  : mode === "change"
                    ? "Changer l’actif"
                    : "Assigner l’actif"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
