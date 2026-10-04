import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { createClientId } from "./clientId";
import {
  ApiError,
  CompetencyReadModel,
  DemandReadModel,
  ResourceReadModel,
  SegmentReadModel,
  getCompetencies,
} from "./api";
import CompetencyPicker from "./CompetencyPicker";
import {
  OverallocationApiError,
  OverallocationContext,
  overallocationContext,
  updateSegmentWithOverallocation,
} from "./manualOverallocationApi";
import PlanningHistoryPanel from "./PlanningHistoryPanel";
import {
  LoadProfile,
  SegmentUpdateWrite,
  SegmentWrite,
  assignSegment,
  cancelSegment,
  createSegment,
  getSegment,
} from "./segments-api";

type ConfirmationChoice = "inherit" | "Tentative" | "Confirmée";
const AD_HOC_SEGMENT_ORIGINS = new Set(["QUICK_SHIFT", "AD_HOC"]);

function isCanonicalAdHocSegment(segment: SegmentReadModel | null | undefined) {
  return Boolean(segment && AD_HOC_SEGMENT_ORIGINS.has((segment.origin ?? "").trim().toUpperCase()));
}

type PlanningSegment = SegmentReadModel & {
  locked_hours?: number;
  overallocated_hours?: number;
  overallocated?: boolean;
  load_profile?: LoadProfile;
};

type FormState = {
  start_date: string;
  end_date: string;
  planned_hours: string;
  status: string;
  description: string;
  required_competency_id: string;
  planning_type: string;
  priority: string;
  outside_standard_hours: boolean;
  confirmation: ConfirmationChoice;
  technician: string;
  load_profile: LoadProfile;
};

type RetryReceipt = { fingerprint: string; key: string };

function newIdempotencyKey() {
  return createClientId();
}

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible d'enregistrer le segment.";
}

function confirmationChoice(segment: SegmentReadModel): ConfirmationChoice {
  if (!segment.demand_number && isCanonicalAdHocSegment(segment)) {
    return segment.confirmation === "Tentative" ? "Tentative" : "Confirmée";
  }
  if (!segment.confirmation_overridden) return "inherit";
  return segment.confirmation === "Tentative" ? "Tentative" : "Confirmée";
}

function hoursLabel(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(Number(value ?? 0));
}

function formFromDemand(demand: DemandReadModel, resources: ResourceReadModel[]): FormState {
  const start = demand.desired_start ?? "";
  return {
    start_date: start,
    end_date: demand.desired_end ?? start,
    planned_hours: demand.estimated_hours == null ? "" : String(demand.estimated_hours),
    status: "À assigner",
    description: demand.description ?? "",
    required_competency_id: demand.required_competency_ids.length === 1
      ? demand.required_competency_ids[0]
      : "",
    planning_type: "Flexible",
    priority: demand.priority ?? "Normale",
    outside_standard_hours: false,
    confirmation: "inherit",
    technician: resources.find((resource) => resource.name === demand.proposed_resource)?.id ?? "",
    load_profile: "UNIFORM",
  };
}

function formFromSegment(segment: SegmentReadModel): FormState {
  const planning = segment as PlanningSegment;
  return {
    start_date: segment.start_date ?? "",
    end_date: segment.end_date ?? segment.start_date ?? "",
    planned_hours: String(segment.planned_hours || ""),
    status: segment.status || "À assigner",
    description: segment.description ?? "",
    required_competency_id: segment.required_competency_id ?? "",
    planning_type: segment.planning_type ?? "Flexible",
    priority: segment.priority ?? "Normale",
    outside_standard_hours: segment.outside_standard_hours,
    confirmation: confirmationChoice(segment),
    technician: segment.automatic_target_resource_id ?? "",
    load_profile: planning.load_profile ?? "UNIFORM",
  };
}

