import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  PlanningSnapshotReadModel,
  ProjectReadModel,
  getProjects,
} from "./api";
import {
  createProjectDirectReservation,
  createResourcePeriodReservation,
} from "./assetApi";
import SearchableCombobox from "./SearchableCombobox";

type DirectMode = "PROJECT_DIRECT" | "RESOURCE_PERIOD";

function newKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `asset-direct-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function errorMessage(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Réservation impossible.";
}

export default function DirectAssetReservationForm({
  snapshot,
  canManage,
  onRefresh,
}: {
  snapshot: PlanningSnapshotReadModel;
  canManage: boolean;
  onRefresh: () => void;
}) {
  const [mode, setMode] = useState<DirectMode>("PROJECT_DIRECT");
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [resourceId, setResourceId] = useState<string | null>(null);
  const [assetTypeId, setAssetTypeId] = useState("");
  const [assetId, setAssetId] = useState("");
  const [startDate, setStartDate] = useState(snapshot.start);
  const [endDate, setEndDate] = useState(snapshot.end);
  const [operatorId, setOperatorId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  useEffect(() => {
    const controller = new AbortController();
    setProjectError(null);
    void getProjects(true, controller.signal)
      .then(setProjects)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setProjectError(errorMessage(reason));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    setStartDate(snapshot.start);
    setEndDate(snapshot.end);
  }, [snapshot.start, snapshot.end]);

  const activeTypes = snapshot.asset_types.filter((row) => row.active);
  const compatibleAssets = snapshot.assets.filter(
    (row) => row.active && row.asset_type_id === assetTypeId,
  );
  const activeResources = snapshot.resources.filter((row) => row.active);

  useEffect(() => {
    if (!compatibleAssets.some((row) => row.id === assetId)) setAssetId("");
  }, [assetId, compatibleAssets]);

  const projectOptions = useMemo(
    () => projects.map((row) => ({
      value: row.id,
      label: `${row.number} — ${row.name}`,
      searchText: `${row.number} ${row.name} ${row.client ?? ""}`,
    })),
    [projects],
  );
  const resourceOptions = useMemo(
    () => activeResources.map((row) => ({
      value: row.id,
      label: row.name,
      searchText: `${row.name} ${row.resource_class ?? ""} ${row.external_id ?? ""}`,
    })),
    [activeResources],
  );

  const valid = Boolean(
    assetTypeId
    && assetId
    && startDate
    && endDate
    && endDate >= startDate
    && (mode === "PROJECT_DIRECT" ? projectId : resourceId),
  );

  async function submit() {
    if (!canManage || busy || !valid) return;
    const fingerprint = [
      mode,
      projectId ?? "none",
      resourceId ?? "none",
      assetTypeId,
      assetId,
      startDate,
      endDate,
      operatorId ?? "none",
      snapshot.planning_version,
    ].join("|");
    const key = retryKeys.current.get(fingerprint) ?? newKey();
    retryKeys.current.set(fingerprint, key);
    setBusy(true);
    setFeedback(null);
    try {
      if (mode === "PROJECT_DIRECT") {
        await createProjectDirectReservation(
          {
            project_id: projectId!,
            asset_type_id: assetTypeId,
            asset_id: assetId,
            start_date: startDate,
            end_date: endDate,
            operator_resource_id: operatorId,
            expected_planning_version: snapshot.planning_version,
          },
          key,
        );
      } else {
        await createResourcePeriodReservation(
          {
            resource_id: resourceId!,
            project_id: projectId,
            asset_type_id: assetTypeId,
            asset_id: assetId,
            start_date: startDate,
            end_date: endDate,
            expected_planning_version: snapshot.planning_version,
          },
          key,
        );
      }
      retryKeys.current.delete(fingerprint);
      setAssetId("");
      setFeedback("Réservation directe enregistrée.");
      onRefresh();
    } catch (reason: unknown) {
      if (reason instanceof TypeError) {
        setFeedback("Réponse incertaine. Réessaie : la même clé d’idempotence sera réutilisée.");
      } else {
        retryKeys.current.delete(fingerprint);
        setFeedback(errorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="asset-unavailability-panel asset-direct-reservation-panel">
      <div className="asset-section-heading">
        <strong>Réserver un actif</strong>
        <span>Sans demande, segment ou quart artificiel</span>
      </div>
      <div className="asset-unavailability-form">
        <label>
          <span>Réserver pour</span>
          <select
            value={mode}
            disabled={!canManage || busy}
            onChange={(event) => {
              const next = event.target.value as DirectMode;
              setMode(next);
              setOperatorId(null);
              if (next === "PROJECT_DIRECT") setResourceId(null);
            }}
          >
            <option value="PROJECT_DIRECT">Un projet</option>
            <option value="RESOURCE_PERIOD">Une ressource pour une période</option>
          </select>
        </label>

        {mode === "PROJECT_DIRECT" ? (
          <SearchableCombobox
            label="Projet"
            value={projectId}
            options={projectOptions}
            onChange={setProjectId}
            disabled={!canManage || busy}
            error={projectError}
            required
          />
        ) : (
          <>
            <SearchableCombobox
              label="Ressource"
              value={resourceId}
              options={resourceOptions}
              onChange={setResourceId}
              disabled={!canManage || busy}
              required
            />
            <SearchableCombobox
              label="Projet (facultatif)"
              value={projectId}
              options={projectOptions}
              onChange={setProjectId}
              disabled={!canManage || busy}
              error={projectError}
              clearable
            />
          </>
        )}

        <label>
          <span>Type d’actif</span>
          <select
            value={assetTypeId}
            onChange={(event) => setAssetTypeId(event.target.value)}
            disabled={!canManage || busy}
          >
            <option value="">Sélectionner…</option>
            {activeTypes.map((row) => (
              <option value={row.id} key={row.id}>
                {row.code} — {row.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Unité</span>
          <select
            value={assetId}
            onChange={(event) => setAssetId(event.target.value)}
            disabled={!canManage || busy || !assetTypeId}
          >
            <option value="">Sélectionner…</option>
            {compatibleAssets.map((row) => (
              <option value={row.id} key={row.id}>
                {row.code} — {row.label}
              </option>
            ))}
          </select>
        </label>

        {mode === "PROJECT_DIRECT" && (
          <SearchableCombobox
            label="Opérateur (facultatif)"
            value={operatorId}
            options={resourceOptions}
            onChange={setOperatorId}
            disabled={!canManage || busy}
            clearable
          />
        )}

        <label>
          <span>Début réel</span>
          <input
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
            disabled={!canManage || busy}
          />
        </label>
        <label>
          <span>Fin réelle</span>
          <input
            type="date"
            min={startDate}
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
            disabled={!canManage || busy}
          />
        </label>
        <button
          type="button"
          className="secondary-button span-2"
          disabled={!canManage || busy || !valid}
          onClick={() => void submit()}
        >
          {busy ? "Réservation…" : "Réserver l’actif"}
        </button>
      </div>
      {feedback && <small role="status">{feedback}</small>}
    </div>
  );
}
