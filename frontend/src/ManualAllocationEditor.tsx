import { FormEvent, useEffect, useMemo, useState } from "react";

import { createClientId } from "./clientId";
import {
  ManualAllocationUpdate,
  ResourceReadModel,
  SegmentReadModel,
} from "./api";
import { useAuth } from "./AuthContext";
import {
  OverallocationApiError,
  OverallocationContext,
  OverallocationPolicy,
  createManualAllocationWithOverallocation,
  overallocationContext,
} from "./manualOverallocationApi";

type ConfirmationChoice = "inherit" | "Tentative" | "Confirmée";

function newIdempotencyKey() {
  return createClientId();
}

function maxIso(left: string, right: string) {
  return left > right ? left : right;
}

function minIso(left: string, right: string) {
  return left < right ? left : right;
}

function hoursLabel(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(Number(value ?? 0));
}

export default function ManualAllocationEditor({
  open,
  segments,
  resources,
  weekStart,
  weekEnd,
  initialSegmentId,
  onClose,
  onSaved,
}: {
  open: boolean;
  segments: SegmentReadModel[];
  resources: ResourceReadModel[];
  weekStart: string;
  weekEnd: string;
  initialSegmentId?: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { can } = useAuth();
  const canManagePlanning = can("manage_planning");
  const activeSegments = useMemo(
    () => segments.filter((segment) => !["Annulé", "Terminé"].includes(segment.status)),
    [segments],
  );
  const sortedResources = useMemo(
    () => [...resources].sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name, "fr-CA")),
    [resources],
  );

  const [segmentId, setSegmentId] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [day, setDay] = useState(weekStart);
  const [hours, setHours] = useState("8");
  const [outsideStandardHours, setOutsideStandardHours] = useState(false);
  const [confirmation, setConfirmation] = useState<ConfirmationChoice>("inherit");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overallocationChoice, setOverallocationChoice] = useState<OverallocationContext | null>(null);

  const selectedSegment = useMemo(
    () => activeSegments.find((segment) => segment.segment_id === segmentId) ?? null,
    [activeSegments, segmentId],
  );

  useEffect(() => {
    if (!open) return;
    const preferred = activeSegments.find((segment) => segment.segment_id === initialSegmentId)
      ?? activeSegments[0]
      ?? null;
    setSegmentId(preferred?.segment_id ?? "");
    setResourceId(preferred?.automatic_target_resource_id ?? "");
    const minDay = preferred?.start_date ? maxIso(preferred.start_date, weekStart) : weekStart;
    const maxDay = preferred?.end_date ? minIso(preferred.end_date, weekEnd) : weekEnd;
    setDay(minDay <= maxDay ? minDay : (preferred?.start_date ?? weekStart));
    setHours(String(Math.min(Number(preferred?.planned_hours ?? 8), 8) || 8));
    setOutsideStandardHours(false);
    setConfirmation("inherit");
    setNote("");
    setError(null);
    setOverallocationChoice(null);
  }, [open, initialSegmentId, activeSegments, weekStart, weekEnd]);

  useEffect(() => {
    if (!open || !selectedSegment) return;
    if (!resourceId && selectedSegment.automatic_target_resource_id) {
      setResourceId(selectedSegment.automatic_target_resource_id);
    }
    const minDay = selectedSegment.start_date ? maxIso(selectedSegment.start_date, weekStart) : weekStart;
    const maxDay = selectedSegment.end_date ? minIso(selectedSegment.end_date, weekEnd) : weekEnd;
    if (day < minDay || day > maxDay) {
      setDay(minDay <= maxDay ? minDay : (selectedSegment.start_date ?? weekStart));
    }
  }, [selectedSegment, open, weekStart, weekEnd, day, resourceId]);

  useEffect(() => {
    if (!open || !canManagePlanning) return;
    const previousOverflow = document.body.style.overflow;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", listener);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", listener);
    };
  }, [open, canManagePlanning, saving, onClose]);

  if (!open || !canManagePlanning) return null;

  async function create(policy: OverallocationPolicy | null = null) {
    if (!selectedSegment || saving) return;
    const parsedHours = Number(hours);
    if (!resourceId.trim()) {
      setError("Choisis une ressource pour ce quart.");
      return;
    }
    if (!day) {
      setError("Choisis une date.");
      return;
    }
    if (!Number.isFinite(parsedHours) || parsedHours <= 0) {
      setError("Les heures doivent être supérieures à zéro.");
      return;
    }

    const payload: ManualAllocationUpdate = {
      resource_id: resourceId,
      day,
      hours: parsedHours,
      outside_standard_hours: outsideStandardHours,
      note: note.trim(),
      confirmation: confirmation === "inherit" ? null : confirmation,
    };

    setSaving(true);
    setError(null);
    try {
      await createManualAllocationWithOverallocation(
        selectedSegment.segment_id,
        payload,
        newIdempotencyKey(),
        policy,
      );
      setOverallocationChoice(null);
      onSaved();
    } catch (reason: unknown) {
      const context = overallocationContext(reason);
      if (
        reason instanceof OverallocationApiError
        && reason.code === "allocation_overallocation_choice_required"
        && context
      ) {
        setOverallocationChoice(context);
        setError(null);
      } else if (reason instanceof Error) {
        setOverallocationChoice(null);
        setError(reason.message);
      } else {
        setOverallocationChoice(null);
        setError("Impossible de créer le quart manuel.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await create(null);
  }

  const minDate = selectedSegment?.start_date ?? weekStart;
  const maxDate = selectedSegment?.end_date ?? weekEnd;

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <section className="shift-dialog manual-allocation-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-allocation-title">
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Planning manuel</span>
            <h2 id="manual-allocation-title">Créer un quart manuel</h2>
            <p>Le quart reste rattaché à un segment existant et sera verrouillé comme décision opérationnelle explicite.</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={saving} aria-label="Fermer">×</button>
        </div>

        <form className="shift-edit-form" onSubmit={submit}>
          <div className="info-banner">
            Ce n’est pas un Quick Shift : aucun nouveau besoin ad hoc n’est créé. Le backend valide la fenêtre du segment, la disponibilité, le hors horaire et la surallocation.
          </div>

          {error && <div className="dialog-error" role="alert">{error}</div>}

          {overallocationChoice && (
            <div className="overallocation-choice" role="alert">
              <strong>Ce quart dépasserait les heures prévues du segment.</strong>
              <span>
                Planifié : {hoursLabel(overallocationChoice.planned_hours)} h · excédent projeté : +{hoursLabel(overallocationChoice.excess_hours)} h.
              </span>
              <div className="overallocation-choice-actions">
                <button type="button" className="primary-button" disabled={saving} onClick={() => void create("INCREASE_PLANNED")}>
                  Augmenter les heures prévues
                </button>
                <button type="button" className="secondary-button" disabled={saving} onClick={() => void create("KEEP_EXCEPTION")}>
                  Conserver la dérogation
                </button>
              </div>
            </div>
          )}

          <div className="dialog-form-grid">
            <label className="span-2">
              <span>Segment</span>
              <select value={segmentId} onChange={(event) => { setSegmentId(event.target.value); setOverallocationChoice(null); }} disabled={saving} required autoFocus>
                <option value="">Sélectionner un segment…</option>
                {activeSegments.map((segment) => (
                  <option value={segment.segment_id} key={segment.segment_id}>
                    {segment.segment_id} — {segment.project_number || "Projet"} · {segment.description || segment.demand_number || "Besoin ressource"}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Ressource</span>
              <select value={resourceId} onChange={(event) => setResourceId(event.target.value)} disabled={saving} required>
                <option value="">Sélectionner…</option>
                {sortedResources.map((resource) => (
                  <option value={resource.id} key={resource.id}>
                    {resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Date</span>
              <input type="date" min={minDate || undefined} max={maxDate || undefined} value={day} onChange={(event) => setDay(event.target.value)} disabled={saving} required />
            </label>
            <label>
              <span>Heures</span>
              <input type="number" min="0.25" step="0.25" value={hours} onChange={(event) => { setHours(event.target.value); setOverallocationChoice(null); }} disabled={saving} required />
            </label>
            <label>
              <span>Confirmation</span>
              <select value={confirmation} onChange={(event) => setConfirmation(event.target.value as ConfirmationChoice)} disabled={saving}>
                <option value="inherit">Héritée du segment</option>
                <option value="Tentative">Tentative</option>
                <option value="Confirmée">Confirmée</option>
              </select>
            </label>
            <label className="checkbox-field span-2">
              <input type="checkbox" checked={outsideStandardHours} onChange={(event) => setOutsideStandardHours(event.target.checked)} disabled={saving} />
              <span>Autoriser explicitement ce quart hors horaire standard</span>
            </label>
            <label className="span-2">
              <span>Note</span>
              <textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} disabled={saving} />
            </label>
          </div>

          {selectedSegment && (
            <div className="confirmation-help">
              <strong>{selectedSegment.project_number || "Projet"} · {selectedSegment.segment_id}</strong>
              <span>
                Cible automatique du reliquat : {selectedSegment.automatic_target_resource_name || "aucune"}.
                La ressource choisie ici s’applique uniquement au quart manuel.
              </span>
              <span>
                Fenêtre {selectedSegment.start_date || "—"} → {selectedSegment.end_date || "—"} · {hoursLabel(selectedSegment.planned_hours)} h prévues
                {selectedSegment.required_competency ? ` · ${selectedSegment.required_competency}` : ""}
              </span>
            </div>
          )}

          <div className="dialog-actions">
            <button type="button" className="secondary-button" onClick={onClose} disabled={saving}>Annuler</button>
            <button type="submit" className="primary-button" disabled={saving || !selectedSegment}>
              {saving ? "Création…" : "Créer et verrouiller"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