export default function SegmentEditor({
  open,
  segmentId,
  demand,
  resources,
  onClose,
  onSaved,
}: {
  open: boolean;
  segmentId: string | null;
  demand: DemandReadModel | null;
  resources: ResourceReadModel[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [segment, setSegment] = useState<SegmentReadModel | null>(null);
  const [competencies, setCompetencies] = useState<CompetencyReadModel[]>([]);
  const [form, setForm] = useState<FormState | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overallocationChoice, setOverallocationChoice] = useState<OverallocationContext | null>(null);
  const [allowLockedOverallocation, setAllowLockedOverallocation] = useState(false);
  const createRetry = useRef<RetryReceipt | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setOverallocationChoice(null);
    setAllowLockedOverallocation(false);
    createRetry.current = null;
    if (!segmentId) {
      setLoading(false);
      setSegment(null);
      setForm(demand ? formFromDemand(demand, resources) : null);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setSegment(null);
    setForm(null);
    getSegment(segmentId, controller.signal)
      .then((row) => {
        setSegment(row);
        setForm(formFromSegment(row));
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, segmentId, demand, resources]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    getCompetencies("", false, controller.signal)
      .then(setCompetencies)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      });
    return () => controller.abort();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving && !cancelling) onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, saving, cancelling, onClose]);

  const sortedResources = useMemo(
    () => [...resources].sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name, "fr-CA")),
    [resources],
  );

  if (!open) return null;

  const effectiveDemandNumber = segment?.demand_number ?? demand?.number ?? null;
  const effectiveProjectNumber = segment?.project_number ?? demand?.project_number ?? null;
  const effectiveProjectName = segment?.project_name ?? demand?.project_name ?? null;
  const editingCanonicalAdHoc = Boolean(
    segmentId
    && segment
    && !effectiveDemandNumber
    && isCanonicalAdHocSegment(segment),
  );
  const busy = loading || saving || cancelling;
  const planningSegment = segment as PlanningSegment | null;
  const currentExcess = Number(planningSegment?.overallocated_hours ?? 0);

  function setField<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((current) => current ? { ...current, [field]: value } : current);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form || busy) return;
    setError(null);

    const plannedHours = Number(form.planned_hours.replace(",", "."));
    if (!effectiveDemandNumber && !editingCanonicalAdHoc) {
      setError("Une demande est requise, sauf pour un besoin ad hoc canonique existant.");
      return;
    }
    if (!form.start_date || !form.end_date) {
      setError("Les dates de début et de fin sont requises.");
      return;
    }
    if (form.end_date < form.start_date) {
      setError("La date de fin ne peut pas précéder la date de début.");
      return;
    }
    if (!Number.isFinite(plannedHours) || plannedHours <= 0) {
      setError("Les heures prévues doivent être supérieures à zéro.");
      return;
    }

    const selectedTargetResourceId = form.technician.trim();
    const editablePayload: SegmentUpdateWrite = {
      technician: segmentId
        ? selectedTargetResourceId
          ? segment?.resource_name ?? null
          : null
        : null,
      start_date: form.start_date,
      end_date: form.end_date,
      planned_hours: plannedHours,
      status: form.status.trim() || "À assigner",
      description: form.description.trim(),
      required_competency: null,
      required_competency_id: form.required_competency_id || null,
      planning_type: form.planning_type.trim() || "Flexible",
      priority: form.priority.trim() || "Normale",
      outside_standard_hours: form.outside_standard_hours,
      confirmation: form.confirmation === "inherit" ? null : form.confirmation,
      load_profile: form.load_profile,
    };

    setSaving(true);
    try {
      let savedSegmentId = segmentId;
      if (segmentId) {
        await updateSegmentWithOverallocation(
          segmentId,
          editablePayload,
          allowLockedOverallocation,
        );
      } else {
        if (!effectiveDemandNumber) {
          setError("Une demande est requise pour créer un segment.");
          return;
        }
        const createPayload: SegmentWrite = {
          demand_number: effectiveDemandNumber,
          project_number: effectiveProjectNumber,
          project_name: effectiveProjectName,
          source_effort_id: null,
          ...editablePayload,
          technician: null,
          start_date: form.start_date,
          end_date: form.end_date,
          planned_hours: plannedHours,
          status: form.status.trim() || "À assigner",
          description: form.description.trim(),
          planning_type: form.planning_type.trim() || "Flexible",
          priority: form.priority.trim() || "Normale",
          outside_standard_hours: form.outside_standard_hours,
          load_profile: form.load_profile,
        };
        const fingerprint = JSON.stringify(createPayload);
        const previous = createRetry.current;
        const key = previous?.fingerprint === fingerprint ? previous.key : newIdempotencyKey();
        createRetry.current = { fingerprint, key };
        const result = await createSegment(createPayload, key);
        savedSegmentId = result.segment_id;
        createRetry.current = null;
      }

      const targetChanged = Boolean(selectedTargetResourceId)
        && selectedTargetResourceId !== (segment?.automatic_target_resource_id ?? "");
      if (savedSegmentId && targetChanged) {
        await assignSegment(savedSegmentId, selectedTargetResourceId);
      }
      setAllowLockedOverallocation(false);
      setOverallocationChoice(null);
      onSaved();
    } catch (reason: unknown) {
      const context = overallocationContext(reason);
      if (
        reason instanceof OverallocationApiError
        && reason.code === "segment_overallocation_choice_required"
        && context
      ) {
        setOverallocationChoice(context);
        setAllowLockedOverallocation(false);
        setError(null);
      } else {
        setError(messageFromError(reason));
      }
    } finally {
      setSaving(false);
    }
  }

  async function cancelCurrentSegment() {
    if (!segmentId || busy) return;
    if (!window.confirm("Annuler ce segment? Le planning sera recalculé par le backend.")) return;
    setCancelling(true);
    setError(null);
    try {
      await cancelSegment(segmentId);
      onSaved();
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section className="segment-dialog" role="dialog" aria-modal="true" aria-labelledby="segment-dialog-title">
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Besoin ressource</span>
            <h2 id="segment-dialog-title">{segmentId ? "Modifier le segment" : "Créer un segment"}</h2>
            <p>
              {effectiveDemandNumber
                ? `Demande ${effectiveDemandNumber}`
                : editingCanonicalAdHoc
                  ? "Besoin ad hoc sans demande"
                  : "Demande non définie"}
              {effectiveProjectNumber ? ` · ${effectiveProjectNumber}` : ""}
              {effectiveProjectName ? ` — ${effectiveProjectName}` : ""}
            </p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={busy} aria-label="Fermer">×</button>
        </div>

        {loading && !form ? <div className="segment-loading">Chargement du segment…</div> : form ? (
          <form className="segment-form" onSubmit={submit}>
            {error && <div className="dialog-error">{error}</div>}

            {currentExcess > 0 && (
              <div className="overallocation-warning" role="status">
                <strong>⚠ Surallocation manuelle active : +{hoursLabel(currentExcess)} h</strong>
                <span>{hoursLabel(planningSegment?.locked_hours)} h verrouillées pour {hoursLabel(segment?.planned_hours)} h prévues. Les quarts verrouillés sont préservés par le moteur.</span>
              </div>
            )}

            {overallocationChoice && (
              <div className="overallocation-choice" role="alert">
                <strong>Cette réduction créerait ou augmenterait une surallocation manuelle.</strong>
                <span>
                  Heures prévues proposées : {hoursLabel(overallocationChoice.planned_hours)} h · quarts verrouillés : {hoursLabel(overallocationChoice.locked_hours)} h · excédent : +{hoursLabel(overallocationChoice.excess_hours)} h.
                </span>
                <div className="overallocation-choice-actions">
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => {
                      setField("planned_hours", String(overallocationChoice.locked_hours ?? overallocationChoice.planned_hours));
                      setOverallocationChoice(null);
                      setAllowLockedOverallocation(false);
                    }}
                  >
                    Ajuster les heures prévues à {hoursLabel(overallocationChoice.locked_hours)} h
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => {
                      setAllowLockedOverallocation(true);
                      setOverallocationChoice(null);
                    }}
                  >
                    Conserver la dérogation (+{hoursLabel(overallocationChoice.excess_hours)} h)
                  </button>
                </div>
                <small>Après avoir choisi la dérogation, clique sur Enregistrer pour la confirmer et la journaliser.</small>
              </div>
            )}

            {allowLockedOverallocation && (
              <div className="overallocation-warning">
                <strong>Dérogation de surallocation sélectionnée</strong>
                <span>L'enregistrement conservera les heures prévues saisies même si elles sont inférieures aux quarts verrouillés.</span>
              </div>
            )}

            <div className="segment-context-grid">
              <div><span>Segment</span><strong>{segmentId || "Nouveau"}</strong></div>
              <div><span>Demande</span><strong>{effectiveDemandNumber || "—"}</strong></div>
              <div><span>Projet</span><strong>{effectiveProjectNumber || "—"}</strong></div>
              <div><span>Origine</span><strong>{segment?.origin || "Demande"}</strong></div>
            </div>

            <div className="segment-form-grid">
              <label>
                <span>Date de début</span>
                <input type="date" value={form.start_date} onChange={(event) => setField("start_date", event.target.value)} required />
              </label>
              <label>
                <span>Date de fin</span>
                <input type="date" value={form.end_date} onChange={(event) => setField("end_date", event.target.value)} required />
              </label>
              <label>
                <span>Heures prévues</span>
                <input type="number" min="0.25" step="0.25" value={form.planned_hours} onChange={(event) => {
                  setField("planned_hours", event.target.value);
                  setOverallocationChoice(null);
                  setAllowLockedOverallocation(false);
                }} required />
              </label>
              <label>
                <span>Statut</span>
                <input value={form.status} onChange={(event) => setField("status", event.target.value)} />
              </label>
              <label>
                <span>Type de planification</span>
                <select value={form.planning_type} onChange={(event) => setField("planning_type", event.target.value)}>
                  {!['Flexible', 'Fixe'].includes(form.planning_type) && <option value={form.planning_type}>{form.planning_type}</option>}
                  <option value="Flexible">Flexible</option>
                  <option value="Fixe">Fixe</option>
                </select>
              </label>
              <label>
                <span>Profil de charge</span>
                <select value={form.load_profile} onChange={(event) => setField("load_profile", event.target.value as LoadProfile)}>
                  <option value="UNIFORM">Uniforme — charge régulière</option>
                  <option value="FRONT_LOADED">Début — concentrer le travail tôt</option>
                  <option value="BACK_LOADED">Fin — concentrer le travail tard</option>
                  <option value="BELL">En cloche — pic au milieu</option>
                </select>
              </label>
              <label>
                <span>Priorité</span>
                <select value={form.priority} onChange={(event) => setField("priority", event.target.value)}>
                  {!['Basse', 'Normale', 'Haute', 'Urgente'].includes(form.priority) && <option value={form.priority}>{form.priority}</option>}
                  <option value="Basse">Basse</option>
                  <option value="Normale">Normale</option>
                  <option value="Haute">Haute</option>
                  <option value="Urgente">Urgente</option>
                </select>
              </label>
              <CompetencyPicker
                competencies={competencies}
                selectedIds={form.required_competency_id ? [form.required_competency_id] : []}
                onChange={(ids) => setField("required_competency_id", ids[0] ?? "")}
                disabled={busy}
                multiple={false}
                label="Compétence requise"
                placeholder="Rechercher une compétence…"
              />
              {!form.required_competency_id && segment?.required_competency && (
                <small className="legacy-competency-note">
                  Valeur historique à convertir au catalogue : {segment.required_competency}
                </small>
              )}
              <label>
                <span>Confirmation</span>
                <select value={form.confirmation} onChange={(event) => setField("confirmation", event.target.value as ConfirmationChoice)}>
                  {!editingCanonicalAdHoc && <option value="inherit">Héritée de la demande</option>}
                  <option value="Tentative">Tentative</option>
                  <option value="Confirmée">Confirmée</option>
                </select>
              </label>
              <label className="segment-assignment-field">
                <span>Cible automatique du reliquat</span>
                <select value={form.technician} onChange={(event) => setField("technician", event.target.value)}>
                  <option value="">Aucune cible automatique</option>
                  {segment?.automatic_target_resource_id && !resources.some((resource) => resource.id === segment.automatic_target_resource_id) && (
                    <option value={segment.automatic_target_resource_id}>
                      {segment.automatic_target_resource_name || segment.resource_name || segment.automatic_target_resource_id} — inactive/inconnue
                    </option>
                  )}
                  {sortedResources.map((resource) => (
                    <option value={resource.id} key={resource.id}>
                      {resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <label className="checkbox-field segment-outside-field">
                <input type="checkbox" checked={form.outside_standard_hours} onChange={(event) => setField("outside_standard_hours", event.target.checked)} />
                <span>Travail autorisé hors horaire standard</span>
              </label>
              <label className="segment-description-field">
                <span>Description</span>
                <textarea rows={4} value={form.description} onChange={(event) => setField("description", event.target.value)} />
              </label>
            </div>

            <div className="segment-help">
              <strong>Segment = besoin ressource; quart = affectation opérationnelle datée.</strong>
              <span>Le profil de charge influence seulement le reliquat automatique : les quarts manuels/verrouillés restent prioritaires, et la capacité/disponibilité demeure autoritaire.</span>
              <span>La cible automatique choisie ici reçoit le reliquat régénéré. Elle ne remplace pas les ressources réelles déjà portées par les quarts verrouillés.</span>
            </div>

            {segmentId && <PlanningHistoryPanel entityType="SEGMENT" reference={segmentId} />}

            <div className="dialog-actions segment-dialog-actions">
              {segmentId && segment?.status !== "Annulé" && (
                <button type="button" className="danger-button" onClick={cancelCurrentSegment} disabled={busy}>
                  {cancelling ? "Annulation…" : "Annuler le segment"}
                </button>
              )}
              <span className="dialog-action-spacer" />
              <button type="button" className="secondary-button" onClick={onClose} disabled={busy}>Fermer</button>
              <button type="submit" className="primary-button" disabled={busy}>
                {saving ? "Enregistrement…" : segmentId ? "Enregistrer" : "Créer le segment"}
              </button>
            </div>
          </form>
        ) : (
          <div className="dialog-error">{error || "Impossible d'initialiser l'éditeur de segment."}</div>
        )}
      </section>
    </div>
  );
}
