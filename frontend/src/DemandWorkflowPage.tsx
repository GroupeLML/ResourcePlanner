import { resourceDisplayName } from "./resourceLabels";
import { useEffect, useMemo, useRef, useState } from "react";

import { createClientId } from "./clientId";
import {
  ApiError,
  type DemandDetailReadModel,
  type DemandReadModel,
  getDemandDetail,
  getDemands,
  getPlanningSnapshot,
} from "./api";
import {
  acceptDemandCancellation,
  approveDemand,
  approveDemandLines,
  cancelDemand,
  rejectDemandCancellation,
  requestDemandCancellation,
  requestDemandCorrection,
  submitDemand,
  type DemandApprovalVoteResult,
  type DemandWorkflowResult,
  type DemandWorkflowState,
  type WorkflowAction,
} from "./demandWorkflowApi";
import {
  getDemandApprovalState,
  getDemandPlanDelta,
  type DemandApprovalState,
  type DemandPlanDelta,
  type DemandPlanDeltaItem,
} from "./planDeltaApi";
import "./planDelta.css";
import "./approval-progress.css";

type WorkflowButtonAction = Extract<
  WorkflowAction,
  | "submit"
  | "approve"
  | "correction"
  | "cancel"
  | "request-cancellation"
  | "accept-cancellation"
  | "reject-cancellation"
>;
type StandardWorkflowButtonAction = Extract<
  WorkflowButtonAction,
  "submit" | "approve" | "correction" | "cancel"
>;

type CancellationAcceptRetry = {
  fingerprint: string;
  key: string;
};

type ApprovalRoutingDiagnosticView = {
  line: string;
  requestLineId: string | null;
  lineKind: string;
  task: string;
  requiredResourceClass: string | null;
  resourceClass: string;
  proposedResource: string | null;
  routingSources: string[];
  assetType: string | null;
  proposedAsset: string | null;
  scope: string;
  reasons: { code: string; label: string }[];
};

const ROUTING_DIAGNOSTIC_LABELS: Record<string, string> = {
  line_inactive: "La ligne est inactive.",
  task_reference_missing: "La référence vers la tâche ERP n’est pas persistée sur cette ligne.",
  routing_source_missing: "Aucune tâche ERP, classe explicite ou ressource proposée ne permet de router cette ligne.",
  task_not_found: "La tâche ERP référencée est introuvable.",
  task_inactive: "La tâche ERP référencée est inactive.",
  resource_class_missing: "La source de routage prioritaire n’a pas de classe de ressource canonique.",
  resource_class_not_found: "La classe de ressource effective est introuvable.",
  resource_class_inactive: "La classe de ressource effective est inactive.",
  proposed_resource_unknown: "La ressource proposée est introuvable.",
  proposed_resource_inactive: "La ressource proposée est inactive.",
  approval_scope_unmapped: "Aucun périmètre d’approbation n’est mappé à cette tâche ou classe effective.",
  approval_scope_ambiguous: "Plusieurs périmètres d’approbation sont candidats; aucun choix automatique n’est permis.",
  approval_scope_inactive: "Le périmètre d’approbation résolu est inactif.",
  approver_not_found: "Un approbateur configuré est introuvable.",
  approver_inactive: "Un approbateur configuré est inactif.",
  approver_permission_missing: "Un approbateur configuré n’a pas la permission approve_demands.",
  no_eligible_approver: "Aucun approbateur actif et autorisé n’est admissible pour ce périmètre.",
  asset_type_unknown: "Le type d’actif référencé est introuvable.",
  asset_type_inactive: "Le type d’actif référencé est inactif.",
  asset_type_approval_scope_unmapped: "Aucun périmètre d’approbation actif n’est associé à ce type d’actif.",
  asset_type_approval_scope_ambiguous: "Plusieurs périmètres d’approbation actifs couvrent ce type d’actif.",
  proposed_asset_unknown: "L’actif proposé est introuvable.",
  proposed_asset_inactive: "L’actif proposé est inactif.",
  proposed_asset_type_mismatch: "L’actif proposé n’appartient pas au type d’actif de la ligne.",
};

function diagnosticRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function diagnosticText(value: unknown): string | null {
  if (typeof value === "string") {
    const normalized = value.trim();
    return normalized || null;
  }
  return typeof value === "number" ? String(value) : null;
}

function scopeDiagnosticText(value: unknown): string | null {
  const scope = diagnosticRecord(value);
  if (!scope) return null;
  const code = diagnosticText(scope.code);
  const label = diagnosticText(scope.label);
  const id = diagnosticText(scope.id);
  const base = code && label
    ? `${code} — ${label}`
    : code || label || id;
  if (!base) return null;
  return scope.active === false ? `${base} (inactif)` : base;
}

function routingReason(code: string): { code: string; label: string } {
  const separator = code.indexOf(":");
  const baseCode = separator >= 0 ? code.slice(0, separator) : code;
  const detail = separator >= 0 ? code.slice(separator + 1) : "";
  const label = ROUTING_DIAGNOSTIC_LABELS[baseCode] || baseCode;
  return {
    code,
    label: detail ? `${label} Référence : ${detail}.` : label,
  };
}

