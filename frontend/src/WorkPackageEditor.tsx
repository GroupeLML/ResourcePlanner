import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  MediumTermBudgetWorkPackageReadModel,
  ProjectReadModel,
  TaskCatalogItemReadModel,
  WorkPackageReadModel,
  WorkPackageWeeklyLoadWrite,
  WorkPackageWrite,
  createWorkPackage,
  getTaskCatalog,
  proposeWorkPackageWeeklyLoads,
  replaceWorkPackageWeeklyLoads,
  updateWorkPackage,
} from "./api";

const STATUS_OPTIONS = [
  ["planned", "Planifié"],
  ["active", "Actif"],
  ["closed", "Fermé"],
  ["cancelled", "Annulé"],
] as const;

type FormState = {
  projectNumber: string;
  taskCatalogItemId: string;
  code: string;
  name: string;
  description: string;
  startDate: string;
  endDate: string;
  plannedHours: string;
  status: string;
};

type WeeklyDraftRow = {
  week_start: string;
  hours: string;
};

function initialState(
  workPackage: WorkPackageReadModel | null,
  defaultProjectNumber: string,
): FormState {
  return {
    projectNumber: workPackage?.project_number || defaultProjectNumber,
    taskCatalogItemId: workPackage?.task_catalog_item_id || "",
    code: workPackage?.code || "",
    name: workPackage?.name || "",
    description: workPackage?.description || "",
    startDate: workPackage?.start_date || "",
    endDate: workPackage?.end_date || "",
    plannedHours: workPackage?.planned_hours == null ? "" : String(workPackage.planned_hours),
    status: workPackage?.status || "planned",
  };
}

function toPayload(form: FormState): WorkPackageWrite {
  const hours = form.plannedHours.trim();
  return {
    project_number: form.projectNumber,
    task_catalog_item_id: form.taskCatalogItemId.trim() || null,
    code: form.code.trim() || null,
    name: form.name.trim(),
    description: form.description.trim() || null,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    planned_hours: hours ? Number(hours.replace(",", ".")) : null,
    status: form.status,
  };
}

function weeklyRows(
  workPackage: MediumTermBudgetWorkPackageReadModel | null | undefined,
): WeeklyDraftRow[] {
  return (workPackage?.weekly_loads ?? []).map((row) => ({
    week_start: row.week_start,
    hours: String(row.hours),
  }));
}

function weeklyFingerprint(rows: WeeklyDraftRow[]) {
  return JSON.stringify(rows.map((row) => ({
    week_start: row.week_start,
    hours: row.hours.trim().replace(",", "."),
  })));
}

function weeklyPayload(rows: WeeklyDraftRow[]): WorkPackageWeeklyLoadWrite[] {
  return rows.map((row) => ({
    week_start: row.week_start,
    hours: Number(row.hours.trim().replace(",", ".")),
  }));
}

function weeklyDiagnosticLabel(code: string | null | undefined) {
  switch (code) {
    case "WEEKLY_LOAD_MISSING":
      return "Aucune répartition hebdomadaire validée.";
    case "WEEKLY_LOAD_DATES_MISSING":
      return "Les dates du WorkPackage sont requises pour répartir la charge.";
    case "WEEKLY_LOAD_TOTAL_UNKNOWN":
      return "La charge totale du WorkPackage est requise pour répartir la charge.";
    case "WEEKLY_LOAD_INCONSISTENT":
      return "La répartition persistée est incohérente et doit être revue.";
    default:
      return code || "Répartition valide.";
  }
}

