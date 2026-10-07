import { resourceDisplayName } from "./resourceLabels";
import { useMemo, useState } from "react";

import {
  ApiError,
  PlanningActionReadModel,
  ResourceRecommendationReadModel,
  getResourceRecommendations,
} from "./api";
import { useAuth } from "./AuthContext";
import { writeSegmentDrag } from "./planningDragDrop";
import { assignSegment } from "./segments-api";

type Props = {
  actions: PlanningActionReadModel[];
  loading: boolean;
  onAssigned: () => void;
  onOpenDemands?: () => void;
  onOpenSegment?: (segmentId: string) => void;
};

function hours(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", {
    maximumFractionDigits: 1,
    minimumFractionDigits: Number(value ?? 0) % 1 ? 1 : 0,
  }).format(Number(value ?? 0));
}

function dateRange(action: PlanningActionReadModel) {
  return action.start_date === action.end_date
    ? action.start_date
    : `${action.start_date} → ${action.end_date}`;
}

function recommendationCategoryLabel(candidate: ResourceRecommendationReadModel) {
  switch (candidate.recommendation_category) {
    case 1:
      return "Catégorie 1 · attitré pleinement compatible";
    case 2:
      return "Catégorie 2 · pleinement compatible";
    case 3:
      return "Catégorie 3 · compétences incomplètes";
    case 4:
      return "Catégorie 4 · capacité prudente partielle";
    case 5:
      return "Catégorie 5 · compétences et capacité partielles";
    case 6:
      return "Catégorie 6 · aucune capacité prudente";
    case 7:
      return "Catégorie 7 · classe ou qualification non compatible";
    default:
      return `Catégorie ${candidate.recommendation_category}`;
  }
}

function competencyDiagnostic(candidate: ResourceRecommendationReadModel) {
  switch (candidate.competency_state) {
    case "NOT_REQUIRED":
      return "Aucune compétence requise";
    case "SATISFIED":
      return "Compétences requises satisfaites";
    case "MISSING":
      return candidate.missing_competency_ids.length > 0
        ? `Compétences manquantes : ${candidate.missing_competency_ids.join(", ")}`
        : "Compétences requises incomplètes";
    case "UNRESOLVED":
      return "Compétences historiques non résolues";
  }
}

function capacityDiagnostic(candidate: ResourceRecommendationReadModel) {
  switch (candidate.capacity_state) {
    case "PRUDENT_FULL":
      return "Capacité prudente suffisante";
    case "PRUDENT_PARTIAL":
      return "Capacité prudente partielle";
    case "TENTATIVE_ONLY":
      return "Capacité seulement après retrait des tentatives";
    case "NONE":
      return "Aucune capacité prudente";
  }
}

function preferredContextDiagnostic(candidate: ResourceRecommendationReadModel) {
  const identity = candidate.preferred_resource_name
    ? resourceDisplayName(candidate.preferred_resource_name)
    : candidate.preferred_resource_id || "La ressource attitrée";
  switch (candidate.preferred_resource_status) {
    case "NONE":
      return null;
    case "ELIGIBLE":
      return `${identity} est attitré(e) à cette tâche et conserve son rang réel selon les critères.`;
    case "INACTIVE_LOCAL":
      return `${identity} est attitré(e), mais inactif(ve) dans RessourcePlanner; aucun avantage de rang n'est appliqué.`;
    case "INACTIVE_ERP":
      return `${identity} est attitré(e), mais inactif(ve) dans l'ERP; aucun avantage de rang n'est appliqué.`;
    case "NO_SCHEDULE_IN_WINDOW":
      return `${identity} est attitré(e), mais n'a aucun horaire dans la fenêtre évaluée.`;
    case "NOT_FOUND":
      return `${identity} est attitré(e), mais la ressource n'est plus résolue localement.`;
    case "INVALID_TASK_CONTEXT":
      return "Le contexte de tâche approuvé ne correspond plus au projet; aucune préférence n'est appliquée.";
    case "UNRESOLVED_CONTEXT":
      return "Le contexte historique ne permet pas de résoudre une ressource attitrée.";
  }
}

