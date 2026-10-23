import { useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "./AuthContext";
import { createClientId } from "./clientId";
import type { ResourceReadModel } from "./api";
import {
  type AssetCatalog,
  type ContinuousVehicleAssignment,
  cancelContinuousVehicleAssignment,
  cancelContinuousVehicleRevocation,
  createContinuousVehicleAssignment,
  getAssetCatalog,
  getContinuousVehicleAssignments,
  rescheduleContinuousVehicleAssignment,
  rescheduleContinuousVehicleRevocation,
  revokeContinuousVehicleAssignment,
  transferContinuousVehicleAssignment,
} from "./assetApi";

function todayIso() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 10);
}

function nextDay(value: string) {
  const date = new Date(`${value}T12:00:00`);
  date.setDate(date.getDate() + 1);
  return date.toISOString().slice(0, 10);
}

function stateLabel(value: ContinuousVehicleAssignment["effective_state"]) {
  if (value === "ACTIVE") return "Actif";
  if (value === "SCHEDULED") return "Planifié";
  return "Terminé";
}

export default function ContinuousVehicleAssignmentPanel({
  resource,
  resources,
}: {
  resource: ResourceReadModel;
  resources: ResourceReadModel[];
}) {
  const { can } = useAuth();
  const canManage = can("manage_planning");
  const [catalog, setCatalog] = useState<AssetCatalog | null>(null);
  const [assignments, setAssignments] = useState<ContinuousVehicleAssignment[]>([]);
  const [planningVersion, setPlanningVersion] = useState(1);
  const [asOf, setAsOf] = useState(todayIso());
  const [assetId, setAssetId] = useState("");
  const [startDate, setStartDate] = useState(todayIso());
  const [actionDate, setActionDate] = useState(todayIso());
  const [targetResourceId, setTargetResourceId] = useState(resource.id);
  const [targetAssetId, setTargetAssetId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const retryKeys = useRef(new Map<string, string>());

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    Promise.all([
      getAssetCatalog(controller.signal),
      getContinuousVehicleAssignments(resource.id, controller.signal),
    ])
      .then(([nextCatalog, state]) => {
        setCatalog(nextCatalog);
        setAssignments(state.assignments);
        setPlanningVersion(state.planning_version);
        setAsOf(state.as_of);
        setTargetResourceId(resource.id);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Attributions de véhicules indisponibles.");
      });
    return () => controller.abort();
  }, [resource.id, refreshKey]);

  const vehicleTypeIds = useMemo(
    () => new Set(
      (catalog?.types ?? [])
        .filter((row) => row.active && row.category === "VEHICLE")
        .map((row) => row.id),
    ),
    [catalog],
  );
  const vehicles = useMemo(
    () => (catalog?.assets ?? [])
      .filter((row) => row.active && vehicleTypeIds.has(row.asset_type_id))
      .sort((a, b) => a.code.localeCompare(b.code, "fr-CA")),
    [catalog, vehicleTypeIds],
  );
  const activeResources = useMemo(
    () => resources
      .filter((row) => row.active)
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name, "fr-CA")),
    [resources],
  );

  async function mutate(fingerprint: string, action: (key: string) => Promise<unknown>) {
    if (busy) return;
    const key = retryKeys.current.get(fingerprint) ?? createClientId();
    retryKeys.current.set(fingerprint, key);
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action(key);
      retryKeys.current.delete(fingerprint);
      setRefreshKey((value) => value + 1);
      setNotice("Attribution de véhicule mise à jour.");
    } catch (reason) {
      if (!(reason instanceof TypeError)) retryKeys.current.delete(fingerprint);
      setError(reason instanceof Error ? reason.message : "Mutation d’attribution impossible.");
    } finally {
      setBusy(false);
    }
  }

  const createAssignment = () => mutate(
    `create|${resource.id}|${assetId}|${startDate}|${planningVersion}`,
    (key) => createContinuousVehicleAssignment(
      {
        resource_id: resource.id,
        asset_id: assetId,
        start_date: startDate,
        expected_planning_version: planningVersion,
      },
      key,
    ),
  );

  function revoke(row: ContinuousVehicleAssignment) {
    return mutate(
      `revoke|${row.requirement_id}|${actionDate}|${planningVersion}`,
      (key) => revokeContinuousVehicleAssignment(
        row.requirement_id,
        {
          effective_from: actionDate,
          expected_planning_version: planningVersion,
        },
        key,
      ),
    );
  }

  function cancelScheduled(row: ContinuousVehicleAssignment) {
    return mutate(
      `cancel|${row.requirement_id}|${planningVersion}`,
      (key) => cancelContinuousVehicleAssignment(
        row.requirement_id,
        planningVersion,
        key,
      ),
    );
  }

  function reschedule(row: ContinuousVehicleAssignment) {
    return mutate(
      `schedule|${row.requirement_id}|${actionDate}|${planningVersion}`,
      (key) => rescheduleContinuousVehicleAssignment(
        row.requirement_id,
        {
          start_date: actionDate,
          expected_planning_version: planningVersion,
        },
        key,
      ),
    );
  }

  function transfer(row: ContinuousVehicleAssignment) {
    return mutate(
      `transfer|${row.requirement_id}|${targetResourceId}|${targetAssetId}|${actionDate}|${planningVersion}`,
      (key) => transferContinuousVehicleAssignment(
        row.requirement_id,
        {
          resource_id: targetResourceId,
          asset_id: targetAssetId,
          effective_from: actionDate,
          expected_planning_version: planningVersion,
        },
        key,
      ),
    );
  }

  function cancelRevocation(row: ContinuousVehicleAssignment) {
    return mutate(
      `cancel-revoke|${row.requirement_id}|${planningVersion}`,
      (key) => cancelContinuousVehicleRevocation(
        row.requirement_id,
        planningVersion,
        key,
      ),
    );
  }

  function rescheduleRevocation(row: ContinuousVehicleAssignment) {
    return mutate(
      `reschedule-revoke|${row.requirement_id}|${actionDate}|${planningVersion}`,
      (key) => rescheduleContinuousVehicleRevocation(
        row.requirement_id,
        {
          effective_from: actionDate,
          expected_planning_version: planningVersion,
        },
        key,
      ),
    );
  }

  return (
    <section className="configuration-card" data-testid="continuous-vehicle-panel">
      <div className="configuration-section-heading">
        <div>
          <span className="eyebrow">#744 · Véhicule attitré</span>
          <h3>Attribution continue</h3>
          <small>État au {asOf}. L’occupation physique reste portée par AssetAllocation.</small>
        </div>
      </div>

      {error && <div className="inline-error">{error}</div>}
      {notice && <div className="configuration-notice">{notice}</div>}

      {assignments.length === 0 ? (
        <small>Aucune attribution continue actuelle ou historique pour cette ressource.</small>
      ) : (
        <div className="availability-list">
          {assignments.map((row) => {
            const futureRevocation = row.end_date && row.end_date >= asOf;
            return (
              <article className="availability-item" key={row.requirement_id}>
                <div>
                  <strong>{row.asset_code} — {row.asset_label}</strong>
                  <span>
                    {stateLabel(row.effective_state)} · {row.start_date} → {row.end_date ?? "sans fin"}
                  </span>
                  <small>
                    {row.asset_type_label} · Qualification: {row.qualification_state}
                  </small>
                </div>

                {canManage && row.effective_state !== "ENDED" && (
                  <div className="editor-actions">
                    <label>
                      Date effective
                      <input
                        type="date"
                        value={actionDate}
                        onChange={(event) => setActionDate(event.target.value)}
                        disabled={busy}
                      />
                    </label>

                    {row.effective_state === "SCHEDULED" ? (
                      <>
                        <button type="button" className="quiet-button" disabled={busy} onClick={() => void reschedule(row)}>
                          Replanifier le début
                        </button>
                        <button type="button" className="danger-button" disabled={busy} onClick={() => void cancelScheduled(row)}>
                          Annuler l’attribution future
                        </button>
                      </>
                    ) : row.end_date === null ? (
                      <button type="button" className="quiet-button" disabled={busy} onClick={() => void revoke(row)}>
                        Révoquer à cette date
                      </button>
                    ) : futureRevocation ? (
                      <>
                        <button type="button" className="quiet-button" disabled={busy} onClick={() => void rescheduleRevocation(row)}>
                          Replanifier la révocation
                        </button>
                        <button type="button" className="quiet-button" disabled={busy} onClick={() => void cancelRevocation(row)}>
                          Annuler la révocation
                        </button>
                      </>
                    ) : null}

                    {(row.end_date === null || row.effective_state === "SCHEDULED") && (
                      <>
                        <label>
                          Ressource cible
                          <select
                            value={targetResourceId}
                            onChange={(event) => setTargetResourceId(event.target.value)}
                            disabled={busy}
                          >
                            {activeResources.map((candidate) => (
                              <option value={candidate.id} key={candidate.id}>
                                {candidate.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          Véhicule cible
                          <select
                            value={targetAssetId}
                            onChange={(event) => setTargetAssetId(event.target.value)}
                            disabled={busy}
                          >
                            <option value="">Sélectionner…</option>
                            {vehicles.map((vehicle) => (
                              <option value={vehicle.id} key={vehicle.id}>
                                {vehicle.code} — {vehicle.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <button
                          type="button"
                          className="quiet-button"
                          disabled={busy || !targetResourceId || !targetAssetId}
                          onClick={() => void transfer(row)}
                        >
                          Transférer / remplacer
                        </button>
                      </>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {canManage && (
        <div className="admin-editor">
          <strong>Nouvelle attribution continue</strong>
          <label>
            Véhicule
            <select value={assetId} onChange={(event) => setAssetId(event.target.value)} disabled={busy}>
              <option value="">Sélectionner…</option>
              {vehicles.map((vehicle) => (
                <option value={vehicle.id} key={vehicle.id}>
                  {vehicle.code} — {vehicle.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Date de début effective
            <input
              type="date"
              min={todayIso()}
              value={startDate}
              onChange={(event) => setStartDate(event.target.value)}
              disabled={busy}
            />
          </label>
          <button
            type="button"
            className="primary-button"
            disabled={busy || !assetId || !startDate}
            onClick={() => void createAssignment()}
          >
            Attitrer le véhicule
          </button>
        </div>
      )}
    </section>
  );
}
