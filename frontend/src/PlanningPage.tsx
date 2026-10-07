import { resourceDisplayName } from "./resourceLabels";
import { useEffect, useMemo, useRef, useState } from "react";

import { createClientId } from "./clientId";
import {
  ApiError,
  PendingDemandLoadReadModel,
  PlanningActionReadModel,
  PlanningCapacityGridReadModel,
  PlanningResourceCapacityReadModel,
  PlanningSegmentCapacityDiagnosticReadModel,
  PlanningSnapshotReadModel,
  ResourceReadModel,
  ShiftReadModel,
  getPlanningActions,
  getPlanningCapacityGrid,
  getPlanningSnapshot,
  getResources,
  moveAllocation,
  reorderPlanningResource,
} from "./api";
import {
  addDays,
  formatDay,
  formatWeekRange,
  isToday,
  sameIsoDate,
  startOfWeek,
  toIsoDate,
  weekDays,
} from "./dates";
import {
  ResourceSortMode,
  loadCollapsedPlanningClasses,
  loadPlanningResourceSort,
  loadPlanningWeekStart,
  saveCollapsedPlanningClasses,
  savePlanningResourceSort,
  savePlanningWeekStart,
} from "./planningPreferences";
import { useAuth } from "./AuthContext";
import { useViewScope } from "./ViewScopeContext";
import ViewScopeSelector from "./ViewScopeSelector";
import {
  SegmentDragPayload,
  ShiftDragPayload,
  hasSegmentDrag,
  hasShiftDrag,
  readSegmentDrag,
  readShiftDrag,
  writeShiftDrag,
} from "./planningDragDrop";
import { assignSegment } from "./segments-api";
import {
  OverallocationApiError,
  OverallocationContext,
  PlanningDropEvaluation,
  duplicateAllocationAtomic,
  evaluateAllocationDrop,
  extendAndMoveAllocationAtomic,
  overallocationContext,
  overrideAndMovePlanningWindow,
  proposeAllocationWindowExtension,
  splitAllocationAtomic,
} from "./manualOverallocationApi";
import AssetPlanningPanel from "./AssetPlanningPanel";
import DemandDetail from "./DemandDetail";
import DemandWorkflowPage from "./DemandWorkflowPage";
import ManualAllocationEditor from "./ManualAllocationEditor";
import PlanningActionPanel from "./PlanningActionPanel";
import PlanningDropDialog, { PlanningDropExecutionRequest } from "./PlanningDropDialog";
import QuickShiftEditor from "./QuickShiftEditor";
import SegmentEditor from "./SegmentEditor";
import ShiftAssetAssignmentDialog, { ShiftAssetAssignmentMode } from "./ShiftAssetAssignmentDialog";
import ShiftEditor from "./ShiftEditor";

type ConfirmationFilter = "all" | "confirmed" | "tentative";
type ResourceGroupEntry = {
  resource: ResourceReadModel;
  shifts: ShiftReadModel[];
  capacity: PlanningResourceCapacityReadModel | null;
  pendingLoads: PendingDemandLoadReadModel[];
};

type ManualResourceOrderControls = {
  canMoveUp: boolean;
  canMoveDown: boolean;
  busy: boolean;
  onMove: (direction: "up" | "down") => void;
};

function compareManualResources(left: ResourceReadModel, right: ResourceReadModel) {
  const manualOrder = left.sort_order - right.sort_order;
  if (manualOrder !== 0) return manualOrder;
  const nameOrder = left.name.localeCompare(right.name, "fr-CA", { sensitivity: "base" });
  if (nameOrder !== 0) return nameOrder;
  const exactNameOrder = left.name.localeCompare(right.name, "fr-CA");
  if (exactNameOrder !== 0) return exactNameOrder;
  return left.id.localeCompare(right.id, "fr-CA");
}

function compareResourceGroupEntries(
  left: ResourceGroupEntry,
  right: ResourceGroupEntry,
  mode: ResourceSortMode,
) {
  if (mode === "manual") {
    return compareManualResources(left.resource, right.resource);
  }

  if (mode === "availability") {
    if (left.capacity && !right.capacity) return -1;
    if (!left.capacity && right.capacity) return 1;
    if (left.capacity && right.capacity) {
      const freeCapacity = right.capacity.prudent_free - left.capacity.prudent_free;
      if (freeCapacity !== 0) return freeCapacity;
    }
  }

  const nameOrder = resourceDisplayName(left.resource.name).localeCompare(resourceDisplayName(right.resource.name), "fr-CA");
  if (nameOrder !== 0) return nameOrder;
  return left.resource.id.localeCompare(right.resource.id, "fr-CA");
}

type EmergencyShiftReadModel = ShiftReadModel & { emergency_override_active?: boolean };
type OverallocationShiftReadModel = ShiftReadModel & {
  segment_planned_hours?: number;
  segment_locked_hours?: number;
  segment_overallocated_hours?: number;
};

type DropDialogState = {
  payload: ShiftDragPayload;
  targetResource: ResourceReadModel;
  targetDay: string;
  evaluation: PlanningDropEvaluation;
  error: string | null;
  overallocationPrompt: OverallocationContext | null;
  actionKeys: Record<string, string>;
};

const STALE_DROP_CODES = new Set([
  "planning_version_conflict",
  "operational_choice_version_conflict",
  "planning_authorization_unknown",
  "planning_authorization_revision_conflict",
  "planning_authorization_revision_required",
  "demand_version_conflict",
  "allocation_window_proposal_stale",
]);

function newDropIdempotencyKey() {
  return createClientId();
}

function dropActionKeys(evaluation: PlanningDropEvaluation) {
  return Object.fromEntries(
    evaluation.actions
      .filter((action) => action.code !== "MOVE" && action.code !== "CANCEL")
      .map((action) => [action.code, newDropIdempotencyKey()]),
  );
}

function normalize(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase("fr-CA");
}

function confirmationKind(value: string | null | undefined): "confirmed" | "tentative" | "unknown" {
  const normalized = normalize(value);
  if (normalized.startsWith("confirm")) return "confirmed";
  if (normalized.startsWith("tent")) return "tentative";
  return "unknown";
}

function confirmationLabel(value: string | null | undefined) {
  const kind = confirmationKind(value);
  if (kind === "confirmed") return "Confirmée";
  if (kind === "tentative") return "Tentative";
  return value || "Non précisée";
}

function hours(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", {
    maximumFractionDigits: 1,
    minimumFractionDigits: Number(value ?? 0) % 1 ? 1 : 0,
  }).format(Number(value ?? 0));
}

function shiftText(shift: ShiftReadModel) {
  return normalize([
    shift.resource_name,
    shift.project_number,
    shift.project_name,
    shift.demand_number,
    shift.project_manager,
    shift.requester,
    shift.note,
  ].filter(Boolean).join(" "));
}

function actionText(action: PlanningActionReadModel) {
  return normalize([
    action.reference,
    action.demand_number,
    action.segment_id,
    action.project_number,
    action.project_name,
    action.task_code,
    action.task_label,
    action.required_competency,
    action.priority,
    action.project_manager,
    action.requester,
  ].filter(Boolean).join(" "));
}

function pendingText(load: PendingDemandLoadReadModel) {
  return normalize([
    load.demand_number,
    load.project_number,
    load.project_name,
    load.required_competencies,
    load.proposed_resource,
  ].filter(Boolean).join(" "));
}

function ProjectLabel({ number, name }: { number: string | null; name: string | null }) {
  return (
    <>
      <strong>{number || "Projet"}</strong>
      {name && <span>{name}</span>}
    </>
  );
}

function shiftAssetDiagnosticLabel(code: string) {
  switch (code) {
    case "asset_assignment_incomplete":
      return "Affectation d’actif incomplète";
    case "asset_inactive":
      return "Actif inactif";
    case "operator_not_qualified":
      return "Opérateur non qualifié";
    case "related_asset_reservations_multiple":
      return "Plusieurs réservations d’actifs liées ou héritées";
    default:
      return code;
  }
}

