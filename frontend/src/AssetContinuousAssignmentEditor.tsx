import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ResourceReadModel } from "./api";
import {
  type AssetAssignmentAuthority,
  type ContinuousVehicleAssignment,
  type ContinuousVehicleUnitState,
  cancelContinuousVehicleAssignment,
  createContinuousVehicleAssignment,
  getContinuousVehicleUnitState,
  rescheduleContinuousVehicleAssignment,
  revokeContinuousVehicleAssignment,
  transferContinuousVehicleAssignment,
} from "./assetApi";
import { createClientId } from "./clientId";
import { resourceDisplayName } from "./resourceLabels";

const STALE_CODES = new Set(["planning_version_conflict", "planning_version_stale"]);

function todayIso() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 10);
}

function earliestLifecycleDate(startDate: string) {
  const next = new Date(`${startDate}T12:00:00`);
  next.setDate(next.getDate() + 1);
  const afterStart = new Date(next.getTime() - next.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 10);
  return afterStart > todayIso() ? afterStart : todayIso();
}

export type ContinuousVehicleAssignmentDraft = {
  assigned: boolean;
  resourceId: string;
  effectiveDate: string;
};

type CommitResult = "none" | "created" | "transferred" | "revoked" | "cancelled" | "rescheduled";

function hasContinuousAssignment(state: ContinuousVehicleUnitState | null) {
  return Boolean(
    state?.has_continuous_assignment
    && state.assignment?.resource_period_mode === "CONTINUOUS",
  );
}

