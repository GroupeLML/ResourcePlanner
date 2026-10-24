import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ResourceReadModel } from "./api";
import {
  type AssetCatalogItem,
  type AssetTypeCatalogItem,
  type ContinuousVehicleUnitState,
  cancelContinuousVehicleAssignment,
  createContinuousVehicleAssignment,
  getContinuousVehicleUnitState,
  revokeContinuousVehicleAssignment,
  transferContinuousVehicleAssignment,
} from "./assetApi";
import { createClientId } from "./clientId";

const STALE_CODES = new Set(["planning_version_conflict", "planning_version_stale"]);

function todayIso() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 10);
}

function resourceLabel(resource: ResourceReadModel) {
  return resource.name;
}

export default function AssetContinuousAssignmentEditor({
  asset,
  assetType,
  resources,
  onChanged,
}: {
  asset: AssetCatalogItem;
  assetType: AssetTypeCatalogItem;
  resources: ResourceReadModel[];
  onChanged: () => void;
}) {
  const [state, setState] = useState<ContinuousVehicleUnitState | null>(null);
  const [loading, setLoading] = useState(true);
  const [assigned, setAssigned] = useState(false);
  const [resourceId, setResourceId] = useState("");
  const [effectiveDate, setEffectiveDate] = useState(todayIso());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  function load(signal?: AbortSignal) {
    setLoading(true);
    setError(null);
    return getContinuousVehicleUnitState(asset.id, signal)
      .then((next) => {
        setState(next);
        setAssigned(Boolean(next.assignment));
        setResourceId(next.assignment?.operator_resource_id ?? "");
        setEffectiveDate(next.as_of);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Attribution continue indisponible.");
      })
      .finally(() => {
        if (!signal?.aborted) setLoading(false);
      });
  }

  useEffect(() => {
    if (assetType.category !== "VEHICLE") return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [asset.id, assetType.category]);

  const selectableResources = useMemo(
    () => resources
      .filter((row) => row.active)
      .slice()
      .sort((left, right) => resourceLabel(left).localeCompare(resourceLabel(right), "fr-CA")),
    [resources],
  );

  if (assetType.category !== "VEHICLE") return null;

  const assignment = state?.assignment ?? null;
  const authorityAllowed = Boolean(state?.assignment_authority.allowed);
  const detailsVisible = Boolean(state?.assignment_details_visible);
  const currentResourceId = assignment?.operator_resource_id ?? null;
  const currentResource = currentResourceId
    ? resources.find((row) => row.id === currentResourceId) ?? null
    : null;

  async function mutate(fingerprint: string, action: (key: string) => Promise<unknown>) {
    if (pending || !state) return;
    const key = retryKeys.current.get(fingerprint) ?? createClientId();
    retryKeys.current.set(fingerprint, key);
    setPending(true);
    setError(null);
    try {
      await action(key);
      retryKeys.current.delete(fingerprint);
      await load();
      onChanged();
    } catch (reason: unknown) {
      if (!(reason instanceof TypeError)) retryKeys.current.delete(fingerprint);
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        setError("Le Planning a changé. L’état courant a été rechargé; vérifie puis réessaie.");
        await load();
      } else {
        setError(
          reason instanceof Error
            ? reason.message
            : "Impossible de modifier l’attribution continue.",
        );
      }
    } finally {
      setPending(false);
    }
  }

  function saveAssignment() {
    if (!state || !authorityAllowed || !detailsVisible) return;

    if (!assignment) {
      if (!assigned || !resourceId) return;
      return mutate(
        `create|${asset.id}|${resourceId}|${effectiveDate}|${state.planning_version}`,
        (key) => createContinuousVehicleAssignment(
          {
            resource_id: resourceId,
            asset_id: asset.id,
            start_date: effectiveDate,
            expected_planning_version: state.planning_version,
          },
          key,
        ),
      );
    }

    if (!assigned) {
      if (assignment.effective_state === "SCHEDULED") {
        return mutate(
          `cancel|${assignment.requirement_id}|${state.planning_version}`,
          (key) => cancelContinuousVehicleAssignment(
            assignment.requirement_id,
            state.planning_version,
            key,
          ),
        );
      }
      return mutate(
        `revoke|${assignment.requirement_id}|${effectiveDate}|${state.planning_version}`,
        (key) => revokeContinuousVehicleAssignment(
          assignment.requirement_id,
          {
            effective_from: effectiveDate,
            expected_planning_version: state.planning_version,
          },
          key,
        ),
      );
    }

    if (resourceId && resourceId !== currentResourceId) {
      return mutate(
        `transfer|${assignment.requirement_id}|${resourceId}|${effectiveDate}|${state.planning_version}`,
        (key) => transferContinuousVehicleAssignment(
          assignment.requirement_id,
          {
            resource_id: resourceId,
            asset_id: asset.id,
            effective_from: effectiveDate,
            expected_planning_version: state.planning_version,
          },
          key,
        ),
      );
    }
  }

  const hasMutation = (
    (!assignment && assigned && Boolean(resourceId))
    || (Boolean(assignment) && !assigned)
    || (Boolean(assignment) && assigned && Boolean(resourceId) && resourceId !== currentResourceId)
  );

  return (
    <div className="asset-unavailability-section" data-testid="asset-continuous-assignment-editor">
      <div className="editor-heading">
        <div>
          <span className="eyebrow">Attribution continue</span>
          <h3>Véhicule attitré</h3>
          <small>L’état provient du RESOURCE_PERIOD / CONTINUOUS canonique.</small>
        </div>
      </div>

      {loading && <div className="subtle-status">Chargement de l’attribution…</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}

      {!loading && state && (
        <>
          <label>
            <input
              type="checkbox"
              checked={assigned}
              disabled={pending || !authorityAllowed || !detailsVisible}
              onChange={(event) => setAssigned(event.target.checked)}
            />
            Attitré
          </label>

          {assignment && (
            <div className="subtle-status" data-testid="asset-continuous-assignment-state">
              {assignment.effective_state === "ACTIVE"
                ? "État : Actif"
                : `État : Planifié pour ${assignment.start_date}`}
              {assignment.end_date ? ` · Fin prévue : ${assignment.end_date}` : ""}
            </div>
          )}

          {assignment && !detailsVisible && (
            <div className="subtle-status">
              Ce véhicule est attitré, mais la ressource bénéficiaire n’est pas visible dans ton périmètre.
            </div>
          )}

          {state.has_ended_history && !assignment && (
            <div className="subtle-status">Une attribution historique terminée existe; le véhicule n’est pas actuellement attitré.</div>
          )}

          {(assigned || assignment) && detailsVisible && (
            <div className="form-grid two-columns">
              <label>
                Ressource
                <select
                  value={resourceId}
                  disabled={pending || !authorityAllowed}
                  onChange={(event) => setResourceId(event.target.value)}
                >
                  <option value="">Sélectionner une ressource…</option>
                  {currentResource && !currentResource.active && (
                    <option value={currentResource.id}>
                      {resourceLabel(currentResource)} — inactive
                    </option>
                  )}
                  {selectableResources.map((resource) => (
                    <option value={resource.id} key={resource.id}>
                      {resourceLabel(resource)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Date effective
                <input
                  type="date"
                  value={effectiveDate}
                  disabled={pending || !authorityAllowed}
                  onChange={(event) => setEffectiveDate(event.target.value)}
                />
              </label>
            </div>
          )}

          {!authorityAllowed && (
            <div className="subtle-status">
              L’attribution de cette unité n’est pas autorisée pour l’utilisateur courant.
            </div>
          )}

          {detailsVisible && (
            <div className="editor-actions">
              <button
                className="primary-button"
                type="button"
                disabled={pending || !authorityAllowed || !hasMutation || !effectiveDate}
                onClick={() => void saveAssignment()}
              >
                {pending ? "Enregistrement…" : "Enregistrer l’attribution"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
