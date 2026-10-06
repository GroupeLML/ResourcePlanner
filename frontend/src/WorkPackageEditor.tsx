import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  MediumTermBudgetWorkPackageReadModel,
  ProjectReadModel,
  TaskCatalogItemReadModel,
  WorkPackageLoadIntervalWrite,
  WorkPackageReadModel,
  WorkPackageWrite,
  cancelWorkPackage,
  closeWorkPackage,
  createWorkPackage,
  reopenWorkPackage,
  getTaskCatalog,
  updateWorkPackage,
} from "./api";
import { createClientId } from "./clientId";
import {
  getResourceClassOptions,
  type ResourceClassOptionReadModel,
} from "./resourceClassesApi";

const RESOURCE_CLASS_DIVERGENCE = "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE";

function resourceClassDisplay(code: string | null | undefined, label: string | null | undefined) {
  if (!code) return "Aucune classe de ressource";
  return label ? `${label} (${code})` : code;
}

function statusLabel(status: string | null | undefined) {
  switch ((status || "").toLowerCase()) {
    case "active":
      return "Actif";
    case "closed":
      return "Fermé";
    case "cancelled":
      return "Annulé";
    case "planned":
    default:
      return "Planifié";
  }
}

function intervalOriginLabel(origin: string | null | undefined) {
  switch (origin) {
    case "LEGACY_AUTO":
      return "Historique AUTO";
    case "LEGACY_MANUAL":
      return "Historique MANUAL";
    case "MANUAL":
      return "Explicite";
    default:
      return origin || "Explicite";
  }
}

type FormState = {
  projectNumber: string;
  taskCatalogItemId: string;
  resourceClassCode: string;
  name: string;
  description: string;
  startDate: string;
  endDate: string;
  plannedHours: string;
};

type IntervalDraftRow = {
  id: string | null;
  start_date: string;
  end_date: string;
  hours: string;
  origin: string | null;
};

function initialState(
  workPackage: WorkPackageReadModel | null,
  defaultProjectNumber: string,
): FormState {
  return {
    projectNumber: workPackage?.project_number || defaultProjectNumber,
    taskCatalogItemId: workPackage?.task_catalog_item_id || "",
    resourceClassCode: workPackage?.resource_class_code || "",
    name: workPackage?.name || "",
    description: workPackage?.description || "",
    startDate: workPackage?.start_date || "",
    endDate: workPackage?.end_date || "",
    plannedHours: workPackage?.planned_hours == null ? "" : String(workPackage.planned_hours),
  };
}

function toPayload(form: FormState): WorkPackageWrite {
  const hours = form.plannedHours.trim();
  return {
    project_number: form.projectNumber,
    task_catalog_item_id: form.taskCatalogItemId.trim() || null,
    name: form.name.trim(),
    description: form.description.trim() || null,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    planned_hours: hours ? Number(hours.replace(",", ".")) : null,
  };
}

function intervalRows(
  workPackage: MediumTermBudgetWorkPackageReadModel | null | undefined,
): IntervalDraftRow[] {
  return (workPackage?.load_intervals ?? []).map((row) => ({
    id: row.id || null,
    start_date: row.start_date,
    end_date: row.end_date,
    hours: String(row.hours),
    origin: row.origin,
  }));
}

function intervalPayload(rows: IntervalDraftRow[]): WorkPackageLoadIntervalWrite[] {
  return rows.map((row) => ({
    ...(row.id ? { id: row.id } : {}),
    start_date: row.start_date,
    end_date: row.end_date,
    hours: Number(row.hours.trim().replace(",", ".")),
  }));
}

