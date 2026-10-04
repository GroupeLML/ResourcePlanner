import { FormEvent, useEffect, useMemo, useState } from "react";

import { createClientId } from "./clientId";
import { useAuth } from "./AuthContext";
import {
  ApiError,
  ResourceReadModel,
  ShiftReadModel,
  getDemandDetail,
} from "./api";
import {
  OverallocationApiError,
  OverallocationContext,
  OverallocationPolicy,
  duplicateAllocationAtomic,
  deleteManualAllocation,
  overallocationContext,
  releaseManualAllocation,
  splitAllocationAtomic,
  updateAllocationWithOverallocation,
} from "./manualOverallocationApi";
import PlanningHistoryPanel from "./PlanningHistoryPanel";\nimport OperationalResponsibilityControl from "./OperationalResponsibilityControl";
import SegmentEditor from "./SegmentEditor";

type ConfirmationChoice = "inherit" | "Tentative" | "Confirmée";
type AtomicMode = "split" | "duplicate";
type OverallocationSource = "edit" | "atomic";
type AtomicAction = {
  mode: AtomicMode;
  idempotencyKey: string;
  expectedPlanningVersion: number;
  expectedApprovalRevisionId: string | null;
  expectedOperationalVersion: number | null;
  resourceId: string;
  day: string;
  transferHours: string;
  outsideStandardHours: boolean;
  retryPolicy: OverallocationPolicy | null;
  ambiguousRetry: boolean;
};
type OverallocationShift = ShiftReadModel & {
  segment_planned_hours?: number;
  segment_locked_hours?: number;
  segment_overallocated_hours?: number;
};

function confirmationChoice(shift: ShiftReadModel): ConfirmationChoice {
  if (shift.confirmation_override === "Tentative") return "Tentative";
  if (shift.confirmation_override) return "Confirmée";
  return "inherit";
}

function hoursLabel(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(Number(value ?? 0));
}

function assetActionReason(reason: string | null | undefined) {
  switch (reason) {
    case "permission_denied":
      return "Permission de planification requise.";
    case "shift_state_invalid":
      return "L’état du quart ne permet pas cette action.";
    case "asset_assignment_missing":
      return "Aucun actif n’est associé à ce quart.";
    case "asset_assignment_incomplete":
      return "L’affectation d’actif est incomplète et doit être régularisée.";
    case "asset_assignment_attached":
      return "Un actif est déjà associé à ce quart.";
    default:
      return reason || "Action non disponible.";
  }
}

function assetDiagnosticLabel(code: string) {
  switch (code) {
    case "asset_assignment_incomplete":
      return "Affectation d’actif incomplète";
    case "asset_inactive":
      return "Actif inactif";
    case "operator_not_qualified":
      return "Opérateur non qualifié";
    case "related_request_reservation_ambiguous":
      return "Plusieurs réservations REQUEST sont liées à ce travail";
    default:
      return code;
  }
}

