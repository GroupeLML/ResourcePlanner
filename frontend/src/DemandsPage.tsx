import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  BusinessContactReadModel,
  CompetencyReadModel,
  ContactLinkReadModel,
  DemandReadModel,
  DemandDetailReadModel,
  DemandRequesterReadModel,
  DemandWrite,
  ProjectReadModel,
  RequestLineContactResolutionReadModel,
  ResourceReadModel,
  TaskCatalogItemReadModel,
  WorkPackageReadModel,
  createDemand,
  getBusinessContacts,
  getCompetencies,
  getDemandDetail,
  getDemandBusinessContacts,
  getDemandRequesters,
  getDemands,
  getProjects,
  getResources,
  getTaskCatalog,
  getWorkPackages,
  setDemandOperationalResponsible,
  updateDemand,
} from "./api";
import CompetencyPicker from "./CompetencyPicker";
import { ContactSelect, ResolutionSummary } from "./BusinessContactUi";
import { useAuth } from "./AuthContext";
import DemandDetail from "./DemandDetail";
import DemandLinesEditor, {
  DemandLineDefaults,
  DemandLineDraft,
  demandLineDraftFromReadModel,
  demandLineWrite,
  lineValidationMessage,
  newDemandLine,
} from "./DemandLinesEditor";
import { useViewScope } from "./ViewScopeContext";
import {
  AssetCatalogItem,
  AssetTypeCatalogItem,
  getAssetCatalog,
} from "./assetApi";
import ViewScopeSelector from "./ViewScopeSelector";
import { ResourceClassOptionReadModel, getResourceClassOptions } from "./resourceClassesApi";

type FormState = {
  project_number: string;
  requester_user_id: string;
  work_package_ref: string;
  task_code: string;
  priority: string;
  confirmation: "Tentative" | "Confirmée";
  desired_start: string;
  desired_end: string;
  description: string;
  resource_count: string;
  required_competency_ids: string[];
  estimated_hours: string;
  estimated_days: string;
  proposed_technician: string;
};

type RetryReceipt = { fingerprint: string; key: string };