export function useContinuousVehicleAssignmentDraft({
  assetId,
  vehicle,
}: {
  assetId: string | null;
  vehicle: boolean;
}) {
  const [state, setState] = useState<ContinuousVehicleUnitState | null>(null);
  const [loadedAssetId, setLoadedAssetId] = useState<string | null>(null);
  const [loading, setLoading] = useState(vehicle && Boolean(assetId));
  const [pending, setPending] = useState(false);
  const [assigned, setAssignedState] = useState(false);
  const [resourceId, setResourceIdState] = useState("");
  const [effectiveDate, setEffectiveDateState] = useState(todayIso());
  const [error, setError] = useState<string | null>(null);
  const retryKeys = useRef(new Map<string, string>());

  const applyCanonicalState = useCallback((next: ContinuousVehicleUnitState) => {
    const continuous = hasContinuousAssignment(next);
    setState(next);
    setLoadedAssetId(next.asset_id);
    setAssignedState(continuous);
    setResourceIdState(continuous ? next.assignment?.operator_resource_id ?? "" : "");
    setEffectiveDateState(
      continuous && next.assignment
        ? next.assignment.start_date
        : next.as_of,
    );
  }, []);

  const load = useCallback(async (targetAssetId: string, signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const next = await getContinuousVehicleUnitState(targetAssetId, signal);
      if (signal?.aborted) return null;
      applyCanonicalState(next);
      return next;
    } catch (reason: unknown) {
      if (reason instanceof DOMException && reason.name === "AbortError") return null;
      setError(reason instanceof Error ? reason.message : "Attribution continue indisponible.");
      throw reason;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [applyCanonicalState]);

  useEffect(() => {
    retryKeys.current.clear();
    setError(null);
    if (!vehicle || !assetId) {
      setState(null);
      setLoadedAssetId(null);
      setLoading(false);
      setAssignedState(false);
      setResourceIdState("");
      setEffectiveDateState(earliestLifecycleDate(state.assignment.start_date));
      return;
    }
    setState(null);
    setLoadedAssetId(null);
    setAssignedState(false);
    setResourceIdState("");
    setLoading(true);
    const controller = new AbortController();
    void load(assetId, controller.signal).catch(() => undefined);
    return () => controller.abort();
  }, [assetId, load, vehicle]);

  const draft = useMemo<ContinuousVehicleAssignmentDraft>(
    () => ({ assigned, resourceId, effectiveDate }),
    [assigned, effectiveDate, resourceId],
  );

  const assignment = state?.assignment ?? null;
  const continuousLoaded = hasContinuousAssignment(state);
  const detailsVisible = Boolean(state?.assignment_details_visible);
  const authority = state?.assignment_authority ?? null;
  const editable = !continuousLoaded || detailsVisible;

  const setAssigned = useCallback((value: boolean) => {
    setAssignedState(value);
    if (state?.assignment?.effective_state === "ACTIVE" && !value) {
      setEffectiveDateState(earliestLifecycleDate(state.assignment.start_date));
    } else if (state?.assignment && value) {
      setEffectiveDateState(state.assignment.start_date);
    }
  }, [state]);

  const setResourceId = useCallback((value: string) => {
    setResourceIdState(value);
    if (
      state?.assignment?.effective_state === "ACTIVE"
      && value
      && value !== state.assignment.operator_resource_id
    ) {
      setEffectiveDateState(earliestLifecycleDate(state.assignment.start_date));
    }
  }, [state]);

  const setEffectiveDate = useCallback((value: string) => {
    setEffectiveDateState(value);
  }, []);

  const hasDraftMutation = useCallback((candidate = draft) => {
    if (!vehicle) return false;
    if (!state || loadedAssetId !== assetId) return Boolean(candidate.assigned);
    const current = state.assignment;
    const hasCurrent = hasContinuousAssignment(state);
    if (!hasCurrent) return candidate.assigned;
    if (!candidate.assigned) return true;
    if (candidate.resourceId && candidate.resourceId !== current?.operator_resource_id) return true;
    return Boolean(
      current?.effective_state === "SCHEDULED"
      && candidate.effectiveDate
      && candidate.effectiveDate !== current.start_date
    );
  }, [assetId, draft, loadedAssetId, state, vehicle]);

  async function mutate(
    fingerprint: string,
    action: (key: string) => Promise<unknown>,
  ) {
    const key = retryKeys.current.get(fingerprint) ?? createClientId();
    retryKeys.current.set(fingerprint, key);
    try {
      await action(key);
      retryKeys.current.delete(fingerprint);
    } catch (reason: unknown) {
      if (!(reason instanceof TypeError)) retryKeys.current.delete(fingerprint);
      throw reason;
    }
  }

  const commit = useCallback(async (
    targetAssetId: string,
    candidate: ContinuousVehicleAssignmentDraft = draft,
  ): Promise<CommitResult> => {
    if (!vehicle || pending) return "none";
    setPending(true);
    setError(null);
    try {
      const fresh = await getContinuousVehicleUnitState(targetAssetId);
      const current = fresh.assignment;
      const currentContinuous = hasContinuousAssignment(fresh);
      const authorityAllowed = Boolean(fresh.assignment_authority.can_assign);

      if (
        (candidate.assigned !== currentContinuous
          || (currentContinuous && candidate.resourceId !== (current?.operator_resource_id ?? ""))
          || (
            currentContinuous
            && current?.effective_state === "SCHEDULED"
            && candidate.effectiveDate !== current.start_date
          ))
        && (!authorityAllowed || (currentContinuous && !fresh.assignment_details_visible))
      ) {
        throw new Error(fresh.assignment_authority.message);
      }

      let result: CommitResult = "none";
      if (!currentContinuous) {
        if (candidate.assigned) {
          if (!candidate.resourceId || !candidate.effectiveDate) {
            throw new Error("La ressource et la date effective sont requises pour attitrer le véhicule.");
          }
          await mutate(
            `create|${targetAssetId}|${candidate.resourceId}|${candidate.effectiveDate}|${fresh.planning_version}`,
            (key) => createContinuousVehicleAssignment(
              {
                resource_id: candidate.resourceId,
                asset_id: targetAssetId,
                start_date: candidate.effectiveDate,
                expected_planning_version: fresh.planning_version,
              },
              key,
            ),
          );
          result = "created";
        }
      } else if (current) {
        if (!candidate.assigned) {
          if (current.effective_state === "SCHEDULED") {
            await mutate(
              `cancel|${current.requirement_id}|${fresh.planning_version}`,
              (key) => cancelContinuousVehicleAssignment(
                current.requirement_id,
                fresh.planning_version,
                key,
              ),
            );
            result = "cancelled";
          } else {
            if (!candidate.effectiveDate) throw new Error("La date effective de révocation est requise.");
            await mutate(
              `revoke|${current.requirement_id}|${candidate.effectiveDate}|${fresh.planning_version}`,
              (key) => revokeContinuousVehicleAssignment(
                current.requirement_id,
                {
                  effective_from: candidate.effectiveDate,
                  expected_planning_version: fresh.planning_version,
                },
                key,
              ),
            );
            result = "revoked";
          }
        } else if (
          candidate.resourceId
          && candidate.resourceId !== current.operator_resource_id
        ) {
          if (!candidate.effectiveDate) throw new Error("La date effective du transfert est requise.");
          await mutate(
            `transfer|${current.requirement_id}|${candidate.resourceId}|${candidate.effectiveDate}|${fresh.planning_version}`,
            (key) => transferContinuousVehicleAssignment(
              current.requirement_id,
              {
                resource_id: candidate.resourceId,
                asset_id: targetAssetId,
                effective_from: candidate.effectiveDate,
                expected_planning_version: fresh.planning_version,
              },
              key,
            ),
          );
          result = "transferred";
        } else if (
          current.effective_state === "SCHEDULED"
          && candidate.effectiveDate
          && candidate.effectiveDate !== current.start_date
        ) {
          await mutate(
            `reschedule|${current.requirement_id}|${candidate.effectiveDate}|${fresh.planning_version}`,
            (key) => rescheduleContinuousVehicleAssignment(
              current.requirement_id,
              {
                start_date: candidate.effectiveDate,
                expected_planning_version: fresh.planning_version,
              },
              key,
            ),
          );
          result = "rescheduled";
        }
      }

      const canonical = await getContinuousVehicleUnitState(targetAssetId);
      applyCanonicalState(canonical);
      return result;
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
        setError("Le Planning a changé. L’état canonique a été rechargé; vérifie puis réessaie.");
        try {
          const canonical = await getContinuousVehicleUnitState(targetAssetId);
          applyCanonicalState(canonical);
        } catch {
          // Preserve the original stale-state message.
        }
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de modifier l’attribution continue.");
      }
      throw reason;
    } finally {
      setPending(false);
    }
  }, [applyCanonicalState, draft, pending, vehicle]);

  const effectiveLoading = Boolean(
    loading
    || (vehicle && assetId && loadedAssetId !== assetId),
  );

  return {
    state,
    loading: effectiveLoading,
    pending,
    error,
    draft,
    assignment,
    authority,
    detailsVisible,
    editable,
    hasContinuousAssignment: continuousLoaded,
    hasDraftMutation,
    setAssigned,
    setResourceId,
    setEffectiveDate,
    load,
    commit,
  };
}