function approvalRoutingDiagnostic(reason: unknown): ApprovalRoutingDiagnosticView | null {
  if (!(reason instanceof ApiError) || reason.code !== "approval_cycle_routing_blocked") {
    return null;
  }
  const context = diagnosticRecord(reason.context);
  if (!context) return null;

  const requestLineId = diagnosticText(context.request_line_id);
  const position = typeof context.request_line_position === "number"
    ? context.request_line_position
    : null;
  const taskCode = diagnosticText(context.task_code);
  const taskLabel = diagnosticText(context.task_label);
  const requiredResourceClass = diagnosticText(context.required_resource_class);
  const resourceClass = diagnosticText(context.effective_resource_class);
  const proposedResource = diagnosticText(context.proposed_resource_id);
  const routingSources = Array.isArray(context.routing_sources)
    ? context.routing_sources
        .map(diagnosticText)
        .filter((value): value is string => Boolean(value))
    : [];
  const lineKind = diagnosticText(context.line_kind) || "WORKFORCE";
  const assetTypeCode = diagnosticText(context.asset_type_code);
  const assetTypeLabel = diagnosticText(context.asset_type_label);
  const assetTypeId = diagnosticText(context.asset_type_id);
  const proposedAssetCode = diagnosticText(context.proposed_asset_code);
  const proposedAssetLabel = diagnosticText(context.proposed_asset_label);
  const proposedAssetId = diagnosticText(context.proposed_asset_id);
  const assetType = assetTypeCode && assetTypeLabel
    ? `${assetTypeCode} — ${assetTypeLabel}`
    : assetTypeCode || assetTypeLabel || assetTypeId;
  const proposedAsset = proposedAssetCode && proposedAssetLabel
    ? `${proposedAssetCode} — ${proposedAssetLabel}`
    : proposedAssetCode || proposedAssetLabel || proposedAssetId;

  const resolvedScope = scopeDiagnosticText(context.approval_scope);
  const candidateScopes = Array.isArray(context.approval_scope_candidates)
    ? context.approval_scope_candidates
        .map(scopeDiagnosticText)
        .filter((value): value is string => Boolean(value))
    : [];
  const suggestedScope = diagnosticText(context.suggested_scope_code);
  const scope = resolvedScope
    || (candidateScopes.length > 0
      ? candidateScopes.join(" · ")
      : suggestedScope
        ? `Aucun scope résolu (classification suggérée : ${suggestedScope})`
        : "Aucun scope résolu");

  const diagnostics = Array.isArray(context.diagnostics)
    ? context.diagnostics
        .map(diagnosticText)
        .filter((value): value is string => Boolean(value))
    : [];

  return {
    line: position !== null ? `Ligne ${position + 1}` : "Ligne concernée",
    requestLineId,
    lineKind,
    task: taskCode
      ? `${taskCode}${taskLabel ? ` — ${taskLabel}` : ""}`
      : lineKind === "ASSET" ? "Non utilisée pour le routage ASSET" : "Aucune tâche ERP résolue",
    requiredResourceClass,
    resourceClass: resourceClass
      || (lineKind === "ASSET" ? "Non utilisée pour le routage ASSET" : "Aucune classe effective résolue"),
    proposedResource,
    routingSources,
    assetType,
    proposedAsset,
    scope,
    reasons: diagnostics.length > 0
      ? diagnostics.map(routingReason)
      : [routingReason("approval_cycle_routing_blocked")],
  };
}

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible d'exécuter l'action.";
}

function normalStatus(status: string | null | undefined): string {
  return (status ?? "").trim().toLocaleLowerCase("fr-CA");
}

function actionLabel(action: WorkflowButtonAction): string {
  switch (action) {
    case "submit": return "Soumettre";
    case "approve": return "Approuver";
    case "correction": return "Demander une correction";
    case "cancel": return "Annuler la demande";
    case "request-cancellation": return "Demander l’annulation";
    case "accept-cancellation": return "Annuler la demande et libérer le planning";
    case "reject-cancellation": return "Refuser";
  }
}

function idempotencyKey(): string {
  return createClientId();
}

function deltaLabel(change: DemandPlanDeltaItem["change"]): string {
  switch (change) {
    case "ADD": return "À ajouter";
    case "MOVE": return "À déplacer";
    case "MODIFY": return "À modifier";
    case "CANCEL": return "À annuler";
  }
}

function deltaSide(
  resource: string | null,
  day: string | null,
  hours: number,
  allocationType: string | null,
  outside: boolean,
): string {
  if (!day || !resource || hours <= 0) return "—";
  const suffix = outside ? " · hors horaire" : "";
  return `${day} · ${resourceDisplayName(resource)} · ${hours} h${allocationType ? ` · ${allocationType}` : ""}${suffix}`;
}

function envelopeDecisionLabel(decision: string | null): string {
  switch (decision) {
    case "WITHIN_ENVELOPE": return "Dans l’enveloppe approuvée";
    case "REAPPROVAL_REQUIRED": return "Réapprobation requise";
    case "EXPLICIT_EXCEPTION_REQUIRED": return "Dérogation explicite requise";
    case "APPROVAL_REFERENCE_UNKNOWN": return "Référence d’approbation inconnue";
    case "INVALID": return "Proposition invalide";
    default: return "Décision d’enveloppe non disponible";
  }
}

function unavailableDeltaMessage(reason: string | null): string {
  switch (reason) {
    case "NO_CURRENT_PLAN":
      return "Aucun plan approuvé antérieur : cette approbation créera le premier plan de la demande.";
    case "PROPOSAL_INCOMPLETE":
      return "Le plan proposé est incomplet; le delta ne peut pas encore être calculé.";
    case "CURRENT_PLAN_UNSUPPORTED":
    case "PROPOSED_PLAN_UNSUPPORTED":
      return "Le moteur ne peut pas comparer cette demande tant qu’un segment non supporté est présent.";
    default:
      return "Le delta n’est pas disponible pour cette demande.";
  }
}

type DemandWorkflowPageProps = {
  demandNumber?: string;
  canonicalDetail?: DemandDetailReadModel | null;
  embedded?: boolean;
  onChanged?: () => void | Promise<void>;
  refreshToken?: number;
  hasUnsavedChanges?: boolean;
  actionsOnly?: boolean;
  showActions?: boolean;
};

