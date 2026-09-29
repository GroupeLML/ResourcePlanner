import { FormEvent, useEffect, useMemo, useState } from "react";

import { useAuth } from "./AuthContext";
import ViewScopeSelector from "./ViewScopeSelector";
import { useViewScope } from "./ViewScopeContext";
import {
  ApiError,
  ProjectReadModel,
  WorkPackageReadModel,
  getProjects,
  getWorkPackages,
} from "./api";
import {
  DeliveryAction,
  DeliveryBoardReadModel,
  DeliveryItemReadModel,
  DeliveryItemStatus,
  DeliveryItemType,
  DeliverySummary,
  activateDeliveryPlan,
  archiveDeliveryPlan,
  createDeliveryItem,
  createDeliveryPlan,
  documentDeliveryBlockage,
  getDeliveryBoardForWorkPackage,
  getDeliverySummary,
  setDeliveryLead,
  updateDeliveryItem,
} from "./deliveryApi";

const KANBAN_COLUMNS: Array<{ status: DeliveryItemStatus; label: string }> = [
  { status: "BACKLOG", label: "Backlog" },
  { status: "TODO", label: "À faire" },
  { status: "IN_PROGRESS", label: "En cours" },
  { status: "BLOCKED", label: "Bloqué" },
  { status: "DONE", label: "Terminé" },
];

function errorMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function hours(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 1 }).format(value)} h`;
}

function percentage(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)} %`;
}

function hasAction(
  actions: DeliveryAction[],
  ...wanted: DeliveryAction[]
) {
  return wanted.some((action) => actions.includes(action));
}

type StoryCardProps = {
  item: DeliveryItemReadModel;
  epicTitle: string | null;
  deliveryVersion: number;
  disabled: boolean;
  onChanged: (board: DeliveryBoardReadModel) => Promise<void> | void;
  onError: (message: string) => void;
};