function assetQualificationLabel(state: string | null | undefined) {
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

function newAtomicIdempotencyKey() {
  return createClientId();
}

export default function ShiftEditor({
  shift,
  resources,
  planningVersion,
  onClose,
  onSaved,
  onStale,
  onOpenDemand,
  onAssetAction,
}: {
  shift: ShiftReadModel;
  resources: ResourceReadModel[];
  planningVersion: number;
  onClose: () => void;
  onSaved: () => void;
  onStale: () => void;
  onOpenDemand: (demandNumber: string) => void;
  onAssetAction: (mode: "assign" | "change" | "release") => void;
}) {
  const { can } = useAuth();
  const canManagePlanning = can("manage_planning");
  const [resourceId, setResourceId] = useState(shift.resource_id);
  const [day, setDay] = useState(shift.work_date);
  const [hours, setHours] = useState(String(shift.hours));
  const [outsideStandardHours, setOutsideStandardHours] = useState(shift.outside_standard_hours);
  const [note, setNote] = useState(shift.note ?? "");
  const [confirmation, setConfirmation] = useState<ConfirmationChoice>(() => confirmationChoice(shift));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [segmentOpen, setSegmentOpen] = useState(false);
  const [overallocationChoice, setOverallocationChoice] = useState<OverallocationContext | null>(null);
  const [overallocationSource, setOverallocationSource] = useState<OverallocationSource | null>(null);
  const [atomicAction, setAtomicAction] = useState<AtomicAction | null>(null);
  const [atomicPreparing, setAtomicPreparing] = useState(false);
  const overallocationShift = shift as OverallocationShift;
  const currentExcess = Number(overallocationShift.segment_overallocated_hours ?? 0);

  useEffect(() => {
    if (!canManagePlanning || segmentOpen) return;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose, saving, segmentOpen, canManagePlanning]);

  const sortedResources = useMemo(
    () => [...resources].sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name, "fr-CA")),
    [resources],
  );

  async function save(policy: OverallocationPolicy | null = null) {
    if (saving || !canManagePlanning) return;
    const parsedHours = Number(hours);
    if (!resourceId.trim() || !day || !Number.isFinite(parsedHours) || parsedHours <= 0) {
      setError("Choisis une ressource du quart, une date et un nombre d'heures supérieur à zéro.");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await updateAllocationWithOverallocation(
        shift.allocation_id,
        {
          resource_id: resourceId,
          day,
          hours: parsedHours,
          outside_standard_hours: outsideStandardHours,
          note: note.trim(),
          confirmation: confirmation === "inherit" ? null : confirmation,
        },
        policy,
      );
      setOverallocationChoice(null);
      setOverallocationSource(null);
      onSaved();
    } catch (reason) {
      const context = overallocationContext(reason);
      if (
        reason instanceof OverallocationApiError
        && reason.code === "allocation_overallocation_choice_required"
        && context
      ) {
        setOverallocationChoice(context);
        setOverallocationSource("edit");
        setError(null);
      } else if (reason instanceof ApiError) {
        setOverallocationChoice(null);
        setOverallocationSource(null);
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setOverallocationChoice(null);
        setOverallocationSource(null);
        setError(reason instanceof Error ? reason.message : "Impossible d'enregistrer le quart.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await save(null);
  }


  async function openAtomic(mode: AtomicMode) {
    if (saving || atomicPreparing || !canManagePlanning) return;
    setAtomicPreparing(true);
    setError(null);
    setOverallocationChoice(null);
    setOverallocationSource(null);
    try {
      let approvalRevisionId: string | null = null;
      let operationalVersion: number | null = null;
      if (shift.demand_number) {
        const detail = await getDemandDetail(shift.demand_number);
        approvalRevisionId = detail.approval_state?.active_revision_id ?? null;
        operationalVersion = detail.approval_state?.operational_version ?? null;
        if (!approvalRevisionId) {
          setError("Aucune révision approuvée active n'est disponible pour ce quart. Rafraîchis le planning avant de continuer.");
          return;
        }
      }
      const half = Math.max(Math.round((Number(shift.hours) / 2) * 100) / 100, 0.01);
      setAtomicAction({
        mode,
        idempotencyKey: newAtomicIdempotencyKey(),
        expectedPlanningVersion: planningVersion,
        expectedApprovalRevisionId: approvalRevisionId,
        expectedOperationalVersion: operationalVersion,
        resourceId: shift.resource_id,
        day: shift.work_date,
        transferHours: String(half),
        outsideStandardHours: shift.outside_standard_hours,
        retryPolicy: null,
        ambiguousRetry: false,
      });
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de préparer l'action sur ce quart.");
      }
    } finally {
      setAtomicPreparing(false);
    }
  }

  function updateAtomic(patch: Partial<AtomicAction>) {
    setAtomicAction((current) => current ? { ...current, ...patch } : current);
    setOverallocationChoice(null);
    setOverallocationSource(null);
    setError(null);
  }

  async function executeAtomic(policy: OverallocationPolicy | null = null) {
    if (!atomicAction || saving || !canManagePlanning) return;
    const transferHours = Number(atomicAction.transferHours);
    if (!atomicAction.resourceId.trim() || !atomicAction.day) {
      setError("Choisis une ressource et une date pour le nouveau quart.");
      return;
    }
    if (
      atomicAction.mode === "split"
      && (!Number.isFinite(transferHours) || transferHours <= 0 || transferHours >= Number(shift.hours))
    ) {
      setError("Les heures transférées doivent être supérieures à zéro et inférieures aux heures du quart source.");
      return;
    }

    setSaving(true);
    setError(null);
    setAtomicAction((current) => current ? {
      ...current,
      retryPolicy: policy,
      ambiguousRetry: false,
    } : current);
    try {
      const common = {
        resource_id: atomicAction.resourceId,
        day: atomicAction.day,
        expected_planning_version: atomicAction.expectedPlanningVersion,
        outside_standard_hours: atomicAction.outsideStandardHours,
        expected_approval_revision_id: atomicAction.expectedApprovalRevisionId,
        expected_operational_version: policy === "INCREASE_PLANNED"
          ? atomicAction.expectedOperationalVersion
          : null,
        overallocation_policy: policy,
      };
      if (atomicAction.mode === "split") {
        await splitAllocationAtomic(
          shift.allocation_id,
          { ...common, transfer_hours: transferHours },
          atomicAction.idempotencyKey,
        );
      } else {
        await duplicateAllocationAtomic(
          shift.allocation_id,
          common,
          atomicAction.idempotencyKey,
        );
      }
      setOverallocationChoice(null);
      setOverallocationSource(null);
      setAtomicAction(null);
      onSaved();
    } catch (reason: unknown) {
      const context = overallocationContext(reason);
      if (
        reason instanceof OverallocationApiError
        && reason.code === "allocation_overallocation_choice_required"
        && context
      ) {
        setOverallocationChoice(context);
        setOverallocationSource("atomic");
        setError(null);
      } else if (
        reason instanceof OverallocationApiError
        && (
          reason.code === "planning_version_conflict"
          || reason.code === "operational_choice_version_conflict"
          || reason.code === "planning_authorization_unknown"
        )
      ) {
        setOverallocationChoice(null);
        setOverallocationSource(null);
        setAtomicAction(null);
        onStale();
      } else if (reason instanceof TypeError) {
        setAtomicAction((current) => current ? { ...current, ambiguousRetry: true } : current);
        setError("La réponse n'a pas été reçue. Réessaie la même commande : la clé idempotente et les versions capturées seront réutilisées.");
      } else if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible d'exécuter l'action sur le quart.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function releaseManual() {
    if (saving || !shift.locked) return;
    setSaving(true);
    setError(null);
    try {
      await releaseManualAllocation(shift.allocation_id);
      onSaved();
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de remettre le quart en automatique.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function deleteManual() {
    if (saving || !shift.locked) return;
    if (!window.confirm("Supprimer ce quart manuel? Le reliquat du segment redeviendra disponible au recalcul.")) return;
    setSaving(true);
    setError(null);
    try {
      await deleteManualAllocation(shift.allocation_id);
      onSaved();
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de supprimer le quart manuel.");
      }
    } finally {
      setSaving(false);
    }
  }

  if (!canManagePlanning) return null;

  if (segmentOpen) {
    return (
      <SegmentEditor
        open
        segmentId={shift.requirement_id || shift.segment_id}
        demand={null}
        resources={resources}
        onClose={() => setSegmentOpen(false)}
        onSaved={onSaved}
        onOpenDemand={onOpenDemand}
      />
    );
  }

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <section className="shift-dialog" role="dialog" aria-modal="true" aria-labelledby="shift-dialog-title">
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Quart {shift.allocation_id}</span>
            <h2 id="shift-dialog-title">Modifier le quart</h2>
            <p>
              {shift.project_number || "Projet"}
              {shift.project_name ? ` — ${shift.project_name}` : ""}
              {shift.demand_number ? ` · demande ${shift.demand_number}` : ""}
            </p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={saving} aria-label="Fermer">×</button>
        </div>

        <form className="shift-edit-form" onSubmit={submit}>
          {shift.source === "AUTO" && !shift.locked && (
            <div className="info-banner">
              Ce quart est généré automatiquement. L'enregistrer ici le transforme en décision manuelle verrouillée afin que le moteur ne l'écrase pas au prochain recalcul.
            </div>
          )}

          {currentExcess > 0 && (
            <div className="overallocation-warning" role="status">
              <strong>⚠ Surallocation manuelle active : +{hoursLabel(currentExcess)} h</strong>
              <span>
                {hoursLabel(overallocationShift.segment_locked_hours)} h verrouillées pour {hoursLabel(overallocationShift.segment_planned_hours)} h prévues. Les quarts manuels sont conservés jusqu'à régularisation explicite.
              </span>
            </div>
          )}

          {error && <div className="dialog-error">{error}</div>}

          {overallocationChoice && (
            <div className="overallocation-choice" role="alert">
              <strong>Ce changement dépasse les heures prévues du segment.</strong>
              <span>
                Planifié : {hoursLabel(overallocationChoice.planned_hours)} h · verrouillé après changement : {hoursLabel(overallocationChoice.projected_locked_hours)} h · excédent : +{hoursLabel(overallocationChoice.excess_hours)} h.
              </span>
              <div className="overallocation-choice-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={saving}
                  onClick={() => void (
                    overallocationSource === "atomic"
                      ? executeAtomic("INCREASE_PLANNED")
                      : save("INCREASE_PLANNED")
                  )}
                >
                  Augmenter les heures prévues à {hoursLabel(overallocationChoice.projected_locked_hours)} h
                </button>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={saving}
                  onClick={() => void (
                    overallocationSource === "atomic"
                      ? executeAtomic("KEEP_EXCEPTION")
                      : save("KEEP_EXCEPTION")
                  )}
                >
                  Conserver la dérogation (+{hoursLabel(overallocationChoice.excess_hours)} h)
                </button>
              </div>
              <small>Cette décision sera journalisée avec ton identité. Le moteur ne réduira jamais silencieusement un quart verrouillé.</small>
            </div>
          )}

          <div className="dialog-context-grid">
            <div><span>Segment</span><strong>{shift.segment_id}</strong></div>
            <div><span>Responsable projet</span><strong>{shift.project_manager || "—"}</strong></div>
            <div><span>Demandeur</span><strong>{shift.requester || "—"}</strong></div>
            <div><span>État</span><strong>{shift.locked ? "Verrouillé" : "Automatique"}</strong></div>
          </div>

          <OperationalResponsibilityControl
            target={{
              kind: "allocation",
              reference: shift.allocation_id,
              expectedPlanningVersion: planningVersion,
              source: shift.source,
              locked: shift.locked,
            }}
            onSaved={onSaved}
          />

          <section className="shift-asset-section" aria-label="Actif">
            <div className="shift-asset-section-heading">
              <div>
                <strong>Actif</strong>
                <span>L’affectation possédée par ce quart provient de la projection backend canonique.</span>
              </div>
              {!shift.asset_assignment && shift.asset_actions?.assign.allowed && (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => onAssetAction("assign")}
                  disabled={saving}
                >
                  Assigner un actif
                </button>
              )}
            </div>

            {shift.asset_assignment ? (
              <div className="shift-asset-owned">
                <div>
                  <span>Actif affecté au quart</span>
                  <strong>
                    {shift.asset_assignment.asset_code}
                    {shift.asset_assignment.asset_label ? ` — ${shift.asset_assignment.asset_label}` : ""}
                    {!shift.asset_assignment.asset_active ? " · Inactif" : ""}
                  </strong>
                  <small>Qualification : {assetQualificationLabel(shift.asset_assignment.qualification_state)}</small>
                </div>
                <div className="shift-asset-section-actions">
                  {shift.asset_actions?.change.allowed && (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => onAssetAction("change")}
                      disabled={saving}
                    >
                      Changer
                    </button>
                  )}
                  {shift.asset_actions?.release.allowed && (
                    <button
                      type="button"
                      className="danger-button"
                      onClick={() => onAssetAction("release")}
                      disabled={saving}
                    >
                      Libérer
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <div className="shift-asset-empty">
                <strong>Aucun actif associé</strong>
                {!shift.asset_actions?.assign.allowed && shift.asset_actions?.assign.reason && (
                  <span>{assetActionReason(shift.asset_actions.assign.reason)}</span>
                )}
              </div>
            )}

            {shift.asset_assignment && (
              <div className="shift-asset-action-reasons">
                {!shift.asset_actions?.change.allowed && shift.asset_actions?.change.reason && (
                  <small>Changer : {assetActionReason(shift.asset_actions.change.reason)}</small>
                )}
                {!shift.asset_actions?.release.allowed && shift.asset_actions?.release.reason && (
                  <small>Libérer : {assetActionReason(shift.asset_actions.release.reason)}</small>
                )}
              </div>
            )}

            {(shift.asset_diagnostics ?? []).length > 0 && (
              <div className="shift-asset-diagnostics" role="status">
                <strong>Diagnostics</strong>
                {(shift.asset_diagnostics ?? []).map((code) => (
                  <span key={code}>{assetDiagnosticLabel(code)}</span>
                ))}
              </div>
            )}

            {(shift.related_asset_reservations ?? []).length > 0 && (
              <div className="shift-related-assets">
                <strong>Réservations liées à la demande</strong>
                {(shift.related_asset_reservations ?? []).map((reservation) => (
                  <div key={reservation.allocation_id}>
                    <span>{reservation.asset_code} — {reservation.asset_label}</span>
                    <small>
                      REQUEST · Qualification : {assetQualificationLabel(reservation.qualification_state)}
                      {!reservation.asset_active ? " · Inactif" : ""}
                    </small>
                  </div>
                ))}
              </div>
            )}
          </section>

          <div className="segment-parent-link">
            <div>
              <strong>Besoin ressource parent</strong>
              <span>Fenêtre, heures prévues, compétence, priorité, confirmation et règles hors horaire appartiennent au segment.</span>
            </div>
            <button type="button" className="secondary-button" onClick={() => setSegmentOpen(true)} disabled={saving}>
              Modifier le segment parent
            </button>
          </div>

          <div className="dialog-form-grid">
            <label><span>Ressource du quart</span><select value={resourceId} onChange={(event) => setResourceId(event.target.value)} required>{sortedResources.map((resource) => <option value={resource.id} key={resource.id}>{resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}</option>)}</select></label>
            <label><span>Date</span><input type="date" value={day} onChange={(event) => setDay(event.target.value)} required /></label>
            <label><span>Heures</span><input type="number" min="0.25" step="0.25" value={hours} onChange={(event) => { setHours(event.target.value); setOverallocationChoice(null); }} required /></label>
            <label><span>Confirmation</span><select value={confirmation} onChange={(event) => setConfirmation(event.target.value as ConfirmationChoice)}><option value="inherit">Héritée du segment</option><option value="Tentative">Tentative</option><option value="Confirmée">Confirmée</option></select></label>
            <label className="checkbox-field"><input type="checkbox" checked={outsideStandardHours} onChange={(event) => setOutsideStandardHours(event.target.checked)} /><span>Autoriser / marquer hors horaire standard</span></label>
            <label className="span-2"><span>Note</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} /></label>
          </div>

          <div className="atomic-action-section" data-testid="atomic-allocation-actions">
            <div className="atomic-action-heading">
              <div>
                <strong>Partager ou dupliquer ce quart</strong>
                <span>Le nouveau quart reste sous le même besoin. Le backend valide le budget, les versions et le reliquat avant de recalculer.</span>
                {shift.asset_assignment && (
                  <span className="shift-asset-source-retains">L’actif associé restera sur le quart source.</span>
                )}
              </div>
              {!atomicAction && (
                <div className="atomic-action-buttons">
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={saving || atomicPreparing}
                    onClick={() => void openAtomic("split")}
                  >
                    Partager
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={saving || atomicPreparing}
                    onClick={() => void openAtomic("duplicate")}
                  >
                    Dupliquer
                  </button>
                </div>
              )}
            </div>

            {atomicPreparing && <small>Chargement du contexte d'autorisation…</small>}

            {atomicAction && (
              <div className="atomic-action-panel" data-testid={`atomic-${atomicAction.mode}-panel`}>
                <div className="atomic-action-title">
                  <strong>{atomicAction.mode === "split" ? "Partager le quart" : "Dupliquer le quart"}</strong>
                  <span>Version du planning capturée : {atomicAction.expectedPlanningVersion}</span>
                </div>
                <div className="dialog-form-grid">
                  <label>
                    <span>Ressource du nouveau quart</span>
                    <select
                      value={atomicAction.resourceId}
                      disabled={saving || atomicAction.ambiguousRetry}
                      onChange={(event) => updateAtomic({ resourceId: event.target.value })}
                    >
                      {sortedResources.map((resource) => (
                        <option value={resource.id} key={resource.id}>
                          {resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Date du nouveau quart</span>
                    <input
                      type="date"
                      value={atomicAction.day}
                      disabled={saving || atomicAction.ambiguousRetry}
                      onChange={(event) => updateAtomic({ day: event.target.value })}
                    />
                  </label>
                  {atomicAction.mode === "split" && (
                    <label>
                      <span>Heures à transférer</span>
                      <input
                        type="number"
                        min="0.01"
                        step="0.01"
                        max={Math.max(Number(shift.hours) - 0.01, 0.01)}
                        value={atomicAction.transferHours}
                        disabled={saving || atomicAction.ambiguousRetry}
                        onChange={(event) => updateAtomic({ transferHours: event.target.value })}
                      />
                    </label>
                  )}
                  <label className="checkbox-field">
                    <input
                      type="checkbox"
                      checked={atomicAction.outsideStandardHours}
                      disabled={saving || atomicAction.ambiguousRetry}
                      onChange={(event) => updateAtomic({ outsideStandardHours: event.target.checked })}
                    />
                    <span>Autoriser / marquer le nouveau quart hors horaire standard</span>
                  </label>
                </div>
                {atomicAction.ambiguousRetry && (
                  <div className="atomic-retry-note" role="status">
                    Le résultat réseau est incertain. Les champs sont figés afin de réessayer exactement la même commande avec la même clé idempotente.
                  </div>
                )}
                <div className="atomic-action-buttons">
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={saving}
                    onClick={() => {
                      setAtomicAction(null);
                      setOverallocationChoice(null);
                      setOverallocationSource(null);
                      setError(null);
                    }}
                  >
                    {atomicAction.ambiguousRetry ? "Annuler et recommencer" : "Annuler"}
                  </button>
                  <button
                    type="button"
                    className="primary-button"
                    disabled={saving}
                    onClick={() => void executeAtomic(atomicAction.retryPolicy)}
                  >
                    {saving
                      ? "Traitement…"
                      : atomicAction.ambiguousRetry
                        ? "Réessayer la même commande"
                        : atomicAction.mode === "split"
                          ? "Partager le quart"
                          : "Dupliquer le quart"}
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="confirmation-help">
            <strong>Ressource réelle de ce quart : {sortedResources.find((resource) => resource.id === resourceId)?.name || shift.resource_name}</strong>
            <span>Changer cette ressource modifie seulement ce quart; la cible automatique du besoin parent reste distincte.</span>
            <strong>Confirmation effective : {shift.confirmation || "—"}</strong>
            <span>« Héritée » supprime l'override du quart. Tentative ou Confirmée crée un choix explicite au niveau du quart.</span>
          </div>

          <PlanningHistoryPanel entityType="SHIFT" reference={shift.allocation_id} />

          <div className="dialog-actions">
            {shift.locked && (
              <>
                <button type="button" className="secondary-button" onClick={() => void releaseManual()} disabled={saving}>
                  Revenir à l’automatique
                </button>
                <button type="button" className="danger-button" onClick={() => void deleteManual()} disabled={saving}>
                  Supprimer le quart manuel
                </button>
              </>
            )}
            <button type="button" className="secondary-button" onClick={onClose} disabled={saving}>Annuler</button>
            <button
              type="submit"
              className="primary-button"
              disabled={saving}
              aria-label="Enregistrer les modifications"
            >
              {saving ? "Enregistrement…" : "Enregistrer et verrouiller"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}