function apiMessage(reason: ApiError) {
  switch (reason.code) {
    case "work_package_version_conflict":
      return "Conflit de version : ce WorkPackage a changé. Recharge les données avant de réessayer; aucune modification concurrente n’a été écrasée.";
    case "work_package_weekly_load_replan_required":
      return "Cette modification de dates ou de charge rendrait la répartition existante incohérente. Une nouvelle répartition explicite est requise; aucune redistribution automatique n’a été faite.";
    case "work_package_weekly_load_dates_required":
      return "Début et fin sont requis pour générer ou enregistrer une répartition.";
    case "work_package_weekly_load_planned_hours_required":
      return "La charge totale est requise pour générer ou enregistrer une répartition.";
    case "work_package_weekly_load_total_mismatch":
      return "La somme hebdomadaire ne correspond pas à la charge totale. Ajuste la répartition puis réessaie.";
    case "work_package_weekly_load_week_start_invalid":
      return "Chaque semaine doit être identifiée par son lundi.";
    case "work_package_weekly_load_outside_window":
      return "Une semaine se trouve hors de la fenêtre du WorkPackage.";
    case "work_package_weekly_load_duplicate_week":
      return "Une même semaine apparaît plusieurs fois dans la répartition.";
    case "work_package_weekly_load_auto_proposal_mismatch":
      return "La proposition AUTO a été modifiée. Enregistre-la comme répartition manuelle.";
    default:
      return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
}

export default function WorkPackageEditor({
  projects,
  workPackage,
  mediumTermWorkPackage,
  defaultProjectNumber,
  onClose,
  onSaved,
  onReload,
}: {
  projects: ProjectReadModel[];
  workPackage: WorkPackageReadModel | null;
  mediumTermWorkPackage?: MediumTermBudgetWorkPackageReadModel | null;
  defaultProjectNumber: string;
  onClose: () => void;
  onSaved: (projectNumber?: string) => void;
  onReload?: () => void;
}) {
  const [form, setForm] = useState(() => initialState(workPackage, defaultProjectNumber));
  const [saving, setSaving] = useState(false);
  const [tasks, setTasks] = useState<TaskCatalogItemReadModel[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const createRetry = useRef<{ fingerprint: string; key: string } | null>(null);
  const editing = Boolean(workPackage);

  const [weeklyDraft, setWeeklyDraft] = useState<WeeklyDraftRow[]>(() => weeklyRows(mediumTermWorkPackage));
  const [weeklyOrigin, setWeeklyOrigin] = useState<"AUTO" | "MANUAL" | null>(
    mediumTermWorkPackage?.weekly_load_origin ?? null,
  );
  const [proposalFingerprint, setProposalFingerprint] = useState<string | null>(null);
  const [proposalVersion, setProposalVersion] = useState<number | null>(null);
  const [weeklyDirty, setWeeklyDirty] = useState(false);
  const [weeklyBusy, setWeeklyBusy] = useState(false);
  const [weeklyError, setWeeklyError] = useState<string | null>(null);
  const [weeklyConflict, setWeeklyConflict] = useState(false);
  const weeklyRetry = useRef<{ fingerprint: string; key: string } | null>(null);

  useEffect(() => {
    setForm(initialState(workPackage, defaultProjectNumber));
    setError(null);
    createRetry.current = null;
  }, [workPackage, defaultProjectNumber]);

  useEffect(() => {
    setWeeklyDraft(weeklyRows(mediumTermWorkPackage));
    setWeeklyOrigin(mediumTermWorkPackage?.weekly_load_origin ?? null);
    setProposalFingerprint(null);
    setProposalVersion(null);
    setWeeklyDirty(false);
    setWeeklyError(null);
    setWeeklyConflict(false);
    weeklyRetry.current = null;
  }, [mediumTermWorkPackage, workPackage?.reference]);

  const projectOptions = useMemo(
    () => [...projects].sort((left, right) => left.number.localeCompare(right.number, "fr-CA")),
    [projects],
  );

  useEffect(() => {
    const projectNumber = form.projectNumber.trim();
    if (!projectNumber) {
      setTasks([]);
      return;
    }
    const controller = new AbortController();
    setTasksLoading(true);
    getTaskCatalog(projectNumber, "", true, controller.signal)
      .then((rows) => {
        const current = workPackage?.task_catalog_item_id
          && workPackage.project_number === projectNumber
          && !rows.some((row) => row.id === workPackage.task_catalog_item_id)
          ? [{
              id: workPackage.task_catalog_item_id,
              project_number: projectNumber,
              code: workPackage.task_code || "",
              label: workPackage.task_label || "Tâche actuelle",
              status: "Actuel",
              active: true,
              billing_rule: null,
              allocation_rule: null,
              completion_percent: null,
              erp_created_at: null,
              branch: null,
              approver_name: null,
              cv_enabled: null,
              time_entry_enabled: null,
              expenses_enabled: null,
              operational_responsible_contact_id: null,
              coordinator_contact_id: null,
            } satisfies TaskCatalogItemReadModel]
          : [];
        setTasks([...current, ...rows]);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setTasks([]);
        setError(reason instanceof Error ? reason.message : "Impossible de charger les tâches ERP.");
      })
      .finally(() => setTasksLoading(false));
    return () => controller.abort();
  }, [
    form.projectNumber,
    workPackage?.project_number,
    workPackage?.task_catalog_item_id,
    workPackage?.task_code,
    workPackage?.task_label,
  ]);

  const distributedHours = useMemo(
    () => weeklyDraft.reduce((sum, row) => {
      const value = Number(row.hours.trim().replace(",", "."));
      return Number.isFinite(value) ? sum + value : sum;
    }, 0),
    [weeklyDraft],
  );

  const baseFormDirty = useMemo(() => {
    if (!workPackage) return false;
    const persisted = initialState(workPackage, defaultProjectNumber);
    return JSON.stringify(form) !== JSON.stringify(persisted);
  }, [form, workPackage, defaultProjectNumber]);

  function field<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setError(null);

    const payload = toPayload(form);
    if (!payload.project_number) {
      setError("Choisis un projet.");
      return;
    }
    if (!payload.task_catalog_item_id && !workPackage) {
      setError("Choisis une tâche ERP pour ce WorkPackage.");
      return;
    }
    if (!payload.name) {
      setError("Le nom du WorkPackage est requis.");
      return;
    }
    if (payload.planned_hours != null && (!Number.isFinite(payload.planned_hours) || payload.planned_hours < 0)) {
      setError("Les heures prévues doivent être un nombre positif ou nul.");
      return;
    }
    if (payload.start_date && payload.end_date && payload.end_date < payload.start_date) {
      setError("La date de fin ne peut pas précéder la date de début.");
      return;
    }

    setSaving(true);
    try {
      if (workPackage) {
        await updateWorkPackage(workPackage.reference, payload, workPackage.version);
      } else {
        const fingerprint = JSON.stringify(payload);
        const previous = createRetry.current;
        const key = previous?.fingerprint === fingerprint
          ? previous.key
          : crypto.randomUUID();
        createRetry.current = { fingerprint, key };
        await createWorkPackage(payload, key);
      }
      onSaved(payload.project_number);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(apiMessage(reason));
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible d’enregistrer le WorkPackage.");
      }
    } finally {
      setSaving(false);
    }
  }

  function markManual(update: (rows: WeeklyDraftRow[]) => WeeklyDraftRow[]) {
    setWeeklyDraft((current) => update(current));
    setWeeklyOrigin("MANUAL");
    setWeeklyDirty(true);
    setWeeklyConflict(false);
    setWeeklyError(null);
    weeklyRetry.current = null;
  }

  async function generateProposal() {
    if (!workPackage || weeklyBusy) return;
    setWeeklyBusy(true);
    setWeeklyError(null);
    setWeeklyConflict(false);
    try {
      const proposal = await proposeWorkPackageWeeklyLoads(workPackage.reference);
      const rows = proposal.loads.map((row) => ({
        week_start: row.week_start,
        hours: String(row.hours),
      }));
      setWeeklyDraft(rows);
      setWeeklyOrigin("AUTO");
      setProposalVersion(proposal.version);
      setProposalFingerprint(weeklyFingerprint(rows));
      setWeeklyDirty(true);
      weeklyRetry.current = null;
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setWeeklyError(apiMessage(reason));
        setWeeklyConflict(reason.code === "work_package_version_conflict");
      } else {
        setWeeklyError(reason instanceof Error ? reason.message : "Impossible de générer la proposition.");
      }
    } finally {
      setWeeklyBusy(false);
    }
  }

  async function saveWeeklyLoads() {
    if (!workPackage || weeklyBusy || weeklyDraft.length === 0) return;
    setWeeklyBusy(true);
    setWeeklyError(null);
    setWeeklyConflict(false);
    try {
      const currentFingerprint = weeklyFingerprint(weeklyDraft);
      const origin: "AUTO" | "MANUAL" = weeklyOrigin === "AUTO"
        && proposalFingerprint === currentFingerprint
        ? "AUTO"
        : "MANUAL";
      const loads = weeklyPayload(weeklyDraft);
      const expectedVersion = proposalVersion
        ?? mediumTermWorkPackage?.version
        ?? workPackage.version;
      const requestFingerprint = JSON.stringify({ expectedVersion, origin, loads });
      const previous = weeklyRetry.current;
      const key = previous?.fingerprint === requestFingerprint
        ? previous.key
        : crypto.randomUUID();
      weeklyRetry.current = { fingerprint: requestFingerprint, key };
      await replaceWorkPackageWeeklyLoads(
        workPackage.reference,
        expectedVersion,
        origin,
        loads,
        key,
      );
      onSaved(workPackage.project_number);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setWeeklyError(apiMessage(reason));
        setWeeklyConflict(reason.code === "work_package_version_conflict");
      } else {
        setWeeklyError(reason instanceof Error ? reason.message : "Impossible d’enregistrer la répartition.");
      }
    } finally {
      setWeeklyBusy(false);
    }
  }

  const weeklySaveOrigin: "AUTO" | "MANUAL" = weeklyOrigin === "AUTO"
    && proposalFingerprint === weeklyFingerprint(weeklyDraft)
    ? "AUTO"
    : "MANUAL";

  return (
    <div className="wp-editor-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="wp-editor"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wp-editor-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="wp-editor-header">
          <div>
            <span className="eyebrow">WorkPackage</span>
            <h2 id="wp-editor-title">{editing ? "Modifier le lot" : "Créer un lot"}</h2>
            {workPackage && (
              <small>
                Référence : {workPackage.reference} · Version {mediumTermWorkPackage?.version ?? workPackage.version}
              </small>
            )}
          </div>
          <button type="button" className="wp-close" onClick={onClose} aria-label="Fermer">×</button>
        </header>

        <form className="wp-form" onSubmit={save}>
          <label className="wp-full">
            <span>Projet *</span>
            <select
              required
              value={form.projectNumber}
              onChange={(event) => {
                const projectNumber = event.target.value;
                setForm((current) => ({
                  ...current,
                  projectNumber,
                  taskCatalogItemId: projectNumber === current.projectNumber
                    ? current.taskCatalogItemId
                    : "",
                }));
              }}
            >
              <option value="">Choisir un projet</option>
              {projectOptions.map((project) => (
                <option value={project.number} key={project.id}>
                  {project.number} — {project.name}
                </option>
              ))}
            </select>
            {editing && (
              <small>
                Le projet peut être changé seulement si aucune demande n’est déjà liée à ce WorkPackage.
              </small>
            )}
          </label>

          <label className="wp-full">
            <span>Tâche ERP *</span>
            <select
              required={!editing}
              value={form.taskCatalogItemId}
              onChange={(event) => field("taskCatalogItemId", event.target.value)}
              disabled={!form.projectNumber || tasksLoading}
            >
              <option value="">
                {tasksLoading ? "Chargement des tâches…" : "Choisir une tâche ERP"}
              </option>
              {tasks
                .filter((task) => task.id)
                .map((task) => (
                  <option value={task.id || ""} key={task.id || `${task.project_number}-${task.code}`}>
                    {task.code} — {task.label}
                  </option>
                ))}
            </select>
            {editing && !workPackage?.task_catalog_item_id && (
              <small>
                WorkPackage historique non classé : choisis explicitement sa tâche ERP pour le régulariser.
              </small>
            )}
          </label>

          <label>
            <span>Code</span>
            <input value={form.code} onChange={(event) => field("code", event.target.value)} placeholder="DEV, INST, MES…" />
          </label>
          <label>
            <span>Statut</span>
            <select value={form.status} onChange={(event) => field("status", event.target.value)}>
              {STATUS_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>

          <label className="wp-full">
            <span>Nom *</span>
            <input required value={form.name} onChange={(event) => field("name", event.target.value)} placeholder="Développement automatisation" />
          </label>

          <label>
            <span>Début</span>
            <input type="date" value={form.startDate} onChange={(event) => field("startDate", event.target.value)} />
          </label>
          <label>
            <span>Fin</span>
            <input type="date" value={form.endDate} onChange={(event) => field("endDate", event.target.value)} />
          </label>

          <label>
            <span>Heures prévues</span>
            <input
              type="number"
              min="0"
              step="0.01"
              value={form.plannedHours}
              onChange={(event) => field("plannedHours", event.target.value)}
              placeholder="80"
            />
          </label>

          <label className="wp-full">
            <span>Description</span>
            <textarea
              rows={4}
              value={form.description}
              onChange={(event) => field("description", event.target.value)}
              placeholder="Portée, livrables ou contexte du lot…"
            />
          </label>

          {error && <div className="wp-error wp-full">{error}</div>}

          <footer className="wp-editor-actions wp-full">
            <button type="button" className="wp-secondary" onClick={onClose} disabled={saving}>Annuler</button>
            <button type="submit" className="wp-primary" disabled={saving}>
              {saving ? "Enregistrement…" : editing ? "Enregistrer le WorkPackage" : "Créer le WorkPackage"}
            </button>
          </footer>
        </form>

        {editing && mediumTermWorkPackage && (
          <section className="wp-weekly-section" aria-labelledby="wp-weekly-title">
            <header className="wp-weekly-header">
              <div>
                <span className="eyebrow">Répartition hebdomadaire</span>
                <h3 id="wp-weekly-title">Charge dans le temps</h3>
                <p>{weeklyDiagnosticLabel(mediumTermWorkPackage.weekly_load_diagnostic)}</p>
              </div>
              <div className="wp-weekly-origin">
                <span>Origine persistée</span>
                <strong>{mediumTermWorkPackage.weekly_load_origin || "Aucune"}</strong>
              </div>
            </header>

            {baseFormDirty && (
              <div className="wp-weekly-notice">
                Les valeurs du formulaire WorkPackage ne sont pas encore enregistrées. La proposition AUTO
                utilise toujours les dates et la charge persistées côté backend.
              </div>
            )}

            <div className="wp-weekly-actions">
              <button
                type="button"
                className="wp-secondary"
                disabled={weeklyBusy || baseFormDirty}
                onClick={generateProposal}
              >
                {weeklyBusy ? "Traitement…" : "Générer une proposition automatique"}
              </button>
              <button
                type="button"
                className="wp-secondary"
                disabled={weeklyBusy}
                onClick={() => markManual((rows) => [...rows, { week_start: "", hours: "0" }])}
              >
                + Ajouter une semaine
              </button>
            </div>

            {proposalFingerprint && weeklySaveOrigin === "AUTO" && (
              <div className="wp-weekly-preview" role="status">
                Proposition AUTO prévisualisée — elle n’est pas encore enregistrée.
              </div>
            )}
            {proposalFingerprint && weeklySaveOrigin === "MANUAL" && (
              <div className="wp-weekly-preview is-manual" role="status">
                Proposition modifiée — l’enregistrement sera MANUAL, pas AUTO.
              </div>
            )}

            <div className="wp-weekly-grid">
              {weeklyDraft.length === 0 ? (
                <div className="wp-weekly-empty">
                  Aucune ligne persistée. Génère une proposition ou saisis une répartition manuelle.
                </div>
              ) : weeklyDraft.map((row, index) => (
                <div className="wp-weekly-row" key={`${row.week_start || "new"}-${index}`}>
                  <label>
                    <span>Lundi de semaine</span>
                    <input
                      type="date"
                      value={row.week_start}
                      onChange={(event) => markManual((rows) => rows.map((candidate, rowIndex) => (
                        rowIndex === index ? { ...candidate, week_start: event.target.value } : candidate
                      )))}
                    />
                  </label>
                  <label>
                    <span>Heures</span>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={row.hours}
                      onChange={(event) => markManual((rows) => rows.map((candidate, rowIndex) => (
                        rowIndex === index ? { ...candidate, hours: event.target.value } : candidate
                      )))}
                    />
                  </label>
                  <button
                    type="button"
                    className="wp-remove-week"
                    onClick={() => markManual((rows) => rows.filter((_, rowIndex) => rowIndex !== index))}
                  >
                    Retirer
                  </button>
                </div>
              ))}
            </div>

            <div className="wp-weekly-total">
              <span>Somme affichée</span>
              <strong>{new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(distributedHours)} h</strong>
              <span>sur {mediumTermWorkPackage.planned_hours == null ? "charge totale inconnue" : `${mediumTermWorkPackage.planned_hours} h prévues`}</span>
              <small>Cette somme est une aide visuelle; FastAPI reste autoritaire pour la validation exacte.</small>
            </div>

            {weeklyError && (
              <div className="wp-error">
                {weeklyError}
                {weeklyConflict && onReload && (
                  <button type="button" className="wp-reload" onClick={onReload}>
                    Recharger le WorkPackage
                  </button>
                )}
              </div>
            )}

            <footer className="wp-weekly-footer">
              <span>
                {weeklySaveOrigin === "AUTO"
                  ? "La proposition AUTO sera acceptée explicitement."
                  : "Les valeurs seront enregistrées comme intention MANUAL."}
              </span>
              <button
                type="button"
                className="wp-primary"
                disabled={weeklyBusy || !weeklyDirty || weeklyDraft.length === 0}
                onClick={saveWeeklyLoads}
              >
                {weeklyBusy
                  ? "Enregistrement…"
                  : weeklySaveOrigin === "AUTO"
                    ? "Accepter la proposition AUTO"
                    : "Enregistrer la répartition manuelle"}
              </button>
            </footer>
          </section>
        )}
      </section>
    </div>
  );
}