function ShiftCard({
  shift,
  diagnostic,
  onEdit,
  onAssignAsset,
  dragEnabled,
}: {
  shift: ShiftReadModel;
  diagnostic: PlanningSegmentCapacityDiagnosticReadModel | null;
  onEdit: (shift: ShiftReadModel) => void;
  onAssignAsset?: (shift: ShiftReadModel) => void;
  dragEnabled: boolean;
}) {
  const confirmation = confirmationKind(shift.confirmation);
  const emergencyOverride = Boolean((shift as EmergencyShiftReadModel).emergency_override_active);
  const overallocationShift = shift as OverallocationShiftReadModel;
  const excess = Number(overallocationShift.segment_overallocated_hours ?? 0);
  const unplaced = Number(diagnostic?.unplaced_hours ?? 0);
  const asset = shift.asset_assignment;
  const relatedAssets = shift.related_asset_reservations ?? [];
  const inheritedAssets = relatedAssets.filter((reservation) =>
    reservation.association_kind.startsWith("INHERITED_"),
  );
  const assetDiagnostics = shift.asset_diagnostics ?? [];
  const meta = [
    shift.allocation_type,
    shift.source !== "AUTO" ? shift.source : null,
    shift.locked ? "Verrouillé" : null,
    shift.outside_standard_hours ? "Hors horaire" : null,
    emergencyOverride ? "⚠ Dérogation urgente" : null,
    excess > 0 ? `⚠ Surallocation manuelle +${hours(excess)} h` : null,
    unplaced > 0 ? `⚠ ${hours(unplaced)} h non placées` : null,
  ].filter(Boolean);
  const editLabel = `Modifier le quart ${shift.project_number || shift.project_name || shift.allocation_id}, ${hours(shift.hours)} heures${emergencyOverride ? ", dérogation urgente active" : ""}${excess > 0 ? `, surallocation manuelle de ${hours(excess)} heures` : ""}${unplaced > 0 ? `, ${hours(unplaced)} heures non placées` : ""}`;
  const title = [
    dragEnabled ? "Glisser vers une autre ressource/journée, ou cliquer pour modifier" : "Cliquer pour modifier",
    emergencyOverride ? "⚠ Dérogation d’approbation urgente — régularisation requise" : null,
    excess > 0 ? `⚠ Surallocation manuelle : ${hours(overallocationShift.segment_locked_hours)} h verrouillées pour ${hours(overallocationShift.segment_planned_hours)} h prévues` : null,
    unplaced > 0 ? `⚠ Capacité standard insuffisante : ${hours(unplaced)} h du segment restent à placer` : null,
    ...assetDiagnostics.map((code) => `⚠ ${shiftAssetDiagnosticLabel(code)}`),
    shift.project_name,
    shift.demand_number ? `Demande ${shift.demand_number}` : null,
    shift.project_manager ? `Responsable: ${shift.project_manager}` : null,
    shift.requester ? `Demandeur: ${shift.requester}` : null,
    shift.note,
  ].filter(Boolean).join("\n");

  return (
    <article
      className={`shift-card shift-${confirmation} ${shift.outside_standard_hours ? "shift-outside" : ""} ${excess > 0 ? "shift-overallocated" : ""} ${unplaced > 0 ? "shift-unplaced" : ""} ${dragEnabled ? "is-draggable" : ""}`}
      draggable={dragEnabled}
      data-allocation-id={shift.allocation_id}
      data-segment-id={shift.segment_id}
      onDragStart={(event) => {
        if (!dragEnabled || (event.target as HTMLElement).closest(".shift-asset-action")) {
          event.preventDefault();
          return;
        }
        writeShiftDrag(event.dataTransfer, {
          kind: "SHIFT",
          allocation_id: shift.allocation_id,
          resource_id: shift.resource_id,
          work_date: shift.work_date,
        });
      }}
    >
      <button
        type="button"
        className="shift-card-main"
        onClick={() => onEdit(shift)}
        aria-label={editLabel}
        title={title}
      >
        <div className="shift-card-heading">
          <div className="shift-project">
            <ProjectLabel number={shift.project_number} name={shift.project_name} />
          </div>
          <strong className="shift-hours">{hours(shift.hours)} h</strong>
        </div>
        <div className="shift-badges">
          <span className={`confirmation-badge confirmation-${confirmation}`}>
            {confirmationLabel(shift.confirmation)}
          </span>
          {shift.demand_number && <span>#{shift.demand_number}</span>}
        </div>
        {meta.length > 0 && <small>{meta.join(" · ")}</small>}
        <small
          className="shift-assets"
          aria-label={
            asset
              ? "Actif affecté au quart"
              : inheritedAssets.length > 0
                ? "Actif lié ou hérité par le quart"
                : "Aucun actif affecté au quart"
          }
        >
          {asset
            ? `Actif : ${asset.asset_code}${asset.asset_active ? "" : " · inactif"}`
            : inheritedAssets.length > 0
              ? `Actif lié : ${inheritedAssets.map((reservation) => reservation.asset_code).join(", ")}`
              : "Aucun actif"}
          {assetDiagnostics.length > 0 && (
            <span
              className="shift-asset-diagnostic"
              aria-label={assetDiagnostics.map(shiftAssetDiagnosticLabel).join(", ")}
              title={assetDiagnostics.map(shiftAssetDiagnosticLabel).join("\n")}
            >
              {" "}⚠
            </span>
          )}
        </small>
      </button>
      {!asset && shift.asset_actions?.assign.allowed && onAssignAsset && (
        <button
          type="button"
          className="shift-asset-action"
          draggable={false}
          onClick={() => onAssignAsset(shift)}
          aria-label={`Assigner un actif à ${resourceDisplayName(shift.resource_name)} pour le quart ${shift.allocation_id}`}
        >
          Assigner un actif
        </button>
      )}
    </article>
  );
}

function PendingLoadCard({
  load,
  onOpenDemand,
}: {
  load: PendingDemandLoadReadModel;
  onOpenDemand: (demandNumber: string) => void;
}) {
  const proposed = load.projected_hours ?? load.window_hours;
  const replacement = normalize(load.mode) === "replacement";
  const dateLabel = load.start_date === load.end_date
    ? load.start_date
    : `${load.start_date} → ${load.end_date}`;

  return (
    <article className={`pending-card ${replacement ? "pending-replacement" : ""}`}>
      <div className="pending-card-heading">
        <div>
          <span className="pending-kicker">
            {replacement ? "Modification en attente" : "Charge potentielle"}
          </span>
          <ProjectLabel number={load.project_number} name={load.project_name} />
        </div>
        <strong>{hours(proposed)} h</strong>
      </div>
      <div className="pending-meta">
        <span>Demande {load.demand_number}</span>
        <span>{dateLabel}</span>
        {load.required_competencies && <span>{load.required_competencies}</span>}
        {load.proposed_resource && <span>Proposé : {resourceDisplayName(load.proposed_resource)}</span>}
      </div>
      {replacement && (
        <div className="pending-delta">
          <span>Plan actuel : {hours(load.current_plan_hours)} h</span>
          {load.delta_hours != null && (
            <strong className={load.delta_hours > 0 ? "positive-delta" : load.delta_hours < 0 ? "negative-delta" : ""}>
              Delta {load.delta_hours > 0 ? "+" : ""}{hours(load.delta_hours)} h
            </strong>
          )}
        </div>
      )}
      <button
        type="button"
        className="pending-detail-button"
        onClick={() => onOpenDemand(load.demand_number)}
      >
        Ouvrir le détail de la demande
      </button>
    </article>
  );
}

function PendingGhostCard({
  load,
  onOpenDemand,
}: {
  load: PendingDemandLoadReadModel;
  onOpenDemand?: (demandNumber: string) => void;
}) {
  const tentative = confirmationKind(load.confirmation) === "tentative";
  return (
    <button
      type="button"
      className={`pending-ghost-card ${tentative ? "is-tentative" : ""}`}
      onClick={() => onOpenDemand?.(load.demand_number)}
      disabled={!onOpenDemand}
      title={`Demande ${load.demand_number} · ressource proposée ${resourceDisplayName(load.proposed_resource) || "—"} · aucune charge ferme comptabilisée`}
    >
      <strong>{load.project_number || "Projet"}</strong>
      <span>{load.project_name || load.demand_number}</span>
      <small>{confirmationLabel(load.confirmation)} · en attente d’approbation · 0 h</small>
    </button>
  );
}

function ResourceRow({
  resource,
  days,
  shifts,
  capacity,
  pendingLoads,
  diagnostics,
  onEditShift,
  onAssignAsset,
  onCreateQuickShift,
  onOpenDemand,
  dragEnabled,
  manualOrder,
  onDropShift,
  onDropSegment,
}: {
  resource: ResourceReadModel;
  days: Date[];
  shifts: ShiftReadModel[];
  capacity: PlanningResourceCapacityReadModel | null;
  pendingLoads: PendingDemandLoadReadModel[];
  diagnostics: Map<string, PlanningSegmentCapacityDiagnosticReadModel>;
  onEditShift: (shift: ShiftReadModel) => void;
  onAssignAsset?: (shift: ShiftReadModel) => void;
  onCreateQuickShift?: (resource: ResourceReadModel, day: string) => void;
  onOpenDemand?: (demandNumber: string) => void;
  dragEnabled: boolean;
  manualOrder?: ManualResourceOrderControls;
  onDropShift: (payload: ShiftDragPayload, resource: ResourceReadModel, day: string) => void;
  onDropSegment: (payload: SegmentDragPayload, resource: ResourceReadModel) => void;
}) {
  const total = shifts
    .filter((shift) => shift.allocation_type !== "Hors horaire requis")
    .reduce((sum, shift) => sum + Number(shift.hours || 0), 0);
  const dayCapacity = new Map((capacity?.days ?? []).map((row) => [row.day, row]));

  return (
    <div className="resource-row">
      <div
        className={`resource-cell resource-identity planning-drop-resource ${capacity?.overloaded ? "resource-overloaded" : ""}`}
        data-resource-id={resource.id}
        title={dragEnabled ? "Déposer ici un besoin pour définir cette ressource comme cible automatique" : undefined}
        onDragOver={(event) => {
          if (!dragEnabled || !hasSegmentDrag(event.dataTransfer)) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
          event.currentTarget.classList.add("is-drop-target");
        }}
        onDragLeave={(event) => event.currentTarget.classList.remove("is-drop-target")}
        onDrop={(event) => {
          event.currentTarget.classList.remove("is-drop-target");
          if (!dragEnabled) return;
          const payload = readSegmentDrag(event.dataTransfer);
          if (!payload) return;
          event.preventDefault();
          onDropSegment(payload, resource);
        }}
      >
        <strong>{resourceDisplayName(resource.name)}</strong>
        <span>{resource.competencies || resource.resource_class || "Ressource"}</span>
        {capacity ? (
          <>
            <small className={capacity.overloaded ? "capacity-danger" : "capacity-good"}>
              {hours(capacity.prudent_free)} h libres / {hours(capacity.capacity_hours)} h
            </small>
            {capacity.tentative_hours > 0 && <small className="capacity-tentative">{hours(capacity.tentative_hours)} h tentatives</small>}
            {capacity.outside_standard_hours > 0 && <small className="capacity-outside">{hours(capacity.outside_standard_hours)} h hors horaire</small>}
          </>
        ) : (
          <small>{hours(total)} h affichées</small>
        )}
        {manualOrder && (
          <div className="resource-reorder-controls" role="group" aria-label={`Ordre manuel de ${resourceDisplayName(resource.name)}`}>
            <button
              type="button"
              onClick={() => manualOrder.onMove("up")}
              disabled={manualOrder.busy || !manualOrder.canMoveUp}
              aria-label={`Monter ${resourceDisplayName(resource.name)}`}
              title="Monter cette ressource"
            >
              ↑
            </button>
            <button
              type="button"
              onClick={() => manualOrder.onMove("down")}
              disabled={manualOrder.busy || !manualOrder.canMoveDown}
              aria-label={`Descendre ${resourceDisplayName(resource.name)}`}
              title="Descendre cette ressource"
            >
              ↓
            </button>
          </div>
        )}
      </div>

      {days.map((day) => {
        const iso = toIsoDate(day);
        const dayShifts = shifts.filter((shift) => sameIsoDate(shift.work_date, day));
        const cellCapacity = dayCapacity.get(iso) ?? null;
        const ghosts = pendingLoads.filter((load) => (
          load.proposed_resource === resource.name
          && load.start_date <= iso
          && load.end_date >= iso
          && (cellCapacity?.available ?? true)
        ));
        const cellClass = [
          "resource-cell",
          "planning-day-cell",
          isToday(day) ? "today-column" : "",
          cellCapacity?.overloaded ? "capacity-overloaded-cell" : "",
          cellCapacity && !cellCapacity.available ? "capacity-unavailable-cell" : "",
        ].filter(Boolean).join(" ");

        return (
          <div
            className={`${cellClass} planning-drop-day`}
            key={iso}
            data-resource-id={resource.id}
            data-day={iso}
            title={dragEnabled ? `Déposer un quart sur ${resourceDisplayName(resource.name)}, ${iso}` : undefined}
            onDragOver={(event) => {
              if (!dragEnabled || !hasShiftDrag(event.dataTransfer)) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
              event.currentTarget.classList.add("is-drop-target");
            }}
            onDragLeave={(event) => event.currentTarget.classList.remove("is-drop-target")}
            onDrop={(event) => {
              event.currentTarget.classList.remove("is-drop-target");
              if (!dragEnabled) return;
              const payload = readShiftDrag(event.dataTransfer);
              if (!payload) return;
              event.preventDefault();
              onDropShift(payload, resource, iso);
            }}
          >
            {onCreateQuickShift && (
              <button
                type="button"
                className="cell-quick-shift-button"
                onClick={() => onCreateQuickShift(resource, iso)}
                aria-label={`Créer un Quick Shift pour ${resourceDisplayName(resource.name)} le ${iso}`}
                title={`Créer un Quick Shift pour ${resourceDisplayName(resource.name)} le ${iso}`}
              >
                +
              </button>
            )}
            {cellCapacity && (
              <div className={`day-capacity ${cellCapacity.overloaded ? "is-overloaded" : ""} ${!cellCapacity.available ? "is-unavailable" : ""}`}>
                {cellCapacity.available ? (
                  <>
                    <strong>{hours(cellCapacity.confirmed_hours + cellCapacity.tentative_hours)}/{hours(cellCapacity.capacity_hours)} h</strong>
                    {cellCapacity.tentative_hours > 0 && <span>{hours(cellCapacity.tentative_hours)} h tent.</span>}
                    {cellCapacity.outside_standard_hours > 0 && <span>{hours(cellCapacity.outside_standard_hours)} h hors horaire</span>}
                  </>
                ) : (
                  <span>{cellCapacity.reason || "Indisponible"}</span>
                )}
              </div>
            )}

            {dayShifts.map((shift) => (
              <ShiftCard
                shift={shift}
                diagnostic={diagnostics.get(shift.segment_id) ?? null}
                onEdit={onEditShift}
                onAssignAsset={onAssignAsset}
                dragEnabled={dragEnabled}
                key={shift.allocation_id}
              />
            ))}
            {ghosts.map((load) => (
              <PendingGhostCard
                load={load}
                onOpenDemand={onOpenDemand}
                key={`ghost-${load.demand_number}-${iso}`}
              />
            ))}
            {dayShifts.length === 0 && ghosts.length === 0 && !cellCapacity && <span className="empty-day">—</span>}
          </div>
        );
      })}
    </div>
  );
}

export default function PlanningPage({
  onOpenDemands,
  onOpenDemand,
}: {
  onOpenDemands?: () => void;
  onOpenDemand?: (demandNumber: string) => void;
}) {
  const { can, principal } = useAuth();
  const { scope, loading: scopeLoading, error: scopeError } = useViewScope();
  const canManagePlanning = can("manage_planning");
  const preferenceOwnerId = principal?.local_user_id ?? null;
  const [loadedPreferenceOwnerId, setLoadedPreferenceOwnerId] = useState<string | null>(preferenceOwnerId);
  const [weekStart, setWeekStart] = useState(() => loadPlanningWeekStart(preferenceOwnerId));
  const [resourceSortMode, setResourceSortMode] = useState<ResourceSortMode>(
    () => loadPlanningResourceSort(preferenceOwnerId),
  );
  const [collapsedClasses, setCollapsedClasses] = useState<Set<string>>(
    () => new Set(loadCollapsedPlanningClasses(preferenceOwnerId)),
  );
  const [snapshot, setSnapshot] = useState<PlanningSnapshotReadModel | null>(null);
  const [capacityGrid, setCapacityGrid] = useState<PlanningCapacityGridReadModel | null>(null);
  const [actions, setActions] = useState<PlanningActionReadModel[]>([]);
  const [catalogResources, setCatalogResources] = useState<ResourceReadModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [project, setProject] = useState("all");
  const [confirmation, setConfirmation] = useState<ConfirmationFilter>("all");
  const [classFilter, setClassFilter] = useState("all");
  const [resourceFilter, setResourceFilter] = useState("all");
  const [onlyWithCapacity, setOnlyWithCapacity] = useState(false);
  const [editingShift, setEditingShift] = useState<ShiftReadModel | null>(null);
  const [editingSegmentId, setEditingSegmentId] = useState<string | null>(null);
  const [quickShiftOpen, setQuickShiftOpen] = useState(false);
  const [quickShiftSeed, setQuickShiftSeed] = useState<{ resourceId: string; day: string } | null>(null);
  const [assetAssignment, setAssetAssignment] = useState<{
    shift: ShiftReadModel;
    mode: ShiftAssetAssignmentMode;
  } | null>(null);
  const [manualAllocationOpen, setManualAllocationOpen] = useState(false);
  const [detailDemandNumber, setDetailDemandNumber] = useState<string | null>(null);
  const [detailContextDirty, setDetailContextDirty] = useState(false);
  const [dropBusy, setDropBusy] = useState<string | null>(null);
  const [resourceReorderBusy, setResourceReorderBusy] = useState<string | null>(null);
  const [dropDialog, setDropDialog] = useState<DropDialogState | null>(null);
  const [dragFeedback, setDragFeedback] = useState<{
    tone: "success" | "error" | "info";
    message: string;
  } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (loadedPreferenceOwnerId === preferenceOwnerId) return;
    setWeekStart(loadPlanningWeekStart(preferenceOwnerId));
    setResourceSortMode(loadPlanningResourceSort(preferenceOwnerId));
    setCollapsedClasses(new Set(loadCollapsedPlanningClasses(preferenceOwnerId)));
    setLoadedPreferenceOwnerId(preferenceOwnerId);
  }, [loadedPreferenceOwnerId, preferenceOwnerId]);

  useEffect(() => {
    if (loadedPreferenceOwnerId !== preferenceOwnerId) return;
    savePlanningWeekStart(preferenceOwnerId, weekStart);
  }, [loadedPreferenceOwnerId, preferenceOwnerId, weekStart]);

  useEffect(() => {
    if (loadedPreferenceOwnerId !== preferenceOwnerId) return;
    savePlanningResourceSort(preferenceOwnerId, resourceSortMode);
  }, [loadedPreferenceOwnerId, preferenceOwnerId, resourceSortMode]);

  useEffect(() => {
    if (loadedPreferenceOwnerId !== preferenceOwnerId) return;
    saveCollapsedPlanningClasses(preferenceOwnerId, [...collapsedClasses]);
  }, [collapsedClasses, loadedPreferenceOwnerId, preferenceOwnerId]);

  function toggleResourceClass(className: string) {
    setCollapsedClasses((current) => {
      const next = new Set(current);
      if (next.has(className)) next.delete(className);
      else next.add(className);
      return next;
    });
  }

  const days = useMemo(() => weekDays(weekStart), [weekStart]);
  const start = toIsoDate(weekStart);
  const end = toIsoDate(addDays(weekStart, 6));
  const snapshotQueryKey = `${start}|${end}|${scope}`;
  const snapshotQueryKeyRef = useRef("");
  const today = toIsoDate(new Date());
  const quickShiftDefaultDay = today >= start && today <= end ? today : start;

  useEffect(() => {
    if (scopeLoading) return;
    if (scopeError) {
      setLoading(false);
      setError(scopeError);
      setSnapshot(null);
      setActions([]);
      setCapacityGrid(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    if (snapshotQueryKeyRef.current !== snapshotQueryKey) {
      setSnapshot(null);
    }
    setActions([]);
    setCapacityGrid(null);
    Promise.all([
      getPlanningSnapshot(start, end, controller.signal, scope),
      getPlanningActions(start, end, controller.signal, scope),
      getPlanningCapacityGrid(start, end, controller.signal, scope),
      getResources(true, controller.signal),
    ])
      .then(([planning, planningActions, capacity, resourceRows]) => {
        snapshotQueryKeyRef.current = snapshotQueryKey;
        setSnapshot(planning);
        setActions(planningActions);
        setCapacityGrid(capacity);
        setCatalogResources(resourceRows);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        if (reason instanceof ApiError) {
          setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
          return;
        }
        setError(reason instanceof Error ? reason.message : "Impossible de charger le planning.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [start, end, refreshKey, scope, scopeLoading, scopeError, snapshotQueryKey]);

  const projectOptions = useMemo(() => {
    if (!snapshot) return [];
    const values = new Map<string, string>();
    [...snapshot.shifts, ...snapshot.pending_loads, ...actions].forEach((item) => {
      if (!item.project_number) return;
      values.set(item.project_number, item.project_name
        ? `${item.project_number} — ${item.project_name}`
        : item.project_number);
    });
    return [...values.entries()].sort((left, right) => left[1].localeCompare(right[1], "fr-CA"));
  }, [snapshot, actions]);

  const classOptions = useMemo(() => {
    if (!snapshot) return [];
    return [...new Set(snapshot.resources.map((resource) => resource.resource_class || "Non classé"))]
      .sort((left, right) => left.localeCompare(right, "fr-CA"));
  }, [snapshot]);

  const resourceOptions = useMemo(() => {
    if (!snapshot) return [];
    return [...snapshot.resources].sort(compareManualResources);
  }, [snapshot]);

  const manualOrderAvailability = useMemo(() => {
    const groups = new Map<string, ResourceReadModel[]>();
    catalogResources.forEach((resource) => {
      const className = resource.resource_class || "Non classé";
      const rows = groups.get(className) ?? [];
      rows.push(resource);
      groups.set(className, rows);
    });

    const result = new Map<string, { canMoveUp: boolean; canMoveDown: boolean }>();
    groups.forEach((rows) => {
      const ordered = [...rows].sort(compareManualResources);
      ordered.forEach((resource, index) => {
        result.set(resource.id, {
          canMoveUp: index > 0,
          canMoveDown: index < ordered.length - 1,
        });
      });
    });
    return result;
  }, [catalogResources]);

  const capacityByResource = useMemo(
    () => new Map((capacityGrid?.resources ?? []).map((row) => [row.resource_id, row])),
    [capacityGrid],
  );
  const diagnosticsBySegment = useMemo(
    () => new Map((capacityGrid?.segment_diagnostics ?? []).map((row) => [row.segment_id, row])),
    [capacityGrid],
  );

  const query = normalize(search);

  const shiftsPassingGlobalFilters = useMemo(() => {
    if (!snapshot) return [];
    return snapshot.shifts.filter((shift) => {
      if (project !== "all" && shift.project_number !== project) return false;
      if (confirmation !== "all" && confirmationKind(shift.confirmation) !== confirmation) return false;
      return true;
    });
  }, [snapshot, project, confirmation]);

  const visibleActions = useMemo(() => actions.filter((action) => {
    if (project !== "all" && action.project_number !== project) return false;
    if (query && !actionText(action).includes(query)) return false;
    if (confirmation !== "all" && confirmationKind(action.confirmation) !== confirmation) return false;
    return true;
  }), [actions, project, confirmation, query]);

  const visiblePendingLoads = useMemo(() => {
    if (!snapshot) return [];
    return snapshot.pending_loads.filter((load) => {
      if (project !== "all" && load.project_number !== project) return false;
      if (query && !pendingText(load).includes(query)) return false;
      if (confirmation !== "all" && confirmationKind(load.confirmation) !== confirmation) return false;
      return true;
    });
  }, [snapshot, project, confirmation, query]);

  const visibleResourceGroups = useMemo(() => {
    if (!snapshot) return [];
    const groups = new Map<string, ResourceGroupEntry[]>();

    snapshot.resources.forEach((resource) => {
      const capacity = capacityByResource.get(resource.id) ?? null;
      if (classFilter !== "all" && (resource.resource_class || "Non classé") !== classFilter) return;
      if (resourceFilter !== "all" && resource.id !== resourceFilter) return;
      if (onlyWithCapacity && (!capacity || capacity.prudent_free <= 0.01)) return;

      const resourceMatches = !query || normalize(`${resource.name} ${resource.resource_class ?? ""} ${resource.competencies ?? ""}`).includes(query);
      const shifts = shiftsPassingGlobalFilters.filter((shift) => {
        if (shift.resource_id !== resource.id) return false;
        if (!query || resourceMatches) return true;
        return shiftText(shift).includes(query);
      });
      const pendingLoads = visiblePendingLoads.filter((load) => load.proposed_resource === resource.name);
      const restrictiveProjectConfirmation = project !== "all" || confirmation !== "all";
      if (query && !resourceMatches && shifts.length === 0 && pendingLoads.length === 0) return;
      if (restrictiveProjectConfirmation && shifts.length === 0 && pendingLoads.length === 0) return;

      const className = resource.resource_class || "Non classé";
      const entries = groups.get(className) ?? [];
      entries.push({ resource, shifts, capacity, pendingLoads });
      groups.set(className, entries);
    });

    return [...groups.entries()]
      .map(([className, rows]) => [
        className,
        [...rows].sort((left, right) => compareResourceGroupEntries(left, right, resourceSortMode)),
      ] as [string, ResourceGroupEntry[]])
      .sort((left, right) => left[0].localeCompare(right[0], "fr-CA"));
  }, [
    snapshot,
    capacityByResource,
    classFilter,
    resourceFilter,
    onlyWithCapacity,
    shiftsPassingGlobalFilters,
    visiblePendingLoads,
    project,
    confirmation,
    query,
    resourceSortMode,
  ]);

  const visibleShiftHours = shiftsPassingGlobalFilters
    .filter((shift) => shift.allocation_type !== "Hors horaire requis")
    .filter((shift) => !query || shiftText(shift).includes(query) || snapshot?.resources.some(
      (resource) => resource.id === shift.resource_id && normalize(`${resource.name} ${resource.resource_class ?? ""}`).includes(query),
    ))
    .reduce((sum, shift) => sum + Number(shift.hours || 0), 0);

  const unplacedDiagnostics = useMemo(() => {
    if (!snapshot || !capacityGrid) return [];
    const segmentById = new Map(snapshot.segments.map((segment) => [segment.segment_id, segment]));
    return capacityGrid.segment_diagnostics
      .filter((diagnostic) => diagnostic.unplaced_hours > 0.01)
      .filter((diagnostic) => {
        const segment = segmentById.get(diagnostic.segment_id);
        if (!segment) return false;
        if (project !== "all" && segment.project_number !== project) return false;
        if (resourceFilter !== "all" && diagnostic.resource_id !== resourceFilter) return false;
        const resource = diagnostic.resource_id ? snapshot.resources.find((row) => row.id === diagnostic.resource_id) : null;
        if (classFilter !== "all" && (resource?.resource_class || "Non classé") !== classFilter) return false;
        if (query && !normalize([
          diagnostic.segment_id,
          segment.project_number,
          segment.project_name,
          segment.demand_number,
          segment.description,
          diagnostic.automatic_target_resource_name,
        ].filter(Boolean).join(" ")).includes(query)) return false;
        return true;
      });
  }, [snapshot, capacityGrid, project, resourceFilter, classFilter, query]);

  const visibleResourceCount = visibleResourceGroups.reduce((sum, [, rows]) => sum + rows.length, 0);

  async function moveResourceInManualOrder(
    resource: ResourceReadModel,
    direction: "up" | "down",
  ) {
    if (!canManagePlanning || resourceSortMode !== "manual" || resourceReorderBusy || dropBusy) return;
    setResourceReorderBusy(resource.id);
    setDragFeedback(null);
    try {
      const result = await reorderPlanningResource(resource.id, direction, createClientId());
      if (result.action === "unchanged") {
        setDragFeedback({
          tone: "info",
          message: direction === "up"
            ? `${resourceDisplayName(resource.name)} est déjà en première position de sa classe.`
            : `${resourceDisplayName(resource.name)} est déjà en dernière position de sa classe.`,
        });
      } else {
        setDragFeedback({
          tone: "success",
          message: `Ordre manuel mis à jour pour ${resourceDisplayName(resource.name)}.`,
        });
        setRefreshKey((value) => value + 1);
      }
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setDragFeedback({
          tone: "error",
          message: `${reason.message}${reason.code ? ` (${reason.code})` : ""}`,
        });
      } else {
        setDragFeedback({
          tone: "error",
          message: reason instanceof Error ? reason.message : "Impossible de réordonner cette ressource.",
        });
      }
    } finally {
      setResourceReorderBusy(null);
    }
  }

  async function moveShiftFromDrop(
    payload: ShiftDragPayload,
    targetResource: ResourceReadModel,
    targetDay: string,
  ) {
    if (!canManagePlanning || dropBusy) return;
    if (payload.resource_id === targetResource.id && payload.work_date === targetDay) {
      setDragFeedback({ tone: "info", message: "Le quart est déjà dans cette cellule; aucune modification appliquée." });
      return;
    }

    setDropBusy(`evaluate:${payload.allocation_id}`);
    setDragFeedback(null);
    try {
      const evaluation = await evaluateAllocationDrop(payload.allocation_id, {
        resource_id: targetResource.id,
        day: targetDay,
        outside_standard_hours: false,
        include_planning_window_override_options: true,
      });
      const automaticExtension = evaluation.actions.find(
        (action) => action.code === "EXTEND_AND_MOVE" && action.enabled && action.auto_execute,
      );
      if (automaticExtension && evaluation.warnings.length === 0) {
        await extendAndMoveAllocationAtomic(
          payload.allocation_id,
          {
            resource_id: targetResource.id,
            day: targetDay,
            expected_planning_version: evaluation.planning_version,
            outside_standard_hours: false,
            overallocation_policy: null,
            expected_approval_revision_id: null,
            expected_operational_version: null,
            confirm_window_extension: false,
          },
          createClientId(),
        );
        setDragFeedback({
          tone: "success",
          message: `Fenêtre du besoin étendue automatiquement et quart déplacé vers ${targetResource.name} le ${targetDay}.`,
        });
        setRefreshKey((value) => value + 1);
        return;
      }
      setDropDialog({
        payload,
        targetResource,
        targetDay,
        evaluation,
        error: null,
        overallocationPrompt: null,
        actionKeys: dropActionKeys(evaluation),
      });
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setDragFeedback({
          tone: "error",
          message: `${reason.message}${reason.code ? ` (${reason.code})` : ""}`,
        });
      } else {
        setDragFeedback({
          tone: "error",
          message: reason instanceof Error ? reason.message : "Impossible d’évaluer ce déplacement.",
        });
      }
    } finally {
      setDropBusy(null);
    }
  }

  async function reevaluateDrop(outsideStandardHours: boolean) {
    if (!dropDialog || dropBusy) return;
    setDropBusy(`evaluate:${dropDialog.payload.allocation_id}`);
    try {
      const evaluation = await evaluateAllocationDrop(dropDialog.payload.allocation_id, {
        resource_id: dropDialog.targetResource.id,
        day: dropDialog.targetDay,
        outside_standard_hours: outsideStandardHours,
        include_planning_window_override_options: true,
      });
      setDropDialog((current) => current ? {
        ...current,
        evaluation,
        error: null,
        overallocationPrompt: null,
      } : null);
    } catch (reason: unknown) {
      const message = reason instanceof ApiError
        ? `${reason.message}${reason.code ? ` (${reason.code})` : ""}`
        : reason instanceof Error ? reason.message : "Impossible de réévaluer ce déplacement.";
      setDropDialog((current) => current ? { ...current, error: message } : null);
    } finally {
      setDropBusy(null);
    }
  }

  async function executeDropAction(request: PlanningDropExecutionRequest) {
    if (!dropDialog || dropBusy) return;
    const current = dropDialog;
    const actionCode = request.code;
    const idempotencyKey = current.actionKeys[actionCode];
    setDropBusy(`execute:${current.payload.allocation_id}:${actionCode}`);
    setDropDialog((value) => value ? { ...value, error: null } : null);

    try {
      const common = {
        resource_id: current.targetResource.id,
        day: current.targetDay,
        expected_planning_version: current.evaluation.planning_version,
        outside_standard_hours: request.outsideStandardHours,
        overallocation_policy: request.overallocationPolicy,
        expected_approval_revision_id: current.evaluation.approval_revision_id,
        expected_operational_version: (
          request.overallocationPolicy === "INCREASE_PLANNED"
            ? current.evaluation.operational_version
            : null
        ),
      };

      if (actionCode === "MOVE") {
        const movePayload = {
          resource_id: current.targetResource.id,
          day: current.targetDay,
          outside_standard_hours: request.outsideStandardHours,
          expected_planning_version: current.evaluation.planning_version,
        };
        await moveAllocation(current.payload.allocation_id, movePayload);
        setDragFeedback({
          tone: "success",
          message: `Quart déplacé vers ${resourceDisplayName(current.targetResource.name)} le ${current.targetDay} et verrouillé comme décision manuelle.`,
        });
      } else if (actionCode === "SPLIT") {
        if (!idempotencyKey || request.transferHours === null) {
          throw new Error("Le partage ne possède pas tous ses paramètres d’exécution.");
        }
        await splitAllocationAtomic(
          current.payload.allocation_id,
          { ...common, transfer_hours: request.transferHours },
          idempotencyKey,
        );
        setDragFeedback({
          tone: "success",
          message: `Quart partagé vers ${resourceDisplayName(current.targetResource.name)} le ${current.targetDay}.`,
        });
      } else if (actionCode === "DUPLICATE") {
        if (!idempotencyKey) throw new Error("La duplication ne possède pas de clé d’idempotence.");
        await duplicateAllocationAtomic(current.payload.allocation_id, common, idempotencyKey);
        setDragFeedback({
          tone: "success",
          message: `Quart dupliqué vers ${resourceDisplayName(current.targetResource.name)} le ${current.targetDay}.`,
        });
      } else if (actionCode === "EXTEND_AND_MOVE") {
        if (!idempotencyKey) throw new Error("L’extension ne possède pas de clé d’idempotence.");
        await extendAndMoveAllocationAtomic(
          current.payload.allocation_id,
          { ...common, outside_standard_hours: request.outsideStandardHours, confirm_window_extension: true },
          idempotencyKey,
        );
        setDragFeedback({
          tone: "success",
          message: `Période étendue et quart déplacé vers ${resourceDisplayName(current.targetResource.name)} le ${current.targetDay}.`,
        });
      } else if (actionCode === "OVERRIDE_WINDOW_AND_MOVE") {
        if (!idempotencyKey || !current.evaluation.approval_revision_id || !request.reason.trim()) {
          throw new Error("La dérogation requiert un motif et une référence approuvée active.");
        }
        await overrideAndMovePlanningWindow(
          current.payload.allocation_id,
          {
            resource_id: current.targetResource.id,
            day: current.targetDay,
            reason: request.reason.trim(),
            expected_planning_version: current.evaluation.planning_version,
            expected_approval_revision_id: current.evaluation.approval_revision_id,
            expected_operational_version: current.evaluation.operational_version,
            outside_standard_hours: request.outsideStandardHours,
            overallocation_policy: request.overallocationPolicy,
          },
          idempotencyKey,
        );
        setDragFeedback({
          tone: "success",
          message: `Dérogation opérationnelle enregistrée et quart déplacé vers ${resourceDisplayName(current.targetResource.name)} le ${current.targetDay}. La demande approuvée reste inchangée.`,
        });
      } else if (actionCode === "PROPOSE_WINDOW_EXTENSION") {
        if (
          !idempotencyKey
          || current.evaluation.request_version === null
          || !current.evaluation.approval_revision_id
        ) {
          throw new Error("La proposition d’extension ne possède plus une référence candidate/approuvée complète.");
        }
        const result = await proposeAllocationWindowExtension(
          current.payload.allocation_id,
          {
            resource_id: current.targetResource.id,
            day: current.targetDay,
            outside_standard_hours: request.outsideStandardHours,
            expected_request_version: current.evaluation.request_version,
            expected_approval_revision_id: current.evaluation.approval_revision_id,
          },
          idempotencyKey,
        );
        setDragFeedback({
          tone: "success",
          message: result.reapproval_required
            ? "Extension soumise pour approbation. Aucun quart n’a été déplacé."
            : "Extension approuvée et autorisation mise à jour. Aucun quart n’a été déplacé; effectue un nouveau déplacement contre le planning actualisé.",
        });
      } else {
        throw new Error(`Action de drop non supportée: ${actionCode}`);
      }

      setDropDialog(null);
      setRefreshKey((value) => value + 1);
    } catch (reason: unknown) {
      if (
        reason instanceof OverallocationApiError
        && reason.code === "allocation_overallocation_choice_required"
      ) {
        setDropDialog((value) => value ? {
          ...value,
          error: reason.message,
          overallocationPrompt: overallocationContext(reason),
        } : null);
      } else if (reason instanceof ApiError && reason.code && STALE_DROP_CODES.has(reason.code)) {
        setDropDialog(null);
        setDragFeedback({
          tone: "info",
          message: "Le planning ou la demande a changé depuis l’évaluation. Le snapshot a été actualisé; recommence le drag-and-drop.",
        });
        setRefreshKey((value) => value + 1);
      } else if (reason instanceof ApiError) {
        setDropDialog((value) => value ? {
          ...value,
          error: `${reason.message}${reason.code ? ` (${reason.code})` : ""}`,
        } : null);
      } else if (reason instanceof TypeError) {
        setDropDialog((value) => value ? {
          ...value,
          error: "La réponse de l’action est incertaine. Réessaie la même action : la même clé d’idempotence sera réutilisée.",
        } : null);
      } else {
        setDropDialog((value) => value ? {
          ...value,
          error: reason instanceof Error ? reason.message : "Impossible d’exécuter l’action choisie.",
        } : null);
      }
    } finally {
      setDropBusy(null);
    }
  }

  async function assignSegmentFromDrop(
    payload: SegmentDragPayload,
    targetResource: ResourceReadModel,
  ) {
    if (!canManagePlanning || dropBusy) return;
    setDropBusy(`segment:${payload.segment_id}`);
    setDragFeedback(null);
    try {
      await assignSegment(payload.segment_id, targetResource.id);
      setDragFeedback({
        tone: "success",
        message: `Cible automatique de ${payload.segment_id} définie à ${resourceDisplayName(targetResource.name)}; le reliquat a été recalculé.`,
      });
      setRefreshKey((value) => value + 1);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setDragFeedback({
          tone: "error",
          message: `${reason.message}${reason.code ? ` (${reason.code})` : ""}`,
        });
      } else {
        setDragFeedback({
          tone: "error",
          message: reason instanceof Error ? reason.message : "Impossible d’attribuer le besoin.",
        });
      }
    } finally {
      setDropBusy(null);
    }
  }

  const dropSourceShift = dropDialog && snapshot
    ? snapshot.shifts.find((shift) => shift.allocation_id === dropDialog.payload.allocation_id) ?? null
    : null;

  return (
    <section className="planning-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Planification opérationnelle</span>
          <h1>Semaine du {formatWeekRange(weekStart)}</h1>
          <p>Planning Web V2 alimenté directement par FastAPI. Capacité, indisponibilités et heures non placées sont calculées côté backend.</p>
        </div>
        <div className="page-actions">
          <ViewScopeSelector />
          <button className="manual-allocation-button" type="button" onClick={() => setManualAllocationOpen(true)}>
            + Quart manuel
          </button>
          <button
            className="quick-shift-button"
            type="button"
            onClick={() => {
              setQuickShiftSeed(null);
              setQuickShiftOpen(true);
            }}
          >
            + Quick Shift
          </button>
          <div className="week-navigation" role="group" aria-label="Navigation par semaine">
            <button type="button" onClick={() => setWeekStart((value) => addDays(value, -7))}>← Précédente</button>
            <button type="button" onClick={() => setWeekStart(startOfWeek(new Date()))}>Aujourd’hui</button>
            <button type="button" onClick={() => setWeekStart((value) => addDays(value, 7))}>Suivante →</button>
          </div>
        </div>
      </div>

      <PlanningActionPanel
        actions={visibleActions}
        loading={loading}
        onOpenDemands={onOpenDemands}
        onOpenSegment={setEditingSegmentId}
        onAssigned={() => setRefreshKey((value) => value + 1)}
      />

      {dragFeedback && (
        <div
          className={`planning-drag-feedback is-${dragFeedback.tone}`}
          role={dragFeedback.tone === "error" ? "alert" : "status"}
        >
          <span>{dragFeedback.message}</span>
          <button type="button" onClick={() => setDragFeedback(null)} aria-label="Fermer le message">×</button>
        </div>
      )}

      <div className="metric-grid">
        <article>
          <span>Charge ferme</span>
          <strong>{snapshot ? `${hours(snapshot.firm_hours)} h` : "—"}</strong>
          <small>Quarts confirmés dans la fenêtre</small>
        </article>
        <article>
          <span>Charge potentielle</span>
          <strong>{snapshot ? `${hours(snapshot.potential_hours)} h` : "—"}</strong>
          <small>Tentatif + demandes additives</small>
        </article>
        <article>
          <span>Heures non placées</span>
          <strong>{capacityGrid ? `${hours(unplacedDiagnostics.reduce((sum, row) => sum + row.unplaced_hours, 0))} h` : "—"}</strong>
          <small>{capacityGrid ? `${unplacedDiagnostics.length} segment(s) à régulariser` : "Diagnostic backend"}</small>
        </article>
        <article>
          <span>Quarts affichés</span>
          <strong>{snapshot ? `${hours(visibleShiftHours)} h` : "—"}</strong>
          <small>{snapshot ? `${shiftsPassingGlobalFilters.length} quart(s) avant recherche` : "Chargement…"}</small>
        </article>
      </div>

      <div className="filter-bar planning-filter-bar">
        <label className="search-field">
          <span>Recherche</span>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Ressource, projet, demande…"
          />
        </label>
        <label>
          <span>Classe</span>
          <select value={classFilter} onChange={(event) => setClassFilter(event.target.value)}>
            <option value="all">Toutes les classes</option>
            {classOptions.map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </label>
        <label>
          <span>Ressource</span>
          <select value={resourceFilter} onChange={(event) => setResourceFilter(event.target.value)}>
            <option value="all">Toutes les ressources</option>
            {resourceOptions.map((resource) => <option value={resource.id} key={resource.id}>{resourceDisplayName(resource.name)}</option>)}
          </select>
        </label>
        <label>
          <span>Projet</span>
          <select value={project} onChange={(event) => setProject(event.target.value)}>
            <option value="all">Tous les projets</option>
            {projectOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>
          <span>Confirmation</span>
          <select value={confirmation} onChange={(event) => setConfirmation(event.target.value as ConfirmationFilter)}>
            <option value="all">Confirmée + Tentative</option>
            <option value="confirmed">Confirmée</option>
            <option value="tentative">Tentative</option>
          </select>
        </label>
        <label className="capacity-filter">
          <input type="checkbox" checked={onlyWithCapacity} onChange={(event) => setOnlyWithCapacity(event.target.checked)} />
          <span>Seulement avec capacité</span>
        </label>
      </div>

      {error && (
        <div className="error-panel">
          <strong>Le planning n’a pas pu être chargé.</strong>
          <span>{error}</span>
          <small>Vérifie que FastAPI fonctionne sur le port 8000 et que la base locale est migrée.</small>
        </div>
      )}

      {!loading && unplacedDiagnostics.length > 0 && snapshot && (
        <section className="unplaced-panel" aria-label="Heures non placées">
          <div className="unplaced-heading">
            <div>
              <span className="eyebrow">Capacité insuffisante</span>
              <strong>Hors horaire requis / heures non placées</strong>
            </div>
            <span className="count-pill">{unplacedDiagnostics.length}</span>
          </div>
          <div className="unplaced-list">
            {unplacedDiagnostics.map((diagnostic) => {
              const segment = snapshot.segments.find((row) => row.segment_id === diagnostic.segment_id);
              if (!segment) return null;
              return (
                <article key={diagnostic.segment_id}>
                  <div>
                    <strong>{segment.project_number || "Projet"} — {segment.description || diagnostic.segment_id}</strong>
                    <span>
                      {diagnostic.automatic_target_resource_name
                        ? `Cible automatique : ${resourceDisplayName(diagnostic.automatic_target_resource_name)}`
                        : "Aucune cible automatique"} · {hours(diagnostic.allocated_hours)}/{hours(diagnostic.planned_hours)} h placées
                    </span>
                  </div>
                  <strong className="unplaced-hours">{hours(diagnostic.unplaced_hours)} h non placées</strong>
                  <button type="button" onClick={() => setEditingSegmentId(diagnostic.segment_id)}>Modifier le segment</button>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <div className="planning-layout">
        <div className="planning-board-panel">
          <div className="planning-board-toolbar">
            <div>
              <strong>Ressources et quarts</strong>
              <span>{loading ? "Actualisation…" : `${visibleResourceCount} ressource(s)`}</span>
              {scope === "mine" && (
                <small className="planning-drag-help">
                  La capacité tient compte de tous les engagements des ressources affichées, y compris ceux hors de votre périmètre.
                </small>
              )}
              {canManagePlanning && (
                <small className="planning-drag-help">
                  Glisser un quart vers une cellule pour le déplacer; glisser un besoin « À attribuer » sur le nom d’une ressource pour l’affecter.
                  Les boutons et éditeurs restent disponibles comme alternative clavier.
                </small>
              )}
            </div>
            <div className="planning-board-options">
              <label className="planning-resource-sort">
                <span>Ordre des ressources</span>
                <select
                  value={resourceSortMode}
                  onChange={(event) => setResourceSortMode(event.target.value as ResourceSortMode)}
                >
                  <option value="manual">Manuel</option>
                  <option value="availability">Disponibilité</option>
                  <option value="alphabetical">Alphabétique</option>
                </select>
              </label>
              <div className="legend">
                <span><i className="legend-dot confirmed" />Confirmée</span>
                <span><i className="legend-dot tentative" />Tentative</span>
                <span><i className="legend-dot outside" />Hors horaire</span>
                <span><i className="legend-dot ghost" />Attente d’approbation</span>
                <span><i className="legend-dot unavailable" />Indisponible</span>
              </div>
            </div>
          </div>

          <div className={`planning-board-scroll ${loading ? "is-loading" : ""}`}>
            <div className="planning-grid">
              <div className="planning-header resource-header">Ressource</div>
              {days.map((day) => (
                <div className={`planning-header day-header ${isToday(day) ? "today-column" : ""}`} key={toIsoDate(day)}>
                  <span>{formatDay(day)}</span>
                  {isToday(day) && <small>Aujourd’hui</small>}
                </div>
              ))}

              {!loading && visibleResourceGroups.length === 0 && (
                <div className="planning-empty">Aucune ressource ou aucun quart ne correspond aux filtres.</div>
              )}

              {visibleResourceGroups.map(([className, rows]) => {
                const collapsed = collapsedClasses.has(className);
                return (
                  <div className="resource-group" key={className}>
                    <button
                      type="button"
                      className="resource-group-heading"
                      aria-expanded={!collapsed}
                      onClick={() => toggleResourceClass(className)}
                    >
                      <span className="resource-group-title">
                        <span aria-hidden="true">{collapsed ? "▶" : "▼"}</span>
                        <strong>{className}</strong>
                      </span>
                      <span>{rows.length} ressource(s)</span>
                    </button>
                    {!collapsed && rows.map(({ resource, shifts, capacity, pendingLoads }) => (
                      <ResourceRow
                        resource={resource}
                        days={days}
                        shifts={shifts}
                        capacity={capacity}
                        pendingLoads={pendingLoads}
                        diagnostics={diagnosticsBySegment}
                        onEditShift={setEditingShift}
                        onAssignAsset={(shift) => setAssetAssignment({ shift, mode: "assign" })}
                        onCreateQuickShift={canManagePlanning ? (targetResource, day) => {
                          setQuickShiftSeed({ resourceId: targetResource.id, day });
                          setQuickShiftOpen(true);
                        } : undefined}
                        onOpenDemand={setDetailDemandNumber}
                        dragEnabled={canManagePlanning && !dropBusy}
                        manualOrder={resourceSortMode === "manual" && canManagePlanning ? {
                          canMoveUp: manualOrderAvailability.get(resource.id)?.canMoveUp ?? false,
                          canMoveDown: manualOrderAvailability.get(resource.id)?.canMoveDown ?? false,
                          busy: Boolean(resourceReorderBusy || dropBusy),
                          onMove: (direction) => void moveResourceInManualOrder(resource, direction),
                        } : undefined}
                        onDropShift={(payload, target, day) => void moveShiftFromDrop(payload, target, day)}
                        onDropSegment={(payload, target) => void assignSegmentFromDrop(payload, target)}
                        key={resource.id}
                      />
                    ))}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <aside className="pending-panel">
          <div className="pending-panel-heading">
            <div>
              <span className="eyebrow">À anticiper</span>
              <strong>Demandes en attente</strong>
            </div>
            <span className="count-pill">{visiblePendingLoads.length}</span>
          </div>
          {loading && !snapshot ? (
            <div className="pending-empty">Chargement…</div>
          ) : visiblePendingLoads.length > 0 ? (
            <div className="pending-list">
              {visiblePendingLoads.map((load) => (
                <PendingLoadCard
                  load={load}
                  onOpenDemand={setDetailDemandNumber}
                  key={`${load.demand_number}-${load.start_date}-${load.mode}`}
                />
              ))}
            </div>
          ) : (
            <div className="pending-empty">Aucune demande potentielle pour cette semaine.</div>
          )}
        </aside>
      </div>

      {snapshot && (
        <AssetPlanningPanel
          snapshot={snapshot}
          canManage={canManagePlanning && !loading}
          onRefresh={() => setRefreshKey((value) => value + 1)}
          onOpenDemand={setDetailDemandNumber}
        />
      )}

      {assetAssignment && snapshot && (
        <ShiftAssetAssignmentDialog
          shift={assetAssignment.shift}
          mode={assetAssignment.mode}
          planningVersion={snapshot.planning_version}
          onClose={() => setAssetAssignment(null)}
          onRefresh={() => setRefreshKey((value) => value + 1)}
        />
      )}

      {dropDialog && dropSourceShift && (
        <PlanningDropDialog
          evaluation={dropDialog.evaluation}
          shift={dropSourceShift}
          targetResource={dropDialog.targetResource}
          busy={Boolean(dropBusy)}
          error={dropDialog.error}
          overallocationPrompt={dropDialog.overallocationPrompt}
          onClose={() => {
            if (dropBusy) return;
            setDropDialog(null);
          }}
          onReevaluate={reevaluateDrop}
          onExecute={executeDropAction}
        />
      )}

      {editingShift && snapshot && (
        <ShiftEditor
          shift={editingShift}
          resources={catalogResources}
          planningVersion={snapshot.planning_version}
          onClose={() => setEditingShift(null)}
          onSaved={() => {
            setEditingShift(null);
            setRefreshKey((value) => value + 1);
          }}
          onStale={() => {
            setEditingShift(null);
            setDragFeedback({
              tone: "info",
              message: "Le planning a changé depuis l'ouverture du quart. Le snapshot a été rafraîchi; rouvre le quart pour réessayer.",
            });
            setRefreshKey((value) => value + 1);
          }}
          onOpenDemand={(demandNumber) => {
            setEditingShift(null);
            setDetailDemandNumber(demandNumber);
          }}
          onAssetAction={(mode) => {
            setAssetAssignment({ shift: editingShift, mode });
            setEditingShift(null);
          }}
        />
      )}

      {editingSegmentId && snapshot && (
        <SegmentEditor
          open
          segmentId={editingSegmentId}
          demand={null}
          resources={catalogResources}
          onClose={() => setEditingSegmentId(null)}
          onSaved={() => {
            setEditingSegmentId(null);
            setRefreshKey((value) => value + 1);
          }}
          onOpenDemand={(demandNumber) => {
            setEditingSegmentId(null);
            setDetailDemandNumber(demandNumber);
          }}
          planningVersion={snapshot.planning_version}
          onStale={() => {
            setEditingSegmentId(null);
            setDragFeedback({
              tone: "info",
              message: "Le planning ou l’approbation a changé. Le snapshot a été rafraîchi; rouvre le segment pour réessayer.",
            });
            setRefreshKey((value) => value + 1);
          }}
        />
      )}

      {snapshot && (
        <ManualAllocationEditor
          open={manualAllocationOpen}
          segments={snapshot.segments}
          resources={catalogResources}
          weekStart={start}
          weekEnd={end}
          onClose={() => setManualAllocationOpen(false)}
          onSaved={() => {
            setManualAllocationOpen(false);
            setRefreshKey((value) => value + 1);
          }}
        />
      )}

      {detailDemandNumber && (
        <div
          className="demand-detail-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.currentTarget !== event.target) return;
            if (detailContextDirty && !window.confirm("Des périodes non enregistrées seront perdues. Fermer le détail?")) return;
            setDetailDemandNumber(null);
            setDetailContextDirty(false);
          }}
        >
          <section className="demand-detail-modal" role="dialog" aria-modal="true" aria-label={`Détail de la demande ${detailDemandNumber}`}>
            <header className="demand-detail-modal-header">
              <div>
                <strong>Détail de la demande</strong>
                <span>{detailDemandNumber}</span>
              </div>
              <div className="demand-detail-modal-header-actions">
                {onOpenDemand && (
                  <button type="button" onClick={() => {
                    const demandNumber = detailDemandNumber;
                    if (!demandNumber) return;
                    if (detailContextDirty && !window.confirm("Des périodes non enregistrées seront perdues. Fermer le détail?")) return;
                    setDetailDemandNumber(null);
                    setDetailContextDirty(false);
                    onOpenDemand(demandNumber);
                  }}>
                    Ouvrir dans Demandes
                  </button>
                )}
                <button type="button" onClick={() => {
                  if (detailContextDirty && !window.confirm("Des périodes non enregistrées seront perdues. Fermer le détail?")) return;
                  setDetailDemandNumber(null);
                  setDetailContextDirty(false);
                }}>
                  Fermer
                </button>
              </div>
            </header>
            <div className="demand-detail-modal-body">
              <DemandWorkflowPage
                demandNumber={detailDemandNumber}
                embedded
                actionsOnly
                hasUnsavedChanges={detailContextDirty}
                onChanged={() => setRefreshKey((value) => value + 1)}
              />
              <DemandDetail
                demandNumber={detailDemandNumber}
                compact
                onDirtyChange={setDetailContextDirty}
                onChanged={() => setRefreshKey((value) => value + 1)}
              />
            </div>
          </section>
        </div>
      )}

      <QuickShiftEditor
        open={quickShiftOpen}
        weekStart={start}
        weekEnd={end}
        defaultDay={quickShiftSeed?.day ?? quickShiftDefaultDay}
        initialProjectNumber={project === "all" ? null : project}
        initialResourceId={quickShiftSeed?.resourceId ?? null}
        onClose={() => {
          setQuickShiftOpen(false);
          setQuickShiftSeed(null);
        }}
        onSaved={({ day, resourceName }) => {
          setQuickShiftOpen(false);
          setQuickShiftSeed(null);
          setDragFeedback({
            tone: "success",
            message: `Quick Shift créé pour ${resourceDisplayName(resourceName)} le ${day}.`,
          });
          setRefreshKey((value) => value + 1);
        }}
      />
    </section>
  );
}
