import { resourceDisplayName } from "./resourceLabels";
import { FormEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";

import { createClientId } from "./clientId";
import { useAuth } from "./AuthContext";
import SearchableCombobox from "./SearchableCombobox";
import {
  ApiError,
  ProjectReadModel,
  QuickShiftCreate,
  ResourceReadModel,
  createQuickShift,
  getProjects,
  getResources,
} from "./api";

type Props = {
  open: boolean;
  weekStart: string;
  weekEnd: string;
  defaultDay: string;
  initialProjectNumber?: string | null;
  initialResourceId?: string | null;
  onClose: () => void;
  onSaved: (created: { day: string; resourceName: string }) => void;
};

type RetryReceipt = {
  fingerprint: string;
  key: string;
};

function newIdempotencyKey() {
  return createClientId();
}

function errorMessage(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible de créer le Quick Shift.";
}

export default function QuickShiftEditor({
  open,
  defaultDay,
  initialProjectNumber,
  initialResourceId,
  onClose,
  onSaved,
}: Props) {
  const { can } = useAuth();
  const canManagePlanning = can("manage_planning");
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [resources, setResources] = useState<ResourceReadModel[]>([]);
  const [loadingChoices, setLoadingChoices] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [day, setDay] = useState(defaultDay);
  const [hours, setHours] = useState("8");
  const [confirmation, setConfirmation] = useState<"Tentative" | "Confirmée">("Confirmée");
  const [outsideStandardHours, setOutsideStandardHours] = useState(false);
  const [description, setDescription] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retryReceipt = useRef<RetryReceipt | null>(null);

  useEffect(() => {
    if (!open || !canManagePlanning) return;
    const controller = new AbortController();
    setLoadingChoices(true);
    setError(null);
    setProjectId("");
    setResourceId("");
    setDay(defaultDay);
    setHours("8");
    setConfirmation("Confirmée");
    setOutsideStandardHours(false);
    setDescription("");
    setNote("");
    retryReceipt.current = null;

    Promise.all([
      getProjects(true, controller.signal),
      getResources(true, controller.signal),
    ])
      .then(([projectRows, resourceRows]) => {
        const sortedProjects = [...projectRows].sort((left, right) =>
          `${left.number} ${left.name}`.localeCompare(`${right.number} ${right.name}`, "fr-CA"),
        );
        const sortedResources = [...resourceRows].sort((left, right) =>
          left.sort_order - right.sort_order || left.name.localeCompare(right.name, "fr-CA"),
        );
        setProjects(sortedProjects);
        setResources(sortedResources);
        setProjectId(
          initialProjectNumber
            ? sortedProjects.find((row) => row.number === initialProjectNumber)?.id ?? ""
            : "",
        );
        setResourceId(
          initialResourceId
            ? sortedResources.find((row) => row.id === initialResourceId)?.id ?? ""
            : "",
        );
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingChoices(false);
      });

    return () => controller.abort();
  }, [open, canManagePlanning, defaultDay, initialProjectNumber, initialResourceId]);

  useEffect(() => {
    if (!open || !canManagePlanning) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [open, canManagePlanning, saving, onClose]);

  const selectedProject = useMemo(
    () => projects.find((row) => row.id === projectId) ?? null,
    [projects, projectId],
  );

  const selectedResource = useMemo(
    () => resources.find((row) => row.id === resourceId) ?? null,
    [resources, resourceId],
  );

  if (!open || !canManagePlanning) return null;

  function closeFromBackdrop(event: MouseEvent<HTMLDivElement>) {
    if (!saving && event.target === event.currentTarget) onClose();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setError(null);

    const numericHours = Number(hours);
    if (!selectedProject) {
      setError("Sélectionne un projet actif.");
      return;
    }
    if (!selectedResource) {
      setError("Sélectionne un technicien actif.");
      return;
    }
    if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day)) {
      setError("Sélectionne une date valide.");
      return;
    }
    if (!Number.isFinite(numericHours) || numericHours <= 0) {
      setError("Les heures doivent être supérieures à zéro.");
      return;
    }

    const payload: QuickShiftCreate = {
      project_number: selectedProject.number,
      project_name: selectedProject.name,
      technician: selectedResource.name,
      day,
      hours: numericHours,
      confirmation,
      outside_standard_hours: outsideStandardHours,
      description: description.trim(),
      note: note.trim(),
    };
    const fingerprint = JSON.stringify(payload);
    const previous = retryReceipt.current;
    const idempotencyKey = previous?.fingerprint === fingerprint
      ? previous.key
      : newIdempotencyKey();
    retryReceipt.current = { fingerprint, key: idempotencyKey };

    setSaving(true);
    try {
      await createQuickShift(payload, idempotencyKey);
      retryReceipt.current = null;
      onSaved({ day, resourceName: selectedResource.name });
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={closeFromBackdrop}>
      <section className="shift-dialog quick-shift-dialog" role="dialog" aria-modal="true" aria-labelledby="quick-shift-title">
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Ajout opérationnel</span>
            <h2 id="quick-shift-title">Créer un Quick Shift</h2>
            <p>Ajoute directement un besoin ad hoc et un quart verrouillé, sans créer de demande fictive.</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} disabled={saving} aria-label="Fermer">×</button>
        </div>

        <form className="shift-edit-form" onSubmit={submit}>
          <div className="info-banner">
            Le projet et la ressource sont lus depuis les référentiels FastAPI. Le backend reste autoritaire pour la disponibilité, les heures et le hors horaire. Une reprise réseau réutilise la même clé d’idempotence pour éviter un doublon.
          </div>

          {error && <div className="dialog-error" role="alert">{error}</div>}

          {selectedProject && (
            <div className="dialog-context-grid quick-shift-context">
              <div><span>Projet</span><strong>{selectedProject.number}</strong></div>
              <div><span>Nom</span><strong>{selectedProject.name || "—"}</strong></div>
              <div><span>Client</span><strong>{selectedProject.client || "—"}</strong></div>
              <div><span>Responsable</span><strong>{selectedProject.project_manager || "—"}</strong></div>
            </div>
          )}

          <div className="dialog-form-grid">
            <label>
              <span>Projet</span>
              <SearchableCombobox
                value={projectId || null}
                options={projects.map((row) => ({
                  value: row.id,
                  label: `${row.number} — ${row.name}`,
                  searchText: [row.number, row.name, row.client].filter(Boolean).join(" "),
                }))}
                onChange={(value) => setProjectId(value ?? "")}
                label="Projet"
                placeholder="Sélectionner un projet…"
                disabled={saving || loadingChoices}
                loading={loadingChoices}
                required
                autoFocus
              />
            </label>
            <label>
              <span>Technicien</span>
              <SearchableCombobox
                value={resourceId || null}
                options={resources.map((row) => ({
                  value: row.id,
                  label: `${resourceDisplayName(row.name)}${row.resource_class ? ` — ${row.resource_class}` : ""}`,
                  searchText: [row.name, row.resource_class, row.competencies].filter(Boolean).join(" "),
                }))}
                onChange={(value) => setResourceId(value ?? "")}
                label="Technicien"
                placeholder="Sélectionner une ressource…"
                disabled={saving || loadingChoices}
                loading={loadingChoices}
                required
              />
            </label>
            <label><span>Date</span><input type="date" value={day} onChange={(event) => setDay(event.target.value)} disabled={saving} required /></label>
            <label><span>Heures</span><input type="number" min="0.25" step="0.25" value={hours} onChange={(event) => setHours(event.target.value)} disabled={saving} required /></label>
            <label><span>Confirmation</span><select value={confirmation} onChange={(event) => setConfirmation(event.target.value as "Tentative" | "Confirmée")} disabled={saving}><option value="Confirmée">Confirmée</option><option value="Tentative">Tentative</option></select></label>
            <label className="checkbox-field"><input type="checkbox" checked={outsideStandardHours} onChange={(event) => setOutsideStandardHours(event.target.checked)} disabled={saving} /><span>Autoriser ce quart en dehors de l’horaire standard</span></label>
            <label className="span-2"><span>Description</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} disabled={saving} rows={2} placeholder="Travail à effectuer…" /></label>
            <label className="span-2"><span>Note</span><textarea value={note} onChange={(event) => setNote(event.target.value)} disabled={saving} rows={2} placeholder="Note opérationnelle facultative…" /></label>
          </div>

          {selectedResource && <div className="confirmation-help"><strong>{resourceDisplayName(selectedResource.name)}</strong><span>{[selectedResource.resource_class, selectedResource.competencies].filter(Boolean).join(" · ") || "Ressource active"}</span></div>}

          <div className="dialog-actions">
            <button className="secondary-button" type="button" onClick={onClose} disabled={saving}>Annuler</button>
            <button className="primary-button" type="submit" disabled={saving || loadingChoices}>{saving ? "Création…" : loadingChoices ? "Chargement…" : "Créer le Quick Shift"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}