function apiMessage(reason: ApiError) {
  switch (reason.code) {
    case "work_package_version_conflict":
      return "Conflit de version : ce WorkPackage a changé. Recharge les données avant de réessayer; aucune modification concurrente n’a été écrasée.";
    case "work_package_resource_class_not_found":
      return "Cette classe de ressource n’existe plus dans le référentiel. Recharge les classes puis choisis une valeur valide.";
    case "work_package_resource_class_inactive":
      return "Cette classe de ressource est inactive et ne peut pas être choisie comme nouvelle affectation. Une classe historique déjà liée demeure toutefois lisible.";
    case "work_package_load_intervals_replan_required":
      return "Cette modification exclurait ou surallouerait un intervalle explicite. Corrige les intervalles dans la même sauvegarde.";
    case "work_package_load_planned_hours_required":
      return "La charge totale du WorkPackage est requise lorsqu’une répartition explicite existe.";
    case "work_package_load_dates_required":
      return "Les dates du WorkPackage sont requises lorsqu’une répartition explicite existe.";
    case "work_package_load_interval_window_invalid":
      return "La fin d’un intervalle ne peut pas précéder son début.";
    case "work_package_load_interval_outside_window":
      return "Chaque intervalle explicite doit rester entièrement dans la période du WorkPackage.";
    case "work_package_load_explicit_exceeds_planned":
      return "La somme des heures explicites ne peut pas dépasser les heures prévues.";
    case "work_package_load_precision_invalid":
      return "Les heures doivent respecter une précision de 0,01 h.";
    case "work_package_load_interval_duplicate_id":
      return "Un même intervalle ne peut apparaître qu’une fois.";
    case "work_package_already_terminal":
      return "Ce WorkPackage est déjà dans cet état terminal.";
    case "work_package_not_terminal":
      return "Ce WorkPackage est déjà ouvert.";
    case "work_package_lifecycle_transition_invalid":
      return "Cette transition de cycle de vie n’est pas autorisée.";
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
  const [intervalDraft, setIntervalDraft] = useState<IntervalDraftRow[]>(
    () => intervalRows(mediumTermWorkPackage),
  );
  const [saving, setSaving] = useState(false);
  const [lifecycleBusy, setLifecycleBusy] = useState(false);
  const [tasks, setTasks] = useState<TaskCatalogItemReadModel[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [resourceClasses, setResourceClasses] = useState<ResourceClassOptionReadModel[]>([]);
  const [resourceClassesLoading, setResourceClassesLoading] = useState(false);
  const [resourceClassTouched, setResourceClassTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [versionConflict, setVersionConflict] = useState(false);
  const createRetry = useRef<{ fingerprint: string; key: string } | null>(null);
  const lifecycleRetry = useRef<{ fingerprint: string; key: string } | null>(null);
  const editing = Boolean(workPackage);

  useEffect(() => {
    setForm(initialState(workPackage, defaultProjectNumber));
    setResourceClassTouched(false);
    setError(null);
    setVersionConflict(false);
    createRetry.current = null;
  }, [workPackage, defaultProjectNumber]);

  useEffect(() => {
    setIntervalDraft(intervalRows(mediumTermWorkPackage));
    setError(null);
    setVersionConflict(false);
  }, [mediumTermWorkPackage, workPackage?.reference]);

  useEffect(() => {
    const controller = new AbortController();
    setResourceClassesLoading(true);
    getResourceClassOptions(controller.signal)
      .then((rows) => setResourceClasses(rows))
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setResourceClasses([]);
        setError(reason instanceof Error ? reason.message : "Impossible de charger les classes de ressource.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setResourceClassesLoading(false);
      });
    return () => controller.abort();
  }, []);

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
              resource_class_code: workPackage.task_resource_class_code,
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
    workPackage?.task_resource_class_code,
  ]);

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === form.taskCatalogItemId) ?? null,
    [tasks, form.taskCatalogItemId],
  );

  const resourceClassOptions = useMemo(() => {
    const rows = resourceClasses
      .filter((row) => row.active)
      .map((row) => ({ ...row }))
      .sort((left, right) => left.label.localeCompare(right.label, "fr-CA"));
    const historicalCode = workPackage?.resource_class_code;
    if (
      historicalCode
      && workPackage?.resource_class_active === false
      && !rows.some((row) => row.code === historicalCode)
    ) {
      rows.push({
        code: historicalCode,
        label: workPackage.resource_class_label || historicalCode,
        active: false,
      });
    }
    return rows;
  }, [
    resourceClasses,
    workPackage?.resource_class_active,
    workPackage?.resource_class_code,
    workPackage?.resource_class_label,
  ]);

  const selectedTaskClassCode = selectedTask?.resource_class_code?.trim() || null;
  const selectedTaskClass = selectedTaskClassCode
    ? resourceClasses.find((row) => row.code === selectedTaskClassCode) ?? null
    : null;
  const selectedTaskClassDisplay = resourceClassDisplay(
    selectedTaskClassCode,
    selectedTaskClass?.label,
  );

  useEffect(() => {
    if (editing || resourceClassTouched || resourceClassesLoading) return;
    const suggestedCode = selectedTask?.resource_class_code?.trim() || "";
    const usableSuggestion = suggestedCode
      && resourceClasses.some((row) => row.code === suggestedCode && row.active)
      ? suggestedCode
      : "";
    setForm((current) => {
      if (current.taskCatalogItemId !== (selectedTask?.id || "")) return current;
      if (current.resourceClassCode === usableSuggestion) return current;
      return { ...current, resourceClassCode: usableSuggestion };
    });
  }, [
    editing,
    resourceClassTouched,
    resourceClasses,
    resourceClassesLoading,
    selectedTask?.id,
    selectedTask?.resource_class_code,
  ]);

  const explicitHours = useMemo(
    () => intervalDraft.reduce((sum, row) => {
      const value = Number(row.hours.trim().replace(",", "."));
      return Number.isFinite(value) ? sum + value : sum;
    }, 0),
    [intervalDraft],
  );
  const plannedHours = useMemo(() => {
    const value = Number(form.plannedHours.trim().replace(",", "."));
    return form.plannedHours.trim() && Number.isFinite(value) ? value : null;
  }, [form.plannedHours]);
  const automaticHours = plannedHours == null ? null : plannedHours - explicitHours;
  const expectedVersion = mediumTermWorkPackage?.version ?? workPackage?.version ?? 1;
  const workPackageStatus = workPackage?.status ?? null;
  const terminal = workPackageStatus === "closed" || workPackageStatus === "cancelled";
  const canClose = Boolean(workPackage) && !terminal;
  const canCancel = Boolean(workPackage) && workPackageStatus !== "cancelled";
  const canReopen = Boolean(workPackage) && terminal;

  function field<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
    setError(null);
    setVersionConflict(false);
  }

  function intervalField<K extends keyof IntervalDraftRow>(
    index: number,
    key: K,
    value: IntervalDraftRow[K],
  ) {
    setIntervalDraft((rows) => rows.map((row, rowIndex) => (
      rowIndex === index ? { ...row, [key]: value } : row
    )));
    setError(null);
    setVersionConflict(false);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setError(null);
    setVersionConflict(false);

    const basePayload = toPayload(form);
    const selectedResourceClassCode = form.resourceClassCode.trim() || null;
    const payload: WorkPackageWrite = workPackage
      ? {
          ...basePayload,
          ...(resourceClassTouched
            ? { resource_class_code: selectedResourceClassCode }
            : {}),
          ...(mediumTermWorkPackage
            ? { load_intervals: intervalPayload(intervalDraft) }
            : {}),
        }
      : {
          ...basePayload,
          resource_class_code: selectedResourceClassCode,
        };

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
    if (workPackage && intervalDraft.some((row) => !row.start_date || !row.end_date || !row.hours.trim())) {
      setError("Chaque intervalle explicite doit avoir un début, une fin et un nombre d’heures.");
      return;
    }
    if (intervalDraft.some((row) => row.start_date && row.end_date && row.end_date < row.start_date)) {
      setError("La fin d’un intervalle ne peut pas précéder son début.");
      return;
    }
    if (plannedHours != null && explicitHours > plannedHours + 0.0001) {
      setError("La somme des heures explicites ne peut pas dépasser les heures prévues.");
      return;
    }

    setSaving(true);
    try {
      if (workPackage) {
        await updateWorkPackage(workPackage.reference, payload, expectedVersion);
      } else {
        const fingerprint = JSON.stringify(payload);
        const previous = createRetry.current;
        const key = previous?.fingerprint === fingerprint
          ? previous.key
          : createClientId();
        createRetry.current = { fingerprint, key };
        await createWorkPackage(payload, key);
      }
      onSaved(payload.project_number);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(apiMessage(reason));
        setVersionConflict(reason.code === "work_package_version_conflict");
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible d’enregistrer le WorkPackage.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function applyLifecycle(action: "close" | "cancel" | "reopen") {
    if (!workPackage || lifecycleBusy) return;
    if (action === "close" && !canClose) return;
    if (action === "cancel" && !canCancel) return;
    if (action === "reopen" && !canReopen) return;

    const confirmation = action === "reopen"
      ? "Réouvrir ce WorkPackage ? Les demandes, besoins, quarts, Delivery et Verification liés ne seront pas modifiés."
      : action === "cancel" && workPackageStatus === "closed"
        ? "Annuler ce WorkPackage clôturé ? Il sera retiré de la structuration budgétaire sans modifier les engagements liés."
        : `Confirmer : ${action === "close" ? "clôturer" : "annuler"} ce WorkPackage ?`;
    if (!window.confirm(confirmation)) return;

    setLifecycleBusy(true);
    setError(null);
    setVersionConflict(false);
    try {
      const fingerprint = JSON.stringify({ action, reference: workPackage.reference, expectedVersion });
      const previous = lifecycleRetry.current;
      const key = previous?.fingerprint === fingerprint ? previous.key : createClientId();
      lifecycleRetry.current = { fingerprint, key };
      if (action === "close") {
        await closeWorkPackage(workPackage.reference, expectedVersion, key);
      } else if (action === "cancel") {
        await cancelWorkPackage(workPackage.reference, expectedVersion, key);
      } else {
        await reopenWorkPackage(workPackage.reference, expectedVersion, key);
      }
      onSaved(workPackage.project_number);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(apiMessage(reason));
        setVersionConflict(reason.code === "work_package_version_conflict");
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de modifier le cycle de vie du WorkPackage.");
      }
    } finally {
      setLifecycleBusy(false);
    }
  }

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
                Référence : {workPackage.reference} · Version {expectedVersion}
              </small>
            )}
          </div>
          <button type="button" className="wp-close" onClick={onClose} aria-label="Fermer">×</button>
        </header>

        {workPackage && (
          <div className="wp-weekly-notice" role="status">
            <strong>Statut : {statusLabel(workPackage.status)}</strong>
            <span> · calculé par le backend à partir du cycle de vie et de la date de début.</span>
            {workPackage.status_diagnostic && (
              <small> Diagnostic historique : {workPackage.status_diagnostic}</small>
            )}
            {(canClose || canCancel || canReopen) && (
              <span className="wp-weekly-actions">
                {canClose && (
                  <button
                    type="button"
                    className="wp-secondary"
                    disabled={lifecycleBusy || saving}
                    onClick={() => applyLifecycle("close")}
                  >
                    Clôturer
                  </button>
                )}
                {canCancel && (
                  <button
                    type="button"
                    className="wp-secondary"
                    disabled={lifecycleBusy || saving}
                    onClick={() => applyLifecycle("cancel")}
                  >
                    Annuler le WorkPackage
                  </button>
                )}
                {canReopen && (
                  <button
                    type="button"
                    className="wp-secondary"
                    disabled={lifecycleBusy || saving}
                    onClick={() => applyLifecycle("reopen")}
                  >
                    Réouvrir
                  </button>
                )}
              </span>
            )}
          </div>
        )}

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
                setError(null);
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

          <label className="wp-full">
            <span>Classe de ressource</span>
            <select
              value={form.resourceClassCode}
              disabled={resourceClassesLoading}
              onChange={(event) => {
                setResourceClassTouched(true);
                field("resourceClassCode", event.target.value);
              }}
            >
              <option value="">
                {resourceClassesLoading ? "Chargement des classes…" : "Aucune classe de ressource"}
              </option>
              {resourceClassOptions.map((resourceClass) => (
                <option
                  key={resourceClass.code}
                  value={resourceClass.code}
                  disabled={!resourceClass.active}
                >
                  {resourceClassDisplay(resourceClass.code, resourceClass.label)}
                  {resourceClass.active ? "" : " — inactive"}
                </option>
              ))}
            </select>
            <small>
              Classe métier canonique du WorkPackage. La tâche ERP peut proposer une valeur à la création,
              mais elle ne remplace jamais automatiquement une classe déjà persistée.
            </small>
            {selectedTaskClassCode && (
              <small>
                Classe de la tâche ERP : {selectedTaskClassDisplay}.{" "}
                {editing
                  ? "Cette valeur reste informative : changer la tâche ne change pas automatiquement la classe du WorkPackage."
                  : resourceClassTouched
                    ? "La valeur choisie dans le sélecteur reste celle qui sera envoyée."
                    : "Cette valeur est proposée dans le sélecteur lorsqu’elle est active; la valeur affichée est celle qui sera envoyée."}
              </small>
            )}
            {workPackage?.resource_class_active === false
              && form.resourceClassCode === workPackage.resource_class_code && (
              <small className="wp-class-inactive">
                Classe historique inactive — elle reste lisible et n’empêche pas les autres modifications.
                Choisissez une classe active pour la remplacer, ou laissez-la inchangée.
              </small>
            )}
          </label>

          {workPackage?.resource_class_diagnostic === RESOURCE_CLASS_DIVERGENCE
            && !resourceClassTouched && (
            <div className="wp-class-diagnostic wp-full" role="status">
              La classe actuelle du WorkPackage est différente de celle de la tâche ERP.
              La classe du WorkPackage demeure la valeur utilisée pour classifier ce WorkPackage;
              aucune correction automatique n’est effectuée.
            </div>
          )}

          {editing && workPackage?.code && (
            <div className="wp-legacy-code wp-full">
              <span>Code WorkPackage historique</span>
              <strong>{workPackage.code}</strong>
              <small>
                Ce code libre est conservé pour compatibilité. Il est distinct de la classe de ressource
                et de la tâche ERP.
              </small>
            </div>
          )}

          <label className="wp-full">
            <span>Nom *</span>
            <input
              required
              value={form.name}
              onChange={(event) => field("name", event.target.value)}
              placeholder="Développement automatisation"
            />
          </label>

          <label>
            <span>Début</span>
            <input
              type="date"
              value={form.startDate}
              onChange={(event) => field("startDate", event.target.value)}
            />
          </label>
          <label>
            <span>Fin</span>
            <input
              type="date"
              value={form.endDate}
              onChange={(event) => field("endDate", event.target.value)}
            />
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

          {editing && mediumTermWorkPackage && (
            <section className="wp-weekly-section wp-full" aria-labelledby="wp-load-title">
              <header className="wp-weekly-header">
                <div>
                  <span className="eyebrow">Répartition facultative</span>
                  <h3 id="wp-load-title">Intervalles explicites</h3>
                  <p>
                    Positionne seulement les portions utiles. Le solde est réparti automatiquement
                    sur toute la période du WorkPackage, en jours calendaires.
                  </p>
                </div>
              </header>

              <div className="wp-weekly-actions">
                <button
                  type="button"
                  className="wp-secondary"
                  onClick={() => {
                    setIntervalDraft((rows) => [
                      ...rows,
                      {
                        id: null,
                        start_date: form.startDate,
                        end_date: form.endDate || form.startDate,
                        hours: "0",
                        origin: "MANUAL",
                      },
                    ]);
                    setError(null);
                  }}
                >
                  + Ajouter un intervalle
                </button>
                {intervalDraft.length > 0 && (
                  <button
                    type="button"
                    className="wp-secondary"
                    onClick={() => {
                      setIntervalDraft([]);
                      setError(null);
                    }}
                  >
                    Tout remettre en automatique
                  </button>
                )}
              </div>

              <div className="wp-weekly-grid">
                {intervalDraft.length === 0 ? (
                  <div className="wp-weekly-empty">
                    Aucun intervalle explicite. Toute la charge sera répartie automatiquement sur la période.
                  </div>
                ) : intervalDraft.map((row, index) => (
                  <div className="wp-weekly-row" key={row.id || `new-${index}`}>
                    <label>
                      <span>Début</span>
                      <input
                        type="date"
                        value={row.start_date}
                        onChange={(event) => intervalField(index, "start_date", event.target.value)}
                      />
                    </label>
                    <label>
                      <span>Fin</span>
                      <input
                        type="date"
                        value={row.end_date}
                        onChange={(event) => intervalField(index, "end_date", event.target.value)}
                      />
                    </label>
                    <label>
                      <span>Heures</span>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={row.hours}
                        onChange={(event) => intervalField(index, "hours", event.target.value)}
                      />
                    </label>
                    <span className="wp-weekly-origin">{intervalOriginLabel(row.origin)}</span>
                    <button
                      type="button"
                      className="wp-remove-week"
                      onClick={() => {
                        setIntervalDraft((rows) => rows.filter((_, rowIndex) => rowIndex !== index));
                        setError(null);
                      }}
                    >
                      Retirer
                    </button>
                  </div>
                ))}
              </div>

              <div className="wp-weekly-total">
                <span>Charge explicite</span>
                <strong>
                  {new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(explicitHours)} h
                </strong>
                <span>
                  Solde automatique :{" "}
                  {automaticHours == null
                    ? "charge totale inconnue"
                    : `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(Math.max(automaticHours, 0))} h`}
                </span>
                <small>
                  FastAPI reste autoritaire pour le calcul au centième, la validation et la projection hebdomadaire.
                </small>
              </div>
            </section>
          )}

          {error && (
            <div className="wp-error wp-full">
              {error}
              {versionConflict && onReload && (
                <button type="button" className="wp-reload" onClick={onReload}>
                  Recharger le WorkPackage
                </button>
              )}
            </div>
          )}

          <footer className="wp-editor-actions wp-full">
            <button
              type="button"
              className="wp-secondary"
              onClick={onClose}
              disabled={saving || lifecycleBusy}
            >
              Retour
            </button>
            <button
              type="submit"
              className="wp-primary"
              disabled={saving || lifecycleBusy}
            >
              {saving ? "Enregistrement…" : editing ? "Enregistrer le WorkPackage" : "Créer le WorkPackage"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
