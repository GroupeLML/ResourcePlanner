import { useEffect, useMemo, useState } from "react";

import { useAuth } from "./AuthContext";
import { useViewScope } from "./ViewScopeContext";
import ViewScopeSelector from "./ViewScopeSelector";
import {
  AcumaticaIntegrationStatus,
  ApiError,
  BusinessContactReadModel,
  ContactLinkReadModel,
  ProjectReadModel,
  TaskCatalogItemReadModel,
  getAcumaticaIntegrationStatus,
  getBusinessContacts,
  getProjectBusinessContacts,
  getProjects,
  getTaskCatalog,
  setProjectManagerContact,
  setTaskBusinessContacts,
  syncAcumaticaProjects,
} from "./api";
import { ContactSelect } from "./BusinessContactUi";

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function sourceLabel(project: ProjectReadModel) {
  return project.erp_external_id ? "Acumatica" : "Local";
}

function apiErrorMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

export default function ProjectsPage() {
  const { can } = useAuth();
  const { scope, loading: scopeLoading } = useViewScope();
  const canSyncProjects = can("sync_projects");
  const canManageContacts = can("manage_resources");
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [contacts, setContacts] = useState<BusinessContactReadModel[]>([]);
  const [selectedProjectNumber, setSelectedProjectNumber] = useState<string | null>(null);
  const [projectContactLink, setProjectContactLink] = useState<ContactLinkReadModel | null>(null);
  const [projectTasks, setProjectTasks] = useState<TaskCatalogItemReadModel[]>([]);
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
  const [refreshKey, setRefreshKey] = useState(0);

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
    if (!selectedProjectNumber) {
      setProjectContactLink(null);
      setProjectTasks([]);
      return;
    }
    const controller = new AbortController();
    Promise.all([
      getProjectBusinessContacts(selectedProjectNumber, controller.signal),
      getTaskCatalog(selectedProjectNumber, "", false, controller.signal),
    ])
      .then(([link, taskRows]) => {
        setProjectContactLink(link);
        setProjectTasks(taskRows);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(apiErrorMessage(reason, "Impossible de charger les contacts métier du projet."));
      });
    return () => controller.abort();
  }, [selectedProjectNumber, refreshKey]);

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

  async function changeProjectManager(contactId: string | null) {
    if (!selectedProjectNumber || contactPending) return;
    setContactPending(true);
    setError(null);
    try {
      const link = await setProjectManagerContact(selectedProjectNumber, contactId);
      setProjectContactLink(link);
    } catch (reason) {
      setError(apiErrorMessage(reason, "Impossible d'enregistrer le chargé de projet métier."));
    } finally {
      setContactPending(false);
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
            disabled={loading || syncing}
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
            <button type="button" onClick={synchronize} disabled={syncing || loading}>
              {syncing ? "Synchronisation…" : "Synchroniser les projets"}
            </button>
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
                  {canManageContacts && <th>Contacts</th>}
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
                    {canManageContacts && (
                      <td>
                        <button
                          className="quiet-button"
                          type="button"
                          onClick={() => setSelectedProjectNumber(project.number)}
                        >
                          Configurer
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {canManageContacts && selectedProjectNumber && (
        <section className="admin-card project-contact-admin">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Contacts métier</span>
              <h2>{selectedProjectNumber}</h2>
              <p>Le chargé de projet est le dernier fallback du responsable opérationnel. Les tâches peuvent définir leur propre responsable et coordonnateur.</p>
            </div>
            <button className="quiet-button" type="button" onClick={() => setSelectedProjectNumber(null)}>Fermer</button>
          </div>

          <label>
            Chargé de projet
            <ContactSelect
              contacts={contacts}
              value={projectContactLink?.project_manager_contact_id ?? null}
              onChange={(value) => void changeProjectManager(value)}
              disabled={contactPending}
              inheritLabel="Aucun contact métier lié"
            />
          </label>

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
                            inheritLabel="Hériter du chargé de projet"
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
        </section>
      )}

    </section>
  );
}