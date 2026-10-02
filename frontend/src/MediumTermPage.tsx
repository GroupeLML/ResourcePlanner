import { CSSProperties, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  DemandReadModel,
  MediumTermBudgetReadModel,
  MediumTermBudgetTaskReadModel,
  MediumTermBudgetWorkPackageReadModel,
  MediumTermUnlinkedSegmentReadModel,
  PendingDemandLoadReadModel,
  PlanningSnapshotReadModel,
  ProjectReadModel,
  ResourceReadModel,
  WorkPackageReadModel,
  getMediumTermBudget,
  getMediumTermUnlinkedSegments,
  getPlanningSnapshot,
  getProjects,
  getResources,
  getWorkPackages,
} from "./api";
import {
  addDays,
  dayDistance,
  formatWeekRange,
  parseIsoDate,
  startOfWeek,
  toIsoDate,
} from "./dates";
import MediumTermCapacityPanel from "./MediumTermCapacityPanel";
import MediumTermUnlinkedSegmentsPanel from "./MediumTermUnlinkedSegmentsPanel";
import SegmentEditor from "./SegmentEditor";
import { useViewScope } from "./ViewScopeContext";
import ViewScopeSelector from "./ViewScopeSelector";
import WorkPackageEditor from "./WorkPackageEditor";

const HORIZONS = [4, 8, 12] as const;
type HorizonWeeks = (typeof HORIZONS)[number];
type BudgetMode = "initial" | "remaining";

type ProjectTaskGroup = {
  project_id: string;
  project_number: string;
  project_name: string;
  tasks: MediumTermBudgetTaskReadModel[];
};

type ManagerTaskGroup = {
  key: string;
  label: string;
  resolution_status: string;
  diagnostics: string[];
  projects: ProjectTaskGroup[];
};

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

const WEEKLY_LOAD_DIAGNOSTIC_LABELS: Record<string, string> = {
  WEEKLY_LOAD_MISSING: "Répartition hebdomadaire manquante",
  WEEKLY_LOAD_DATES_MISSING: "Dates requises pour la répartition",
  WEEKLY_LOAD_TOTAL_UNKNOWN: "Charge totale inconnue",
  WEEKLY_LOAD_INCONSISTENT: "Répartition hebdomadaire incohérente",
};

const RESOURCE_CLASS_DIVERGENCE = "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE";

const PROJECTION_DIAGNOSTIC_LABELS: Record<string, string> = {
  UNCLASSIFIED_WORK_PACKAGES: "Des WorkPackages historiques ne sont liés à aucune tâche ERP.",
  UNCLASSIFIED_WORK_PACKAGE_LOAD: "Une charge WorkPackage sans classe canonique est conservée dans « Non classé ».",
};

function workPackageResourceClassLabel(workPackage: MediumTermBudgetWorkPackageReadModel) {
  if (!workPackage.resource_class_code) return "Non définie";
  return workPackage.resource_class_label
    ? `${workPackage.resource_class_label} (${workPackage.resource_class_code})`
    : workPackage.resource_class_code;
}

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function hours(value: number | null | undefined) {
  if (value == null) return "—";
  return `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(value)} h`;
}

function cad(value: number | null | undefined) {
  if (value == null) return "Indisponible";
  return new Intl.NumberFormat("fr-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 2,
  }).format(value);
}