function todayIso() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function newIdempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `web-demand-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible d'enregistrer la demande.";
}

function optionalNumber(value: string) {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized.replace(",", "."));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function inclusiveCalendarDays(start: string, end: string): number {
  if (!start || !end) return 0;
  return Math.floor((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
}

function emptyForm(projectNumber = "", requesterUserId = ""): FormState {
  return {
    project_number: projectNumber,
    requester_user_id: requesterUserId,
    work_package_ref: "",
    task_code: "",
    priority: "Normale",
    confirmation: "Confirmée",
    desired_start: todayIso(),
    desired_end: "",
    description: "",
    resource_count: "1",
    required_competency_ids: [],
    estimated_hours: "",
    estimated_days: "",
    proposed_technician: "",
  };
}

function lineDefaultsFromForm(form: FormState, resources: ResourceReadModel[]): DemandLineDefaults {
  const proposed = resources.find((row) => row.name === form.proposed_technician) ?? null;
  return {
    kind: "WORKFORCE",
    required_resource_class: proposed?.resource_class ?? "",
    required_competency_ids: [...form.required_competency_ids],
    desired_start: form.desired_start,
    desired_end: form.desired_end,
    desired_active_days: form.estimated_days,
    estimated_hours: form.estimated_hours,
    work_package_ref: form.work_package_ref,
    task_code: form.task_code,
    proposed_resource_id: proposed?.id ?? "",
    asset_type_id: "",
    proposed_asset_id: "",
    confirmation: form.confirmation,
    description: "",
  };
}

function formFromDemand(demand: DemandReadModel): FormState {
  return {
    project_number: demand.project_number ?? "",
    requester_user_id: demand.requester_user_id ?? "",
    work_package_ref: demand.work_package_ref ?? "",
    task_code: demand.task_code ?? "",
    priority: demand.priority ?? "Normale",
    confirmation: (demand.confirmation === "Tentative" ? "Tentative" : "Confirmée"),
    desired_start: demand.desired_start ?? "",
    desired_end: demand.desired_end ?? "",
    description: demand.description ?? "",
    resource_count: String(demand.resource_count || 1),
    required_competency_ids: demand.required_competency_ids ?? [],
    estimated_hours: demand.estimated_hours == null ? "" : String(demand.estimated_hours),
    estimated_days: demand.estimated_days == null ? "" : String(demand.estimated_days),
    proposed_technician: demand.proposed_resource ?? "",
  };
}

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function effectiveDemandStatus(demand: DemandReadModel) {
  return demand.effective_status || demand.status;
}

function demandCreatedAtMs(demand: DemandReadModel) {
  const value = Date.parse(demand.created_at ?? "");
  return Number.isNaN(value) ? 0 : value;
}

function demandSearchText(demand: DemandReadModel) {
  const lineText = (demand.lines ?? []).flatMap((line) => [
    line.required_resource_class,
    line.required_competencies,
    line.work_package_name,
    line.task_code,
    line.task_label,
    line.proposed_resource,
    line.description,
  ]);
  return normalize([
    demand.number,
    effectiveDemandStatus(demand),
    demand.project_number,
    demand.project_name,
    demand.client,
    demand.project_manager,
    demand.requester,
    demand.description,
    demand.work_package_name,
    demand.required_competencies,
    demand.proposed_resource,
    ...lineText,
  ].filter(Boolean).join(" "));
}

function DemandCard({ demand, selected, onClick }: { demand: DemandReadModel; selected: boolean; onClick: () => void }) {
  const tentative = demand.confirmation === "Tentative";
  return (
    <button type="button" className={`demand-card ${selected ? "selected" : ""}`} onClick={onClick}>
      <div className="demand-card-topline">
        <strong>{demand.number}</strong>
        <span className={`demand-status status-${normalize(effectiveDemandStatus(demand)).replace(/[^a-z0-9]+/g, "-")}`}>{effectiveDemandStatus(demand) || "—"}</span>
      </div>
      <div className="demand-project">
        <strong>{demand.project_number || "Projet non défini"}</strong>
        <span>{demand.project_name || ""}</span>
      </div>
      {demand.cancellation_state === "PENDING" && (
        <span className="demand-cancellation-badge" data-testid="demand-cancellation-pending">
          Annulation demandée
        </span>
      )}
      <div className="demand-card-meta">
        <span>{demand.desired_start || "Date à définir"}{demand.desired_end && demand.desired_end !== demand.desired_start ? ` → ${demand.desired_end}` : ""}</span>
        <span>{demand.line_mode ? `${(demand.lines ?? []).filter((line) => line.active).length} ligne(s)` : `${demand.resource_count || 1} ressource(s)`}</span>
        <span className={tentative ? "confirmation-tentative-text" : "confirmation-confirmed-text"}>{tentative ? "Tentative" : "Confirmée"}</span>
      </div>
    </button>
  );
}

type DemandsPageProps = { initialDemandNumber?: string | null };

export default function DemandsPage({ initialDemandNumber = null }: DemandsPageProps) {
  const { can, principal } = useAuth();
  const { scope, loading: scopeLoading } = useViewScope();
  const canManageDemands = can("manage_demands");
  const canDelegateRequester = Boolean(
    principal?.roles.some((role) => role === "COORDINATOR" || role === "ADMIN"),
  );
  const [demands, setDemands] = useState<DemandReadModel[]>([]);
  const [requesters, setRequesters] = useState<DemandRequesterReadModel[]>([]);
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [resources, setResources] = useState<ResourceReadModel[]>([]);
  const [competencies, setCompetencies] = useState<CompetencyReadModel[]>([]);
  const [resourceClasses, setResourceClasses] = useState<ResourceClassOptionReadModel[]>([]);
  const [assetTypes, setAssetTypes] = useState<AssetTypeCatalogItem[]>([]);
  const [assets, setAssets] = useState<AssetCatalogItem[]>([]);
  const [contacts, setContacts] = useState<BusinessContactReadModel[]>([]);
  const [demandContactLink, setDemandContactLink] = useState<ContactLinkReadModel | null>(null);
  const [lineContactResolutions, setLineContactResolutions] = useState<Record<string, RequestLineContactResolutionReadModel>>({});
  const [tasks, setTasks] = useState<TaskCatalogItemReadModel[]>([]);
  const [workPackages, setWorkPackages] = useState<WorkPackageReadModel[]>([]);
  const [selectedNumber, setSelectedNumber] = useState<string | null>(null);
  const [selectedDemand, setSelectedDemand] = useState<DemandReadModel | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<DemandDetailReadModel | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(() => emptyForm());
  const [lineMode, setLineMode] = useState(false);
  const [lines, setLines] = useState<DemandLineDraft[]>([]);
  const [generationCount, setGenerationCount] = useState("2");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editorDirty, setEditorDirty] = useState(false);
  const [contextDirty, setContextDirty] = useState(false);
  const [overridePending, setOverridePending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [projectFilter, setProjectFilter] = useState("all");
  const [includeTerminated, setIncludeTerminated] = useState(false);
  const [sortOrder, setSortOrder] = useState<"newest" | "oldest">("newest");
  const createRetry = useRef<RetryReceipt | null>(null);

  const initialSelectionApplied = useRef(false);

  useEffect(() => {
    if (
      initialSelectionApplied.current
      || !initialDemandNumber
      || demands.length === 0
    ) return;
    initialSelectionApplied.current = true;
    if (!demands.some((row) => row.number === initialDemandNumber)) return;
    setCreating(false);
    setSelectedNumber(initialDemandNumber);
    setSelectedDetail(null);
    setDemandContactLink(null);
    setLineContactResolutions({});
  }, [initialDemandNumber, demands]);

  useEffect(() => {
    if (scopeLoading) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getDemands(controller.signal, scope),
      getProjects(true, controller.signal, scope),
      getResources(true, controller.signal),
      getCompetencies("", false, controller.signal),
      getResourceClassOptions(controller.signal),
      getAssetCatalog(controller.signal),
      getBusinessContacts(false, controller.signal),
      canManageDemands
        ? getDemandRequesters(controller.signal)
        : Promise.resolve([] as DemandRequesterReadModel[]),
    ])
      .then(([demandRows, projectRows, resourceRows, competencyRows, classRows, assetCatalog, contactRows, requesterRows]) => {
        setDemands(demandRows);
        setProjects(projectRows);
        setResources(resourceRows);
        setCompetencies(competencyRows);
        setResourceClasses(classRows);
        setAssetTypes(assetCatalog.types);
        setAssets(assetCatalog.assets);
        setContacts(contactRows);
        setRequesters(requesterRows);
        setSelectedNumber((current) => {
          if (current && demandRows.some((row) => row.number === current)) return current;
          return demandRows[0]?.number ?? null;
        });
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [scope, scopeLoading, canManageDemands]);

  useEffect(() => {
    if (creating || !selectedNumber) return;
    const controller = new AbortController();
    setDetailLoading(true);
    setError(null);
    Promise.all([
      getDemandDetail(selectedNumber, controller.signal),
      getDemandBusinessContacts(selectedNumber, controller.signal),
    ])
      .then(([detail, contactLink]) => {
        const demand = detail.demand;
        setSelectedDetail(detail);
        setSelectedDemand(demand);
        setDemandContactLink(contactLink);
        setForm(formFromDemand(demand));
        setLineMode(Boolean(demand.line_mode));
        const activeLines = (demand.lines ?? []).filter((line) => line.active);
        setLines(activeLines.map(demandLineDraftFromReadModel));
        setGenerationCount(String(Math.max(activeLines.length, 1)));
        setLineContactResolutions(
          Object.fromEntries(
            detail.lines
              .filter((row) => row.contacts)
              .map((row) => [row.line.line_id, row.contacts!]),
          ),
        );
        setEditorDirty(false);
        setContextDirty(false);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedNumber, creating]);

  useEffect(() => {
    const projectNumber = form.project_number;
    if (!projectNumber) {
      setWorkPackages([]);
      setTasks([]);
      return;
    }
    const controller = new AbortController();
    Promise.all([
      getWorkPackages(projectNumber, false, controller.signal, scope),
      getTaskCatalog(projectNumber, "", false, controller.signal),
    ])
      .then(([packageRows, taskRows]) => {
        setWorkPackages(packageRows);
        setTasks(taskRows);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason));
      });
    return () => controller.abort();
  }, [form.project_number, scope]);

  useEffect(() => {
    if (!editorDirty && !contextDirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [editorDirty, contextDirty]);

  const statusOptions = useMemo(
    () => [...new Set(demands.map(effectiveDemandStatus).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fr-CA")),
    [demands],
  );

  const visibleDemands = useMemo(() => {
    const query = normalize(search);
    const filtered = demands.filter((demand) => {
      const effectiveStatus = effectiveDemandStatus(demand);
      if (!includeTerminated && statusFilter === "all" && demand.terminal) return false;
      if (statusFilter !== "all" && effectiveStatus !== statusFilter) return false;
      if (projectFilter !== "all" && demand.project_number !== projectFilter) return false;
      if (query && !demandSearchText(demand).includes(query)) return false;
      return true;
    });
    const direction = sortOrder === "newest" ? -1 : 1;
    return [...filtered].sort(
      (left, right) => direction * (demandCreatedAtMs(left) - demandCreatedAtMs(right)),
    );
  }, [demands, search, statusFilter, projectFilter, includeTerminated, sortOrder]);

  const selectedProject = useMemo(
    () => projects.find((row) => row.number === form.project_number) ?? null,
    [projects, form.project_number],
  );

  const selectedWorkPackage = useMemo(
    () => workPackages.find((row) => row.reference === form.work_package_ref) ?? null,
    [workPackages, form.work_package_ref],
  );

  const visibleTasks = useMemo(
    () => tasks.filter((task) => task.active || task.code === form.task_code),
    [tasks, form.task_code],
  );

  const selectedTask = useMemo(
    () => tasks.find((row) => row.code === form.task_code) ?? null,
    [tasks, form.task_code],
  );

  async function reloadDemand(number: string) {
    const [rows, detail, contactLink] = await Promise.all([
      getDemands(undefined, scope),
      getDemandDetail(number),
      getDemandBusinessContacts(number),
    ]);
    const demand = detail.demand;
    const activeLines = (demand.lines ?? []).filter((line) => line.active);
    setDemands(rows);
    setSelectedNumber(demand.number);
    setSelectedDetail(detail);
    setSelectedDemand(demand);
    setDemandContactLink(contactLink);
    setLineContactResolutions(
      Object.fromEntries(
        detail.lines
          .filter((row) => row.contacts)
          .map((row) => [row.line.line_id, row.contacts!]),
      ),
    );
    setForm(formFromDemand(demand));
    setLineMode(Boolean(demand.line_mode));
    setLines(activeLines.map(demandLineDraftFromReadModel));
    setGenerationCount(String(Math.max(activeLines.length, 1)));
    setEditorDirty(false);
    setContextDirty(false);
  }

  function confirmDiscardChanges() {
    if (!editorDirty && !contextDirty) return true;
    return window.confirm("Des modifications non enregistrées seront perdues. Continuer?");
  }

  function beginCreate() {
    if (!confirmDiscardChanges()) return;
    const firstProject = projects[0]?.number ?? "";
    setDetailLoading(false);
    setCreating(true);
    setSelectedNumber(null);
    setSelectedDemand(null);
    setSelectedDetail(null);
    setDemandContactLink(null);
    setLineContactResolutions({});
    setForm(emptyForm(firstProject, principal?.local_user_id ?? ""));
    setLineMode(false);
    setLines([]);
    setGenerationCount("2");
    setNotice(null);
    setError(null);
    setEditorDirty(false);
    setContextDirty(false);
    createRetry.current = null;
  }

  function selectDemand(number: string) {
    if (saving || number === selectedNumber) return;
    if (!confirmDiscardChanges()) return;
    setCreating(false);
    setSelectedNumber(number);
    setSelectedDetail(null);
    setDemandContactLink(null);
    setLineContactResolutions({});
    setLineMode(false);
    setLines([]);
    setNotice(null);
    setError(null);
    createRetry.current = null;
  }

  function setField<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [field]: value }));
    setEditorDirty(true);
  }

  function activateLineMode() {
    const requested = Number(form.resource_count);
    const count = Number.isInteger(requested) && requested > 0 ? requested : 1;
    const defaults = lineDefaultsFromForm(form, resources);
    setLineMode(true);
    setEditorDirty(true);
    setGenerationCount(String(count));
    setLines(Array.from({ length: count }, () => newDemandLine(defaults)));
    setError(null);
    setNotice("Mode lignes activé. Chaque ligne est maintenant un besoin planifiable indépendant.");
  }

  function revertUnsavedLineMode() {
    setLineMode(false);
    setLines([]);
    setEditorDirty(true);
    setNotice("Retour au besoin simple. Aucune ligne n’a encore été enregistrée.");
    setError(null);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setError(null);
    setNotice(null);

    if (!selectedProject) {
      setError("Sélectionne un projet actif.");
      return;
    }

    let payload: DemandWrite;
    if (lineMode) {
      if (lines.length === 0) {
        setError("Ajoute au moins une ligne planifiable.");
        return;
      }
      const invalid = lines
        .map((line, index) => lineValidationMessage(line, index))
        .find((message): message is string => Boolean(message));
      if (invalid) {
        setError(invalid);
        return;
      }
      payload = {
        project_number: selectedProject.number,
        project_name: selectedProject.name,
        client: selectedProject.client ?? "",
        ...(creating
          ? { requester_user_id: form.requester_user_id || principal?.local_user_id || null }
          : (
            canDelegateRequester
            && form.requester_user_id
            && form.requester_user_id !== (selectedDemand?.requester_user_id ?? "")
              ? { requester_user_id: form.requester_user_id }
              : {}
          )),
        request_type: selectedDemand?.request_type || "Projet",
        priority: form.priority,
        description: form.description.trim(),
        lines: lines.map(demandLineWrite),
        ...(selectedDemand ? { expected_version: selectedDemand.version } : {}),
      };
    } else {
      const resourceCount = Number(form.resource_count);
      const estimatedHours = optionalNumber(form.estimated_hours);
      const estimatedDays = optionalNumber(form.estimated_days);
      if (!form.desired_start) {
        setError("La date de début souhaitée est requise.");
        return;
      }
      if (form.desired_end && form.desired_end < form.desired_start) {
        setError("La date de fin ne peut pas précéder la date de début.");
        return;
      }
      if (!Number.isInteger(resourceCount) || resourceCount < 1) {
        setError("Le nombre de ressources doit être un entier supérieur ou égal à 1.");
        return;
      }
      if (Number.isNaN(estimatedHours) || Number.isNaN(estimatedDays)) {
        setError("Les estimations doivent être numériques.");
        return;
      }
      if (estimatedHours != null && estimatedHours <= 0) {
        setError("Les heures estimées totales doivent être supérieures à zéro lorsqu'elles sont renseignées.");
        return;
      }
      if (estimatedDays != null) {
        if (!Number.isInteger(estimatedDays) || estimatedDays < 1) {
          setError("Les jours actifs souhaités doivent être un entier supérieur ou égal à 1.");
          return;
        }
        const end = form.desired_end || form.desired_start;
        const windowDays = inclusiveCalendarDays(form.desired_start, end);
        if (estimatedDays > windowDays) {
          setError(`La cible de ${estimatedDays} jours actifs dépasse les ${windowDays} dates de la fenêtre demandée.`);
          return;
        }
      }
      payload = {
        project_number: selectedProject.number,
        project_name: selectedProject.name,
        client: selectedProject.client ?? "",
        ...(creating
          ? { requester_user_id: form.requester_user_id || principal?.local_user_id || null }
          : (
            canDelegateRequester
            && form.requester_user_id
            && form.requester_user_id !== (selectedDemand?.requester_user_id ?? "")
              ? { requester_user_id: form.requester_user_id }
              : {}
          )),
        work_package_ref: form.work_package_ref || null,
        task_code: form.task_code || null,
        request_type: selectedDemand?.request_type || "Projet",
        priority: form.priority,
        confirmation: form.confirmation,
        desired_start: form.desired_start,
        desired_end: form.desired_end || null,
        description: form.description.trim(),
        resource_count: resourceCount,
        required_competencies: null,
        required_competency_ids: form.required_competency_ids,
        estimated_hours: estimatedHours,
        estimated_days: estimatedDays,
        proposed_technician: form.proposed_technician || null,
      };
    }

    setSaving(true);
    try {
      if (creating) {
        const fingerprint = JSON.stringify(payload);
        const previous = createRetry.current;
        const key = previous?.fingerprint === fingerprint ? previous.key : newIdempotencyKey();
        createRetry.current = { fingerprint, key };
        const result = await createDemand(payload, key);
        createRetry.current = null;
        setCreating(false);
        await reloadDemand(result.demand_number);
        setNotice(`Demande ${result.demand_number} créée en brouillon.`);
      } else if (selectedDemand) {
        const result = await updateDemand(
          selectedDemand.number,
          payload,
          "Demande modifiée via React V2",
        );
        await reloadDemand(result.demand_number);
        setNotice(
          result.reapproval_required
            ? "Modification enregistrée. La demande doit être approuvée de nouveau; le plan approuvé précédent reste inchangé jusque-là."
            : "Modification enregistrée.",
        );
      }
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setSaving(false);
    }
  }

  async function changeOperationalOverride(contactId: string | null) {
    if (!selectedDemand || overridePending) return;
    setOverridePending(true);
    setError(null);
    setNotice(null);
    try {
      const result = await setDemandOperationalResponsible(
        selectedDemand.number,
        contactId,
        selectedDemand.version,
      );
      await reloadDemand(selectedDemand.number);
      setNotice(
        result.reapproval_required
          ? "Responsable opérationnel modifié. Une nouvelle approbation est requise; le planning existant conserve son contexte approuvé précédent."
          : result.changed
            ? "Responsable opérationnel modifié."
            : "Aucun changement de responsable opérationnel.",
      );
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setOverridePending(false);
    }
  }

  const missingProposedResource = form.proposed_technician
    && !resources.some((row) => row.name === form.proposed_technician)
    ? form.proposed_technician
    : null;

  return (
    <section className="demands-page">
      <div className="page-heading demands-heading">
        <div>
          <span className="eyebrow">Besoins planifiables</span>
          <h1>Demandes</h1>
          <p>Création et modification des besoins avant leur transformation en périodes, approbation et plan opérationnel.</p>
        </div>
        <div className="page-heading-actions">
          <ViewScopeSelector />
          <button type="button" className="primary-button demand-new-button" onClick={beginCreate} disabled={saving}>
            + Nouvelle demande
          </button>
        </div>
      </div>

      {error && <div className="error-panel"><strong>Action impossible.</strong><span>{error}</span></div>}
      {notice && <div className="demand-notice" role="status">{notice}</div>}

      <div className="demand-filter-bar">
        <label>
          <span>Recherche</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Demande, projet, client, responsable…" />
        </label>
        <label>
          <span>Statut</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="all">Tous les statuts</option>
            {statusOptions.map((status) => <option value={status} key={status}>{status}</option>)}
          </select>
        </label>
        <label>
          <span>Projet</span>
          <select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
            <option value="all">Tous les projets</option>
            {projects.map((project) => <option value={project.number} key={project.id}>{project.number} — {project.name}</option>)}
          </select>
        </label>
        <label>
          <span>Tri</span>
          <select value={sortOrder} onChange={(event) => setSortOrder(event.target.value as "newest" | "oldest")}>
            <option value="newest">Plus récentes d’abord</option>
            <option value="oldest">Plus anciennes d’abord</option>
          </select>
        </label>
        <label className="terminal-filter-toggle">
          <input
            type="checkbox"
            checked={includeTerminated}
            onChange={(event) => setIncludeTerminated(event.target.checked)}
          />
          <span>Inclure les demandes terminées</span>
        </label>
      </div>

      <div className="demands-workspace">
        <aside className="demand-list-panel">
          <div className="demand-list-heading">
            <strong>{loading ? "Chargement…" : `${visibleDemands.length} demande(s)`}</strong>
            <span>{demands.length} au total</span>
          </div>
          <div className="demand-list">
            {!loading && visibleDemands.length === 0 && <div className="demand-list-empty">Aucune demande ne correspond aux filtres.</div>}
            {visibleDemands.map((demand) => (
              <DemandCard
                demand={demand}
                selected={!creating && selectedNumber === demand.number}
                onClick={() => selectDemand(demand.number)}
                key={demand.number}
              />
            ))}
          </div>
        </aside>

        <div className="demand-editor-panel">
          {creating || selectedDemand ? (
            <form onSubmit={save} className="demand-editor-form">
              <div className="demand-editor-heading">
                <div>
                  <span className="eyebrow">{creating ? "Brouillon" : selectedDemand?.status}</span>
                  <h2>{creating ? "Nouvelle demande" : selectedDemand?.number}</h2>
                  {!creating && selectedDemand && (
                    <p>
                      {selectedDemand.project_number} — {selectedDemand.project_name || "Projet"}
                      {selectedDemand.work_package_name ? ` · ${selectedDemand.work_package_name}` : ""}
                    </p>
                  )}
                </div>
                {!creating && selectedDemand && (
                  selectedDemand.line_mode ? (
                    <div className="demand-confirmation-pill confirmed">
                      {(selectedDemand.lines ?? []).filter((line) => line.active).length} ligne(s)
                    </div>
                  ) : (
                    <div className={`demand-confirmation-pill ${selectedDemand.confirmation === "Tentative" ? "tentative" : "confirmed"}`}>
                      {selectedDemand.confirmation || "Confirmée"}
                    </div>
                  )
                )}
              </div>

              {detailLoading && <div className="editor-loading">Actualisation du détail…</div>}
              <div className="project-master-card">
                <div>
                  <span>Responsable projet</span>
                  <strong>{selectedProject?.project_manager || selectedDemand?.project_manager || "Non défini"}</strong>
                </div>
                <div>
                  <span>Client</span>
                  <strong>{selectedProject?.client || selectedDemand?.client || "Non défini"}</strong>
                </div>
                <small>Ces données proviennent du projet et restent en lecture seule dans la demande.</small>
              </div>



              <div className="demand-form-grid">
                <label className="span-2">
                  <span>Projet</span>
                  <select
                    value={form.project_number}
                    onChange={(event) => {
                      const projectNumber = event.target.value;
                      setTaskSearch("");
                      setEditorDirty(true);
                      setForm((current) => ({
                        ...current,
                        project_number: projectNumber,
                        work_package_ref: "",
                        task_code: "",
                      }));
                      if (lineMode) {
                        setLines((current) => current.map((line) => ({
                          ...line,
                          work_package_ref: "",
                          task_code: "",
                        })));
                      }
                    }}
                    disabled={saving}
                    required
                  >
                    <option value="">Sélectionner un projet…</option>
                    {projects.map((project) => <option value={project.number} key={project.id}>{project.number} — {project.name}</option>)}
                  </select>
                </label>

                <label>
                  <span>Demandeur</span>
                  {canDelegateRequester ? (
                    <select
                      value={form.requester_user_id}
                      onChange={(event) => setField("requester_user_id", event.target.value)}
                      disabled={saving}
                    >
                      {!form.requester_user_id && selectedDemand?.requester && (
                        <option value="">Historique — {selectedDemand.requester}</option>
                      )}
                      {requesters.map((requester) => (
                        <option value={requester.user_id} key={requester.user_id}>
                          {requester.display_name}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={selectedDemand?.requester || principal?.display_name || ""}
                      disabled
                      readOnly
                      aria-label="Demandeur"
                    />
                  )}
                  {!selectedDemand?.requester_user_id && selectedDemand?.requester && (
                    <small>Demande historique : identité canonique non attribuée.</small>
                  )}
                </label>

                <label>
                  <span>Priorité</span>
                  <select value={form.priority} onChange={(event) => setField("priority", event.target.value)} disabled={saving}>
                    <option value="Basse">Basse</option>
                    <option value="Normale">Normale</option>
                    <option value="Haute">Haute</option>
                    <option value="Urgente">Urgente</option>
                  </select>
                </label>

                <label className="span-2">
                  <span>Description / contexte de la demande</span>
                  <textarea rows={4} value={form.description} onChange={(event) => setField("description", event.target.value)} disabled={saving} placeholder="Contexte commun, travaux demandés et contraintes…" />
                </label>
              </div>

              <div className={`demand-line-mode-card ${lineMode ? "active" : ""}`}>
                <div>
                  <strong>{lineMode ? "Besoins par ligne activés" : "Besoin simple ou lignes multiples?"}</strong>
                  <span>
                    {lineMode
                      ? "Chaque ligne peut représenter de la main-d’œuvre ou un actif physique avec ses propres dates et références."
                      : "Le mode simple reste disponible pour les demandes historiques de main-d’œuvre. Active les lignes pour créer des demandes mixtes."}
                  </span>
                </div>
                {!lineMode ? (
                  <button type="button" className="secondary-button" onClick={activateLineMode} disabled={saving}>
                    Passer aux lignes multiples
                  </button>
                ) : (creating || !selectedDemand?.line_mode) ? (
                  <button type="button" className="text-button" onClick={revertUnsavedLineMode} disabled={saving}>
                    Revenir au besoin simple
                  </button>
                ) : (
                  <span className="request-line-mode-lock">Mode lignes enregistré</span>
                )}
              </div>

              {lineMode ? (
                <DemandLinesEditor
                  lines={lines}
                  onChange={(next) => {
                    setLines(next);
                    setEditorDirty(true);
                  }}
                  defaults={lineDefaultsFromForm(form, resources)}
                  generationCount={generationCount}
                  onGenerationCountChange={setGenerationCount}
                  competencies={competencies}
                  resourceClasses={resourceClasses}
                  resources={resources}
                  workPackages={workPackages}
                  tasks={tasks}
                  assetTypes={assetTypes}
                  assets={assets}
                  disabled={saving}
                />
              ) : (
                <div className="demand-form-grid demand-flat-need-grid">
                  <label>
                    <span>Tâche ERP</span>
                    <select
                      value={form.task_code}
                      onChange={(event) => setField("task_code", event.target.value)}
                      disabled={saving || !form.project_number}
                    >
                      <option value="">Aucune tâche sélectionnée</option>
                      {form.task_code && !selectedTask && (
                        <option value={form.task_code}>
                          {form.task_code} — {selectedDemand?.task_label || "tâche historique/non cataloguée"}
                        </option>
                      )}
                      {visibleTasks.map((task) => (
                        <option value={task.code} key={`${task.project_number}:${task.code}`}>
                          {task.code} — {task.label}{task.active ? "" : " · inactive"}
                        </option>
                      ))}
                    </select>
                    {selectedTask && (
                      <small>
                        {selectedTask.status}
                        {selectedTask.time_entry_enabled == null ? "" : ` · Temps: ${selectedTask.time_entry_enabled ? "oui" : "non"}`}
                        {selectedTask.expenses_enabled == null ? "" : ` · Dépenses: ${selectedTask.expenses_enabled ? "oui" : "non"}`}
                      </small>
                    )}
                  </label>

                  <label className="span-2">
                    <span>Plage moyen terme / WorkPackage</span>
                    <select value={form.work_package_ref} onChange={(event) => setField("work_package_ref", event.target.value)} disabled={saving || !form.project_number}>
                      <option value="">Aucune plage liée</option>
                      {workPackages.map((item) => (
                        <option value={item.reference} key={item.id}>
                          {item.code ? `${item.code} — ` : ""}{item.name}{item.start_date ? ` · ${item.start_date}${item.end_date && item.end_date !== item.start_date ? ` → ${item.end_date}` : ""}` : ""}{item.status ? ` · ${item.status}` : ""}
                        </option>
                      ))}
                    </select>
                    {selectedWorkPackage && <small>{selectedWorkPackage.planned_hours == null ? "" : `${selectedWorkPackage.planned_hours} h prévues · `}{selectedWorkPackage.description || "Plage moyen terme sélectionnée"}</small>}
                  </label>

                  <label>
                    <span>Confirmation</span>
                    <select value={form.confirmation} onChange={(event) => setField("confirmation", event.target.value as FormState["confirmation"])} disabled={saving}>
                      <option value="Confirmée">Confirmée</option>
                      <option value="Tentative">Tentative</option>
                    </select>
                  </label>

                  <label>
                    <span>Ressource proposée</span>
                    <select value={form.proposed_technician} onChange={(event) => setField("proposed_technician", event.target.value)} disabled={saving}>
                      <option value="">Aucune ressource proposée</option>
                      {missingProposedResource && <option value={missingProposedResource}>{missingProposedResource} — inactive/non listée</option>}
                      {resources.map((resource) => <option value={resource.name} key={resource.id}>{resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}</option>)}
                    </select>
                  </label>

                  <label>
                    <span>Début souhaité</span>
                    <input type="date" value={form.desired_start} onChange={(event) => setField("desired_start", event.target.value)} disabled={saving} required />
                  </label>

                  <label>
                    <span>Fin souhaitée</span>
                    <input type="date" min={form.desired_start || undefined} value={form.desired_end} onChange={(event) => setField("desired_end", event.target.value)} disabled={saving} />
                  </label>

                  <label>
                    <span>Nombre de ressources simultanées</span>
                    <input type="number" min="1" step="1" value={form.resource_count} onChange={(event) => setField("resource_count", event.target.value)} disabled={saving} required />
                    <small>Décrit le parallélisme; ne multiplie jamais les heures estimées. Cette valeur sert aussi de quantité initiale lorsque tu passes aux lignes multiples.</small>
                  </label>

                  <CompetencyPicker
                    competencies={competencies}
                    selectedIds={form.required_competency_ids}
                    onChange={(ids) => setField("required_competency_ids", ids)}
                    disabled={saving}
                    label="Compétences requises"
                    placeholder="Rechercher une compétence requise…"
                  />
                  {form.required_competency_ids.length === 0 && selectedDemand?.required_competencies && (
                    <small className="legacy-competency-note">
                      Valeur historique à convertir au catalogue : {selectedDemand.required_competencies}
                    </small>
                  )}

                  <label>
                    <span>Heures estimées totales</span>
                    <input type="number" min="0.25" step="0.25" value={form.estimated_hours} onChange={(event) => setField("estimated_hours", event.target.value)} disabled={saving} placeholder="Optionnel au brouillon" />
                    <small>Volume total de main-d’œuvre pour la demande, toutes ressources confondues.</small>
                  </label>

                  <label>
                    <span>Jours actifs souhaités</span>
                    <input type="number" min="1" step="1" value={form.estimated_days} onChange={(event) => setField("estimated_days", event.target.value)} disabled={saving} placeholder="Optionnel" />
                    <small>Cible de répartition dans la fenêtre. Les jours ne créent pas d'heures; les heures doivent être complétées avant l'approbation.</small>
                  </label>
                </div>
              )}

              <div className="demand-editor-note">
                <strong>Approbation ≠ confirmation.</strong>
                <span>
                  {lineMode
                    ? "Chaque ligne conserve sa propre confirmation et son propre effort. Si les heures d’une ligne sont vides, le backend applique et persiste la politique de 8 h par jour actif."
                    : "Une demande peut être approuvée tout en demeurant Tentative. Les heures représentent toujours le volume total; les jours actifs guident seulement sa répartition selon la capacité disponible."}
                </span>
              </div>

              {!creating && selectedDemand && (
                <details className="demand-advanced-options">
                  <summary>
                    <strong>Options avancées</strong>
                    <span>Responsabilité opérationnelle et dérogations de contexte.</span>
                  </summary>
                <section className="demand-contact-card">
                  <div className="panel-heading">
                    <div>
                      <span className="eyebrow">Responsabilité opérationnelle</span>
                      <h3>Contacts effectifs</h3>
                      <p>La hiérarchie est calculée par FastAPI. React affiche la source; il ne décide pas du fallback.</p>
                    </div>
                  </div>

                  <label>
                    Override du responsable pour toute la demande
                    <ContactSelect
                      contacts={contacts}
                      value={demandContactLink?.operational_responsible_override_contact_id ?? null}
                      onChange={(value) => void changeOperationalOverride(value)}
                      disabled={!canManageDemands || overridePending || saving}
                      inheritLabel="Hériter de la tâche puis du chargé de projet"
                    />
                    {!canManageDemands && <small>Lecture seule : permission manage_demands requise pour modifier.</small>}
                    {selectedDemand.status === "En planification" && canManageDemands && (
                      <small>Modifier cet override déclenchera une nouvelle approbation sans modifier immédiatement le planning approuvé.</small>
                    )}
                  </label>

                  <div className="demand-line-contact-resolutions">
                    {(selectedDemand.lines ?? []).filter((line) => line.active).map((line, index) => {
                      const resolution = lineContactResolutions[line.line_id];
                      if (!resolution) return null;
                      return (
                        <article className="demand-line-contact-row" key={line.line_id}>
                          <div className="demand-line-contact-heading">
                            <strong>Ligne {index + 1}</strong>
                            <span>
                              {resolution.task_code ? `Tâche ${resolution.task_code}` : "Sans tâche"}
                              {resolution.proposed_resource_name ? ` · ${resolution.proposed_resource_name}` : ""}
                            </span>
                          </div>
                          <ResolutionSummary title="Responsable opérationnel" resolution={resolution.operational_responsible} />
                          <ResolutionSummary title="Coordonnateur" resolution={resolution.coordinator} />
                          {resolution.diagnostics.length > 0 && (
                            <small className="contact-diagnostics">{resolution.diagnostics.join(" · ")}</small>
                          )}
                        </article>
                      );
                    })}
                  </div>
                  <small className="contact-context-note">Résolution courante des lignes enregistrées. Le contexte d'un planning déjà approuvé est conservé séparément sur ses besoins jusqu'à la réapprobation.</small>
                </section>
                </details>
              )}

              <div className="demand-editor-actions">
                {creating && (
                  <button type="button" className="secondary-button" onClick={() => {
                    if (!confirmDiscardChanges()) return;
                    setCreating(false);
                    setEditorDirty(false);
                    setSelectedNumber(demands[0]?.number ?? null);
                  }} disabled={saving}>Annuler</button>
                )}
                <button type="submit" className="primary-button" disabled={saving || detailLoading}>
                  {saving ? "Enregistrement…" : creating ? "Créer le brouillon" : "Enregistrer les modifications"}
                </button>
              </div>
            </form>
          ) : (
            <div className="demand-editor-empty">
              <strong>Aucune demande sélectionnée</strong>
              <span>Sélectionne une demande ou crée un nouveau brouillon.</span>
            </div>
          )}
          {!creating && selectedDemand && (
            <DemandDetail
              demandNumber={selectedDemand.number}
              canonicalDetail={selectedDetail}
              hasUnsavedChanges={editorDirty || contextDirty}
              onChanged={() => reloadDemand(selectedDemand.number)}
              onDirtyChange={setContextDirty}
            />
          )}
        </div>
      </div>
    </section>
  );
}