function ActionCard({
  action,
  onRecommend,
  onOpenDemands,
  onOpenSegment,
  canDragAssignment,
}: {
  action: PlanningActionReadModel;
  onRecommend: (action: PlanningActionReadModel) => void;
  onOpenDemands?: () => void;
  onOpenSegment?: (segmentId: string) => void;
  canDragAssignment: boolean;
}) {
  const assignment = action.kind === "ASSIGNMENT";
  const task = [action.task_code, action.task_label].filter(Boolean).join(" — ");
  const draggable = assignment && Boolean(action.segment_id) && canDragAssignment;
  return (
    <article
      className={`planning-action-card action-${action.kind.toLowerCase()} ${draggable ? "is-draggable" : ""}`}
      draggable={draggable}
      data-segment-id={action.segment_id || undefined}
      onDragStart={(event) => {
        if (!draggable || !action.segment_id) {
          event.preventDefault();
          return;
        }
        writeSegmentDrag(event.dataTransfer, {
          kind: "SEGMENT",
          segment_id: action.segment_id,
        });
      }}
      title={draggable ? "Glisser ce besoin sur une ressource pour définir sa cible automatique" : undefined}
    >
      <div className="planning-action-card-heading">
        <div>
          <span className="planning-action-kicker">
            {assignment ? "Cible automatique à définir" : "Approbation requise"}
          </span>
          <strong>
            {action.project_number || "Projet"}
            {action.project_name ? ` — ${action.project_name}` : ""}
          </strong>
        </div>
        <strong>{hours(action.planned_hours)} h</strong>
      </div>

      <div className="planning-action-meta">
        {action.demand_number && <span>Demande {action.demand_number}</span>}
        {action.segment_id && <span>{action.segment_id}</span>}
        {task && <span>{task}</span>}
        <span>{dateRange(action)}</span>
        {action.required_competency && <span>Compétence : {action.required_competency}</span>}
        {action.priority && <span>Priorité : {action.priority}</span>}
        {action.confirmation && <span>{action.confirmation}</span>}
        {action.emergency_override_active && <span>⚠ Dérogation urgente</span>}
        {draggable && <span className="drag-hint">↕ Glisser pour définir la cible automatique</span>}
      </div>

      <div className="planning-action-buttons">
        {assignment && canDragAssignment && (
          <button type="button" className="primary-action" onClick={() => onRecommend(action)}>
            Trouver une ressource
          </button>
        )}
        {assignment && action.segment_id && onOpenSegment && (
          <button type="button" onClick={() => onOpenSegment(action.segment_id!)}>
            Modifier le segment
          </button>
        )}
        {action.demand_number && onOpenDemands && (
          <button type="button" onClick={onOpenDemands}>
            Voir la demande
          </button>
        )}
      </div>
    </article>
  );
}

