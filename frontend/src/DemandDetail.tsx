import { useEffect, useState } from "react";

import {
  ApiError,
  type DemandDetailReadModel,
  type ViewScope,
  getDemandDetail,
} from "./api";
import DemandHistoryPage from "./DemandHistoryPage";
import DemandPeriodsPage from "./DemandPeriodsPage";
import DemandWorkflowPage from "./DemandWorkflowPage";

function detailError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible de charger le détail de la demande.";
}

function hours(value: number) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 1 }).format(value);
}

function technicalValue(value: string | number | null | undefined) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export default function DemandDetail({
  demandNumber,
  viewScope,
  readOnly = false,
  onChanged,
  onDirtyChange,
  canonicalDetail,
  hasUnsavedChanges = false,
  compact = false,
}: {
  demandNumber: string;
  viewScope?: ViewScope;
  readOnly?: boolean;
  onChanged?: () => void | Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  canonicalDetail?: DemandDetailReadModel | null;
  hasUnsavedChanges?: boolean;
  compact?: boolean;
}) {
  const [loadedDetail, setLoadedDetail] = useState<DemandDetailReadModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (canonicalDetail) {
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getDemandDetail(demandNumber, controller.signal, viewScope)
      .then(setLoadedDetail)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(detailError(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demandNumber, refreshKey, canonicalDetail, viewScope]);

  const detail = canonicalDetail ?? loadedDetail;

  async function changed() {
    setRefreshKey((value) => value + 1);
    await onChanged?.();
  }

  if (loading && !detail) {
    return <div className="demand-detail-loading">Chargement du contexte unifié…</div>;
  }

  if (error && !detail) {
    return <div className="error-panel"><strong>Détail indisponible.</strong><span>{error}</span></div>;
  }

  if (!detail) return null;

  const activeLines = detail.lines.filter((row) => row.line.active);
  const availableActions = detail.workflow.available_actions;
  const approval = detail.approval_state;
  const technicalContext = detail.technical_context;

  return (
    <section className={`demand-detail-context ${compact ? "compact" : ""}`} data-demand-number={demandNumber}>
      <header className="demand-detail-context-header">
        <div>
          <span className="eyebrow">Contexte unifié</span>
          <h3>{detail.demand.number} · {detail.demand.project_number || "Sans projet"}</h3>
          <p>
            {activeLines.length} besoin(s) ·
            {" "}{detail.materialized_plan.requirement_count} besoin(s) humain(s) matérialisé(s) ·
            {" "}{detail.materialized_plan.asset_requirement_count} besoin(s) d’actif matérialisé(s)
          </p>
        </div>
        <div className="demand-detail-statuses">
          <span>{detail.demand.status}</span>
          {detail.demand.cancellation_state === "PENDING" && (
            <strong className="demand-cancellation-badge" data-testid="detail-cancellation-pending">
              Annulation demandée
            </strong>
          )}
          {detail.policy.reapproval_required && <strong>Réapprobation requise</strong>}
        </div>
      </header>

      {error && <div className="error-panel"><span>{error}</span></div>}

      <details className="demand-detail-section" open data-testid="demand-workflow-section">
        <summary>
          <span>Workflow et impact</span>
          <small>État, autorisation active et aperçu plan actuel → plan proposé.</small>
        </summary>
        <DemandWorkflowPage
          demandNumber={demandNumber}
          viewScope={viewScope}
          canonicalDetail={detail}
          hasUnsavedChanges={hasUnsavedChanges}
          embedded
          showActions={false}
          onChanged={changed}
          refreshToken={refreshKey}
        />
      </details>

      <div className="demand-detail-summary-grid">
        <article>
          <span>Plan humain actif</span>
          <strong>{hours(detail.materialized_plan.covered_hours)} / {hours(detail.materialized_plan.planned_hours)} h</strong>
          <small>{hours(detail.materialized_plan.locked_hours)} h verrouillées</small>
        </article>
        <article>
          <span>Actifs réservables</span>
          <strong>{detail.materialized_plan.asset_assigned_count} / {detail.materialized_plan.asset_requirement_count} réservé(s)</strong>
          <small>
            {detail.materialized_plan.asset_unbudgeted_requirement_count > 0
              ? `${detail.materialized_plan.asset_unbudgeted_requirement_count} sans budget d’usage`
              : `${hours(detail.materialized_plan.asset_usage_hours)} h d’usage budgétées`}
          </small>
        </article>
        <article>
          <span>Autorisation</span>
          <strong>{approval?.approval_reference_status || "Aucune référence active"}</strong>
          <small>{detail.policy.reapproval_required ? "Réapprobation requise" : "Contexte opérationnel courant"}</small>
        </article>
        <article>
          <span>Actions disponibles</span>
          <strong>{availableActions.length}</strong>
          <small>{availableActions.length ? availableActions.join(" · ") : "Aucune action"}</small>
        </article>
      </div>

      {detail.materialized_plan.asset_requirements.length > 0 && (
        <details className="demand-detail-section asset-detail-section" open>
          <summary>
            <span>Actifs matérialisés</span>
            <small>Types requis, fenêtre approuvée et réservation réelle de chaque unité.</small>
          </summary>
          <div className="asset-detail-list">
            {detail.materialized_plan.asset_requirements.map((requirement) => (
              <article className="asset-detail-row" key={requirement.requirement_id}>
                <div>
                  <strong>{requirement.asset_type_code} — {requirement.asset_type_label}</strong>
                  <span>{requirement.start_date} → {requirement.end_date}</span>
                </div>
                <div>
                  <strong>{requirement.asset_label || "À réserver"}</strong>
                  <span>
                    {requirement.asset_code || requirement.status}
                    {requirement.allocation_locked ? " · verrouillée" : ""}
                  </span>
                </div>
                <small>
                  {requirement.usage_hours == null
                    ? "Occupation par unité/jour — aucun budget d’usage horaire."
                    : `Budget d’usage : ${hours(requirement.usage_hours)} h (distinct de la capacité humaine).`}
                </small>
              </article>
            ))}
          </div>
        </details>
      )}

      <details className="demand-detail-section">
        <summary>
          <span>Historique</span>
          <small>Chronologie auditée de cette demande, sans changer de contexte.</small>
        </summary>
        <DemandHistoryPage demandNumber={demandNumber} viewScope={viewScope} embedded refreshToken={refreshKey} />
      </details>

      <details
        className="demand-detail-section demand-detail-advanced-options"
        data-testid="demand-advanced-options"
      >
        <summary>
          <span>Options avancées</span>
          <small>Périodes de travail et réglages secondaires de la demande.</small>
        </summary>
        <div className="demand-detail-advanced-content">
          <div className="demand-detail-subsection-heading">
            <strong>Périodes de travail</strong>
            <small>Définir plusieurs périodes ou plusieurs fenêtres possibles par besoin.</small>
          </div>
          <DemandPeriodsPage
            demandNumber={demandNumber}
            viewScope={viewScope}
            canonicalDemand={detail.demand}
            embedded
            onChanged={changed}
            onDirtyChange={onDirtyChange}
            canEdit={!readOnly && detail.policy.can_edit_periods}
          />
        </div>
      </details>

      {technicalContext && (
        <details
          className="demand-detail-section demand-detail-technical"
          data-testid="demand-technical-context"
        >
          <summary>
            <span>Diagnostic technique / Contexte backend</span>
            <small>Versions, concurrence, provenance d’enveloppe et matérialisation détaillée.</small>
          </summary>
          <div className="demand-detail-technical-content">
            <div className="demand-detail-technical-grid">
              <article>
                <span>Versions internes</span>
                <strong>
                  Demande v{technicalContext.request_version} · workflow v{technicalContext.workflow_version}
                </strong>
                <small>
                  expected_request_version={technicalContext.expected_request_version}
                  {" · "}expected_operational_version={technicalValue(technicalContext.expected_operational_version)}
                </small>
              </article>
              <article>
                <span>Enveloppe / concurrence</span>
                <strong>{technicalValue(technicalContext.envelope_decision)}</strong>
                <small>
                  révision={technicalValue(technicalContext.active_revision_id)}
                  {" · "}version approuvée={technicalValue(technicalContext.approved_request_version)}
                  {" · "}version opérationnelle={technicalValue(technicalContext.operational_version)}
                </small>
              </article>
              <article>
                <span>Résolution backend</span>
                <strong>{technicalValue(technicalContext.envelope_reason)}</strong>
                <small>{technicalContext.diagnostics.length} diagnostic(s) backend</small>
              </article>
            </div>

            {technicalContext.diagnostics.length > 0 && (
              <div className="demand-detail-diagnostics">
                <strong>Diagnostics backend</strong>
                <ul>
                  {technicalContext.diagnostics.map((diagnostic) => <li key={diagnostic}>{diagnostic}</li>)}
                </ul>
              </div>
            )}

            {detail.materialized_plan.requirements.length > 0 && (
              <div className="demand-detail-technical-list">
                <strong>Besoins humains matérialisés</strong>
                {detail.materialized_plan.requirements.map((requirement) => (
                  <article key={requirement.requirement_id}>
                    <span>{requirement.requirement_id} · segment {requirement.segment_id}</span>
                    <small>
                      ligne={technicalValue(requirement.source_request_line_id)}
                      {" · "}révision={technicalValue(requirement.approval_revision_id)}
                      {" · "}entrée={technicalValue(requirement.approved_entry_key)}
                    </small>
                  </article>
                ))}
              </div>
            )}

            {detail.materialized_plan.asset_requirements.length > 0 && (
              <div className="demand-detail-technical-list">
                <strong>Actifs matérialisés</strong>
                {detail.materialized_plan.asset_requirements.map((requirement) => (
                  <article key={requirement.requirement_id}>
                    <span>{requirement.requirement_id} · {requirement.asset_type_code} — {requirement.asset_type_label}</span>
                    <small>
                      ligne={requirement.source_request_line_id}
                      {" · "}période={technicalValue(requirement.source_period_id)}
                      {" · "}révision={technicalValue(requirement.approval_revision_id)}
                      {" · "}entrée={requirement.approved_entry_key}
                    </small>
                  </article>
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </section>
  );
}
