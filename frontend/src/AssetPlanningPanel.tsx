import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  AssetPlanningDiagnosticReadModel,
  AssetRequirementPlanningReadModel,
  PlanningSnapshotReadModel,
} from "./api";
import {
  AssetOperatorCandidate,
  addAssetUnavailability,
  getAssetOperatorCandidates,
  removeAssetUnavailability,
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
  return `asset-plan-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Action impossible.";
}

function diagnosticTone(code: string) {
  if (code.includes("UNASSIGNED") || code.includes("NOT_SELECTED")) return "warning";
  return "danger";
}

const QUALIFICATION_LABELS: Record<string, string> = {
  SATISFIED: "Qualification satisfaite",
  MISSING_OPERATOR: "Qualification manquante",
  SKILL_MISMATCH: "Compétence non conforme",
  NO_OVERLAP: "Aucun chevauchement",
};

function AssetRequirementCard({
  requirement,
  snapshot,
  canManage,
  busy,
  onReserve,
  onOperator,
}: {
  requirement: AssetRequirementPlanningReadModel;
  snapshot: PlanningSnapshotReadModel;
  canManage: boolean;
  busy: boolean;
  onReserve: (requirement: AssetRequirementPlanningReadModel, assetId: string | null) => void;
  onOperator: (requirement: AssetRequirementPlanningReadModel, resourceId: string | null) => void;
}) {
  const compatible = snapshot.assets.filter((asset) => (
    asset.asset_type_id === requirement.asset_type_id
    && (asset.active || asset.id === requirement.asset_id)
  ));
  const [selected, setSelected] = useState(requirement.asset_id ?? "");
  const [operatorSelected, setOperatorSelected] = useState(requirement.operator_resource_id ?? "");
  const [operatorCandidates, setOperatorCandidates] = useState<AssetOperatorCandidate[]>([]);
  const [candidateError, setCandidateError] = useState("");

  useEffect(() => {
    setOperatorSelected(requirement.operator_resource_id ?? "");
    if (!requirement.asset_id || requirement.required_competency_ids.length === 0) {
      setOperatorCandidates([]);
      setCandidateError("");
      return undefined;
    }
    const controller = new AbortController();
    setCandidateError("");
    void getAssetOperatorCandidates(requirement.requirement_id, controller.signal)
      .then((payload) => setOperatorCandidates(payload.candidates))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setCandidateError(messageFromError(reason));
      });
    return () => controller.abort();
  }, [
    requirement.requirement_id,
    requirement.asset_id,
    requirement.operator_resource_id,
    requirement.qualification_state,
    requirement.required_competency_ids,
  ]);

  return (
    <article className="asset-requirement-card" data-requirement-id={requirement.requirement_id}>
      <div className="asset-requirement-heading">
        <div>
          <span>{requirement.demand_number} · {requirement.project_number}</span>
          <strong>{requirement.asset_type_code} — {requirement.asset_type_label}</strong>
          <small>{requirement.start_date} → {requirement.end_date}</small>
        </div>
        <span className={requirement.asset_id ? "asset-status is-assigned" : "asset-status"}>
          {requirement.asset_id ? "Réservé" : "À réserver"}
        </span>
      </div>

      {requirement.usage_hours != null && (
        <small className="asset-usage-budget">
          Budget d’usage {requirement.usage_hours} h — distinct des heures de main-d’œuvre.
        </small>
      )}

      <div className="asset-reservation-controls">
        <label>
          <span>Unité</span>
          <select
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            disabled={!canManage || busy}
            aria-label={`Unité pour ${requirement.asset_type_label} ${requirement.demand_number}`}
          >
            <option value="">Aucune réservation</option>
            {compatible.map((asset) => (
              <option value={asset.id} key={asset.id}>
                {asset.code} — {asset.label}{asset.active ? "" : " — inactive"}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="secondary-button"
          disabled={!canManage || busy || selected === (requirement.asset_id ?? "")}
          onClick={() => onReserve(requirement, selected || null)}
        >
          {busy ? "Enregistrement…" : selected ? "Réserver cette unité" : "Libérer"}
        </button>
      </div>

      {requirement.asset_id && (
        <div className="asset-current-allocation">
          <strong>{requirement.asset_code} — {requirement.asset_label}</strong>
          <span>
            {requirement.allocation_start_date} → {requirement.allocation_end_date}
            {requirement.allocation_locked ? " · décision manuelle verrouillée" : ""}
          </span>
        </div>
      )}

      {requirement.required_competency_ids.length > 0 && (
        <div className="asset-current-allocation asset-qualification">
          <strong>
            {QUALIFICATION_LABELS[requirement.qualification_state] ?? requirement.qualification_state}
          </strong>
          <span>
            Prérequis : {requirement.required_competency_names.join(", ")}
          </span>
          {requirement.asset_id && (
            <div className="asset-reservation-controls">
              <label>
                <span>Opérateur qualifiant</span>
                <select
                  value={operatorSelected}
                  onChange={(event) => setOperatorSelected(event.target.value)}
                  disabled={!canManage || busy}
                  aria-label={`Opérateur qualifiant pour ${requirement.asset_type_label} ${requirement.demand_number}`}
                >
                  <option value="">Aucun opérateur</option>
                  {requirement.operator_resource_id
                    && !operatorCandidates.some((row) => row.resource_id === requirement.operator_resource_id) && (
                    <option value={requirement.operator_resource_id}>
                      {requirement.operator_resource_name ?? requirement.operator_resource_id} — non admissible actuellement
                    </option>
                  )}
                  {operatorCandidates.map((candidate) => (
                    <option value={candidate.resource_id} key={candidate.resource_id}>
                      {candidate.resource_name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="secondary-button"
                disabled={
                  !canManage
                  || busy
                  || operatorSelected === (requirement.operator_resource_id ?? "")
                }
                onClick={() => onOperator(requirement, operatorSelected || null)}
              >
                Enregistrer l’opérateur
              </button>
            </div>
          )}
          {candidateError && <small role="alert">{candidateError}</small>}
        </div>
      )}
    </article>
  );
}

export default function AssetPlanningPanel({
  snapshot,
  canManage,
  onRefresh,
}: {
  snapshot: PlanningSnapshotReadModel;
  canManage: boolean;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ tone: "info" | "success" | "error"; message: string } | null>(null);
  const [unavailableAssetId, setUnavailableAssetId] = useState("");
  const [unavailableStart, setUnavailableStart] = useState(snapshot.start);
  const [unavailableEnd, setUnavailableEnd] = useState(snapshot.end);
  const [unavailableReason, setUnavailableReason] = useState("");
  const retryKeys = useRef(new Map<string, string>());

  const typesById = useMemo(
    () => new Map(snapshot.asset_types.map((row) => [row.id, row])),
    [snapshot.asset_types],
  );
  const assetsById = useMemo(
    () => new Map(snapshot.assets.map((row) => [row.id, row])),
    [snapshot.assets],
  );
  const capacityByAssetDay = useMemo(
    () => new Map(snapshot.asset_capacity.map((row) => [`${row.asset_id}:${row.day}`, row])),
    [snapshot.asset_capacity],
  );
  const days = useMemo(() => {
    const result: string[] = [];
    const cursor = new Date(`${snapshot.start}T12:00:00`);
    const end = new Date(`${snapshot.end}T12:00:00`);
    while (cursor <= end) {
      result.push(cursor.toISOString().slice(0, 10));
      cursor.setDate(cursor.getDate() + 1);
    }
    return result;
  }, [snapshot.start, snapshot.end]);

  async function reserve(requirement: AssetRequirementPlanningReadModel, assetId: string | null) {
    if (busy) return;
    const fingerprint = [
      requirement.requirement_id,
      assetId ?? "release",
      requirement.start_date,
      requirement.end_date,
      snapshot.planning_version,
    ].join("|");
    const key = retryKeys.current.get(fingerprint) ?? mutationKey();
    retryKeys.current.set(fingerprint, key);
    setBusy(`reserve:${requirement.requirement_id}`);
    setFeedback(null);
    try {
      await reserveAssetRequirement(
        requirement.requirement_id,
        {
          asset_id: assetId,
          start_date: assetId ? requirement.start_date : null,
          end_date: assetId ? requirement.end_date : null,
          expected_planning_version: snapshot.planning_version,
        },
        key,
      );
      retryKeys.current.delete(fingerprint);
      setFeedback({
        tone: "success",
        message: assetId
          ? "Réservation enregistrée. Le planning partagé a été actualisé."
          : "Réservation libérée. Le planning partagé a été actualisé.",
      });
      onRefresh();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        retryKeys.current.delete(fingerprint);
        setFeedback({
          tone: "info",
          message: "Le planning a changé depuis l’ouverture de cette vue. Le snapshot est actualisé; vérifie la disponibilité puis réessaie.",
        });
        onRefresh();
      } else if (reason instanceof TypeError) {
        setFeedback({
          tone: "info",
          message: "La réponse est incertaine. Réessaie la même réservation : la même clé d’idempotence sera réutilisée.",
        });
      } else {
        retryKeys.current.delete(fingerprint);
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(null);
    }
  }

  async function assignOperator(
    requirement: AssetRequirementPlanningReadModel,
    resourceId: string | null,
  ) {
    if (busy) return;
    const fingerprint = [
      "operator",
      requirement.requirement_id,
      resourceId ?? "none",
      snapshot.planning_version,
    ].join("|");
    const key = retryKeys.current.get(fingerprint) ?? mutationKey();
    retryKeys.current.set(fingerprint, key);
    setBusy(`operator:${requirement.requirement_id}`);
    setFeedback(null);
    try {
      await setAssetRequirementOperator(
        requirement.requirement_id,
        {
          operator_resource_id: resourceId,
          expected_planning_version: snapshot.planning_version,
        },
        key,
      );
      retryKeys.current.delete(fingerprint);
      setFeedback({
        tone: "success",
        message: resourceId
          ? "Opérateur qualifiant enregistré."
          : "Opérateur qualifiant retiré.",
      });
      onRefresh();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        retryKeys.current.delete(fingerprint);
        setFeedback({
          tone: "info",
          message: "Le planning a changé. Le snapshot est actualisé; vérifie l’opérateur puis réessaie.",
        });
        onRefresh();
      } else if (reason instanceof TypeError) {
        setFeedback({
          tone: "info",
          message: "La réponse est incertaine. Réessaie la même affectation : la même clé d’idempotence sera réutilisée.",
        });
      } else {
        retryKeys.current.delete(fingerprint);
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(null);
    }
  }

  async function addUnavailability() {
    if (!unavailableAssetId || !unavailableStart || !unavailableEnd || busy) return;
    setBusy("unavailability:add");
    setFeedback(null);
    try {
      await addAssetUnavailability(unavailableAssetId, {
        start_date: unavailableStart,
        end_date: unavailableEnd,
        reason: unavailableReason.trim() || null,
        expected_planning_version: snapshot.planning_version,
      });
      setUnavailableReason("");
      setFeedback({ tone: "success", message: "Indisponibilité ajoutée au planning partagé." });
      onRefresh();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        setFeedback({
          tone: "info",
          message: "Le planning a changé. Le snapshot est actualisé; vérifie les dates puis réessaie.",
        });
        onRefresh();
      } else {
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(null);
    }
  }

  async function removeUnavailability(assetId: string, id: string) {
    if (busy) return;
    setBusy(`unavailability:${id}`);
    setFeedback(null);
    try {
      await removeAssetUnavailability(assetId, id, snapshot.planning_version);
      setFeedback({ tone: "success", message: "Indisponibilité retirée." });
      onRefresh();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        setFeedback({
          tone: "info",
          message: "Le planning a changé. Le snapshot est actualisé; vérifie l’indisponibilité puis réessaie.",
        });
        onRefresh();
      } else {
        setFeedback({ tone: "error", message: messageFromError(reason) });
      }
    } finally {
      setBusy(null);
    }
  }

  const visibleAssets = snapshot.assets.filter((asset) => asset.active);
  const unassigned = snapshot.asset_requirements.filter((row) => !row.asset_id).length;

  return (
    <section className="asset-planning-panel" aria-label="Planning des actifs">
      <div className="asset-planning-heading">
        <div>
          <span className="eyebrow">Ressources réservables non humaines</span>
          <h2>Actifs et réservations</h2>
          <p>
            Occupation exclusive par unité/jour. Les réservations d’actifs partagent le même planning_version que les mutations humaines.
          </p>
        </div>
        <div className="asset-planning-metrics">
          <span><strong>{snapshot.asset_requirements.length}</strong> besoin(s)</span>
          <span><strong>{unassigned}</strong> à réserver</span>
          <span><strong>{snapshot.asset_diagnostics.length}</strong> diagnostic(s)</span>
        </div>
      </div>

      {feedback && (
        <div className={`asset-planning-feedback is-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"}>
          {feedback.message}
        </div>
      )}

      {snapshot.asset_diagnostics.length > 0 && (
        <div className="asset-diagnostic-list" aria-label="Diagnostics actifs">
          {snapshot.asset_diagnostics.map((diagnostic: AssetPlanningDiagnosticReadModel, index) => (
            <article
              className={`asset-diagnostic is-${diagnosticTone(diagnostic.code)}`}
              key={`${diagnostic.code}:${diagnostic.requirement_id ?? diagnostic.asset_id ?? index}`}
            >
              <strong>{diagnostic.code}</strong>
              <span>{diagnostic.message}</span>
            </article>
          ))}
        </div>
      )}

      <div className="asset-planning-columns">
        <div className="asset-requirements-panel">
          <div className="asset-section-heading">
            <strong>Besoins approuvés</strong>
            <span>Sélection d’unité manuelle seulement</span>
          </div>
          <div className="asset-requirement-list">
            {snapshot.asset_requirements.length === 0 ? (
              <div className="asset-empty">Aucun besoin d’actif approuvé dans cette fenêtre.</div>
            ) : snapshot.asset_requirements.map((requirement) => (
              <AssetRequirementCard
                key={`${requirement.requirement_id}:${requirement.asset_id ?? "unassigned"}`}
                requirement={requirement}
                snapshot={snapshot}
                canManage={canManage}
                busy={
                  busy === `reserve:${requirement.requirement_id}`
                  || busy === `operator:${requirement.requirement_id}`
                }
                onReserve={(row, assetId) => void reserve(row, assetId)}
                onOperator={(row, resourceId) => void assignOperator(row, resourceId)}
              />
            ))}
          </div>
        </div>

        <div className="asset-unavailability-panel">
          <div className="asset-section-heading">
            <strong>Indisponibilités</strong>
            <span>Maintenance, prêt, bris ou autre blocage</span>
          </div>
          {canManage && (
            <div className="asset-unavailability-form">
              <label>
                <span>Actif</span>
                <select
                  value={unavailableAssetId}
                  onChange={(event) => setUnavailableAssetId(event.target.value)}
                  disabled={Boolean(busy)}
                >
                  <option value="">Sélectionner…</option>
                  {visibleAssets.map((asset) => (
                    <option value={asset.id} key={asset.id}>
                      {asset.code} — {asset.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Début</span>
                <input type="date" value={unavailableStart} onChange={(event) => setUnavailableStart(event.target.value)} disabled={Boolean(busy)} />
              </label>
              <label>
                <span>Fin</span>
                <input type="date" min={unavailableStart} value={unavailableEnd} onChange={(event) => setUnavailableEnd(event.target.value)} disabled={Boolean(busy)} />
              </label>
              <label className="span-2">
                <span>Raison</span>
                <input value={unavailableReason} onChange={(event) => setUnavailableReason(event.target.value)} placeholder="Ex. entretien préventif" disabled={Boolean(busy)} />
              </label>
              <button
                type="button"
                className="secondary-button span-2"
                onClick={() => void addUnavailability()}
                disabled={Boolean(busy) || !unavailableAssetId || !unavailableStart || !unavailableEnd || unavailableEnd < unavailableStart}
              >
                Ajouter l’indisponibilité
              </button>
            </div>
          )}
          <div className="asset-unavailability-list">
            {snapshot.asset_unavailability.length === 0 ? (
              <div className="asset-empty">Aucune indisponibilité dans cette fenêtre.</div>
            ) : snapshot.asset_unavailability.map((row) => {
              const asset = assetsById.get(row.asset_id);
              return (
                <article key={row.id}>
                  <div>
                    <strong>{asset ? `${asset.code} — ${asset.label}` : row.asset_id}</strong>
                    <span>{row.start_date} → {row.end_date}</span>
                    {row.reason && <small>{row.reason}</small>}
                  </div>
                  {canManage && (
                    <button
                      type="button"
                      className="text-button danger"
                      disabled={Boolean(busy)}
                      onClick={() => void removeUnavailability(row.asset_id, row.id)}
                    >
                      Retirer
                    </button>
                  )}
                </article>
              );
            })}
          </div>
        </div>
      </div>

      <div className="asset-capacity-panel">
        <div className="asset-section-heading">
          <strong>Occupation par unité</strong>
          <span>1 unité disponible ou occupée par jour — aucune conversion en heures humaines</span>
        </div>
        <div className="asset-capacity-scroll">
          <div className="asset-capacity-grid" style={{ gridTemplateColumns: `minmax(180px, 1.2fr) repeat(${days.length}, minmax(92px, 1fr))` }}>
            <div className="asset-capacity-header">Actif</div>
            {days.map((day) => <div className="asset-capacity-header" key={day}>{day.slice(5)}</div>)}
            {visibleAssets.map((asset) => {
              const type = typesById.get(asset.asset_type_id);
              return (
                <div className="asset-capacity-row" key={asset.id}>
                  <div className="asset-capacity-identity">
                    <strong>{asset.code} — {asset.label}</strong>
                    <span>{type?.label || asset.asset_type_id}</span>
                  </div>
                  {days.map((day) => {
                    const capacity = capacityByAssetDay.get(`${asset.id}:${day}`);
                    const occupiedUnits = Number(capacity?.occupied_units ?? 0);
                    const visibleOccupations = capacity?.visible_occupations ?? [];
                    const hasHiddenOccupancy = Boolean(capacity?.has_hidden_occupancy);
                    const state = capacity?.unavailable
                      ? "Indisponible"
                      : occupiedUnits > 0
                        ? (
                          visibleOccupations.length === 0 && hasHiddenOccupancy
                            ? "Occupé hors périmètre"
                            : "Occupé"
                        )
                        : "Libre";
                    const stateClass = capacity?.unavailable
                      ? "unavailable"
                      : occupiedUnits > 0
                        ? "occupied"
                        : "free";
                    return (
                      <div
                        className={`asset-capacity-cell is-${stateClass}`}
                        key={day}
                      >
                        <strong>{state}</strong>
                        {visibleOccupations.length > 0 && (
                          <div className="asset-capacity-occupations">
                            {visibleOccupations.map((occupation) => (
                              <div
                                className="asset-capacity-occupation"
                                key={occupation.allocation_id}
                              >
                                {occupation.project_number && <span>{occupation.project_number}</span>}
                                {occupation.operator_resource_name && (
                                  <small>{occupation.operator_resource_name}</small>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {hasHiddenOccupancy
                          && (visibleOccupations.length > 0 || capacity?.unavailable) && (
                          <small className="asset-capacity-hidden">
                            {visibleOccupations.length > 0
                              ? "+ occupation hors périmètre"
                              : "Occupation hors périmètre"}
                          </small>
                        )}
                        {occupiedUnits > 1 && (
                          <small className="asset-capacity-conflict">
                            {occupiedUnits} occupations · conflit
                          </small>
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
