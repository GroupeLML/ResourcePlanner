import { useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "./AuthContext";
import { useViewScope } from "./ViewScopeContext";
import ViewScopeSelector from "./ViewScopeSelector";
import {
  AcumaticaIntegrationStatus,
  ApiError,
  BusinessContactReadModel,
  GlobalProjectTaskSyncResult,
  MediumTermBudgetReadModel,
  ProjectManagersReadModel,
  ProjectReadModel,
  ProjectTaskSyncMetadata,
  TaskCatalogItemReadModel,
  getAcumaticaIntegrationStatus,
  getAcumaticaProjectTaskSyncRun,
  getCurrentAcumaticaProjectTaskSyncRun,
  getAcumaticaProjectTaskSyncMetadata,
  addProjectCoManager,
  getBusinessContacts,
  getMediumTermBudgetSummary,
  getProjectManagerCandidateContacts,
  getProjectManagers,
  getProjects,
  getTaskCatalog,
  removeProjectCoManager,
  setTaskBusinessContacts,
  syncAcumaticaActiveProjectTasks,
  syncAcumaticaProjectTasks,
  syncAcumaticaProjects,
} from "./api";
import { ContactSelect } from "./BusinessContactUi";

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function sourceLabel(project: ProjectReadModel) {
  return project.erp_external_id ? "Acumatica" : "Local";
}

const BUDGET_DIAGNOSTIC_LABELS: Record<string, string> = {
  BUDGET_UNAVAILABLE: "Budget ERP non disponible",
  WORK_PACKAGE_LOAD_UNAVAILABLE: "Charge WorkPackage non disponible",
  NO_WORK_PACKAGES: "Budget positif sans WorkPackage",
  PARTIALLY_COVERED: "Budget partiellement structuré",
  FULLY_COVERED: "Budget entièrement structuré",
  OVERALLOCATED: "Dépassement du budget",
};

const BUDGET_ATTENTION = new Set([
  "BUDGET_UNAVAILABLE",
  "WORK_PACKAGE_LOAD_UNAVAILABLE",
  "NO_WORK_PACKAGES",
  "OVERALLOCATED",
]);

const FINANCIAL_DIAGNOSTIC_LABELS: Record<string, string> = {
  ERP_FINANCIAL_BUDGET_UNAVAILABLE: "Budget financier ERP non disponible",
  ERP_FINANCIAL_BUDGET_INCOMPLETE: "Budget financier ERP incomplet",
};

const ACTUAL_PROJECTION_DIAGNOSTIC_LABELS: Record<string, string> = {
  resource_class_cost_missing: "Coût horaire moyen non disponible",
  resource_class_cost_zero: "Coût horaire moyen nul",
  resource_class_cost_negative: "Coût horaire moyen invalide",
  WEEKLY_LOAD_INCOMPLETE: "Répartition hebdomadaire WorkPackage incomplète",
};

const PROJECT_MANAGER_STATUS_LABELS: Record<string, string> = {
  RESOLVED: "Résolu",
  UNRESOLVED_USER: "Utilisateur RessourcePlanner non lié",
  UNRESOLVED_CONTACT: "BusinessContact non lié",
  INVALID_REFERENCE: "Référence invalide",
  IDENTITY_CONFLICT: "Conflit d’identité",
};

const PROJECT_MANAGER_DIAGNOSTIC_LABELS: Record<string, string> = {
  ERP_PROJECT_MANAGER_MISSING: "Chargé principal ERP absent",
  ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED: "Utilisateur RessourcePlanner non lié",
  ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED: "BusinessContact non lié",
  ERP_PROJECT_MANAGER_APP_USER_INACTIVE: "Utilisateur RessourcePlanner inactif",
  ERP_PROJECT_MANAGER_BUSINESS_CONTACT_INACTIVE: "BusinessContact inactif",
  PROJECT_MANAGER_IDENTITY_CONFLICT: "Conflit d’identité",
  PROJECT_MANAGER_BROKEN_REFERENCE: "Référence invalide",
  PROJECT_CO_MANAGER_APP_USER_NOT_LINKED: "Co-chargé sans AppUser",
  PROJECT_CO_MANAGER_APP_USER_INACTIVE: "Utilisateur RessourcePlanner inactif",
  PROJECT_CO_MANAGER_CONTACT_INACTIVE: "BusinessContact inactif",
};

function projectManagerDiagnosticLabel(code: string) {
  return PROJECT_MANAGER_DIAGNOSTIC_LABELS[code] || code;
}

function formatCurrency(value: number | null | undefined) {
  if (value == null) return "—";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
  }).format(value);
}

function formatProjectDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("fr-CA", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

function workPackageWindow(start: string | null, end: string | null) {
  if (!start && !end) return "Date non définie";
  return `${start ? formatProjectDate(start) : "…"} → ${end ? formatProjectDate(end) : "…"}`;
}

function formatHours(value: number | null | undefined) {
  if (value == null) return "—";
  return `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(value)} h`;
}

function formatHourlyRate(value: number | null | undefined) {
  if (value == null) return "—";
  return `${formatCurrency(value)}/h`;
}

function budgetDiagnosticLabel(code: string) {
  return BUDGET_DIAGNOSTIC_LABELS[code] || code;
}

function financialDiagnosticLabel(code: string | null) {
  return code ? FINANCIAL_DIAGNOSTIC_LABELS[code] || code : null;
}

function actualProjectionDiagnosticLabel(code: string | null) {
  return code ? ACTUAL_PROJECTION_DIAGNOSTIC_LABELS[code] || code : null;
}

function apiErrorMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

const GLOBAL_TASK_SYNC_ACTIVE = new Set(["PENDING", "RUNNING"]);

function isGlobalTaskSyncActive(run: GlobalProjectTaskSyncResult | null) {
  return Boolean(run && GLOBAL_TASK_SYNC_ACTIVE.has(run.status));
}

function globalTaskSyncSummary(run: GlobalProjectTaskSyncResult) {
  const outcome = run.status === "COMPLETED_WITH_ERRORS"
    ? "Synchronisation complétée avec erreurs projet"
    : "Synchronisation complétée";
  const metrics = [
    `${run.projects_inspected} projets inspectés`,
    `${run.projects_synchronized} synchronisés`,
    `${run.projects_ignored} ignorés`,
    `${run.projects_rejected} rejetés`,
    `${run.tasks_received} tâches reçues`,
    `${run.tasks_created} créées`,
    `${run.tasks_updated} mises à jour`,
    `${run.tasks_unchanged} inchangées`,
    `${run.tasks_deactivated} désactivées`,
    `${run.tasks_rejected} non admissibles`,
  ];
  if (run.source_requests != null) metrics.push(`${run.source_requests} requête(s) ERP`);
  if (run.source_rows_scanned != null) metrics.push(`${run.source_rows_scanned} lignes ERP parcourues`);
  if (run.source_read_duration_ms != null) {
    metrics.push(`lecture source ${(run.source_read_duration_ms / 1000).toFixed(1)} s`);
  }
  if (run.duration_ms != null) metrics.push(`durée totale ${(run.duration_ms / 1000).toFixed(1)} s`);
  return `${outcome} · ${metrics.join(" · ")}`;
}

export default function ProjectsPage() {
  const { can } = useAuth();
  const { scope, loading: scopeLoading } = useViewScope();
  const canSyncProjects = can("sync_projects");
  const canManageContacts = can("manage_resources");
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [contacts, setContacts] = useState<BusinessContactReadModel[]>([]);
  const [selectedProjectNumber, setSelectedProjectNumber] = useState<string | null>(null);
  const [projectManagers, setProjectManagers] = useState<ProjectManagersReadModel | null>(null);
  const [coManagerContacts, setCoManagerContacts] = useState<BusinessContactReadModel[]>([]);
  const [coManagerSearch, setCoManagerSearch] = useState("");
  const [selectedCoManagerId, setSelectedCoManagerId] = useState("");
  const [coManagerPending, setCoManagerPending] = useState(false);
  const [coManagerMessage, setCoManagerMessage] = useState<string | null>(null);
  const [coManagerError, setCoManagerError] = useState<string | null>(null);
  const coManagerIntentKeys = useRef(new Map<string, string>());
  const [projectTasks, setProjectTasks] = useState<TaskCatalogItemReadModel[]>([]);
  const [projectBudget, setProjectBudget] = useState<MediumTermBudgetReadModel | null>(null);
  const [projectBudgetLoading, setProjectBudgetLoading] = useState(false);
  const [projectBudgetError, setProjectBudgetError] = useState<string | null>(null);
  const [contactPending, setContactPending] = useState(false);
  const [integration, setIntegration] = useState<AcumaticaIntegrationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("actif");
  const [managerFilter, setManagerFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState<"all" | "erp" | "local">("all");
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [globalTaskSyncing, setGlobalTaskSyncing] = useState(false);
  const [globalTaskSyncRun, setGlobalTaskSyncRun] = useState<GlobalProjectTaskSyncResult | null>(null);
  const [globalTaskSyncMessage, setGlobalTaskSyncMessage] = useState<string | null>(null);
  const [globalTaskSyncError, setGlobalTaskSyncError] = useState<string | null>(null);
  const [globalTaskSyncTrackingError, setGlobalTaskSyncTrackingError] = useState<string | null>(null);
  const [taskSyncing, setTaskSyncing] = useState(false);
  const [taskSyncMessage, setTaskSyncMessage] = useState<string | null>(null);
  const [taskSyncError, setTaskSyncError] = useState<string | null>(null);
  const [taskSyncMetadata, setTaskSyncMetadata] = useState<ProjectTaskSyncMetadata | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const selectedProject = useMemo(
    () => projects.find((project) => project.number === selectedProjectNumber) ?? null,
    [projects, selectedProjectNumber],
  );

  const coManagerCandidates = useMemo(() => {
    const nominated = new Set(
      projectManagers?.co_managers
        .map((manager) => manager.business_contact_id)
        .filter((value): value is string => Boolean(value)) ?? [],
    );
    const primaryContactId = projectManagers?.primary?.business_contact_id ?? null;
    const query = normalize(coManagerSearch);
    return coManagerContacts.filter((contact) => {
      if (!contact.active || nominated.has(contact.id) || contact.id === primaryContactId) {
        return false;
      }
      if (!query) return true;
      return normalize([
        contact.display_name,
        contact.email,
        contact.phone,
        contact.external_id,
        contact.id,
      ].filter(Boolean).join(" ")).includes(query);
    });
  }, [coManagerContacts, coManagerSearch, projectManagers]);

  useEffect(() => {
    if (scopeLoading) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getProjects(false, controller.signal, scope),
      getAcumaticaIntegrationStatus(controller.signal),
      getBusinessContacts(false, controller.signal),
    ])
      .then(([projectRows, status, contactRows]) => {
        setProjects(projectRows);
        setIntegration(status);
        setContacts(contactRows);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(apiErrorMessage(reason, "Impossible de charger les projets."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refreshKey, scope, scopeLoading]);

  useEffect(() => {
    if (!canSyncProjects || !integration?.project_tasks_configured) return;
    const controller = new AbortController();
    getCurrentAcumaticaProjectTaskSyncRun(controller.signal)
      .then((run) => {
        if (controller.signal.aborted || !run) return;
        setGlobalTaskSyncRun(run);
        setGlobalTaskSyncTrackingError(null);
        const active = isGlobalTaskSyncActive(run);
        setGlobalTaskSyncing(active);
        if (active) {
          setGlobalTaskSyncError(null);
          setGlobalTaskSyncMessage(null);
        } else if (run.status === "COMPLETED" || run.status === "COMPLETED_WITH_ERRORS") {
          setGlobalTaskSyncError(null);
          setGlobalTaskSyncMessage(globalTaskSyncSummary(run));
        } else {
          setGlobalTaskSyncMessage(null);
          setGlobalTaskSyncError(
            run.status === "INTERRUPTED"
              ? "La synchronisation précédente a été interrompue par un redémarrage du backend."
              : `La synchronisation a échoué${run.error_code ? ` (${run.error_code})` : ""}.`,
          );
        }
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setGlobalTaskSyncTrackingError(
          apiErrorMessage(reason, "Impossible de retrouver le run de synchronisation courant."),
        );
      });
    return () => controller.abort();
  }, [canSyncProjects, integration?.project_tasks_configured]);

  useEffect(() => {
    if (!globalTaskSyncRun || !isGlobalTaskSyncActive(globalTaskSyncRun)) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      try {
        const run = await getAcumaticaProjectTaskSyncRun(
          globalTaskSyncRun.run_id,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setGlobalTaskSyncRun(run);
        setGlobalTaskSyncTrackingError(null);
        if (isGlobalTaskSyncActive(run)) {
          setGlobalTaskSyncing(true);
          timer = setTimeout(() => void poll(), 1000);
          return;
        }

        setGlobalTaskSyncing(false);
        if (run.status === "COMPLETED" || run.status === "COMPLETED_WITH_ERRORS") {
          setGlobalTaskSyncError(null);
          setGlobalTaskSyncMessage(globalTaskSyncSummary(run));
          setRefreshKey((value) => value + 1);
        } else {
          setGlobalTaskSyncMessage(null);
          setGlobalTaskSyncError(
            run.status === "INTERRUPTED"
              ? "La synchronisation a été interrompue par un redémarrage du backend. Vous pouvez la relancer."
              : `La synchronisation a échoué${run.error_code ? ` (${run.error_code})` : ""}. Vous pouvez la relancer.`,
          );
        }
      } catch (reason: unknown) {
        if (controller.signal.aborted) return;
        setGlobalTaskSyncTrackingError(
          apiErrorMessage(reason, "Le suivi de la synchronisation est temporairement indisponible."),
        );
        timer = setTimeout(() => void poll(), 1500);
      }
    };

    timer = setTimeout(() => void poll(), 250);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [globalTaskSyncRun?.run_id, globalTaskSyncRun?.status]);

  useEffect(() => {
    if (!selectedProject) {
      setProjectManagers(null);
      setCoManagerContacts([]);
      setProjectTasks([]);
      setTaskSyncMetadata(null);
      return;
    }
    const controller = new AbortController();
    setProjectManagers(null);
    setCoManagerMessage(null);
    setCoManagerError(null);
    setSelectedCoManagerId("");
    Promise.all([
      getProjectManagers(selectedProject.number, controller.signal, scope),
      canManageContacts
        ? getProjectManagerCandidateContacts(controller.signal)
        : Promise.resolve([]),
      canManageContacts
        ? getTaskCatalog(selectedProject.number, "", false, controller.signal)
        : Promise.resolve([]),
      canSyncProjects
        ? getAcumaticaProjectTaskSyncMetadata(selectedProject.id, controller.signal)
        : Promise.resolve(null),
    ])
      .then(([managerRows, candidateRows, taskRows, metadata]) => {
        setProjectManagers(managerRows);
        setCoManagerContacts(candidateRows);
        setProjectTasks(taskRows);
        setTaskSyncMetadata(metadata);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(apiErrorMessage(reason, "Impossible de charger les données du projet."));
      });
    return () => controller.abort();
  }, [selectedProject, refreshKey, canManageContacts, canSyncProjects, scope]);

  useEffect(() => {
    if (!selectedProject) {
      setProjectBudget(null);
      setProjectBudgetError(null);
      setProjectBudgetLoading(false);
      return;
    }
    const controller = new AbortController();
    setProjectBudgetLoading(true);
    setProjectBudgetError(null);
    getMediumTermBudgetSummary(selectedProject.number, controller.signal, scope)
      .then(setProjectBudget)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setProjectBudget(null);
        setProjectBudgetError(
          apiErrorMessage(reason, "Impossible de charger les budgets de tâches ERP."),
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setProjectBudgetLoading(false);
      });
    return () => controller.abort();
  }, [selectedProject, refreshKey, scope]);

  const managers = useMemo(() => {
    const values = new Set(
      projects.map((project) => project.project_manager || "Non assigné"),
    );
    return [...values].sort((left, right) => left.localeCompare(right, "fr-CA"));
  }, [projects]);

  const projectStatuses = useMemo(() => {
    const values = new Map<string, string>();
    projects.forEach((project) => {
      const status = project.status?.trim();
      if (!status) return;
      const key = normalize(status);
      if (!values.has(key)) values.set(key, status);
    });
    return [...values.entries()].sort(([, left], [, right]) => left.localeCompare(right, "fr-CA"));
  }, [projects]);

  const filteredProjects = useMemo(() => {
    const query = normalize(search);
    return projects.filter((project) => {
      if (statusFilter !== "all" && normalize(project.status) !== statusFilter) return false;
      const manager = project.project_manager || "Non assigné";
      if (managerFilter !== "all" && manager !== managerFilter) return false;
      if (sourceFilter === "erp" && !project.erp_external_id) return false;
      if (sourceFilter === "local" && project.erp_external_id) return false;
      if (!query) return true;
      return normalize([
        project.number,
        project.name,
        project.client,
        project.project_manager,
        project.status,
        sourceLabel(project),
      ].filter(Boolean).join(" ")).includes(query);
    });
  }, [projects, search, statusFilter, managerFilter, sourceFilter]);

  const activeCount = projects.filter((project) => project.active).length;
  const inactiveCount = projects.length - activeCount;
  const erpCount = projects.filter((project) => Boolean(project.erp_external_id)).length;

  function coManagerIntentKey(
    action: "ADD" | "REMOVE",
    businessContactId: string,
    expectedVersion: number,
  ) {
    return `${action}:${selectedProject?.number ?? ""}:${businessContactId}:${expectedVersion}`;
  }

  function idempotencyKeyFor(intent: string) {
    const existing = coManagerIntentKeys.current.get(intent);
    if (existing) return existing;
    const created = crypto.randomUUID();
    coManagerIntentKeys.current.set(intent, created);
    return created;
  }

  async function reloadProjectManagers() {
    if (!selectedProject) return;
    const refreshed = await getProjectManagers(selectedProject.number, undefined, scope);
    setProjectManagers(refreshed);
  }

  async function addCoManager() {
    if (
      !selectedProject
      || !projectManagers
      || !selectedCoManagerId
      || coManagerPending
      || !canManageContacts
    ) return;

    const expectedVersion = projectManagers.co_managers_version;
    const intent = coManagerIntentKey("ADD", selectedCoManagerId, expectedVersion);
    const idempotencyKey = idempotencyKeyFor(intent);
    setCoManagerPending(true);
    setCoManagerMessage(null);
    setCoManagerError(null);
    try {
      const result = await addProjectCoManager(
        selectedProject.number,
        selectedCoManagerId,
        expectedVersion,
        idempotencyKey,
      );
      coManagerIntentKeys.current.delete(intent);
      setProjectManagers((current) => current
        ? { ...current, co_managers_version: result.version }
        : current);
      setSelectedCoManagerId("");
      setCoManagerSearch("");
      setCoManagerMessage("Co-chargé ajouté.");
      try {
        await reloadProjectManagers();
      } catch (reason) {
        setCoManagerError(
          apiErrorMessage(reason, "Ajout enregistré, mais la liste n’a pas pu être rechargée."),
        );
      }
    } catch (reason) {
      if (reason instanceof ApiError) {
        coManagerIntentKeys.current.delete(intent);
        if (reason.code === "project_co_managers_version_conflict") {
          try {
            await reloadProjectManagers();
          } catch {
            // Keep the explicit concurrency message below even if refresh also fails.
          }
          setCoManagerError(
            "La liste des co-chargés a changé. Elle a été rechargée; confirmez votre intention par une nouvelle action.",
          );
        } else {
          setCoManagerError(apiErrorMessage(reason, "Impossible d’ajouter le co-chargé."));
        }
      } else {
        setCoManagerError(
          "Réponse réseau incertaine. Réessayez la même action pour rejouer la même intention.",
        );
      }
    } finally {
      setCoManagerPending(false);
    }
  }

  async function removeCoManager(businessContactId: string) {
    if (
      !selectedProject
      || !projectManagers
      || coManagerPending
      || !canManageContacts
    ) return;

    const expectedVersion = projectManagers.co_managers_version;
    const intent = coManagerIntentKey("REMOVE", businessContactId, expectedVersion);
    const idempotencyKey = idempotencyKeyFor(intent);
    setCoManagerPending(true);
    setCoManagerMessage(null);
    setCoManagerError(null);
    try {
      const result = await removeProjectCoManager(
        selectedProject.number,
        businessContactId,
        expectedVersion,
        idempotencyKey,
      );
      coManagerIntentKeys.current.delete(intent);
      setProjectManagers((current) => current
        ? { ...current, co_managers_version: result.version }
        : current);
      setCoManagerMessage("Co-chargé retiré.");
      try {
        await reloadProjectManagers();
      } catch (reason) {
        setCoManagerError(
          apiErrorMessage(reason, "Retrait enregistré, mais la liste n’a pas pu être rechargée."),
        );
      }
    } catch (reason) {
      if (reason instanceof ApiError) {
        coManagerIntentKeys.current.delete(intent);
        if (reason.code === "project_co_managers_version_conflict") {
          try {
            await reloadProjectManagers();
          } catch {
            // Keep the explicit concurrency message below even if refresh also fails.
          }
          setCoManagerError(
            "La liste des co-chargés a changé. Elle a été rechargée; confirmez votre intention par une nouvelle action.",
          );
        } else {
          setCoManagerError(apiErrorMessage(reason, "Impossible de retirer le co-chargé."));
        }
      } else {
        setCoManagerError(
          "Réponse réseau incertaine. Réessayez la même action pour rejouer la même intention.",
        );
      }
    } finally {
      setCoManagerPending(false);
    }
  }

  async function changeTaskContact(
    task: TaskCatalogItemReadModel,
    field: "operational_responsible_contact_id" | "coordinator_contact_id",
    contactId: string | null,
  ) {
    if (!task.id || contactPending) return;
    setContactPending(true);
    setError(null);
    try {
      const payload = field === "operational_responsible_contact_id"
        ? { operational_responsible_contact_id: contactId }
        : { coordinator_contact_id: contactId };
      const link = await setTaskBusinessContacts(task.id, payload);
      setProjectTasks((current) => current.map((row) => (
        row.id === task.id
          ? {
              ...row,
              operational_responsible_contact_id: link.operational_responsible_contact_id,
              coordinator_contact_id: link.coordinator_contact_id,
            }
          : row
      )));
    } catch (reason) {
      setError(apiErrorMessage(reason, "Impossible d'enregistrer les contacts de la tâche."));
    } finally {
      setContactPending(false);
    }
  }

  async function synchronize() {
    if (!integration?.configured || syncing || !canSyncProjects) return;
    setSyncing(true);
    setSyncMessage(null);
    setSyncError(null);
    try {
      const result = await syncAcumaticaProjects();
      setSyncMessage(
        `${result.received} reçu(s) · ${result.created} créé(s) · ${result.updated} mis à jour · ${result.unchanged} inchangé(s)`,
      );
      const projectRows = await getProjects(false, undefined, scope);
      setProjects(projectRows);
    } catch (reason: unknown) {
      setSyncError(apiErrorMessage(reason, "La synchronisation Acumatica a échoué."));
    } finally {
      setSyncing(false);
    }
  }

  async function synchronizeAllProjectTasks() {
    if (
      !integration?.project_tasks_configured
      || globalTaskSyncing
      || !canSyncProjects
    ) return;
    setGlobalTaskSyncing(true);
    setGlobalTaskSyncRun(null);
    setGlobalTaskSyncMessage(null);
    setGlobalTaskSyncError(null);
    setGlobalTaskSyncTrackingError(null);
    try {
      const run = await syncAcumaticaActiveProjectTasks();
      setGlobalTaskSyncRun(run);
      const active = isGlobalTaskSyncActive(run);
      setGlobalTaskSyncing(active);
      if (!active) {
        if (run.status === "COMPLETED" || run.status === "COMPLETED_WITH_ERRORS") {
          setGlobalTaskSyncMessage(globalTaskSyncSummary(run));
          setRefreshKey((value) => value + 1);
        } else {
          setGlobalTaskSyncError(
            `La synchronisation a échoué${run.error_code ? ` (${run.error_code})` : ""}.`,
          );
        }
      }
    } catch (reason: unknown) {
      setGlobalTaskSyncing(false);
      setGlobalTaskSyncError(
        apiErrorMessage(reason, "La synchronisation globale des tâches ERP a échoué."),
      );
    }
  }

  async function synchronizeProjectTasks() {
    if (!selectedProject || taskSyncing || !canSyncProjects) return;
    setTaskSyncMessage(null);
    setTaskSyncError(null);
    if (!selectedProject.erp_external_id) {
      setTaskSyncError("Ce projet local ne possède pas d’identifiant ERP.");
      return;
    }
    if (!integration?.project_tasks_configured) {
      setTaskSyncError("La synchronisation des tâches ERP n’est pas configurée sur ce serveur.");
      return;
    }

    setTaskSyncing(true);
    try {
      const result = await syncAcumaticaProjectTasks(selectedProject.id);
      setTaskSyncMessage(
        `${result.source_rows} lignes reçues · ${result.task_count} tâches · ${result.rejected_rows} rejet · `
        + `${result.created} créées · ${result.updated} mises à jour · ${result.unchanged} inchangées`,
      );
      const [taskRows, metadata, budget] = await Promise.all([
        getTaskCatalog(selectedProject.number, "", false),
        getAcumaticaProjectTaskSyncMetadata(selectedProject.id),
        getMediumTermBudgetSummary(selectedProject.number, undefined, scope),
      ]);
      setProjectTasks(taskRows);
      setTaskSyncMetadata(metadata);
      setProjectBudget(budget);
    } catch (reason: unknown) {
      setTaskSyncError(apiErrorMessage(reason, "La synchronisation des tâches ERP a échoué."));
    } finally {
      setTaskSyncing(false);
    }
  }

  return (
    <section className="projects-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Portefeuille projets</span>
          <h1>Projets</h1>
          <p>
            Référentiel local utilisé par la planification. Les métadonnées ERP sont synchronisées vers SQL avant d’être consommées par React.
          </p>
        </div>
        <div className="page-heading-actions">
          <ViewScopeSelector />
          <button
            className="projects-refresh"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading || syncing || globalTaskSyncing}
          >
            Actualiser
          </button>
        </div>
      </div>

      <div className="metric-grid projects-metrics">
        <article><span>Total</span><strong>{loading ? "—" : projects.length}</strong><small>Projets en SQL</small></article>
        <article><span>Actifs</span><strong>{loading ? "—" : activeCount}</strong><small>État calculé par le backend</small></article>
        <article><span>Inactifs</span><strong>{loading ? "—" : inactiveCount}</strong><small>Historique conservé</small></article>
        <article><span>Liés ERP</span><strong>{loading ? "—" : erpCount}</strong><small>Identifiant externe présent</small></article>
      </div>

      <section className={`acumatica-card ${integration?.configured ? "is-configured" : "is-local"}`}>
        <div>
          <span className="eyebrow">Intégration ERP</span>
          <h2>Acumatica</h2>
          {integration?.configured ? (
            <p>
              Configurée · endpoint <strong>{integration.endpoint || "—"}</strong> · version <strong>{integration.version || "—"}</strong> · entité <strong>{integration.entity || "—"}</strong>
            </p>
          ) : (
            <p>Non configurée sur ce serveur. Le portefeuille local SQL demeure entièrement utilisable.</p>
          )}
        </div>
        {integration?.configured && (
          canSyncProjects ? (
            <div className="page-heading-actions">
              <button
                type="button"
                onClick={synchronize}
                disabled={syncing || globalTaskSyncing || loading}
              >
                {syncing ? "Synchronisation…" : "Synchroniser les projets"}
              </button>
              {integration.project_tasks_configured && (
                <button
                  type="button"
                  onClick={() => void synchronizeAllProjectTasks()}
                  disabled={globalTaskSyncing || syncing || loading}
                >
                  {globalTaskSyncing
                    ? "Synchronisation en cours…"
                    : "Synchroniser les tâches des projets actifs"}
                </button>
              )}
            </div>
          ) : null
        )}
      </section>

      {syncMessage && <div className="projects-sync-message" role="status">{syncMessage}</div>}
      {syncError && (
        <div className="error-panel">
          <strong>La synchronisation n’a pas été complétée.</strong>
          <span>{syncError}</span>
          <small>Les projets déjà présents en SQL restent inchangés et disponibles.</small>
        </div>
      )}
      {globalTaskSyncing && globalTaskSyncRun && (
        <div className="projects-sync-message" role="status">
          Synchronisation en cours… {globalTaskSyncRun.projects_processed} / {globalTaskSyncRun.projects_total || "…"} projets traités
          {globalTaskSyncRun.source_requests != null ? ` · ${globalTaskSyncRun.source_requests} requête(s) ERP` : ""}
          {globalTaskSyncRun.source_rows_scanned != null ? ` · ${globalTaskSyncRun.source_rows_scanned} lignes ERP parcourues` : ""}
        </div>
      )}
      {!globalTaskSyncing && globalTaskSyncMessage && (
        <div className="projects-sync-message" role="status">{globalTaskSyncMessage}</div>
      )}
      {globalTaskSyncTrackingError && globalTaskSyncing && (
        <div className="projects-sync-message" role="status">
          Suivi temporairement indisponible · {globalTaskSyncTrackingError}
        </div>
      )}
      {!globalTaskSyncing && globalTaskSyncRun?.status === "COMPLETED_WITH_ERRORS" && (
        <div className="projects-sync-message" role="status">
          Erreurs projet : {globalTaskSyncRun.project_results
            .filter((row) => row.status === "rejected")
            .map((row) => `${row.project_number} (${row.error_code || row.reason_code || "rejet"})`)
            .join(" · ")}
        </div>
      )}
      {globalTaskSyncError && (
        <div className="error-panel">
          <strong>La synchronisation globale des tâches n’a pas été complétée.</strong>
          <span>{globalTaskSyncError}</span>
        </div>
      )}

      <div className="filter-bar projects-filters">
        <label className="search-field">
          <span>Recherche</span>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Numéro, projet, client, chargé…"
          />
        </label>
        <label>
          <span>État</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="all">Tous</option>
            {!projectStatuses.some(([key]) => key === "actif") && <option value="actif">Actif</option>}
            {projectStatuses.map(([key, status]) => <option value={key} key={key}>{status}</option>)}
          </select>
        </label>
        <label>
          <span>Chargé de projet</span>
          <select value={managerFilter} onChange={(event) => setManagerFilter(event.target.value)}>
            <option value="all">Tous</option>
            {managers.map((manager) => <option value={manager} key={manager}>{manager}</option>)}
          </select>
        </label>
        <label>
          <span>Source</span>
          <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value as typeof sourceFilter)}>
            <option value="all">Toutes</option>
            <option value="erp">Acumatica</option>
            <option value="local">Local</option>
          </select>
        </label>
      </div>

      {error && (
        <div className="error-panel">
          <strong>Le portefeuille projets n’a pas pu être chargé.</strong>
          <span>{error}</span>
          <small>Vérifie que FastAPI fonctionne et que la base locale est disponible.</small>
        </div>
      )}

      <div className={`projects-table-panel ${loading ? "is-loading" : ""}`}>
        <div className="projects-table-header">
          <strong>{loading ? "Chargement…" : `${filteredProjects.length} projet(s) affiché(s)`}</strong>
          <span>Lecture autoritaire depuis RessourcePlanner SQL</span>
        </div>
        {!loading && !error && filteredProjects.length === 0 ? (
          <div className="projects-empty">Aucun projet ne correspond aux filtres.</div>
        ) : (
          <div className="projects-table-scroll">
            <table className="projects-table">
              <thead>
                <tr>
                  <th>Projet</th>
                  <th>Client</th>
                  <th>Chargé de projet</th>
                  <th>Statut</th>
                  <th>Source</th>
                  <th>Détail</th>
                </tr>
              </thead>
              <tbody>
                {filteredProjects.map((project) => (
                  <tr key={project.id} className={project.active ? "" : "is-inactive"}>
                    <td>
                      <strong>{project.number}</strong>
                      <span>{project.name}</span>
                    </td>
                    <td>{project.client || <span className="projects-muted">Non précisé</span>}</td>
                    <td>{project.project_manager || <span className="projects-muted">Non assigné</span>}</td>
                    <td>
                      <span className={`project-status ${project.active ? "is-active" : "is-inactive"}`}>
                        {project.status}
                      </span>
                    </td>
                    <td>
                      <span className={`project-source ${project.erp_external_id ? "is-erp" : "is-local"}`}>
                        {sourceLabel(project)}
                      </span>
                    </td>
                    <td>
                      <button
                        className="quiet-button"
                        type="button"
                        onClick={() => setSelectedProjectNumber(project.number)}
                      >
                        Ouvrir
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {selectedProjectNumber && (
        <section className="admin-card project-contact-admin">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Détail projet</span>
              <h2>{selectedProjectNumber}</h2>
              <p>Finance ERP en CAD, budget dérivé en heures et WorkPackages restent des lectures distinctes de la projection backend.</p>
            </div>
            <button className="quiet-button" type="button" onClick={() => setSelectedProjectNumber(null)}>Fermer</button>
          </div>

          {canSyncProjects && selectedProject && (
            <div className="acumatica-card">
              <div>
                <span className="eyebrow">Catalogue tâches</span>
                <h3>RP_ProjectTasks</h3>
                {selectedProject.erp_external_id ? (
                  <p>
                    Projet ERP <strong>{selectedProject.number}</strong> · identité technique disponible.
                    {taskSyncMetadata?.last_success_at
                      ? ` Dernier succès : ${new Date(taskSyncMetadata.last_success_at).toLocaleString("fr-CA")}.`
                      : " Aucune synchronisation réussie enregistrée."}
                  </p>
                ) : (
                  <p>Projet local sans identité ERP : aucune synchronisation OData ne sera tentée.</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => void synchronizeProjectTasks()}
                disabled={
                  taskSyncing
                  || !selectedProject.erp_external_id
                  || !integration?.project_tasks_configured
                }
              >
                {taskSyncing ? "Synchronisation…" : "Synchroniser les tâches ERP"}
              </button>
            </div>
          )}
          {taskSyncMessage && <div className="projects-sync-message" role="status">{taskSyncMessage}</div>}
          {taskSyncError && (
            <div className="error-panel">
              <strong>La synchronisation des tâches n’a pas été complétée.</strong>
              <span>{taskSyncError}</span>
            </div>
          )}

          <div className="project-task-contact-list">
            <div className="projects-table-header">
              <strong>Référence hebdomadaire</strong>
              <span>
                Semaine du {formatProjectDate(projectBudget?.reference_week_start)}
                {" · "}
                BudgetActual considéré jusqu’au {formatProjectDate(projectBudget?.actual_through_date)}
              </span>
            </div>
            {projectBudget?.reference_basis === "ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK" && (
              <div className="projects-sync-message" role="status">
                BudgetActual ERP représente la consommation absorbée avant cette semaine; la charge WorkPackage future commence au lundi de référence.
              </div>
            )}
          </div>

          <div className="project-task-contact-list">
            <div className="projects-table-header">
              <strong>Budgets tâches ERP</strong>
              <span>
                Finance ERP en CAD et planification en heures — deux axes distincts.
                {projectBudget?.erp_budget_last_success_at
                  ? ` Budgets ERP synchronisés : ${new Date(projectBudget.erp_budget_last_success_at).toLocaleString("fr-CA")}.`
                  : " Aucune synchronisation ERP réussie enregistrée pour ces budgets."}
              </span>
            </div>
            {projectBudgetLoading ? (
              <p className="projects-empty">Chargement des budgets…</p>
            ) : projectBudgetError ? (
              <div className="error-panel">
                <strong>Les budgets du projet n’ont pas pu être chargés.</strong>
                <span>{projectBudgetError}</span>
              </div>
            ) : projectBudget && projectBudget.tasks.length > 0 ? (
              <div className="projects-table-scroll">
                <table className="projects-table">
                  <thead>
                    <tr>
                      <th>Tâche ERP</th>
                      <th>Budget ERP</th>
                      <th>Actual ERP</th>
                      <th>Restant ERP</th>
                      <th>Coût moyen</th>
                      <th>Budget restant selon Actual</th>
                      <th>Charge WP à partir du lundi</th>
                      <th>Marge après charge future</th>
                      <th>Budget total dérivé</th>
                      <th>WorkPackages structurés</th>
                      <th>Solde de structuration</th>
                      <th>WorkPackages liés</th>
                      <th>Diagnostic</th>
                    </tr>
                  </thead>
                  <tbody>
                    {projectBudget.tasks.map((task) => (
                      <tr key={task.task_catalog_item_id}>
                        <td><strong>{task.task_code}</strong><span>{task.task_label}</span></td>
                        <td>{formatCurrency(task.budget_amount_cad)}</td>
                        <td>{formatCurrency(task.budget_actual_cad)}</td>
                        <td>{formatCurrency(task.remaining_budget_cad)}</td>
                        <td>{formatHourlyRate(task.average_hourly_cost_cad)}</td>
                        <td>{formatHours(task.remaining_budget_hours_from_actual)}</td>
                        <td>{formatHours(task.future_work_package_hours)}</td>
                        <td>{formatHours(task.remaining_after_work_packages_hours)}</td>
                        <td>{formatHours(task.budget_hours)}</td>
                        <td>{formatHours(task.planned_wp_hours)}</td>
                        <td>{formatHours(task.remaining_budget_hours)}</td>
                        <td>
                          {task.work_packages.length === 0 ? (
                            <span className="projects-muted">Aucun WorkPackage associé</span>
                          ) : (
                            <ul>
                              {task.work_packages.map((workPackage) => (
                                <li key={workPackage.id}>
                                  <strong>{workPackage.name}</strong>
                                  <span> · {workPackageWindow(workPackage.start_date, workPackage.end_date)} · {workPackage.status}</span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </td>
                        <td>
                          {financialDiagnosticLabel(task.financial_diagnostic) && (
                            <span className="project-status is-inactive">
                              ⚑ {financialDiagnosticLabel(task.financial_diagnostic)}
                            </span>
                          )}
                          {actualProjectionDiagnosticLabel(task.actual_hours_diagnostic) && (
                            <span className="project-status is-inactive">
                              ⚑ {actualProjectionDiagnosticLabel(task.actual_hours_diagnostic)}
                            </span>
                          )}
                          {actualProjectionDiagnosticLabel(task.future_work_package_diagnostic) && (
                            <span className="project-status is-inactive">
                              ⚑ {actualProjectionDiagnosticLabel(task.future_work_package_diagnostic)}
                            </span>
                          )}
                          <span className={BUDGET_ATTENTION.has(task.diagnostic_state) ? "project-status is-inactive" : "projects-muted"}>
                            {BUDGET_ATTENTION.has(task.diagnostic_state) ? "⚑ " : ""}
                            {budgetDiagnosticLabel(task.diagnostic_state)}
                          </span>
                          {task.budget_source_diagnostic && <small>{task.budget_source_diagnostic}</small>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="projects-empty">Aucune tâche ERP DEPMO dans la projection de ce projet.</p>
            )}
          </div>

          {projectBudget && projectBudget.unclassified_work_packages.length > 0 && (
            <div className="projects-sync-message" role="status">
              ⚑ {projectBudget.unclassified_work_packages.length} WorkPackage(s) historique(s) non classé(s). Aucun rattachement à une tâche ERP n’est déduit du nom ou du code.
            </div>
          )}

          <div className="project-task-contact-list">
            <div className="projects-table-header">
              <strong>Chargés de projet</strong>
              <span>Le principal est autoritaire dans l’ERP; les co-chargés sont administrés dans RessourcePlanner.</span>
            </div>

            {!projectManagers ? (
              <p className="projects-empty">Chargement des chargés de projet…</p>
            ) : (
              <>
                <div className="projects-sync-message" role="status">
                  <strong>Chargé de projet principal (ERP)</strong>{" "}
                  {projectManagers.primary?.display_name || "Non défini dans l’ERP"}
                  {" · Source : ERP"}
                  {projectManagers.primary?.employee_external_id
                    ? ` · ${projectManagers.primary.employee_external_id}`
                    : ""}
                  {projectManagers.primary
                    ? ` · ${PROJECT_MANAGER_STATUS_LABELS[projectManagers.primary.resolution_status] || projectManagers.primary.resolution_status}`
                    : ""}
                  {projectManagers.primary?.diagnostics.length
                    ? ` · ${projectManagers.primary.diagnostics.map(projectManagerDiagnosticLabel).join(" · ")}`
                    : ""}
                  {!projectManagers.primary && projectManagers.diagnostics.length > 0
                    ? ` · ${projectManagers.diagnostics.map(projectManagerDiagnosticLabel).join(" · ")}`
                    : ""}
                </div>

                <div className="projects-table-header">
                  <strong>Co-chargés RessourcePlanner</strong>
                  <span>Version {projectManagers.co_managers_version}</span>
                </div>
                {projectManagers.co_managers.length === 0 ? (
                  <p className="projects-empty">Aucun co-chargé RessourcePlanner.</p>
                ) : (
                  <div className="projects-table-scroll">
                    <table className="projects-table">
                      <thead>
                        <tr><th>Co-chargé</th><th>État</th><th>Source</th><th>Action</th></tr>
                      </thead>
                      <tbody>
                        {projectManagers.co_managers.map((manager) => (
                          <tr key={manager.business_contact_id ?? manager.display_name}>
                            <td>
                              <strong>{manager.display_name}</strong>
                              {manager.app_user_id
                                ? <span>AppUser : {manager.app_user_id}</span>
                                : <span>AppUser non lié</span>}
                            </td>
                            <td>
                              <span>
                                {PROJECT_MANAGER_STATUS_LABELS[manager.resolution_status]
                                  || manager.resolution_status}
                                {manager.contact_active === false ? " · contact inactif" : ""}
                                {manager.user_active === false ? " · utilisateur inactif" : ""}
                              </span>
                              {manager.diagnostics.length > 0 && (
                                <small>
                                  {manager.diagnostics.map(projectManagerDiagnosticLabel).join(" · ")}
                                </small>
                              )}
                            </td>
                            <td>{manager.sources.join(" + ")}</td>
                            <td>
                              {canManageContacts && manager.business_contact_id && (
                                <button
                                  type="button"
                                  onClick={() => void removeCoManager(manager.business_contact_id!)}
                                  disabled={coManagerPending}
                                >
                                  Retirer
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {canManageContacts && (
                  <div className="projects-sync-message">
                    <strong>Ajouter un co-chargé</strong>
                    <input
                      type="search"
                      value={coManagerSearch}
                      onChange={(event) => setCoManagerSearch(event.target.value)}
                      placeholder="Rechercher un contact actif"
                      aria-label="Rechercher un co-chargé"
                      disabled={coManagerPending}
                    />
                    <select
                      value={selectedCoManagerId}
                      onChange={(event) => setSelectedCoManagerId(event.target.value)}
                      disabled={coManagerPending}
                      aria-label="Sélectionner un co-chargé"
                    >
                      <option value="">Sélectionner un BusinessContact actif</option>
                      {coManagerCandidates.map((contact) => (
                        <option key={contact.id} value={contact.id}>
                          {contact.display_name} · {contact.email || contact.external_id || contact.id}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      onClick={() => void addCoManager()}
                      disabled={coManagerPending || !selectedCoManagerId}
                    >
                      Ajouter
                    </button>
                  </div>
                )}

                {coManagerMessage && (
                  <div className="projects-sync-message" role="status">{coManagerMessage}</div>
                )}
                {coManagerError && (
                  <div className="projects-sync-message is-error" role="alert">{coManagerError}</div>
                )}
              </>
            )}
          </div>

          {canManageContacts && (
            <div className="project-task-contact-list">
              <div className="projects-table-header">
                <strong>Tâches ERP</strong>
                <span>Responsable opérationnel et coordonnateur sont deux fonctions distinctes.</span>
              </div>
              {projectTasks.length === 0 ? (
                <p className="projects-empty">Aucune tâche ERP pour ce projet.</p>
              ) : (
                <div className="projects-table-scroll">
                  <table className="projects-table">
                    <thead>
                      <tr><th>Tâche</th><th>Responsable opérationnel</th><th>Coordonnateur</th></tr>
                    </thead>
                    <tbody>
                      {projectTasks.map((task) => (
                        <tr key={task.id ?? `${task.project_number}:${task.code}`}>
                          <td><strong>{task.code}</strong><span>{task.label}</span></td>
                          <td>
                            <ContactSelect
                              contacts={contacts}
                              value={task.operational_responsible_contact_id}
                              onChange={(value) => void changeTaskContact(task, "operational_responsible_contact_id", value)}
                              disabled={contactPending || !task.id}
                              inheritLabel="Hériter du chargé de projet ERP"
                            />
                          </td>
                          <td>
                            <ContactSelect
                              contacts={contacts}
                              value={task.coordinator_contact_id}
                              onChange={(value) => void changeTaskContact(task, "coordinator_contact_id", value)}
                              disabled={contactPending || !task.id}
                              inheritLabel="Aucun coordonnateur de tâche"
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </section>
      )}

    </section>
  );
}