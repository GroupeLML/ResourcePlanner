import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  AssetRequirementPlanningReadModel,
  PlanningSnapshotReadModel,
  ShiftReadModel,
} from "./api";
import {
  getAssetOperatorCandidates,
  reserveAssetRequirement,
  setAssetRequirementOperator,
} from "./assetApi";

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

export default function ShiftAssetAssignmentDialog({
  shift,
  snapshot,
  onClose,
  onRefresh,
}: {
  shift: ShiftReadModel;
  snapshot: PlanningSnapshotReadModel;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const requirements = useMemo(
    () => snapshot.asset_requirements.filter((requirement) => (
      requirement.demand_number === shift.demand_number
      && requirement.start_date <= shift.work_date
      && requirement.end_date >= shift.work_date
    )),
    [snapshot.asset_requirements, shift.demand_number, shift.work_date],
  );
  const [requirementId, setRequirementId] = useState(requirements[0]?.requirement_id ?? "");
  const requirement = requirements.find((row) => row.requirement_id === requirementId)
    ?? requirements[0]
    ?? null;
  const [selectedAssetId, setSelectedAssetId] = useState(requirement?.asset_id ?? "");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{
    tone: "info" | "success" | "error";
    message: string;
  } | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  useEffect(() => {
    if (!requirements.some((row) => row.requirement_id === requirementId)) {
      setRequirementId(requirements[0]?.requirement_id ?? "");
    }
  }, [requirements, requirementId]);

  useEffect(() => {
    setSelectedAssetId(requirement?.asset_id ?? "");
    setFeedback(null);
  }, [requirement?.requirement_id, requirement?.asset_id]);

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [busy, onClose]);

  const compatibleAssets = useMemo(() => {
    if (!requirement) return [];
    return snapshot.assets.filter((asset) => (
      asset.asset_type_id === requirement.asset_type_id
      && (asset.active || asset.id === requirement.asset_id)
    ));
  }, [snapshot.assets, requirement]);

  const selectedAsset = compatibleAssets.find((asset) => asset.id === selectedAssetId) ?? null;
  const diagnostics = useMemo(() => {
    if (!requirement) return [];
    return snapshot.asset_diagnostics.filter((diagnostic) => (
      diagnostic.requirement_id === requirement.requirement_id
      && (!selectedAssetId || !diagnostic.asset_id || diagnostic.asset_id === selectedAssetId)
    ));
  }, [snapshot.asset_diagnostics, requirement, selectedAssetId]);

  const selectedCapacity = useMemo(() => {
    if (!requirement || !selectedAssetId) return [];
    return snapshot.asset_capacity.filter((row) => (
      row.asset_id === selectedAssetId
      && row.day >= requirement.start_date
      && row.day <= requirement.end_date
    ));
  }, [snapshot.asset_capacity, requirement, selectedAssetId]);

  const alreadyComplete = Boolean(
    requirement
    && selectedAssetId
    && selectedAssetId === requirement.asset_id
    && (
      requirement.required_competency_ids.length === 0
      || (
        requirement.operator_resource_id === shift.resource_id
        && requirement.qualification_state === "SATISFIED"
      )
    ),
  );
  const noChange = Boolean(
    requirement
    && selectedAssetId === (requirement.asset_id ?? "")
    && (
      selectedAssetId === ""
      || alreadyComplete
    ),
  );

  function keyFor(fingerprint: string) {
    const existing = retryKeys.current.get(fingerprint);
    if (existing) return existing;
    const key = mutationKey();
    retryKeys.current.set(fingerprint, key);
    return key;
  }

  async function save() {
    if (!requirement || busy || noChange) return;
    setBusy(true);
    setFeedback(null);

    const reserveFingerprint = [
      "reserve",
      requirement.requirement_id,
      selectedAssetId || "release",
      requirement.start_date,
      requirement.end_date,
      snapshot.planning_version,
    ].join("|");
    let reservationKnownSuccessful = selectedAssetId === (requirement.asset_id ?? "");

    try {
      if (!reservationKnownSuccessful) {
        await reserveAssetRequirement(
          requirement.requirement_id,
          {
            asset_id: selectedAssetId || null,
            start_date: selectedAssetId ? requirement.start_date : null,
            end_date: selectedAssetId ? requirement.end_date : null,
            expected_planning_version: snapshot.planning_version,
          },
          keyFor(reserveFingerprint),
        );
        reservationKnownSuccessful = true;
      }

      if (!selectedAssetId) {
        retryKeys.current.delete(reserveFingerprint);
        setFeedback({ tone: "success", message: "Réservation libérée." });
        onRefresh();
        onClose();
        return;
      }

      if (requirement.required_competency_ids.length === 0) {
        retryKeys.current.delete(reserveFingerprint);
        setFeedback({ tone: "success", message: `${selectedAsset?.label || selectedAsset?.code || "Actif"} réservé.` });
        onRefresh();
        onClose();
        return;
      }

      const operatorState = await getAssetOperatorCandidates(requirement.requirement_id);
      const shiftCandidate = operatorState.candidates.find(
        (candidate) => candidate.resource_id === shift.resource_id,
      );
      if (!shiftCandidate) {
        retryKeys.current.delete(reserveFingerprint);
        setFeedback({
          tone: "error",
          message: `L’unité est réservée, mais ${shift.resource_name} n’est pas admissible comme opérateur. Affectation incomplète : vérifie la qualification avant de poursuivre.`,
        });
        onRefresh();
        return;
      }

      if (
        operatorState.operator_resource_id === shift.resource_id
        && operatorState.qualification_state === "SATISFIED"
      ) {
        retryKeys.current.delete(reserveFingerprint);
        setFeedback({
          tone: "success",
          message: `${selectedAsset?.label || selectedAsset?.code || "Actif"} est déjà correctement associé à ${shift.resource_name}.`,
        });
        onRefresh();
        onClose();
        return;
      }

      const operatorFingerprint = [
        "operator",
        requirement.requirement_id,
        shift.resource_id,
        operatorState.planning_version,
      ].join("|");
      try {
        await setAssetRequirementOperator(
          requirement.requirement_id,
          {
            operator_resource_id: shift.resource_id,
            expected_planning_version: operatorState.planning_version,
          },
          keyFor(operatorFingerprint),
        );
        retryKeys.current.delete(operatorFingerprint);
        retryKeys.current.delete(reserveFingerprint);
        setFeedback({
          tone: "success",
          message: `${selectedAsset?.label || selectedAsset?.code || "Actif"} réservé avec ${shift.resource_name} comme opérateur.`,
        });
        onRefresh();
        onClose();
      } catch (reason: unknown) {
        if (reason instanceof TypeError) {
          setFeedback({
            tone: "info",
            message: "La réservation est connue, mais la réponse d’association opérateur est incertaine. Réessaie : la même clé d’idempotence sera réutilisée.",
          });
          return;
        }
        retryKeys.current.delete(operatorFingerprint);
        retryKeys.current.delete(reserveFingerprint);
        if (isStale(reason)) {
          setFeedback({
            tone: "info",
            message: "Le planning a changé pendant l’association de l’opérateur. Le snapshot est rafraîchi; vérifie la réservation avant de reprendre.",
          });
        } else {
          setFeedback({
            tone: "error",
            message: `L’unité est réservée, mais l’opérateur n’a pas pu être associé : ${messageFromError(reason)}`,
          });
        }
        onRefresh();
      }
    } catch (reason: unknown) {
      if (reason instanceof TypeError) {
        setFeedback({
          tone: "info",
          message: "La réponse de réservation est incertaine. Réessaie : la même clé d’idempotence sera réutilisée.",
        });
        return;
      }
      retryKeys.current.delete(reserveFingerprint);
      if (isStale(reason)) {
        setFeedback({
          tone: "info",
          message: "Le planning a changé. Le snapshot est rafraîchi; vérifie la disponibilité puis reprends l’affectation.",
        });
        onRefresh();
      } else {
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(false);
    }
  }

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
            <span className="eyebrow">Raccourci Planning</span>
            <h2 id="shift-asset-title">Assigner un actif</h2>
            <p>Réutilise le besoin d’actif approuvé et sa fenêtre canonique.</p>
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

        <div className="dialog-context-grid">
          <div><span>Projet / demande</span><strong>{shift.project_number || "—"} · {shift.demand_number || "—"}</strong></div>
          <div><span>Ressource</span><strong>{shift.resource_name}</strong></div>
          <div><span>Date du quart</span><strong>{shift.work_date}</strong></div>
        </div>

        {feedback && (
          <div
            className={feedback.tone === "error" ? "dialog-error" : "info-banner"}
            role={feedback.tone === "error" ? "alert" : "status"}
          >
            {feedback.message}
          </div>
        )}

        {requirements.length === 0 || !requirement ? (
          <div className="info-banner">
            Aucun besoin d’actif de la même demande ne couvre la date de ce quart.
          </div>
        ) : (
          <>
            <div className="dialog-form-grid">
              <label className="span-2">
                <span>Besoin actif</span>
                <select
                  value={requirement.requirement_id}
                  onChange={(event) => setRequirementId(event.target.value)}
                  disabled={busy}
                  aria-label="Besoin actif"
                >
                  {requirements.map((row) => (
                    <option value={row.requirement_id} key={row.requirement_id}>
                      {row.asset_type_code} — {row.asset_type_label} · {row.start_date} → {row.end_date} · {row.asset_id ? "Réservé" : "À réserver"}
                    </option>
                  ))}
                </select>
              </label>

              <label className="span-2">
                <span>Unité</span>
                <select
                  value={selectedAssetId}
                  onChange={(event) => {
                    setSelectedAssetId(event.target.value);
                    setFeedback(null);
                  }}
                  disabled={busy}
                  aria-label="Unité"
                >
                  <option value="">Aucune réservation</option>
                  {compatibleAssets.map((asset) => (
                    <option value={asset.id} key={asset.id}>
                      {asset.code} — {asset.label}{asset.active ? "" : " — inactive"}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="shift-asset-summary">
              <div>
                <span>Fenêtre du besoin</span>
                <strong>{requirement.start_date} → {requirement.end_date}</strong>
              </div>
              <div>
                <span>État</span>
                <strong>{requirement.asset_id ? "Réservé" : "À réserver"}</strong>
              </div>
              <div>
                <span>Unité actuelle</span>
                <strong>{requirement.asset_id ? `${requirement.asset_code} — ${requirement.asset_label}` : "—"}</strong>
              </div>
              <div>
                <span>Qualification</span>
                <strong>
                  {requirement.required_competency_names.length > 0
                    ? requirement.required_competency_names.join(", ")
                    : "Aucune requise"}
                </strong>
              </div>
              <div>
                <span>Opérateur actuel</span>
                <strong>{requirement.operator_resource_name || "—"}</strong>
              </div>
              <div>
                <span>Opérateur proposé</span>
                <strong>{shift.resource_name}</strong>
              </div>
            </div>

            {selectedCapacity.length > 0 && (
              <div className="info-banner">
                État backend dans la fenêtre affichée : {selectedCapacity.every((row) => row.available)
                  ? "unité disponible selon le snapshot"
                  : "au moins une journée est occupée ou indisponible"}. La mutation backend reste autoritaire.
              </div>
            )}

            {diagnostics.length > 0 && (
              <div className="shift-asset-diagnostics" role="status">
                <strong>Diagnostics Planning</strong>
                {diagnostics.map((diagnostic, index) => (
                  <span key={`${diagnostic.code}-${index}`}>
                    {diagnostic.message} ({diagnostic.code})
                  </span>
                ))}
              </div>
            )}

            {requirement.required_competency_names.length > 0 && (
              <div className="info-banner">
                La ressource du quart est proposée comme opérateur. Le backend vérifie son admissibilité avant l’association; un échec de qualification reste bloquant.
              </div>
            )}

            {alreadyComplete && (
              <div className="info-banner">
                Cet actif est déjà correctement associé à ce quart et à son opérateur.
              </div>
            )}
          </>
        )}

        <div className="dialog-actions">
          <button className="secondary-button" type="button" onClick={onClose} disabled={busy}>
            Fermer
          </button>
          {requirement && (
            <button
              className="primary-button"
              type="button"
              onClick={() => void save()}
              disabled={busy || noChange}
            >
              {busy
                ? "Enregistrement…"
                : selectedAssetId
                  ? requirement.asset_id === selectedAssetId ? "Valider l’opérateur" : "Réserver et associer"
                  : "Libérer l’actif"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
