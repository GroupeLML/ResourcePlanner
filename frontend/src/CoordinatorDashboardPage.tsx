import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  CoordinatorDashboardActionReadModel,
  CoordinatorDashboardReadModel,
  getCoordinatorDashboard,
} from "./api";

type CoordinatorDashboardPageProps = {
  onOpenDemand: (demandNumber: string) => void;
  onOpenDemands: () => void;
  onOpenPlanning: () => void;
};

type CategoryFilter = "ALL" | "ASSIGNMENT" | "CANCELLATION" | "APPROVAL" | "COVERAGE";

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error
    ? reason.message
    : "Impossible de charger le dashboard coordonnateur.";
}

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("fr-CA", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

function attentionLabel(value: string) {
  if (value === "URGENT") return "Urgent";
  if (value === "OVERDUE") return "En retard";
  if (value === "SOON") return "Bientôt";
  return "À suivre";
}

function responsibilitySourceLabel(value: string | null | undefined) {
  switch ((value ?? "").toUpperCase()) {
    case "SHIFT_OVERRIDE": return "quart";
    case "RESOURCE_REQUIREMENT_OVERRIDE": return "besoin";
    case "REQUEST_OVERRIDE": return "demande";
    case "TASK_RESPONSIBLE": return "tâche";
    case "PROJECT_OVERRIDE": return "projet";
    case "PROJECT_MANAGER": return "chargé ERP";
    default: return value || "non résolu";
  }
}

function actionSearchText(action: CoordinatorDashboardActionReadModel) {
  return normalize(
    [
      action.label,
      action.detail,
      action.demand_number,
      action.project_number,
      action.project_name,
      action.status,
      action.priority,
      action.operational_responsible_display_name,
      action.operational_responsible_source_type,
    ]
      .filter(Boolean)
      .join(" "),
  );
}

export default function CoordinatorDashboardPage({
  onOpenDemand,
  onOpenDemands,
  onOpenPlanning,
}: CoordinatorDashboardPageProps) {
  const [dashboard, setDashboard] = useState<CoordinatorDashboardReadModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState<CategoryFilter>("ALL");
  const [search, setSearch] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getCoordinatorDashboard(controller.signal)
      .then(setDashboard)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const visibleActions = useMemo(() => {
    const query = normalize(search);
    return (dashboard?.actions ?? []).filter((action) => {
      if (category !== "ALL" && action.category !== category) return false;
      return !query || actionSearchText(action).includes(query);
    });
  }, [dashboard, category, search]);

  if (loading && !dashboard) {
    return (
      <section className="coordinator-dashboard">
        <div className="dashboard-loading">Chargement du dashboard coordonnateur…</div>
      </section>
    );
  }

  if (error && !dashboard) {
    return (
      <section className="coordinator-dashboard">
        <div className="error-panel">
          <strong>Impossible de charger le dashboard coordonnateur.</strong>
          <span>{error}</span>
        </div>
      </section>
    );
  }

  if (!dashboard) return null;

  return (
    <section className="coordinator-dashboard">
      <header className="coordinator-dashboard-heading">
        <div>
          <span className="eyebrow">Coordination</span>
          <h1>Tableau de bord coordonnateur</h1>
          <p>
            Actions et demandes calculées par les projections backend au {formatDate(dashboard.as_of)}.
          </p>
        </div>
        <div className="coordinator-dashboard-shortcuts">
          <button type="button" className="secondary" onClick={onOpenDemands}>
            Demandes
          </button>
          <button type="button" onClick={onOpenPlanning}>
            Planning
          </button>
        </div>
      </header>

      {error && (
        <div className="dashboard-inline-error">
          Actualisation incomplète : {error}
        </div>
      )}

      <div className="coordinator-kpi-grid" aria-label="Indicateurs coordonnateur">
        <article>
          <span>Mon périmètre</span>
          <strong>{dashboard.kpis.personal_demands}</strong>
          <small>demandes actives</small>
        </article>
        <article>
          <span>À traiter</span>
          <strong>{dashboard.kpis.total_actions}</strong>
          <small>actions backend</small>
        </article>
        <article>
          <span>Attributions</span>
          <strong>{dashboard.kpis.assignments}</strong>
          <small>humaines ou actifs</small>
        </article>
        <article>
          <span>Annulations</span>
          <strong>{dashboard.kpis.cancellations}</strong>
          <small>à résoudre</small>
        </article>
        <article>
          <span>Approbations</span>
          <strong>{dashboard.kpis.approvals}</strong>
          <small>lignes admissibles</small>
        </article>
        <article>
          <span>Couverture / conflits</span>
          <strong>{dashboard.kpis.coverage_issues}</strong>
          <small>
            {dashboard.kpis.partial_coverages} partielle(s) · {dashboard.kpis.conflicts} conflit(s)
          </small>
        </article>
        <article className="attention-kpi">
          <span>Attention</span>
          <strong>{dashboard.kpis.attention_items}</strong>
          <small>urgentes, en retard ou proches</small>
        </article>
      </div>

      <section className="coordinator-dashboard-panel">
        <div className="coordinator-panel-heading">
          <div>
            <span className="eyebrow">File d’action</span>
            <h2>À traiter</h2>
          </div>
          <span>{visibleActions.length} / {dashboard.actions.length}</span>
        </div>

        <div className="coordinator-action-filters">
          <label>
            <span>Rechercher</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Demande, projet, action…"
            />
          </label>
          <label>
            <span>Type</span>
            <select
              value={category}
              onChange={(event) => setCategory(event.target.value as CategoryFilter)}
            >
              <option value="ALL">Toutes les actions</option>
              <option value="ASSIGNMENT">Attributions</option>
              <option value="CANCELLATION">Annulations</option>
              <option value="APPROVAL">Approbations</option>
              <option value="COVERAGE">Couverture / conflits</option>
            </select>
          </label>
        </div>

        <div className="coordinator-action-table-wrap">
          <table className="coordinator-action-table">
            <thead>
              <tr>
                <th>Attention</th>
                <th>Action</th>
                <th>Demande / projet</th>
                <th>Début</th>
                <th>État</th>
                <th aria-label="Navigation" />
              </tr>
            </thead>
            <tbody>
              {visibleActions.map((action) => (
                <tr key={action.action_id}>
                  <td>
                    <span className={`dashboard-attention attention-${action.attention.toLowerCase()}`}>
                      {attentionLabel(action.attention)}
                    </span>
                  </td>
                  <td>
                    <strong>{action.label}</strong>
                    {action.detail && <small>{action.detail}</small>}
                    {action.remaining_hours != null && (
                      <small>{action.remaining_hours} h restantes</small>
                    )}
                    {action.operational_responsible_status && (
                      <small>
                        Responsable : {action.operational_responsible_display_name || "Non résolu"}
                        {" · source "}
                        {responsibilitySourceLabel(action.operational_responsible_source_type)}
                      </small>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="dashboard-link-button"
                      onClick={() => onOpenDemand(action.demand_number)}
                    >
                      {action.demand_number}
                    </button>
                    <small>
                      {action.project_number || "Projet non défini"}
                      {action.project_name ? ` — ${action.project_name}` : ""}
                    </small>
                  </td>
                  <td>
                    <span>{formatDate(action.start_date)}</span>
                    {action.days_until_start != null && (
                      <small>
                        {action.days_until_start < 0
                          ? `${Math.abs(action.days_until_start)} j en retard`
                          : `dans ${action.days_until_start} j`}
                      </small>
                    )}
                  </td>
                  <td>
                    <span>{action.status || "—"}</span>
                    {action.priority && <small>{action.priority}</small>}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="secondary dashboard-open-button"
                      onClick={() => (
                        action.target === "PLANNING"
                          ? onOpenPlanning()
                          : onOpenDemand(action.demand_number)
                      )}
                    >
                      Ouvrir
                    </button>
                  </td>
                </tr>
              ))}
              {visibleActions.length === 0 && (
                <tr>
                  <td colSpan={6} className="dashboard-empty-row">
                    Aucune action ne correspond aux filtres.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="coordinator-dashboard-panel">
        <div className="coordinator-panel-heading">
          <div>
            <span className="eyebrow">Périmètre #410</span>
            <h2>Mes demandes actives</h2>
          </div>
          <span>{dashboard.personal_demands.length}</span>
        </div>
        <div className="coordinator-demand-grid">
          {dashboard.personal_demands.map((demand) => (
            <button
              type="button"
              className="coordinator-demand-card"
              onClick={() => onOpenDemand(demand.demand_number)}
              key={demand.demand_number}
            >
              <div>
                <strong>{demand.demand_number}</strong>
                <span className={`dashboard-attention attention-${demand.attention.toLowerCase()}`}>
                  {attentionLabel(demand.attention)}
                </span>
              </div>
              <span>
                {demand.project_number || "Projet non défini"}
                {demand.project_name ? ` — ${demand.project_name}` : ""}
              </span>
              <small>
                {demand.effective_status} · début {formatDate(demand.desired_start)}
                {demand.cancellation_pending ? " · annulation demandée" : ""}
              </small>
            </button>
          ))}
          {dashboard.personal_demands.length === 0 && (
            <div className="dashboard-empty-card">
              Aucune demande active dans votre périmètre coordonnateur.
            </div>
          )}
        </div>
      </section>
    </section>
  );
}