function StoryCard({
  item,
  epicTitle,
  deliveryVersion,
  disabled,
  onChanged,
  onError,
}: StoryCardProps) {
  const [status, setStatus] = useState<DeliveryItemStatus>(item.status);
  const [remaining, setRemaining] = useState(
    item.remaining_hours === null ? "" : String(item.remaining_hours),
  );
  const [estimate, setEstimate] = useState(
    item.current_estimate_hours === null ? "" : String(item.current_estimate_hours),
  );
  const [assignee, setAssignee] = useState(item.assignee_user_id ?? "");
  const [blockageNote, setBlockageNote] = useState("");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    setStatus(item.status);
    setRemaining(item.remaining_hours === null ? "" : String(item.remaining_hours));
    setEstimate(
      item.current_estimate_hours === null ? "" : String(item.current_estimate_hours),
    );
    setAssignee(item.assignee_user_id ?? "");
  }, [
    item.assignee_user_id,
    item.current_estimate_hours,
    item.remaining_hours,
    item.status,
  ]);

  const canStatus = hasAction(
    item.actions,
    "MANAGE_STORY_STATUS_BLOCKING",
    "UPDATE_OWN_STORY_STATUS",
  );
  const canRemaining = hasAction(
    item.actions,
    "ESTIMATE_STORIES",
    "UPDATE_OWN_REMAINING_HOURS",
  );
  const canEstimate = item.actions.includes("ESTIMATE_STORIES");
  const canAssign = item.actions.includes("ASSIGN_STORIES");
  const canBlockage = item.actions.includes("DOCUMENT_OWN_BLOCKAGE");

  async function save() {
    const changes: Record<string, unknown> = {};
    if (canStatus && status !== item.status) changes.status = status;
    if (canRemaining) {
      const next = remaining.trim() === "" ? null : Number(remaining);
      if (next !== item.remaining_hours) changes.remaining_hours = next;
    }
    if (canEstimate) {
      const next = estimate.trim() === "" ? null : Number(estimate);
      if (next !== item.current_estimate_hours) changes.current_estimate_hours = next;
    }
    if (canAssign) {
      const next = assignee.trim() || null;
      if (next !== item.assignee_user_id) changes.assignee_user_id = next;
    }
    if (Object.keys(changes).length === 0) return;

    setPending(true);
    onError("");
    try {
      const board = await updateDeliveryItem(item.id, {
        expected_delivery_version: deliveryVersion,
        ...changes,
      });
      await onChanged(board);
    } catch (reason) {
      onError(errorMessage(reason, "Impossible de modifier la Story."));
    } finally {
      setPending(false);
    }
  }

  async function saveBlockage() {
    const note = blockageNote.trim();
    if (!note) return;
    setPending(true);
    onError("");
    try {
      const board = await documentDeliveryBlockage(
        item.id,
        note,
        deliveryVersion,
      );
      setBlockageNote("");
      await onChanged(board);
    } catch (reason) {
      onError(errorMessage(reason, "Impossible d'enregistrer le blocage."));
    } finally {
      setPending(false);
    }
  }

  return (
    <article className="delivery-story-card" data-status={item.status}>
      <div className="delivery-story-heading">
        <div>
          <span>{epicTitle ? `Epic · ${epicTitle}` : "Story sans Epic"}</span>
          <strong>{item.title}</strong>
        </div>
        {item.priority && <small>{item.priority}</small>}
      </div>

      {item.description && <p>{item.description}</p>}

      <dl className="delivery-story-facts">
        <div>
          <dt>Référence</dt>
          <dd>{hours(item.reference_estimate_hours)}</dd>
        </div>
        <div>
          <dt>Estimation</dt>
          <dd>{hours(item.current_estimate_hours)}</dd>
        </div>
        <div>
          <dt>Restant</dt>
          <dd>{hours(item.remaining_hours)}</dd>
        </div>
        <div>
          <dt>Échéance</dt>
          <dd>{item.due_date || "—"}</dd>
        </div>
      </dl>

      {(canStatus || canRemaining || canEstimate || canAssign) && (
        <div className="delivery-story-controls" aria-label={`Actions pour ${item.title}`}>
          {canStatus && (
            <label>
              <span>Statut</span>
              <select
                value={status}
                disabled={disabled || pending}
                onChange={(event) => setStatus(event.target.value as DeliveryItemStatus)}
              >
                {KANBAN_COLUMNS.map((column) => (
                  <option key={column.status} value={column.status}>
                    {column.label}
                  </option>
                ))}
                <option value="CANCELLED">Annulé</option>
              </select>
            </label>
          )}
          {canRemaining && (
            <label>
              <span>Restant (h)</span>
              <input
                type="number"
                min="0"
                step="0.5"
                value={remaining}
                disabled={disabled || pending}
                onChange={(event) => setRemaining(event.target.value)}
              />
            </label>
          )}
          {canEstimate && (
            <label>
              <span>Estimation (h)</span>
              <input
                type="number"
                min="0.1"
                step="0.5"
                value={estimate}
                disabled={disabled || pending}
                onChange={(event) => setEstimate(event.target.value)}
              />
            </label>
          )}
          {canAssign && (
            <label className="delivery-control-wide">
              <span>AppUser assigné</span>
              <input
                value={assignee}
                disabled={disabled || pending}
                placeholder="Identifiant AppUser"
                onChange={(event) => setAssignee(event.target.value)}
              />
            </label>
          )}
          <button
            type="button"
            disabled={disabled || pending}
            onClick={() => void save()}
          >
            Enregistrer
          </button>
        </div>
      )}

      {canBlockage && item.status === "BLOCKED" && (
        <div className="delivery-blockage-note">
          <label>
            <span>Note de blocage</span>
            <textarea
              value={blockageNote}
              disabled={disabled || pending}
              placeholder="Décrire la dépendance ou le blocage…"
              onChange={(event) => setBlockageNote(event.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={disabled || pending || !blockageNote.trim()}
            onClick={() => void saveBlockage()}
          >
            Ajouter la note
          </button>
        </div>
      )}
    </article>
  );
}

export default function DeliveryPage() {
  const { can } = useAuth();
  const { scope, loading: scopeLoading } = useViewScope();
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [workPackages, setWorkPackages] = useState<WorkPackageReadModel[]>([]);
  const [projectNumber, setProjectNumber] = useState("");
  const [workPackageId, setWorkPackageId] = useState("");
  const [board, setBoard] = useState<DeliveryBoardReadModel | null>(null);
  const [summary, setSummary] = useState<DeliverySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [leadUserId, setLeadUserId] = useState("");

  const [itemType, setItemType] = useState<DeliveryItemType>("STORY");
  const [itemTitle, setItemTitle] = useState("");
  const [itemEpic, setItemEpic] = useState("");
  const [itemAssignee, setItemAssignee] = useState("");
  const [itemEstimate, setItemEstimate] = useState("");
  const [itemRemaining, setItemRemaining] = useState("");
  const [itemDueDate, setItemDueDate] = useState("");
  const [itemDescription, setItemDescription] = useState("");

  useEffect(() => {
    if (scopeLoading) return;
    const controller = new AbortController();
    setLoading(true);
    getProjects(false, controller.signal, scope)
      .then((rows) => {
        setProjects(rows);
        setProjectNumber((current) => (
          current && rows.some((project) => project.number === current)
            ? current
            : rows[0]?.number ?? ""
        ));
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(errorMessage(reason, "Impossible de charger les projets."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [scope, scopeLoading]);

  useEffect(() => {
    if (!projectNumber) {
      setWorkPackages([]);
      setWorkPackageId("");
      return;
    }
    const controller = new AbortController();
    getWorkPackages(projectNumber, false, controller.signal, scope)
      .then((rows) => {
        setWorkPackages(rows);
        setWorkPackageId((current) => (
          current && rows.some((workPackage) => workPackage.id === current)
            ? current
            : rows[0]?.id ?? ""
        ));
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(errorMessage(reason, "Impossible de charger les WorkPackages."));
      });
    return () => controller.abort();
  }, [projectNumber, scope]);

  async function reloadDelivery(signal?: AbortSignal) {
    if (!workPackageId) {
      setBoard(null);
      setSummary(null);
      return;
    }
    const [nextBoard, nextSummary] = await Promise.all([
      getDeliveryBoardForWorkPackage(workPackageId, signal),
      getDeliverySummary(workPackageId, signal),
    ]);
    setBoard(nextBoard);
    setSummary(nextSummary);
    setLeadUserId(nextBoard?.plan.lead_user_id ?? "");
  }

  useEffect(() => {
    if (!workPackageId) {
      setBoard(null);
      setSummary(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    reloadDelivery(controller.signal)
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(errorMessage(reason, "Impossible de charger Delivery."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [workPackageId]);

  const epics = useMemo(
    () => (board?.items ?? [])
      .filter((item) => item.item_type === "EPIC")
      .sort((left, right) => left.position - right.position),
    [board],
  );
  const stories = useMemo(
    () => (board?.items ?? [])
      .filter((item) => item.item_type === "STORY")
      .sort((left, right) => left.position - right.position),
    [board],
  );
  const epicTitles = useMemo(
    () => new Map(epics.map((epic) => [epic.id, epic.title])),
    [epics],
  );
  const cancelledStories = stories.filter((story) => story.status === "CANCELLED");

  async function adoptBoard(nextBoard: DeliveryBoardReadModel) {
    setBoard(nextBoard);
    setLeadUserId(nextBoard.plan.lead_user_id ?? "");
    try {
      const nextSummary = await getDeliverySummary(workPackageId);
      setSummary(nextSummary);
    } catch (reason) {
      setError(errorMessage(reason, "Le board est à jour, mais le résumé n'a pas pu être rechargé."));
    }
  }

  async function runMutation(
    operation: () => Promise<DeliveryBoardReadModel>,
    fallback: string,
  ) {
    setPending(true);
    setError("");
    try {
      await adoptBoard(await operation());
    } catch (reason) {
      setError(errorMessage(reason, fallback));
    } finally {
      setPending(false);
    }
  }

  async function createPlan() {
    await runMutation(
      () => createDeliveryPlan(workPackageId, leadUserId.trim() || null),
      "Impossible de créer le DeliveryPlan.",
    );
  }

  async function saveLead() {
    if (!board) return;
    await runMutation(
      () => setDeliveryLead(
        board.plan.id,
        leadUserId.trim() || null,
        board.plan.delivery_version,
      ),
      "Impossible de modifier le Team Lead.",
    );
  }

  async function submitItem(event: FormEvent) {
    event.preventDefault();
    if (!board || !itemTitle.trim()) return;
    const estimate = itemEstimate.trim() ? Number(itemEstimate) : null;
    const remaining = itemRemaining.trim() ? Number(itemRemaining) : null;
    await runMutation(
      () => createDeliveryItem(board.plan.id, {
        expected_delivery_version: board.plan.delivery_version,
        item_type: itemType,
        title: itemTitle.trim(),
        parent_id: itemType === "STORY" ? itemEpic || null : null,
        description: itemDescription.trim() || null,
        status: "BACKLOG",
        assignee_user_id: itemType === "STORY" ? itemAssignee.trim() || null : null,
        current_estimate_hours: itemType === "STORY" ? estimate : null,
        remaining_hours: itemType === "STORY" ? remaining : null,
        due_date: itemType === "STORY" ? itemDueDate || null : null,
      }),
      "Impossible de créer l'élément Delivery.",
    );
    setItemTitle("");
    setItemDescription("");
    setItemAssignee("");
    setItemEstimate("");
    setItemRemaining("");
    setItemDueDate("");
  }

  const selectedWorkPackage = workPackages.find((row) => row.id === workPackageId) ?? null;
  const planActions = board?.actions ?? [];
  const planIsEditable = board?.plan.status !== "ARCHIVED";

  return (
    <section className="delivery-page">
      <div className="page-heading delivery-page-heading">
        <div>
          <span className="eyebrow">Delivery</span>
          <h1>WorkPackages, Epics et Stories</h1>
          <p>
            Le board suit le travail technique; la capacité réservée reste autoritaire
            dans Planning et n'est jamais traitée comme un actual.
          </p>
        </div>
        <ViewScopeSelector />
      </div>

      {error && (
        <div className="error-panel" role="alert">
          <strong>Delivery</strong>
          <span>{error}</span>
        </div>
      )}

      <div className="delivery-selector-panel">
        <label>
          <span>Projet</span>
          <select
            value={projectNumber}
            disabled={scopeLoading || loading}
            onChange={(event) => setProjectNumber(event.target.value)}
          >
            {projects.length === 0 && <option value="">Aucun projet</option>}
            {projects.map((project) => (
              <option key={project.id} value={project.number}>
                {project.number} · {project.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>WorkPackage</span>
          <select
            value={workPackageId}
            disabled={!projectNumber || loading}
            onChange={(event) => setWorkPackageId(event.target.value)}
          >
            {workPackages.length === 0 && <option value="">Aucun WorkPackage</option>}
            {workPackages.map((workPackage) => (
              <option key={workPackage.id} value={workPackage.id}>
                {workPackage.reference} · {workPackage.name}
              </option>
            ))}
          </select>
        </label>
        {selectedWorkPackage && (
          <div className="delivery-selected-reference">
            <span>Référence WorkPackage</span>
            <strong>{selectedWorkPackage.reference}</strong>
            <small>{hours(selectedWorkPackage.planned_hours)} de référence</small>
          </div>
        )}
      </div>

      {!workPackageId ? (
        <div className="placeholder-panel">
          <strong>Sélectionnez un WorkPackage.</strong>
          <p>Le board Delivery et son forecast apparaîtront ici.</p>
        </div>
      ) : loading ? (
        <div className="placeholder-panel">Chargement de Delivery…</div>
      ) : (
        <>
          {summary && (
            <section className="delivery-summary" aria-label="Résumé Delivery">
              <article>
                <span>Progression</span>
                <strong>{percentage(summary.progress.progress_ratio)}</strong>
                <small>
                  {hours(summary.progress.completed_reference_hours)} / {hours(summary.progress.total_reference_hours)}
                  {" "}· couverture {percentage(summary.progress.coverage_ratio)}
                </small>
              </article>
              <article>
                <span>Travail restant</span>
                <strong>{hours(summary.forecast.total_remaining_hours)}</strong>
                <small>
                  {summary.forecast.open_story_count} Story(s) ouverte(s) · couverture {percentage(summary.forecast.coverage_ratio)}
                </small>
              </article>
              <article>
                <span>Capacité réservée</span>
                <strong>{hours(summary.planning_capacity.human_reserved_hours)}</strong>
                <small>
                  Planning v{summary.planning_capacity.observed_planning_version}
                  {" "}· plan actif/approuvé
                </small>
              </article>
              <article>
                <span>Balance forecast</span>
                <strong>{hours(summary.forecast_capacity_balance_hours)}</strong>
                <small>Capacité réservée − restant Delivery</small>
              </article>
              {summary.planning_capacity.asset_reserved_capacity.length > 0 && (
                <article>
                  <span>Actifs réservés</span>
                  <strong>{summary.planning_capacity.asset_reserved_capacity.length}</strong>
                  <small>
                    {summary.planning_capacity.asset_reserved_capacity
                      .map((asset) => `${asset.asset_type_id}: ${asset.reserved_days} j`)
                      .join(" · ")}
                  </small>
                </article>
              )}
            </section>
          )}

          {summary && summary.diagnostics.length > 0 && (
            <div className="delivery-diagnostics" aria-label="Diagnostics Delivery">
              {summary.diagnostics.map((diagnostic, index) => (
                <span key={`${diagnostic.code}-${index}`}>
                  {diagnostic.code}
                </span>
              ))}
            </div>
          )}

          {!board ? (
            <section className="delivery-empty-plan">
              <div>
                <span className="eyebrow">Aucun DeliveryPlan</span>
                <h2>{summary?.work_package.name ?? selectedWorkPackage?.name}</h2>
                <p>
                  Le WorkPackage demeure valide sans DeliveryPlan. Créez le board
                  seulement lorsque le suivi technique doit commencer.
                </p>
              </div>
              {can("manage_delivery") && (
                <div className="delivery-create-plan">
                  <label>
                    <span>Team Lead AppUser (facultatif)</span>
                    <input
                      value={leadUserId}
                      placeholder="Identifiant AppUser"
                      onChange={(event) => setLeadUserId(event.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={pending}
                    onClick={() => void createPlan()}
                  >
                    Créer le DeliveryPlan
                  </button>
                </div>
              )}
            </section>
          ) : (
            <>
              <section className="delivery-plan-toolbar">
                <div>
                  <span className="eyebrow">DeliveryPlan</span>
                  <h2>
                    {summary?.work_package.reference ?? selectedWorkPackage?.reference}
                    {" "}· {board.plan.status}
                  </h2>
                  <p>
                    Version Delivery {board.plan.delivery_version}; concurrence indépendante
                    de Planning.
                  </p>
                </div>
                <div className="delivery-plan-actions">
                  {planActions.includes("ASSIGN_TEAM_LEAD") && planIsEditable && (
                    <label>
                      <span>Team Lead AppUser</span>
                      <input
                        value={leadUserId}
                        disabled={pending}
                        placeholder="Identifiant AppUser"
                        onChange={(event) => setLeadUserId(event.target.value)}
                      />
                      <button
                        type="button"
                        disabled={pending}
                        onClick={() => void saveLead()}
                      >
                        Enregistrer le lead
                      </button>
                    </label>
                  )}
                  {planActions.includes("MANAGE_PLAN_LIFECYCLE") && board.plan.status === "DRAFT" && (
                    <button
                      type="button"
                      disabled={pending}
                      onClick={() => void runMutation(
                        () => activateDeliveryPlan(
                          board.plan.id,
                          board.plan.delivery_version,
                        ),
                        "Impossible d'activer le DeliveryPlan.",
                      )}
                    >
                      Activer
                    </button>
                  )}
                  {planActions.includes("MANAGE_PLAN_LIFECYCLE") && board.plan.status === "ACTIVE" && (
                    <button
                      type="button"
                      className="secondary-button"
                      disabled={pending}
                      onClick={() => void runMutation(
                        () => archiveDeliveryPlan(
                          board.plan.id,
                          board.plan.delivery_version,
                        ),
                        "Impossible d'archiver le DeliveryPlan.",
                      )}
                    >
                      Archiver
                    </button>
                  )}
                </div>
              </section>

              <div className="delivery-workspace">
                <aside className="delivery-epics" aria-label="Epics">
                  <div className="delivery-section-heading">
                    <div>
                      <span className="eyebrow">Structure</span>
                      <h3>Epics</h3>
                    </div>
                    <strong>{epics.length}</strong>
                  </div>
                  {epics.length === 0 ? (
                    <p className="delivery-empty-copy">Aucun Epic.</p>
                  ) : epics.map((epic) => {
                    const count = stories.filter((story) => story.parent_id === epic.id).length;
                    return (
                      <article key={epic.id} className="delivery-epic-card">
                        <strong>{epic.title}</strong>
                        {epic.description && <p>{epic.description}</p>}
                        <span>{count} Story(s)</span>
                      </article>
                    );
                  })}
                </aside>

                <section className="delivery-kanban-section" aria-label="Kanban Delivery">
                  <div className="delivery-section-heading">
                    <div>
                      <span className="eyebrow">Exécution</span>
                      <h3>Kanban</h3>
                    </div>
                    <span>{stories.length - cancelledStories.length} Story(s) active(s)</span>
                  </div>
                  <div className="delivery-kanban-scroll">
                    <div className="delivery-kanban">
                      {KANBAN_COLUMNS.map((column) => {
                        const columnStories = stories.filter(
                          (story) => story.status === column.status,
                        );
                        return (
                          <section
                            key={column.status}
                            className="delivery-kanban-column"
                            aria-labelledby={`delivery-column-${column.status}`}
                          >
                            <header>
                              <strong id={`delivery-column-${column.status}`}>
                                {column.label}
                              </strong>
                              <span>{columnStories.length}</span>
                            </header>
                            <div className="delivery-kanban-list">
                              {columnStories.length === 0 ? (
                                <p className="delivery-empty-copy">Aucune Story</p>
                              ) : columnStories.map((story) => (
                                <StoryCard
                                  key={story.id}
                                  item={story}
                                  epicTitle={
                                    story.parent_id
                                      ? epicTitles.get(story.parent_id) ?? null
                                      : null
                                  }
                                  deliveryVersion={board.plan.delivery_version}
                                  disabled={pending || !planIsEditable}
                                  onChanged={adoptBoard}
                                  onError={setError}
                                />
                              ))}
                            </div>
                          </section>
                        );
                      })}
                    </div>
                  </div>
                  {cancelledStories.length > 0 && (
                    <details className="delivery-cancelled">
                      <summary>{cancelledStories.length} Story(s) annulée(s)</summary>
                      <ul>
                        {cancelledStories.map((story) => (
                          <li key={story.id}>{story.title}</li>
                        ))}
                      </ul>
                    </details>
                  )}
                </section>
              </div>

              {planActions.includes("MANAGE_STRUCTURE") && planIsEditable && (
                <form className="delivery-create-item" onSubmit={(event) => void submitItem(event)}>
                  <div className="delivery-section-heading">
                    <div>
                      <span className="eyebrow">Découpage</span>
                      <h3>Ajouter un Epic ou une Story</h3>
                    </div>
                  </div>
                  <div className="delivery-create-grid">
                    <label>
                      <span>Type</span>
                      <select
                        value={itemType}
                        disabled={pending}
                        onChange={(event) => setItemType(event.target.value as DeliveryItemType)}
                      >
                        <option value="STORY">Story</option>
                        <option value="EPIC">Epic</option>
                      </select>
                    </label>
                    <label className="delivery-field-wide">
                      <span>Titre</span>
                      <input
                        required
                        value={itemTitle}
                        disabled={pending}
                        onChange={(event) => setItemTitle(event.target.value)}
                      />
                    </label>
                    {itemType === "STORY" && (
                      <>
                        <label>
                          <span>Epic parent</span>
                          <select
                            value={itemEpic}
                            disabled={pending}
                            onChange={(event) => setItemEpic(event.target.value)}
                          >
                            <option value="">Sans Epic</option>
                            {epics.map((epic) => (
                              <option key={epic.id} value={epic.id}>{epic.title}</option>
                            ))}
                          </select>
                        </label>
                        <label>
                          <span>AppUser assigné</span>
                          <input
                            value={itemAssignee}
                            disabled={pending || !planActions.includes("ASSIGN_STORIES")}
                            placeholder="Identifiant AppUser"
                            onChange={(event) => setItemAssignee(event.target.value)}
                          />
                        </label>
                        <label>
                          <span>Estimation (h)</span>
                          <input
                            type="number"
                            min="0.1"
                            step="0.5"
                            value={itemEstimate}
                            disabled={pending}
                            onChange={(event) => setItemEstimate(event.target.value)}
                          />
                        </label>
                        <label>
                          <span>Restant (h)</span>
                          <input
                            type="number"
                            min="0"
                            step="0.5"
                            value={itemRemaining}
                            disabled={pending}
                            onChange={(event) => setItemRemaining(event.target.value)}
                          />
                        </label>
                        <label>
                          <span>Échéance</span>
                          <input
                            type="date"
                            value={itemDueDate}
                            disabled={pending}
                            onChange={(event) => setItemDueDate(event.target.value)}
                          />
                        </label>
                      </>
                    )}
                    <label className="delivery-field-wide">
                      <span>Description</span>
                      <textarea
                        value={itemDescription}
                        disabled={pending}
                        onChange={(event) => setItemDescription(event.target.value)}
                      />
                    </label>
                  </div>
                  <button type="submit" disabled={pending || !itemTitle.trim()}>
                    Ajouter {itemType === "EPIC" ? "l'Epic" : "la Story"}
                  </button>
                </form>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