function erpFreshness(value: string | null | undefined) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function isoWeekNumber(value: Date) {
  const date = new Date(Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  return Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

function placement(
  workPackage: MediumTermBudgetWorkPackageReadModel,
  horizonStart: Date,
  horizonWeeks: number,
) {
  if (!workPackage.start_date && !workPackage.end_date) return null;
  const horizonEnd = addDays(horizonStart, horizonWeeks * 7 - 1);
  const rawStart = workPackage.start_date ? parseIsoDate(workPackage.start_date) : horizonStart;
  const rawEnd = workPackage.end_date ? parseIsoDate(workPackage.end_date) : rawStart;
  if (rawEnd < horizonStart || rawStart > horizonEnd) return null;

  const clampedStart = rawStart < horizonStart ? horizonStart : rawStart;
  const clampedEnd = rawEnd > horizonEnd ? horizonEnd : rawEnd;
  const firstWeek = Math.max(0, Math.floor(dayDistance(horizonStart, clampedStart) / 7));
  const lastWeek = Math.min(
    horizonWeeks - 1,
    Math.floor(dayDistance(horizonStart, clampedEnd) / 7),
  );
  return { column: firstWeek + 2, span: Math.max(1, lastWeek - firstWeek + 1) };
}

function isTentative(demand: DemandReadModel) {
  return normalize(demand.confirmation).includes("tentative");
}

function demandTone(demand: DemandReadModel, pending: boolean, hasApprovedPlan: boolean) {
  if (pending) return "pending";
  if (hasApprovedPlan && isTentative(demand)) return "tentative";
  if (hasApprovedPlan) return "planned";
  const status = normalize(demand.status);
  if (status.includes("correction")) return "correction";
  if (status.includes("brouillon")) return "draft";
  return "neutral";
}

function demandDetails(demand: DemandReadModel, label: string) {
  const window = `${demand.desired_start || "Date à préciser"} → ${demand.desired_end || demand.desired_start || "Date à préciser"}`;
  const estimate = demand.estimated_hours == null ? "Heures à préciser" : hours(demand.estimated_hours);
  const resources = `${demand.resource_count} ressource${demand.resource_count > 1 ? "s" : ""}`;
  const competencies = demand.required_competencies || "Compétence à préciser";
  return [
    demand.project_number || "Projet non précisé",
    demand.number,
    label,
    window,
    estimate,
    resources,
    competencies,
  ].join(" · ");
}

function diagnosticLabel(code: string) {
  return BUDGET_DIAGNOSTIC_LABELS[code] || code;
}

function WorkPackageRow({
  project,
  workPackage,
  baseWorkPackage,
  demands,
  pendingLoads,
  plannedDemandNumbers,
  horizonStart,
  horizonWeeks,
  onOpenDemands,
  onEdit,
}: {
  project: ProjectReadModel;
  workPackage: MediumTermBudgetWorkPackageReadModel;
  baseWorkPackage: WorkPackageReadModel | null;
  demands: DemandReadModel[];
  pendingLoads: PendingDemandLoadReadModel[];
  plannedDemandNumbers: Set<string>;
  horizonStart: Date;
  horizonWeeks: number;
  onOpenDemands: () => void;
  onEdit: (workPackage: WorkPackageReadModel) => void;
}) {
  const grid = placement(workPackage, horizonStart, horizonWeeks);
  const template = `300px repeat(${horizonWeeks}, minmax(96px, 1fr))`;
  const pendingByDemand = new Map(pendingLoads.map((load) => [load.demand_number, load]));
  const loadDiagnostic = workPackage.weekly_load_diagnostic
    ? WEEKLY_LOAD_DIAGNOSTIC_LABELS[workPackage.weekly_load_diagnostic] || workPackage.weekly_load_diagnostic
    : null;
  const resourceClassLabel = workPackageResourceClassLabel(workPackage);
  const classDivergesFromTask = workPackage.resource_class_diagnostic === RESOURCE_CLASS_DIVERGENCE;

  return (
    <div className="mt-timeline-row" style={{ gridTemplateColumns: template }}>
      <div className="mt-package-identity">
        <div className="mt-package-title">
          <strong>{workPackage.reference}</strong>
          <span className="mt-status-pill">{workPackage.status}</span>
        </div>
        <span>{workPackage.name}</span>
        <small
          className={`mt-package-resource-class ${workPackage.resource_class_active === false ? "is-inactive" : ""}`}
        >
          Classe de ressource : {resourceClassLabel}
          {workPackage.resource_class_active === false ? " · inactive" : ""}
        </small>
        {workPackage.code && <small>Code WorkPackage : {workPackage.code}</small>}
        <small>
          {workPackage.start_date || "Date à préciser"}
          {workPackage.end_date ? ` → ${workPackage.end_date}` : ""}
          {workPackage.planned_hours != null ? ` · ${hours(workPackage.planned_hours)}` : ""}
        </small>
        <div className="mt-package-load-state">
          {workPackage.weekly_load_origin && (
            <span>Répartition {workPackage.weekly_load_origin}</span>
          )}
          {loadDiagnostic && <span className="is-attention">⚑ {loadDiagnostic}</span>}
          {classDivergesFromTask && (
            <span
              className="is-attention"
              title="La classe du WorkPackage demeure autoritaire; aucune correction automatique vers la classe de la tâche ERP n’est effectuée."
            >
              ⚑ Classe WorkPackage différente de la tâche ERP
            </span>
          )}
          {!workPackage.current_load_included && (
            <span>Hors charge courante{workPackage.budget_included ? " · conservé au budget" : ""}</span>
          )}
        </div>
        {baseWorkPackage ? (
          <button className="mt-edit-package" type="button" onClick={() => onEdit(baseWorkPackage)}>
            Modifier / répartir
          </button>
        ) : (
          <small>Édition indisponible jusqu’au rechargement du catalogue WorkPackage.</small>
        )}
      </div>

      {Array.from({ length: horizonWeeks }, (_, index) => (
        <div className="mt-week-cell" style={{ gridColumn: index + 2 }} key={index} />
      ))}

      <article
        className={`mt-package-bar ${grid ? "" : "is-unscheduled"} ${workPackage.current_load_included ? "" : "is-current-load-excluded"}`}
        style={grid
          ? { gridColumn: `${grid.column} / span ${grid.span}` }
          : { gridColumn: `2 / span ${horizonWeeks}` }}
        title={`${project.number} — ${workPackage.name}`}
      >
        <div className="mt-package-bar-heading">
          <strong>{workPackage.name}</strong>
          <span>{hours(workPackage.planned_hours)}</span>
        </div>
        <small>Classe de ressource : {resourceClassLabel}</small>
        {baseWorkPackage?.description && <small>{baseWorkPackage.description}</small>}
        <div className="mt-demand-chips">
          {demands.length === 0 ? (
            <span className="mt-demand-empty">Aucune demande dans l’horizon</span>
          ) : demands.map((demand) => {
            const pendingLoad = pendingByDemand.get(demand.number);
            const planned = plannedDemandNumbers.has(demand.number);
            const replacement = normalize(pendingLoad?.mode) === "replacement";
            const tentative = isTentative(demand);
            const tone = demandTone(demand, Boolean(pendingLoad), planned);
            const label = pendingLoad
              ? replacement ? "Soumise · modification en attente" : "Soumise · charge potentielle"
              : planned
                ? tentative ? "Plan approuvé · tentative" : "Plan approuvé · confirmée"
                : demand.status;
            return (
              <button
                type="button"
                className={`mt-demand-chip ${tone}`}
                key={demand.number}
                onClick={onOpenDemands}
                title={demandDetails(demand, label)}
                aria-label={demandDetails(demand, label)}
              >
                <strong>{demand.number}</strong>
                <span>{label}</span>
                <small>
                  {hours(demand.estimated_hours)} · {demand.resource_count} res. · {demand.required_competencies || "Comp. à préciser"}
                </small>
              </button>
            );
          })}
        </div>
      </article>
    </div>
  );
}

function TaskHeader({
  task,
  budgetMode,
}: {
  task: MediumTermBudgetTaskReadModel;
  budgetMode: BudgetMode;
}) {
  const attention = BUDGET_ATTENTION.has(task.diagnostic_state);
  const financialValue = budgetMode === "initial"
    ? task.budget_amount_cad
    : task.remaining_budget_cad;
  const financialLabel = budgetMode === "initial" ? "Budget initial" : "Budget restant";
  const freshness = erpFreshness(task.erp_budget_last_success_at);

  return (
    <header className={`mt-task-strip ${attention ? "has-attention" : ""}`}>
      <div className="mt-task-title">
        <strong>{task.task_code}</strong>
        <span>{task.task_label}</span>
        {attention && <span className="mt-yellow-flag" title={task.diagnostic_state}>⚑</span>}
      </div>
      <div className="mt-task-budget" aria-label={`Budget de la tâche ${task.task_code}`}>
        <span>{financialLabel} <strong>{cad(financialValue)}</strong></span>
        <span>Charge WP <strong>{hours(task.planned_wp_hours)}</strong></span>
        <span>Solde structuré <strong>{hours(task.remaining_budget_hours)}</strong></span>
        <span>{task.associated_work_package_count} WP</span>
      </div>
      <small>
        {diagnosticLabel(task.diagnostic_state)}
        {task.financial_diagnostic ? ` · ${task.financial_diagnostic}` : ""}
        {task.budget_source_diagnostic ? ` · ${task.budget_source_diagnostic}` : ""}
        {freshness ? ` · ERP synchronisé ${freshness}` : ""}
      </small>
    </header>
  );
}

export default function MediumTermPage({ onOpenDemands }: { onOpenDemands: () => void }) {
  const { scope, loading: scopeLoading, error: scopeError } = useViewScope();
  const [horizonStart, setHorizonStart] = useState(() => startOfWeek(new Date()));
  const [horizonWeeks, setHorizonWeeks] = useState<HorizonWeeks>(8);
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [catalogProjects, setCatalogProjects] = useState<ProjectReadModel[]>([]);
  const [workPackages, setWorkPackages] = useState<WorkPackageReadModel[]>([]);
  const [resources, setResources] = useState<ResourceReadModel[]>([]);
  const [unlinkedSegments, setUnlinkedSegments] = useState<MediumTermUnlinkedSegmentReadModel[]>([]);
  const [snapshot, setSnapshot] = useState<PlanningSnapshotReadModel | null>(null);
  const [projection, setProjection] = useState<MediumTermBudgetReadModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [projectionLoading, setProjectionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectionError, setProjectionError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [taskFilter, setTaskFilter] = useState("");
  const [resourceClassFilter, setResourceClassFilter] = useState("");
  const [includeInactiveProjects, setIncludeInactiveProjects] = useState(false);
  const [budgetMode, setBudgetMode] = useState<BudgetMode>("initial");
  const [collapsedManagers, setCollapsedManagers] = useState<Set<string>>(() => new Set());
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(() => new Set());
  const [editor, setEditor] = useState<WorkPackageReadModel | null | undefined>(undefined);
  const [segmentEditorId, setSegmentEditorId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const horizonEnd = useMemo(
    () => addDays(horizonStart, horizonWeeks * 7 - 1),
    [horizonStart, horizonWeeks],
  );
  const start = toIsoDate(horizonStart);
  const end = toIsoDate(horizonEnd);

  useEffect(() => {
    if (scopeLoading) return;
    if (scopeError) {
      setLoading(false);
      setError(scopeError);
      setProjects([]);
      setWorkPackages([]);
      setSnapshot(null);
      setUnlinkedSegments([]);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getProjects(!includeInactiveProjects, controller.signal, scope),
      getProjects(true, controller.signal, "global"),
      getWorkPackages("", false, controller.signal, scope),
      getResources(true, controller.signal),
      getPlanningSnapshot(start, end, controller.signal, scope),
      getMediumTermUnlinkedSegments(start, end, controller.signal, scope),
    ])
      .then(([projectRows, projectCatalogRows, packageRows, resourceRows, planning, unlinkedRows]) => {
        setProjects(projectRows);
        setCatalogProjects(projectCatalogRows);
        setWorkPackages(packageRows);
        setResources(resourceRows);
        setSnapshot(planning);
        setUnlinkedSegments(unlinkedRows);
        setProjectFilter((current) => (
          current && projectRows.some((project) => project.number === current)
            ? current
            : ""
        ));
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        if (reason instanceof ApiError) {
          setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
          return;
        }
        setError(reason instanceof Error ? reason.message : "Impossible de charger le moyen terme.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [start, end, refreshKey, scope, scopeLoading, scopeError, includeInactiveProjects]);

  useEffect(() => {
    if (scopeLoading || scopeError) {
      setProjection(null);
      setProjectionError(null);
      return;
    }
    const controller = new AbortController();
    setProjectionLoading(true);
    setProjectionError(null);
    getMediumTermBudget(
      projectFilter,
      start,
      end,
      controller.signal,
      scope,
      {
        taskCatalogItemId: taskFilter || undefined,
        resourceClassCode: resourceClassFilter || undefined,
        includeInactiveProjects,
      },
    )
      .then(setProjection)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setProjection(null);
        if (reason instanceof ApiError) {
          if (reason.status === 404) {
            setProjectionError("Le projet sélectionné n’existe plus dans ce périmètre. Recharge la vue ou choisis un autre projet.");
          } else if (reason.status === 422) {
            setProjectionError(`La fenêtre Moyen terme est invalide : ${reason.message}`);
          } else {
            setProjectionError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
          }
          return;
        }
        setProjectionError(reason instanceof Error ? reason.message : "Impossible de charger la projection Moyen terme.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setProjectionLoading(false);
      });
    return () => controller.abort();
  }, [
    projectFilter,
    taskFilter,
    resourceClassFilter,
    includeInactiveProjects,
    start,
    end,
    refreshKey,
    scope,
    scopeLoading,
    scopeError,
  ]);

  useEffect(() => {
    if (!projection || !taskFilter) return;
    if (!projection.task_options.some((task) => task.task_catalog_item_id === taskFilter)) {
      setTaskFilter("");
    }
  }, [projection, taskFilter]);

  useEffect(() => {
    if (!projection || !resourceClassFilter) return;
    if (!projection.resource_classes.some((resourceClass) => resourceClass.code === resourceClassFilter)) {
      setResourceClassFilter("");
    }
  }, [projection, resourceClassFilter]);

  const weeks = useMemo(
    () => Array.from({ length: horizonWeeks }, (_, index) => addDays(horizonStart, index * 7)),
    [horizonStart, horizonWeeks],
  );

  const projectOptions = useMemo(
    () => [...projects].sort((left, right) => left.number.localeCompare(right.number, "fr-CA")),
    [projects],
  );

  const taskOptions = useMemo(
    () => [...(projection?.task_options ?? [])].sort((left, right) => (
      left.project_number.localeCompare(right.project_number, "fr-CA")
      || left.task_code.localeCompare(right.task_code, "fr-CA")
      || left.task_label.localeCompare(right.task_label, "fr-CA")
    )),
    [projection],
  );

  const resourceClassOptions = useMemo(
    () => [...(projection?.resource_classes ?? [])].sort((left, right) => (
      left.label.localeCompare(right.label, "fr-CA") || left.code.localeCompare(right.code, "fr-CA")
    )),
    [projection],
  );

  const workPackagesByReference = useMemo(
    () => new Map(workPackages.map((workPackage) => [workPackage.reference, workPackage])),
    [workPackages],
  );

  const demandByPackage = useMemo(() => {
    const result = new Map<string, DemandReadModel[]>();
    (snapshot?.demands ?? []).forEach((demand) => {
      if (!demand.work_package_ref) return;
      const rows = result.get(demand.work_package_ref) ?? [];
      rows.push(demand);
      result.set(demand.work_package_ref, rows);
    });
    return result;
  }, [snapshot]);

  const pendingByPackage = useMemo(() => {
    const result = new Map<string, PendingDemandLoadReadModel[]>();
    (snapshot?.pending_loads ?? []).forEach((load) => {
      if (!load.work_package_ref) return;
      const rows = result.get(load.work_package_ref) ?? [];
      rows.push(load);
      result.set(load.work_package_ref, rows);
    });
    return result;
  }, [snapshot]);

  const plannedDemandNumbers = useMemo(
    () => new Set(
      (snapshot?.segments ?? [])
        .map((segment) => segment.demand_number)
        .filter((number): number is string => Boolean(number)),
    ),
    [snapshot],
  );

  const query = normalize(search);
  const visibleTasks = useMemo(
    () => (projection?.tasks ?? []).filter((task) => {
      if (!query) return true;
      return normalize([
        task.task_code,
        task.task_label,
        task.project_number,
        task.project_name,
        task.project_manager_display_name,
        ...task.work_packages.flatMap((workPackage) => [
          workPackage.reference,
          workPackage.code,
          workPackage.name,
          workPackage.resource_class_code,
          workPackage.resource_class_label,
        ]),
      ].filter(Boolean).join(" ")).includes(query);
    }),
    [projection, query],
  );

  const managerGroups = useMemo<ManagerTaskGroup[]>(() => {
    const groups: ManagerTaskGroup[] = [];
    visibleTasks.forEach((task) => {
      const managerKey = task.manager_group_key;
      let manager = groups.find((candidate) => candidate.key === managerKey);
      if (!manager) {
        manager = {
          key: managerKey,
          label: task.manager_display_name || "Sans chargé de projet ERP",
          resolution_status: task.manager_resolution_status,
          diagnostics: task.manager_diagnostics,
          projects: [],
        };
        groups.push(manager);
      }

      let project = manager.projects.find(
        (candidate) => candidate.project_id === task.project_id,
      );
      if (!project) {
        project = {
          project_id: task.project_id,
          project_number: task.project_number,
          project_name: task.project_name,
          tasks: [],
        };
        manager.projects.push(project);
      }
      project.tasks.push(task);
    });

    groups.forEach((manager) => {
      manager.projects.sort((left, right) => (
        left.project_number.localeCompare(right.project_number, "fr-CA")
        || left.project_name.localeCompare(right.project_name, "fr-CA")
      ));
      manager.projects.forEach((project) => {
        project.tasks.sort((left, right) => (
          left.task_code.localeCompare(right.task_code, "fr-CA")
          || left.task_label.localeCompare(right.task_label, "fr-CA")
        ));
      });
    });

    return groups.sort((left, right) => (
      left.label.localeCompare(right.label, "fr-CA")
    ));
  }, [visibleTasks]);

  const visibleUnclassified = useMemo(
    () => (projection?.unclassified_work_packages ?? []).filter((workPackage) => (
      !query || normalize([
        workPackage.reference,
        workPackage.code,
        workPackage.name,
        workPackage.resource_class_code,
        workPackage.resource_class_label,
      ].filter(Boolean).join(" ")).includes(query)
    )),
    [projection, query],
  );

  const projectedPackageCount = useMemo(
    () => (projection?.tasks ?? []).reduce(
      (count, task) => count + task.work_packages.length,
      projection?.unclassified_work_packages.length ?? 0,
    ),
    [projection],
  );

  const editorMediumTerm = useMemo(() => {
    if (!editor || !projection) return null;
    for (const task of projection.tasks) {
      const row = task.work_packages.find((candidate) => candidate.reference === editor.reference);
      if (row) return row;
    }
    return projection.unclassified_work_packages.find(
      (candidate) => candidate.reference === editor.reference,
    ) ?? null;
  }, [editor, projection]);

  const headerStyle: CSSProperties = {
    gridTemplateColumns: `300px repeat(${horizonWeeks}, minmax(96px, 1fr))`,
  };

  function renderWorkPackage(workPackage: MediumTermBudgetWorkPackageReadModel) {
    const project = projects.find((candidate) => candidate.number === workPackage.project_number);
    if (!project) return null;
    return (
      <WorkPackageRow
        key={workPackage.id}
        project={project}
        workPackage={workPackage}
        baseWorkPackage={workPackagesByReference.get(workPackage.reference) ?? null}
        demands={demandByPackage.get(workPackage.reference) ?? []}
        pendingLoads={pendingByPackage.get(workPackage.reference) ?? []}
        plannedDemandNumbers={plannedDemandNumbers}
        horizonStart={horizonStart}
        horizonWeeks={horizonWeeks}
        onOpenDemands={onOpenDemands}
        onEdit={setEditor}
      />
    );
  }

  function toggleManager(key: string) {
    setCollapsedManagers((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleProject(projectId: string) {
    setCollapsedProjects((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }

  return (
    <section className="medium-term-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Planification moyen terme</span>
          <h1>{start} → {end}</h1>
          <p>
            Gantt de pilotage des WorkPackages. Budget, charge hebdomadaire, capacité et diagnostics
            proviennent du read model FastAPI.
          </p>
        </div>
        <div className="page-actions">
          <ViewScopeSelector />
          <label className="mt-budget-mode">
            <span>Lecture budget</span>
            <select
              aria-label="Mode budget Moyen terme"
              value={budgetMode}
              onChange={(event) => setBudgetMode(event.target.value as BudgetMode)}
            >
              <option value="initial">Budget initial</option>
              <option value="remaining">Budget restant</option>
            </select>
          </label>
          <button className="mt-create-package" type="button" onClick={() => setEditor(null)}>
            + WorkPackage
          </button>
          <button className="mt-demands-button" type="button" onClick={onOpenDemands}>
            Ouvrir les demandes
          </button>
          <div className="week-navigation" role="group" aria-label="Navigation moyen terme">
            <button type="button" onClick={() => setHorizonStart((value) => addDays(value, -28))}>← 4 sem.</button>
            <button type="button" onClick={() => setHorizonStart(startOfWeek(new Date()))}>Aujourd’hui</button>
            <button type="button" onClick={() => setHorizonStart((value) => addDays(value, 28))}>4 sem. →</button>
          </div>
        </div>
      </div>

      <div className="metric-grid mt-metrics">
        <article>
          <span>Portefeuille analysé</span>
          <strong>{loading || projectionLoading ? "—" : projection?.project_number || "Tous les projets"}</strong>
          <small>
            {projection?.project_name || `${projection?.project_count ?? 0} projet(s) dans le périmètre`}
          </small>
        </article>
        <article>
          <span>Tâches ERP DEPMO</span>
          <strong>{projectionLoading ? "—" : projection?.tasks.length ?? 0}</strong>
          <small>Read model backend</small>
        </article>
        <article>
          <span>WorkPackages</span>
          <strong>{projectionLoading ? "—" : projectedPackageCount}</strong>
          <small>Classés + historiques non classés</small>
        </article>
        <article>
          <span>Semaines projetées</span>
          <strong>{projectionLoading ? "—" : projection?.weeks.length ?? 0}</strong>
          <small>Fenêtre demandée au backend</small>
        </article>
      </div>

      <div className="filter-bar mt-filters">
        <label className="search-field">
          <span>Recherche</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Chargé, projet, tâche ERP, WorkPackage…" />
        </label>
        <label>
          <span>Projet</span>
          <select
            value={projectFilter}
            onChange={(event) => {
              setProjectFilter(event.target.value);
              setTaskFilter("");
            }}
          >
            <option value="">Tous les projets</option>
            {projectOptions.map((project) => (
              <option value={project.number} key={project.id}>
                {project.number} — {project.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Tâche ERP</span>
          <select value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}>
            <option value="">Toutes les tâches</option>
            {taskOptions.map((task) => (
              <option value={task.task_catalog_item_id} key={task.task_catalog_item_id}>
                {task.project_number} · {task.task_code} — {task.task_label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Classe de ressource</span>
          <select
            value={resourceClassFilter}
            onChange={(event) => setResourceClassFilter(event.target.value)}
          >
            <option value="">Toutes les classes</option>
            {resourceClassOptions.map((resourceClass) => (
              <option value={resourceClass.code} key={resourceClass.code}>
                {resourceClass.label} ({resourceClass.code}){resourceClass.active ? "" : " — inactive"}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Projets</span>
          <select
            value={includeInactiveProjects ? "all" : "active"}
            onChange={(event) => setIncludeInactiveProjects(event.target.value === "all")}
          >
            <option value="active">Actifs seulement</option>
            <option value="all">Actifs + historique</option>
          </select>
        </label>
        <label>
          <span>Horizon</span>
          <select value={horizonWeeks} onChange={(event) => setHorizonWeeks(Number(event.target.value) as HorizonWeeks)}>
            {HORIZONS.map((weeksCount) => <option value={weeksCount} key={weeksCount}>{weeksCount} semaines</option>)}
          </select>
        </label>
      </div>

      {error && (
        <div className="error-panel">
          <strong>Le moyen terme n’a pas pu être chargé.</strong>
          <span>{error}</span>
          <small>Vérifie que FastAPI fonctionne et que la base est migrée.</small>
        </div>
      )}

      {projectionError && (
        <div className="error-panel">
          <strong>La projection Moyen terme n’a pas pu être chargée.</strong>
          <span>{projectionError}</span>
        </div>
      )}

      <MediumTermCapacityPanel
        weeks={projection?.weeks ?? []}
        diagnostics={projection?.weekly_diagnostics ?? []}
        loading={projectionLoading}
      />

      <div className={`mt-board ${loading || projectionLoading ? "is-loading" : ""}`}>
        <div className="mt-board-scroll">
          <div className="mt-header" style={headerStyle}>
            <div className="mt-project-header">Chargé de projet → Projet ERP → Tâche ERP → WorkPackage</div>
            {weeks.map((week, index) => (
              <div className="mt-week-header" key={toIsoDate(week)} style={{ gridColumn: index + 2 }}>
                <strong>S{String(isoWeekNumber(week)).padStart(2, "0")}</strong>
                <span>{formatWeekRange(week)}</span>
              </div>
            ))}
          </div>

          {!loading && !projectionLoading && !projection && !error && !projectionError && (
            <div className="mt-empty">Aucune projection Moyen terme n’est disponible.</div>
          )}

          {projection && (
            <div className="mt-project-block">
              {projection.diagnostics.length > 0 && (
                <div className="mt-projection-diagnostics">
                  {projection.diagnostics.map((code) => (
                    <span key={code}>⚑ {PROJECTION_DIAGNOSTIC_LABELS[code] || code}</span>
                  ))}
                </div>
              )}

              {projection.tasks.length === 0 && (
                <div className="mt-no-package">
                  Aucune tâche ERP DEPMO ne correspond aux filtres autoritaires.
                </div>
              )}

              {managerGroups.map((manager) => {
                const managerCollapsed = collapsedManagers.has(manager.key);
                return (
                  <section className="mt-manager-group" key={manager.key}>
                    <button
                      className="mt-manager-toggle"
                      type="button"
                      aria-expanded={!managerCollapsed}
                      onClick={() => toggleManager(manager.key)}
                    >
                      <strong>{managerCollapsed ? "▶" : "▼"} {manager.label}</strong>
                      <span>{manager.projects.length} projet(s)</span>
                    </button>

                    {!managerCollapsed && manager.projects.map((project) => {
                      const projectCollapsed = collapsedProjects.has(project.project_id);
                      return (
                        <section className="mt-project-block" key={project.project_id}>
                          <button
                            className="mt-project-strip mt-collapse-toggle"
                            type="button"
                            aria-expanded={!projectCollapsed}
                            onClick={() => toggleProject(project.project_id)}
                          >
                            <strong>{projectCollapsed ? "▶" : "▼"} {project.project_number}</strong>
                            <span>{project.project_name}</span>
                            <small>{project.tasks.length} tâche(s) ERP</small>
                          </button>

                          {!projectCollapsed && project.tasks.map((task) => (
                            <section className="mt-task-group" key={task.task_catalog_item_id}>
                              <TaskHeader task={task} budgetMode={budgetMode} />
                              {task.work_packages.length === 0 ? (
                                <div className="mt-no-package is-compact">
                                  Aucun WorkPackage
                                </div>
                              ) : task.work_packages.map(renderWorkPackage)}
                            </section>
                          ))}
                        </section>
                      );
                    })}
                  </section>
                );
              })}

              {projection.unclassified_work_packages.length > 0 && (
                <section className="mt-task-group is-unclassified">
                  <header className="mt-task-strip has-attention">
                    <div className="mt-task-title">
                      <strong>WorkPackages sans tâche ERP</strong>
                      <span className="mt-yellow-flag">⚑</span>
                    </div>
                    <small>
                      Historique compatible sans tâche ERP. Aucun rattachement n’est déduit du nom ou du code.
                    </small>
                  </header>
                  {visibleUnclassified.map(renderWorkPackage)}
                </section>
              )}

              {query && visibleTasks.length === 0 && visibleUnclassified.length === 0 && (
                <div className="mt-empty">Aucune tâche ou WorkPackage ne correspond à la recherche.</div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="mt-legend">
        <span><i className="planned" /> Charge courante incluse</span>
        <span><i className="pending" /> ⚑ Diagnostic backend nécessitant une attention</span>
        <span><i className="draft" /> Hors charge courante / historique</span>
        <small>
          Les montants Budget initial / Budget restant restent financiers en CAD. Charge WP, solde
          structuré et capacité restent en heures. React ne convertit jamais les dollars en heures.
        </small>
      </div>

      <MediumTermUnlinkedSegmentsPanel
        rows={unlinkedSegments.filter((row) => !projectFilter || row.project_number === projectFilter)}
        workPackages={workPackages}
        loading={loading}
        onOpenDemands={onOpenDemands}
        onOpenSegment={setSegmentEditorId}
        onLinked={() => setRefreshKey((value) => value + 1)}
      />

      {segmentEditorId && (
        <SegmentEditor
          open
          segmentId={segmentEditorId}
          demand={null}
          resources={resources}
          onClose={() => setSegmentEditorId(null)}
          onSaved={() => {
            setSegmentEditorId(null);
            setRefreshKey((value) => value + 1);
          }}
        />
      )}

      {editor !== undefined && (
        <WorkPackageEditor
          projects={catalogProjects}
          workPackage={editor}
          mediumTermWorkPackage={editorMediumTerm}
          defaultProjectNumber={editor?.project_number || projectFilter || projectOptions[0]?.number || ""}
          onClose={() => setEditor(undefined)}
          onReload={() => {
            setEditor(undefined);
            setRefreshKey((value) => value + 1);
          }}
          onSaved={(projectNumber) => {
            if (projectNumber) setProjectFilter(projectNumber);
            setEditor(undefined);
            setRefreshKey((value) => value + 1);
          }}
        />
      )}
    </section>
  );
}