export default function DemandWorkflowPage({
  demandNumber,
  canonicalDetail,
  embedded = false,
  onChanged,
  refreshToken = 0,
  hasUnsavedChanges = false,
  actionsOnly = false,
  showActions = true,
}: DemandWorkflowPageProps = {}) {
  const [demands, setDemands] = useState<DemandReadModel[]>([]);
  const [selectedNumber, setSelectedNumber] = useState("");
  const [selectedDemand, setSelectedDemand] = useState<DemandReadModel | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<DemandDetailReadModel | null>(null);
  const [workflowState, setWorkflowState] = useState<DemandWorkflowState | null>(null);
  const [approvalComment, setApprovalComment] = useState("");
  const [correctionComment, setCorrectionComment] = useState("");
  const [cancellationReason, setCancellationReason] = useState("");
  const [cancellationResolutionComment, setCancellationResolutionComment] = useState("");
  const [cancellationReviewOpen, setCancellationReviewOpen] = useState(false);
  const [cancellationPlanningVersion, setCancellationPlanningVersion] = useState<number | null>(null);
  const [cancellationImpactLoading, setCancellationImpactLoading] = useState(false);
  const [cancellationImpactError, setCancellationImpactError] = useState<string | null>(null);
  const acceptRetry = useRef<CancellationAcceptRetry | null>(null);
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<WorkflowButtonAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [routingDiagnostic, setRoutingDiagnostic] = useState<ApprovalRoutingDiagnosticView | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [approvalState, setApprovalState] = useState<DemandApprovalState | null>(null);
  const [approvalStateError, setApprovalStateError] = useState<string | null>(null);
  const [planDelta, setPlanDelta] = useState<DemandPlanDelta | null>(null);
  const [planDeltaLoading, setPlanDeltaLoading] = useState(false);
  const [planDeltaError, setPlanDeltaError] = useState<string | null>(null);

  const currentDetail = canonicalDetail ?? selectedDetail;
  const currentDemand = canonicalDetail?.demand ?? selectedDemand;
  const currentWorkflowState = currentDetail
    ? currentDetail.workflow as DemandWorkflowState
    : workflowState;
  const currentApprovalCycle = currentDetail?.approval_cycle ?? null;
  const actorApprovalLines = currentApprovalCycle?.actor_approvable_request_line_ids ?? [];

  async function refresh(number?: string) {
    const rows = demandNumber
      ? [(await getDemandDetail(demandNumber)).demand]
      : await getDemands();
    setDemands(rows);
    const wanted = demandNumber || number || selectedNumber || rows[0]?.number || "";
    const nextNumber = rows.some((row) => row.number === wanted) ? wanted : rows[0]?.number || "";
    setSelectedNumber(nextNumber);
    if (!nextNumber) {
      setSelectedDemand(null);
      setSelectedDetail(null);
      setWorkflowState(null);
      return;
    }
    const detail = await getDemandDetail(nextNumber);
    setSelectedDemand(detail.demand);
    setSelectedDetail(detail);
    setWorkflowState(detail.workflow as DemandWorkflowState);
  }

  useEffect(() => {
    if (canonicalDetail) {
      setDemands([canonicalDetail.demand]);
      setSelectedNumber(canonicalDetail.demand.number);
      setSelectedDemand(canonicalDetail.demand);
      setWorkflowState(canonicalDetail.workflow as DemandWorkflowState);
      setLoading(false);
      setError(null);
      setRoutingDiagnostic(null);
      return;
    }
    let active = true;
    setLoading(true);
    setWorkflowState(null);
    setRoutingDiagnostic(null);
    const demandRequest = demandNumber
      ? getDemandDetail(demandNumber).then((detail) => [detail.demand])
      : getDemands();
    demandRequest
      .then(async (rows) => {
        if (!active) return;
        setDemands(rows);
        const first = demandNumber || rows[0]?.number || "";
        setSelectedNumber(first);
        if (first) {
          const detail = await getDemandDetail(first);
          if (active) {
            setSelectedDemand(detail.demand);
            setSelectedDetail(detail);
            setWorkflowState(detail.workflow as DemandWorkflowState);
          }
        } else if (active) {
          setWorkflowState(null);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [demandNumber, refreshToken, canonicalDetail]);

  useEffect(() => {
    if (canonicalDetail || !selectedNumber || loading) return;
    let active = true;
    setError(null);
    setRoutingDiagnostic(null);
    getDemandDetail(selectedNumber)
      .then((detail) => {
        if (active) {
          setSelectedDemand(detail.demand);
          setSelectedDetail(detail);
          setWorkflowState(detail.workflow as DemandWorkflowState);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(errorMessage(reason));
      });
    return () => { active = false; };
  }, [selectedNumber, loading]);

  useEffect(() => {
    if (actionsOnly || !currentDemand) {
      setApprovalState(null);
      setApprovalStateError(null);
      return;
    }
    let active = true;
    setApprovalStateError(null);
    getDemandApprovalState(currentDemand.number)
      .then((state) => {
        if (active) setApprovalState(state);
      })
      .catch((reason: unknown) => {
        if (active) {
          setApprovalState(null);
          setApprovalStateError(errorMessage(reason));
        }
      });
    return () => { active = false; };
  }, [actionsOnly, currentDemand?.number, currentDemand?.version, currentDemand?.status]);

  useEffect(() => {
    if (actionsOnly || !currentDemand || normalStatus(currentDemand.status) !== "soumise") {
      setPlanDelta(null);
      setPlanDeltaError(null);
      setPlanDeltaLoading(false);
      return;
    }
    let active = true;
    setPlanDeltaLoading(true);
    setPlanDeltaError(null);
    getDemandPlanDelta(currentDemand.number)
      .then((delta) => {
        if (active) setPlanDelta(delta);
      })
      .catch((reason: unknown) => {
        if (active) {
          setPlanDelta(null);
          setPlanDeltaError(errorMessage(reason));
        }
      })
      .finally(() => {
        if (active) setPlanDeltaLoading(false);
      });
    return () => { active = false; };
  }, [actionsOnly, currentDemand?.number, currentDemand?.status]);

  const actions = useMemo(
    () =>
      (currentWorkflowState?.available_actions ?? []).filter(
        (action): action is WorkflowButtonAction =>
          action === "submit" ||
          action === "approve" ||
          action === "correction" ||
          action === "cancel" ||
          action === "request-cancellation" ||
          action === "accept-cancellation" ||
          action === "reject-cancellation",
      ),
    [currentWorkflowState],
  );

  async function refreshAfterMutation(number: string) {
    if (canonicalDetail) {
      await onChanged?.();
      return;
    }
    await refresh(number);
    await onChanged?.();
  }

  async function loadCancellationImpactVersion() {
    if (!currentDemand) return;
    setCancellationImpactLoading(true);
    setCancellationImpactError(null);
    setCancellationPlanningVersion(null);
    const fallback = new Date().toISOString().slice(0, 10);
    const start = currentDemand.desired_start || fallback;
    const end = currentDemand.desired_end && currentDemand.desired_end >= start
      ? currentDemand.desired_end
      : start;
    try {
      const planning = await getPlanningSnapshot(start, end);
      setCancellationPlanningVersion(planning.planning_version);
    } catch (reason: unknown) {
      setCancellationImpactError(errorMessage(reason));
    } finally {
      setCancellationImpactLoading(false);
    }
  }

  async function openCancellationReview() {
    if (hasUnsavedChanges) {
      setError("Enregistre les modifications avant de poursuivre.");
      return;
    }
    setError(null);
    setCancellationReviewOpen(true);
    await loadCancellationImpactVersion();
  }

  async function runCancellationRequest() {
    if (!currentDemand || pendingAction) return;
    if (hasUnsavedChanges) {
      setError("Enregistre les modifications avant de poursuivre.");
      return;
    }
    if (!cancellationReason.trim()) {
      setError("Une raison est requise pour demander l’annulation.");
      return;
    }
    setPendingAction("request-cancellation");
    setError(null);
    setNotice(null);
    try {
      const expectedVersion = currentWorkflowState?.version ?? currentDemand.version;
      const result = await requestDemandCancellation(
        currentDemand.number,
        cancellationReason.trim(),
        expectedVersion,
      );
      await refreshAfterMutation(result.demand_number);
      setCancellationReason("");
      setNotice("Annulation demandée. Le planning reste actif jusqu’à la décision du coordonnateur.");
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setPendingAction(null);
    }
  }

  async function runCancellationResolution(action: "accept-cancellation" | "reject-cancellation") {
    if (!currentDemand || pendingAction) return;
    if (hasUnsavedChanges) {
      setError("Enregistre les modifications avant de poursuivre.");
      return;
    }
    const requestId = currentDemand.cancellation_request_id;
    if (!requestId) {
      setError("L’identité de la demande d’annulation n’est pas disponible. Actualise la demande.");
      return;
    }
    if (!cancellationResolutionComment.trim()) {
      setError("Un commentaire de résolution est requis.");
      return;
    }
    if (action === "accept-cancellation" && cancellationPlanningVersion === null) {
      setError("Actualise l’impact du planning avant d’accepter l’annulation.");
      return;
    }

    setPendingAction(action);
    setError(null);
    setRoutingDiagnostic(null);
    setNotice(null);
    try {
      const expectedVersion = currentWorkflowState?.version ?? currentDemand.version;
      if (action === "reject-cancellation") {
        const result = await rejectDemandCancellation(
          currentDemand.number,
          requestId,
          cancellationResolutionComment.trim(),
          expectedVersion,
        );
        await refreshAfterMutation(result.demand_number);
        setCancellationResolutionComment("");
        setCancellationReviewOpen(false);
        setCancellationPlanningVersion(null);
        setNotice("Demande d’annulation refusée. Le planning actif est conservé.");
      } else {
        const expectedPlanningVersion = cancellationPlanningVersion as number;
        const fingerprint = JSON.stringify({
          demand_number: currentDemand.number,
          cancellation_request_id: requestId,
          comment: cancellationResolutionComment.trim(),
          expected_version: expectedVersion,
          expected_planning_version: expectedPlanningVersion,
        });
        const key = acceptRetry.current?.fingerprint === fingerprint
          ? acceptRetry.current.key
          : idempotencyKey();
        acceptRetry.current = { fingerprint, key };
        const result = await acceptDemandCancellation(
          currentDemand.number,
          requestId,
          cancellationResolutionComment.trim(),
          expectedVersion,
          expectedPlanningVersion,
          key,
        );
        acceptRetry.current = null;
        await refreshAfterMutation(result.demand_number);
        setCancellationResolutionComment("");
        setCancellationReviewOpen(false);
        setCancellationPlanningVersion(null);
        setNotice(
          `Demande annulée. Planning libéré : ${result.deleted_human_shifts ?? 0} quart(s) et ${result.deleted_asset_allocations ?? 0} réservation(s) d’actif supprimés.`,
        );
      }
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setPendingAction(null);
    }
  }

  async function runAction(action: StandardWorkflowButtonAction) {
    if (!currentDemand || pendingAction) return;
    if (hasUnsavedChanges) {
      setError("Enregistre les modifications avant de poursuivre.");
      return;
    }
    if (action === "correction" && !correctionComment.trim()) {
      setError("Un commentaire est requis pour demander une correction.");
      return;
    }
    if (action === "cancel" && !window.confirm(`Annuler la demande ${currentDemand.number}?`)) return;

    setPendingAction(action);
    setError(null);
    setNotice(null);
    try {
      const expectedVersion = currentWorkflowState?.version ?? currentDemand.version;
      let result: DemandWorkflowResult | DemandApprovalVoteResult;
      if (action === "submit") {
        result = await submitDemand(currentDemand.number, expectedVersion);
      } else if (action === "approve" && currentApprovalCycle) {
        if (actorApprovalLines.length === 0) {
          throw new Error("Aucune ligne restante n’est admissible pour votre approbation.");
        }
        result = await approveDemandLines(
          currentDemand.number,
          currentApprovalCycle.approval_cycle_id,
          actorApprovalLines,
          approvalComment.trim(),
          expectedVersion,
          idempotencyKey(),
        );
      } else if (action === "approve") {
        result = await approveDemand(
          currentDemand.number,
          approvalComment.trim(),
          expectedVersion,
        );
      } else if (action === "correction") {
        result = await requestDemandCorrection(
          currentDemand.number,
          correctionComment.trim(),
          expectedVersion,
        );
      } else {
        result = await cancelDemand(currentDemand.number, expectedVersion);
      }

      await refreshAfterMutation(result.demand_number);
      if (action === "approve") {
        const planning = result.planning;
        if ("quorum_complete" in result) {
          setNotice(
            result.quorum_complete
              ? planning
                ? `Demande approuvée — quorum complet. Planification recalculée : ${planning.segments} segment(s), ${planning.allocations} allocation(s), ${planning.unallocated_hours} h non allouée(s).`
                : "Demande approuvée — quorum complet."
              : `Approbation enregistrée — ${result.satisfied_requirements} lignes sur ${result.total_requirements} satisfaites.`,
          );
        } else {
          setNotice(
            planning
              ? `Demande approuvée. Planification recalculée : ${planning.segments} segment(s), ${planning.allocations} allocation(s), ${planning.unallocated_hours} h non allouée(s).`
              : "Demande approuvée.",
          );
        }
        setApprovalComment("");
      } else if (action === "correction") {
        setNotice("Demande retournée pour correction.");
        setCorrectionComment("");
      } else if (action === "submit") {
        setNotice("Demande soumise pour approbation.");
      } else {
        setNotice("Demande annulée.");
      }
    } catch (reason: unknown) {
      const routing = approvalRoutingDiagnostic(reason);
      if (routing) {
        setRoutingDiagnostic(routing);
        setError(null);
      } else {
        setRoutingDiagnostic(null);
        setError(errorMessage(reason));
      }
    } finally {
      setPendingAction(null);
    }
  }

  const busy = pendingAction !== null;
  const canQuickApprove = actions.includes("approve")
    && (!currentApprovalCycle || actorApprovalLines.length > 0);

  return (
    <section className={`demand-workflow-page ${actionsOnly ? "actions-only" : ""}`}>
      {!embedded && (
        <div className="page-heading">
          <div>
            <span className="eyebrow">Cycle de vie</span>
            <h1>Workflow des demandes</h1>
            <p>Soumission, approbation, retour pour correction et annulation via les règles autoritaires de FastAPI.</p>
          </div>
        </div>
      )}

      {showActions && error && <div className="error-panel"><strong>Action impossible.</strong><span>{error}</span></div>}
      {showActions && routingDiagnostic && (
        <div className="error-panel approval-routing-diagnostic" data-testid="approval-routing-diagnostic" role="alert">
          <strong>Routage d’approbation à corriger</strong>
          <span>La demande n’a pas été soumise. Corrige la configuration ou la référence indiquée, puis soumets-la de nouveau.</span>
          <dl className="approval-routing-context">
            <div>
              <dt>RequestLine</dt>
              <dd>
                {routingDiagnostic.line}
                {routingDiagnostic.requestLineId ? <small>{routingDiagnostic.requestLineId}</small> : null}
              </dd>
            </div>
            <div><dt>Nature</dt><dd>{routingDiagnostic.lineKind}</dd></div>
            <div><dt>Tâche ERP</dt><dd>{routingDiagnostic.task}</dd></div>
            {routingDiagnostic.lineKind !== "ASSET" && (
              <>
                <div>
                  <dt>Sources de routage</dt>
                  <dd>{routingDiagnostic.routingSources.length > 0 ? routingDiagnostic.routingSources.join(" · ") : "Aucune source de classe prioritaire"}</dd>
                </div>
                <div><dt>Classe explicite</dt><dd>{routingDiagnostic.requiredResourceClass || "Aucune"}</dd></div>
                <div><dt>Ressource proposée</dt><dd>{routingDiagnostic.proposedResource || "Aucune"}</dd></div>
              </>
            )}
            <div><dt>Classe effective</dt><dd>{routingDiagnostic.resourceClass}</dd></div>
            {routingDiagnostic.lineKind === "ASSET" && (
              <>
                <div><dt>Type d’actif</dt><dd>{routingDiagnostic.assetType || "Non résolu"}</dd></div>
                <div><dt>Actif proposé</dt><dd>{routingDiagnostic.proposedAsset || "Aucun"}</dd></div>
              </>
            )}
            <div><dt>Scope d’approbation</dt><dd>{routingDiagnostic.scope}</dd></div>
          </dl>
          <div className="approval-routing-reasons">
            <strong>Pourquoi le routage bloque</strong>
            <ul>
              {routingDiagnostic.reasons.map((item) => (
                <li key={item.code}>
                  <span>{item.label}</span>
                  <code>{item.code}</code>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {showActions && notice && <div className="demand-notice" role="status">{notice}</div>}
      {showActions && hasUnsavedChanges && (
        <div className="demand-notice workflow-dirty-warning" role="status">
          Enregistre les modifications avant de poursuivre.
        </div>
      )}

      <div className={`workflow-layout ${embedded ? "embedded" : ""}`}>
        {!embedded && (        <aside className="workflow-list-panel">
          <label>
            <span>Demande</span>
            <select
              value={selectedNumber}
              onChange={(event) => {
                setSelectedNumber(event.target.value);
                setNotice(null);
                setError(null);
                setApprovalComment("");
                setCorrectionComment("");
              }}
              disabled={busy || loading}
            >
              {demands.map((demand) => (
                <option value={demand.number} key={demand.number}>
                  {demand.number} — {demand.project_number || "Sans projet"} — {demand.status}
                </option>
              ))}
            </select>
          </label>
          {currentDemand && (
            <div className="workflow-summary-card">
              <strong>{currentDemand.number}</strong>
              <span>{currentDemand.project_number} — {currentDemand.project_name || "Projet"}</span>
              <span>{currentDemand.requester ? `Demandeur : ${currentDemand.requester}` : "Demandeur non défini"}</span>
            </div>
          )}
        </aside>)}

        <div className="workflow-detail-panel">
          {currentDemand ? (
            <>
              {showActions && (actions.includes("submit")
                || actions.includes("cancel")
                || actions.includes("request-cancellation")
                || canQuickApprove) && (
                <div className="workflow-primary-actions" data-testid="primary-demand-actions">
                  {actions.includes("submit") && (
                    <button type="button" className="primary-button" disabled={busy || hasUnsavedChanges} onClick={() => runAction("submit")}>
                      {pendingAction === "submit" ? "Soumission…" : actionLabel("submit")}
                    </button>
                  )}
                  {canQuickApprove && (
                    <div className="workflow-primary-action-group">
                      <label className="workflow-comment-field">
                        <span>Commentaire d’approbation (optionnel)</span>
                        <textarea
                          rows={2}
                          value={approvalComment}
                          onChange={(event) => setApprovalComment(event.target.value)}
                          disabled={busy}
                          placeholder="Contexte ou décision d’approbation…"
                        />
                      </label>
                      <button
                        type="button"
                        className="primary-button"
                        aria-label="Approuver"
                        data-testid="approval-action"
                        disabled={busy || hasUnsavedChanges}
                        onClick={() => runAction("approve")}
                      >
                        {pendingAction === "approve"
                          ? "Approbation…"
                          : currentApprovalCycle
                            ? `Approuver mes lignes (${actorApprovalLines.length})`
                            : actionLabel("approve")}
                      </button>
                    </div>
                  )}
                  {actions.includes("cancel") && (
                    <button type="button" className="secondary-button workflow-cancel" disabled={busy || hasUnsavedChanges} onClick={() => runAction("cancel")}>
                      {pendingAction === "cancel" ? "Annulation…" : actionLabel("cancel")}
                    </button>
                  )}
                  {actions.includes("request-cancellation") && (
                    <div className="workflow-primary-action-group" data-testid="cancellation-request-panel">
                      <label className="workflow-comment-field">
                        <span>Raison de la demande d’annulation (requise)</span>
                        <textarea
                          rows={2}
                          value={cancellationReason}
                          onChange={(event) => setCancellationReason(event.target.value)}
                          disabled={busy}
                          placeholder="Pourquoi ce plan doit-il être annulé?"
                        />
                      </label>
                      <button
                        type="button"
                        className="secondary-button workflow-cancel"
                        disabled={busy || hasUnsavedChanges || !cancellationReason.trim()}
                        onClick={() => void runCancellationRequest()}
                      >
                        {pendingAction === "request-cancellation" ? "Envoi…" : actionLabel("request-cancellation")}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {!actionsOnly && <div className="workflow-state-grid">
                <div className="workflow-state-card">
                  <span>Approbation / statut</span>
                  <strong>{currentDemand.status || "Non défini"}</strong>
                  <small>Ce statut pilote le cycle de vie de la demande.</small>
                </div>
                <div className="workflow-state-card">
                  <span>Confirmation</span>
                  <strong>{currentDemand.confirmation || "Confirmée"}</strong>
                  <small>La confirmation décrit la certitude du besoin, indépendamment de son approbation.</small>
                </div>
                {approvalState && (
                  <div className="workflow-state-card" data-testid="approval-state">
                    <span>Autorisation active</span>
                    <strong>{approvalState.approval_reference_status || "Aucune référence"}</strong>
                    <small>
                      {approvalState.active_revision_id
                        ? `Révision ${approvalState.active_revision_id.slice(0, 8)} · demande approuvée v${approvalState.approved_request_version ?? "—"} · opérationnel v${approvalState.operational_version ?? "—"}`
                        : "Aucune révision approuvée active."}
                    </small>
                    {approvalState.authorization_fingerprint && (
                      <small title={approvalState.authorization_fingerprint}>
                        Empreinte {approvalState.authorization_fingerprint.slice(0, 12)}…
                      </small>
                    )}
                  </div>
                )}
              </div>}

              {!actionsOnly && currentApprovalCycle && (
                <section className="approval-progress-panel" data-testid="approval-progress">
                  <div className="approval-progress-heading">
                    <div>
                      <span className="eyebrow">Multi-approbation</span>
                      <h3>Progression par ligne</h3>
                    </div>
                    <strong>
                      {currentApprovalCycle.satisfied_requirements} / {currentApprovalCycle.total_requirements} satisfaites
                    </strong>
                  </div>
                  <div className="approval-progress-list">
                    {currentApprovalCycle.requirements.map((requirement) => {
                      const line = currentDetail?.lines.find(
                        (row) => row.line.line_id === requirement.request_line_id,
                      )?.line;
                      const resourceClassLabel = line?.required_resource_class_label?.trim() || null;
                      const fallbackLineNumber = line ? `Ligne ${line.position + 1}` : "Ligne";
                      const assetTypeCode = line?.asset_type_code?.trim() || null;
                      const assetTypeName = line?.asset_type_label?.trim() || null;
                      const assetTypeLabel = assetTypeCode && assetTypeName
                        ? `${assetTypeCode} — ${assetTypeName}`
                        : assetTypeCode || assetTypeName;
                      const proposedAssetCode = line?.proposed_asset_code?.trim() || null;
                      const proposedAssetName = line?.proposed_asset_label?.trim() || null;
                      const proposedAssetLabel = proposedAssetCode && proposedAssetName
                        ? `${proposedAssetCode} — ${proposedAssetName}`
                        : proposedAssetCode || proposedAssetName;
                      const fallbackLineLabel = line?.kind === "ASSET"
                        ? proposedAssetLabel || assetTypeLabel || fallbackLineNumber
                        : resourceClassLabel || line?.description || fallbackLineNumber;
                      const lineLabel = line?.kind !== "ASSET" && line?.task_code
                        ? `${line.task_code} — ${line.task_label || line.description || resourceClassLabel || fallbackLineNumber}`
                        : fallbackLineLabel;
                      return (
                        <article
                          className={`approval-progress-line ${requirement.satisfied ? "is-satisfied" : ""}`}
                          data-line-id={requirement.request_line_id}
                          key={requirement.requirement_id}
                        >
                          <div>
                            <strong>{lineLabel}</strong>
                            <span>{requirement.satisfied ? "Satisfaite" : "En attente"}</span>
                          </div>
                          <small>
                            Approbateur(s) admissible(s) : {requirement.approvers.map((item) => item.display_name).join(", ") || "aucun"}
                          </small>
                          {resourceClassLabel && (
                            <small>Classe : {resourceClassLabel}</small>
                          )}
                          {requirement.actor_can_approve && (
                            <small className="approval-progress-mine">À approuver par vous</small>
                          )}
                          {requirement.decisions.map((decision) => (
                            <small key={`${decision.action_id}-${decision.app_user_id}`}>
                              ✓ {decision.display_name}
                              {decision.comment ? ` — ${decision.comment}` : ""}
                            </small>
                          ))}
                        </article>
                      );
                    })}
                  </div>
                  <small className="approval-progress-compatibility">
                    {currentDemand.approved_by_name
                      ? `Compatibilité globale : finalisation par ${currentDemand.approved_by_name}. La preuve du quorum reste le cycle par ligne.`
                      : "Les champs globaux d’approbation restent vides tant que le quorum complet n’est pas atteint."}
                  </small>
                </section>
              )}

              {!actionsOnly && currentDemand.cancellation_state === "PENDING" && (
                <div className="workflow-cancellation-state" data-testid="cancellation-pending-state">
                  <strong>Annulation demandée</strong>
                  <span>
                    Le statut opérationnel reste {currentDemand.status}; le planning demeure actif jusqu’à une décision explicite.
                  </span>
                  {currentDemand.cancellation_reason && (
                    <small>Raison : {currentDemand.cancellation_reason}</small>
                  )}
                </div>
              )}

              {!actionsOnly && <div className="workflow-separation-note">
                <strong>Approbation ≠ confirmation.</strong>
                <span>Une demande peut être approuvée tout en restant Tentative; les deux concepts ne sont jamais fusionnés par l’interface.</span>
              </div>}

              {!actionsOnly && approvalStateError && (
                <div className="plan-delta-unavailable">{approvalStateError}</div>
              )}

              {!actionsOnly && approvalState && (
                <div
                  className={`workflow-envelope-decision ${approvalState.envelope_decision === "REAPPROVAL_REQUIRED" ? "requires-approval" : ""}`}
                  data-testid="envelope-decision"
                >
                  <strong>{envelopeDecisionLabel(approvalState.envelope_decision)}</strong>
                  <span>
                    Décision backend : {approvalState.envelope_reason || "aucune raison"}.
                    {approvalState.candidate_matches_approved === false
                      ? " La proposition candidate diffère de l’autorisation active; l’ancien plan reste la référence tant qu’elle n’est pas approuvée."
                      : " La candidate correspond à l’autorisation approuvée active."}
                  </span>
                </div>
              )}

              {!actionsOnly && normalStatus(currentDemand.status) === "soumise" && (
                <div className="plan-delta-panel" data-testid="plan-delta-preview">
                  <div className="plan-delta-heading">
                    <div>
                      <span className="eyebrow">Impact de l’approbation</span>
                      <h3>Plan actuel → plan proposé</h3>
                      <p>Prévisualisation en lecture seule calculée avec le moteur de planification. Aucun quart n’est modifié avant l’approbation.</p>
                    </div>
                    {planDelta?.available && (
                      <strong>{planDelta.net_hours >= 0 ? "+" : ""}{planDelta.net_hours} h touchées nettes</strong>
                    )}
                  </div>

                  {planDeltaLoading && <div className="plan-delta-empty">Calcul du delta…</div>}
                  {planDeltaError && <div className="plan-delta-unavailable">{planDeltaError}</div>}

                  {!planDeltaLoading && planDelta && !planDelta.available && (
                    <div className="plan-delta-unavailable">
                      <span>{unavailableDeltaMessage(planDelta.reason)}</span>
                      {planDelta.diagnostics.length > 0 && (
                        <ul className="plan-delta-diagnostics" aria-label="Blocages du plan proposé">
                          {planDelta.diagnostics.map((diagnostic) => (
                            <li key={`${diagnostic.code}-${diagnostic.requirement_id ?? "global"}`}>
                              <strong>{diagnostic.code}</strong> — {diagnostic.message}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {!planDeltaLoading && planDelta?.available && (
                    <>
                      <div className="plan-delta-summary">
                        <span className="plan-delta-badge">{planDelta.add_count} ajout(s)</span>
                        <span className="plan-delta-badge">{planDelta.move_count} déplacement(s)</span>
                        <span className="plan-delta-badge">{planDelta.modify_count} modification(s)</span>
                        <span className="plan-delta-badge">{planDelta.cancel_count} annulation(s)</span>
                      </div>

                      {!planDelta.has_changes ? (
                        <div className="plan-delta-empty">L’approbation ne changerait aucun quart du plan actuel.</div>
                      ) : (
                        <div className="plan-delta-list">
                          {planDelta.items.map((item, index) => (
                            <div className="plan-delta-row" key={`${item.segment_id}-${item.change}-${index}`}>
                              <strong>{deltaLabel(item.change)}</strong>
                              <div className="plan-delta-side">
                                <small>Actuel</small>
                                <span>{deltaSide(
                                  item.current_resource_name,
                                  item.current_date,
                                  item.current_hours,
                                  item.current_allocation_type,
                                  item.current_outside_standard_hours,
                                )}</span>
                              </div>
                              <div className="plan-delta-side">
                                <small>Proposé</small>
                                <span>{deltaSide(
                                  item.proposed_resource_name,
                                  item.proposed_date,
                                  item.proposed_hours,
                                  item.proposed_allocation_type,
                                  item.proposed_outside_standard_hours,
                                )}</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}

              {showActions && actions.includes("correction") && (
                <label className="workflow-comment-field">
                  <span>Commentaire de correction (requis)</span>
                  <textarea
                    rows={3}
                    value={correctionComment}
                    onChange={(event) => setCorrectionComment(event.target.value)}
                    disabled={busy}
                    placeholder="Indiquer précisément ce qui doit être corrigé…"
                  />
                </label>
              )}

              {showActions && (actions.includes("accept-cancellation") || actions.includes("reject-cancellation")) && !cancellationReviewOpen && (
                <div className="workflow-cancellation-treatment">
                  <button
                    type="button"
                    className="primary-button"
                    disabled={busy || hasUnsavedChanges}
                    onClick={() => void openCancellationReview()}
                  >
                    Traiter l’annulation
                  </button>
                </div>
              )}

              {showActions && (actions.includes("accept-cancellation") || actions.includes("reject-cancellation")) && cancellationReviewOpen && (
                <div className="workflow-cancellation-review" data-testid="cancellation-review">
                  <div className="plan-delta-heading">
                    <div>
                      <span className="eyebrow">Impact avant décision</span>
                      <h3>Planning qui sera libéré</h3>
                      <p>Ce résumé provient du détail matérialisé backend. Aucun élément n’est supprimé tant que l’acceptation explicite n’a pas réussi.</p>
                    </div>
                    {cancellationPlanningVersion !== null && (
                      <strong>Planning v{cancellationPlanningVersion}</strong>
                    )}
                  </div>

                  {cancellationImpactLoading && <div className="plan-delta-empty">Actualisation de la version du planning…</div>}
                  {cancellationImpactError && (
                    <div className="plan-delta-unavailable">
                      <span>{cancellationImpactError}</span>
                      <button type="button" className="text-button" onClick={() => void loadCancellationImpactVersion()}>
                        Réessayer
                      </button>
                    </div>
                  )}

                  {canonicalDetail && (
                    <>
                      <div className="cancellation-impact-grid">
                        <article>
                          <span>Plan humain</span>
                          <strong>{canonicalDetail.materialized_plan.requirement_count} besoin(s)</strong>
                          <small>
                            {canonicalDetail.materialized_plan.covered_hours} h couvertes · {canonicalDetail.materialized_plan.locked_hours} h verrouillées
                          </small>
                        </article>
                        <article>
                          <span>Actifs réservés</span>
                          <strong>{canonicalDetail.materialized_plan.asset_assigned_count} unité(s)</strong>
                          <small>{canonicalDetail.materialized_plan.asset_usage_hours} h d’usage budgétées</small>
                        </article>
                      </div>
                      {canonicalDetail.materialized_plan.requirements.length > 0 && (
                        <div className="cancellation-impact-list">
                          {canonicalDetail.materialized_plan.requirements.map((requirement) => (
                            <div key={requirement.requirement_id}>
                              <strong>{requirement.segment_id}</strong>
                              <span>
                                {requirement.covered_hours} h affectées · {requirement.locked_hours} h verrouillées
                                {requirement.mobilized_resources.length > 0
                                  ? ` · ${requirement.mobilized_resources.map((resource) => resourceDisplayName(resource.resource_name)).join(", ")}`
                                  : ""}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                      {canonicalDetail.materialized_plan.asset_requirements.some((requirement) => requirement.allocation_id) && (
                        <div className="cancellation-impact-list">
                          {canonicalDetail.materialized_plan.asset_requirements
                            .filter((requirement) => requirement.allocation_id)
                            .map((requirement) => (
                              <div key={requirement.requirement_id}>
                                <strong>{requirement.asset_type_code} — {requirement.asset_type_label}</strong>
                                <span>
                                  {requirement.asset_code || "Unité réservée"}
                                  {requirement.allocation_locked ? " · verrouillée" : ""}
                                </span>
                              </div>
                            ))}
                        </div>
                      )}
                    </>
                  )}

                  <label className="workflow-comment-field">
                    <span>Commentaire de résolution (requis)</span>
                    <textarea
                      rows={3}
                      value={cancellationResolutionComment}
                      onChange={(event) => setCancellationResolutionComment(event.target.value)}
                      disabled={busy}
                      placeholder="Documenter la décision…"
                    />
                  </label>

                  <div className="workflow-actions">
                    {actions.includes("reject-cancellation") && (
                      <button
                        type="button"
                        className="secondary-button"
                        disabled={busy || hasUnsavedChanges || !cancellationResolutionComment.trim()}
                        onClick={() => void runCancellationResolution("reject-cancellation")}
                      >
                        {pendingAction === "reject-cancellation" ? "Refus…" : "Refuser"}
                      </button>
                    )}
                    {actions.includes("accept-cancellation") && (
                      <button
                        type="button"
                        className="secondary-button workflow-cancel"
                        disabled={
                          busy ||
                          hasUnsavedChanges ||
                          !cancellationResolutionComment.trim() ||
                          cancellationPlanningVersion === null
                        }
                        onClick={() => void runCancellationResolution("accept-cancellation")}
                      >
                        {pendingAction === "accept-cancellation"
                          ? "Annulation…"
                          : "Annuler la demande et libérer le planning"}
                      </button>
                    )}
                  </div>
                </div>
              )}

              {!actionsOnly && actions.includes("approve") && currentApprovalCycle && actorApprovalLines.length === 0 && !currentApprovalCycle.quorum_complete && (
                <span className="workflow-terminal-state">
                  Aucune ligne en attente n’est admissible pour votre approbation.
                </span>
              )}

              {showActions && (actions.length === 0 || actions.includes("correction")) && (
                <div className="workflow-actions">
                  {actions.length === 0 && <span className="workflow-terminal-state">Aucune transition usuelle disponible pour ce statut.</span>}
                  {actions.includes("correction") && (
                    <button type="button" className="secondary-button" disabled={busy || hasUnsavedChanges || !correctionComment.trim()} onClick={() => runAction("correction")}>
                      {pendingAction === "correction" ? "Envoi…" : actionLabel("correction")}
                    </button>
                  )}
                </div>
              )}

              {!actionsOnly && (
                <small className="workflow-authority-note">
                  {showActions
                    ? "Les actions affichées proviennent de la projection backend canonique; FastAPI demeure l’autorité pour accepter ou refuser chaque commande."
                    : "Les actions disponibles restent dérivées de la projection backend canonique et sont exposées dans le header de la demande; FastAPI demeure l’autorité."}
                </small>
              )}
            </>
          ) : (
            <div className="demand-editor-empty">
              <strong>{loading ? "Chargement des demandes…" : "Aucune demande disponible"}</strong>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