export function ContinuousVehicleAssignmentFields({
  controller,
  resources,
  creating,
  creatingCanAssign,
}: {
  controller: ReturnType<typeof useContinuousVehicleAssignmentDraft>;
  resources: ResourceReadModel[];
  creating: boolean;
  creatingCanAssign: boolean;
}) {
  const {
    state,
    loading,
    pending,
    error,
    draft,
    assignment,
    authority,
    detailsVisible,
    editable,
    setAssigned,
    setResourceId,
    setEffectiveDate,
  } = controller;

  const selectableResources = useMemo(
    () => resources
      .filter((row) => row.active)
      .slice()
      .sort((left, right) => resourceDisplayName(left.name).localeCompare(resourceDisplayName(right.name), "fr-CA")),
    [resources],
  );
  const currentResource = assignment?.operator_resource_id
    ? resources.find((row) => row.id === assignment.operator_resource_id) ?? null
    : null;
  const scheduledResourceId = state?.scheduled_assignment?.operator_resource_id ?? null;
  const scheduledResource = scheduledResourceId
    ? resources.find((row) => row.id === scheduledResourceId) ?? null
    : null;

  if (!creating && loading) {
    return (
      <div className="subtle-status" data-testid="asset-continuous-loading">
        Chargement de l’état Attitré…
      </div>
    );
  }

  const authorityAllowed = creating ? creatingCanAssign : Boolean(authority?.can_assign);
  const controlsDisabled = pending || !authorityAllowed || (!creating && !editable);

  return (
    <div className="asset-continuous-fields" data-testid="asset-continuous-assignment-fields">
      {error && <div className="inline-error" role="alert">{error}</div>}
      <label className="asset-assigned-checkbox">
        <input
          type="checkbox"
          checked={draft.assigned}
          disabled={controlsDisabled}
          onChange={(event) => setAssigned(event.target.checked)}
        />
        Attitré
      </label>

      {!draft.assigned && assignment?.effective_state === "ACTIVE" && editable && (
        <label>
          Date effective de révocation
          <input
            type="date"
            value={draft.effectiveDate}
            disabled={controlsDisabled}
            min={earliestLifecycleDate(assignment.start_date)}
            onChange={(event) => setEffectiveDate(event.target.value)}
            required
          />
        </label>
      )}

      {draft.assigned && (
        <>
          {assignment && !detailsVisible && (
            <div className="subtle-status">
              Ce véhicule est attitré à une ressource hors de votre périmètre.
            </div>
          )}
          {editable && (
            <>
              <label>
                Ressource
                <select
                  value={draft.resourceId}
                  disabled={controlsDisabled}
                  onChange={(event) => setResourceId(event.target.value)}
                  required={draft.assigned}
                >
                  <option value="">Sélectionner une ressource…</option>
                  {currentResource && !currentResource.active && (
                    <option value={currentResource.id}>
                      {resourceDisplayName(currentResource.name)} — inactive
                    </option>
                  )}
                  {selectableResources.map((resource) => (
                    <option value={resource.id} key={resource.id}>
                      {resourceDisplayName(resource.name)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {assignment?.effective_state === "ACTIVE"
                  && draft.resourceId !== assignment.operator_resource_id
                    ? "Date effective du transfert"
                    : assignment?.effective_state === "ACTIVE" && !draft.assigned
                      ? "Date effective de révocation"
                      : "Date effective"}
                <input
                  type="date"
                  value={draft.effectiveDate}
                  disabled={controlsDisabled}
                  onChange={(event) => setEffectiveDate(event.target.value)}
                  required={draft.assigned}
                />
                {assignment?.effective_state === "ACTIVE"
                  && draft.assigned
                  && draft.resourceId === assignment.operator_resource_id && (
                    <small>Date de début canonique de l’attribution continue.</small>
                  )}
              </label>
            </>
          )}
        </>
      )}

      {assignment && (
        <div className="subtle-status" data-testid="asset-continuous-assignment-state">
          Mode : CONTINUOUS · {assignment.effective_state === "ACTIVE"
            ? "État : Actif"
            : `État : Planifié pour ${assignment.start_date}`}
          {assignment.end_date ? ` · Fin prévue : ${assignment.end_date}` : ""}
        </div>
      )}

      {state?.scheduled_assignment && (
        <div className="subtle-status" data-testid="asset-continuous-scheduled-transition">
          Transition planifiée pour {state.scheduled_assignment.start_date}
          {state.scheduled_assignment_details_visible && scheduledResourceId
            ? ` vers ${scheduledResource ? resourceDisplayName(scheduledResource.name) : scheduledResourceId}`
            : " vers une ressource protégée"}
        </div>
      )}

      {creating && !creatingCanAssign && (
        <div className="subtle-status" data-testid="asset-continuous-authority-diagnostic">
          La permission manage_planning est requise pour attitrer cette nouvelle unité.
        </div>
      )}
      {!creating && authority && !authority.can_assign && (
        <div className="subtle-status" data-testid="asset-continuous-authority-diagnostic">
          {authority.message}
        </div>
      )}
    </div>
  );
}

export type ContinuousAssignmentController = ReturnType<typeof useContinuousVehicleAssignmentDraft>;
export type { AssetAssignmentAuthority, ContinuousVehicleAssignment };