export default function PlanningActionPanel({
  actions,
  loading,
  onAssigned,
  onOpenDemands,
  onOpenSegment,
}: Props) {
  const { can } = useAuth();
  const canAssign = can("manage_planning");
  const [selectedAction, setSelectedAction] = useState<PlanningActionReadModel | null>(null);
  const [recommendations, setRecommendations] = useState<ResourceRecommendationReadModel[]>([]);
  const [recommendationLoading, setRecommendationLoading] = useState(false);
  const [recommendationError, setRecommendationError] = useState<string | null>(null);
  const [assigningResource, setAssigningResource] = useState<string | null>(null);

  const approvals = useMemo(
    () => actions.filter((action) => action.kind === "APPROVAL"),
    [actions],
  );
  const assignments = useMemo(
    () => actions.filter((action) => action.kind === "ASSIGNMENT"),
    [actions],
  );

  const openRecommendations = (action: PlanningActionReadModel) => {
    if (!action.segment_id) return;
    setSelectedAction(action);
    setRecommendations([]);
    setRecommendationError(null);
    setRecommendationLoading(true);
    getResourceRecommendations(action.segment_id)
      .then(setRecommendations)
      .catch((reason: unknown) => {
        if (reason instanceof ApiError) {
          setRecommendationError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
        } else {
          setRecommendationError(
            reason instanceof Error ? reason.message : "Impossible de calculer les recommandations.",
          );
        }
      })
      .finally(() => setRecommendationLoading(false));
  };

  const closeRecommendations = () => {
    if (assigningResource) return;
    setSelectedAction(null);
    setRecommendations([]);
    setRecommendationError(null);
  };

  const assign = async (candidate: ResourceRecommendationReadModel) => {
    if (!selectedAction?.segment_id || !canAssign) return;
    if (candidate.fallback_requires_confirmation) {
      const missing = candidate.missing_competency_ids.length > 0
        ? ` Compétences manquantes : ${candidate.missing_competency_ids.join(", ")}.`
        : "";
      const confirmed = window.confirm(
        `Cette ressource est un repli avec des compétences incomplètes.${missing} Confirmer son utilisation comme cible?`,
      );
      if (!confirmed) return;
    }
    setAssigningResource(candidate.resource_id);
    setRecommendationError(null);
    try {
      await assignSegment(selectedAction.segment_id, candidate.resource_id);
      setSelectedAction(null);
      setRecommendations([]);
      setRecommendationError(null);
      onAssigned();
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setRecommendationError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setRecommendationError(reason instanceof Error ? reason.message : "Impossible d’assigner la ressource.");
      }
    } finally {
      setAssigningResource(null);
    }
  };

  return (
    <>
      <section className="planning-action-panel" aria-label="Éléments de planification à traiter">
        <div className="planning-action-panel-heading">
          <div>
            <span className="eyebrow">À traiter</span>
            <strong>Approbations et travaux à planifier</strong>
          </div>
          <div className="planning-action-counts">
            <span>{approvals.length} à approuver</span>
            <span>{assignments.length} à attribuer</span>
          </div>
        </div>

        {loading ? (
          <div className="planning-action-empty">Chargement des éléments à traiter…</div>
        ) : actions.length === 0 ? (
          <div className="planning-action-empty">Aucune action de planification dans cette fenêtre.</div>
        ) : (
          <div className="planning-action-columns">
            <div>
              <div className="planning-action-section-title">
                <strong>En attente d’approbation</strong>
                <span>{approvals.length}</span>
              </div>
              <div className="planning-action-list">
                {approvals.length > 0
                  ? approvals.map((action) => (
                    <ActionCard
                      key={`approval-${action.reference}`}
                      action={action}
                      onRecommend={openRecommendations}
                      onOpenDemands={onOpenDemands}
                      onOpenSegment={onOpenSegment}
                      canDragAssignment={canAssign}
                    />
                  ))
                  : <div className="planning-action-empty compact">Aucune demande à approuver.</div>}
              </div>
            </div>

            <div>
              <div className="planning-action-section-title">
                <strong>Travaux à planifier</strong>
                <span>{assignments.length}</span>
              </div>
              <div className="planning-action-list">
                {assignments.length > 0
                  ? assignments.map((action) => (
                    <ActionCard
                      key={`assignment-${action.reference}`}
                      action={action}
                      onRecommend={openRecommendations}
                      onOpenDemands={onOpenDemands}
                      onOpenSegment={onOpenSegment}
                      canDragAssignment={canAssign}
                    />
                  ))
                  : <div className="planning-action-empty compact">Tous les reliquats automatiques ont une cible.</div>}
              </div>
            </div>
          </div>
        )}
      </section>

      {selectedAction && (
        <div className="recommendation-backdrop" role="presentation" onMouseDown={closeRecommendations}>
          <section
            className="recommendation-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Trouver une ressource"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="recommendation-heading">
              <div>
                <span className="eyebrow">Trouver une ressource</span>
                <h2>
                  {selectedAction.project_number || "Projet"}
                  {selectedAction.project_name ? ` — ${selectedAction.project_name}` : ""}
                </h2>
                <p>
                  {hours(selectedAction.planned_hours)} h · {dateRange(selectedAction)}
                  {selectedAction.required_competency
                    ? ` · Compétence : ${selectedAction.required_competency}`
                    : ""}
                </p>
              </div>
              <button type="button" onClick={closeRecommendations} disabled={Boolean(assigningResource)}>
                Fermer
              </button>
            </div>

            <p className="recommendation-explainer">
              Ordre calculé par le backend selon ADR-025. La ressource attitrée n'est prioritaire que si elle
              respecte la classe, les compétences et la capacité prudente requises. React conserve le rang reçu;
              choisir une ressource reste une action manuelle.
            </p>

            {recommendations[0] && preferredContextDiagnostic(recommendations[0]) && (
              <div className="recommendation-context" data-testid="preferred-resource-context">
                <strong>Ressource attitrée</strong>
                <span>{preferredContextDiagnostic(recommendations[0])}</span>
              </div>
            )}

            {recommendationError && <div className="error-panel">{recommendationError}</div>}

            {recommendationLoading ? (
              <div className="planning-action-empty">Calcul des disponibilités…</div>
            ) : recommendations.length === 0 ? (
              <div className="planning-action-empty">Aucune ressource planifiable trouvée.</div>
            ) : (
              <div className="recommendation-list">
                {recommendations.map((candidate) => (
                  <article
                    className={[
                      "recommendation-card",
                      candidate.recommended ? "is-recommended" : "",
                      candidate.preferred ? "is-preferred" : "",
                      candidate.fallback_requires_confirmation ? "is-fallback" : "",
                    ].filter(Boolean).join(" ")}
                    key={candidate.resource_id}
                    data-recommendation-rank={candidate.rank}
                  >
                    <div className="recommendation-card-heading">
                      <div>
                        <strong>{resourceDisplayName(candidate.resource_name)}</strong>
                        <span>{candidate.resource_class || "Non classé"}</span>
                        <div className="recommendation-badges">
                          {candidate.recommended && (
                            <span className="recommendation-badge is-recommended-badge">Recommandé</span>
                          )}
                          {candidate.preferred && (
                            <span className="recommendation-badge is-preferred-badge">Attitré</span>
                          )}
                          {candidate.fallback_requires_confirmation && (
                            <span className="recommendation-badge is-fallback-badge">Repli à confirmer</span>
                          )}
                        </div>
                      </div>
                      <span className="rank-pill">#{candidate.rank}</span>
                    </div>

                    <div className="recommendation-signals">
                      <span className="signal-muted">{recommendationCategoryLabel(candidate)}</span>
                      <span
                        className={
                          candidate.competency_state === "SATISFIED"
                            || candidate.competency_state === "NOT_REQUIRED"
                            ? "signal-good"
                            : "signal-warn"
                        }
                      >
                        {competencyDiagnostic(candidate)}
                      </span>
                      {candidate.required_class && (
                        <span className={candidate.class_match ? "signal-good" : "signal-warn"}>
                          {candidate.class_match
                            ? `Classe ${candidate.required_class} satisfaite`
                            : `Classe différente de ${candidate.required_class}`}
                        </span>
                      )}
                      <span className={candidate.capacity_state === "PRUDENT_FULL" ? "signal-good" : "signal-warn"}>
                        {capacityDiagnostic(candidate)}
                      </span>
                    </div>

                    <div className="recommendation-capacity">
                      <span>{hours(candidate.prudent_free)} h libres prudentes</span>
                      <span>{hours(candidate.free_after_confirmed)} h après confirmés</span>
                      <span>{hours(candidate.tentative_hours)} h tentatives</span>
                      <span>{hours(candidate.capacity_hours)} h capacité</span>
                    </div>

                    {candidate.overtime_needed > 0 && (
                      <small className="recommendation-warning">
                        Environ {hours(candidate.overtime_needed)} h ne tiennent pas dans la capacité prudente.
                      </small>
                    )}

                    {candidate.fallback_requires_confirmation && (
                      <small className="recommendation-confirmation-note">
                        Ce repli a des compétences manquantes et demande votre confirmation explicite.
                      </small>
                    )}

                    <button
                      type="button"
                      className="primary-action"
                      disabled={!canAssign || Boolean(assigningResource)}
                      onClick={() => void assign(candidate)}
                      title={!canAssign ? "Permission manage_planning requise" : undefined}
                    >
                      {assigningResource === candidate.resource_id
                        ? "Définition…"
                        : candidate.fallback_requires_confirmation
                          ? "Confirmer ce repli"
                          : "Utiliser comme cible"}
                    </button>
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}
