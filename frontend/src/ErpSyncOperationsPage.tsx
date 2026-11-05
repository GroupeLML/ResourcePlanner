import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "./api";
import { useAuth } from "./AuthContext";
import {
  ErpSyncBaselines,
  ErpSyncCompensationPreview,
  ErpSyncOperationsSnapshot,
  ErpSyncPlausibilityReview,
  approveErpBaseline,
  approveErpPlausibilityException,
  confirmErpCompensation,
  continueErpPlausibilityReview,
  declineErpPlausibilityReview,
  getErpSyncBaselines,
  getErpSyncOperations,
  getErpSyncPlausibilityReview,
  previewErpCompensation,
  retryErpSync,
  runErpSyncNow,
} from "./erpSyncOperationsApi";

import "./erp-sync-operations.css";

const ERROR_MESSAGES: Record<string, string> = {
  erp_sync_recovery_required: "Une récupération ERP est requise avant de lancer de nouveaux travaux.",
  erp_sync_run_active: "Une autre tentative ERP est déjà active.",
  erp_sync_command_not_claimed: "La commande ERP n’a pas pu acquérir le contrôle d’exécution.",
  erp_sync_retry_not_claimed: "La reprise ERP n’a pas pu acquérir le contrôle d’exécution.",
  erp_sync_configuration_incompatible: "La configuration ERP active n’est pas compatible avec l’autorité SQL.",
  erp_sync_protocol_incompatible: "Le protocole ERP actif n’est pas compatible avec l’autorité SQL.",
  erp_sync_plausibility_review_not_pending: "Cette revue de plausibilité n’est plus active. Actualisez l’état.",
  erp_sync_plausibility_exception_scope_mismatch: "Le périmètre d’exception n’est plus valide pour la source courante.",
  erp_sync_compensation_preview_stale: "La prévisualisation de compensation est périmée. Générez-en une nouvelle.",
  erp_sync_compensation_current_state_changed: "L’état local a changé depuis la prévisualisation; la compensation est refusée.",
  erp_sync_compensation_dependency_changed: "Une dépendance locale interdit maintenant cette compensation.",
  erp_sync_compensation_evidence_expired: "La preuve nécessaire à la compensation a expiré.",
  erp_sync_evidence_capability_unavailable: "Le stockage de preuve ERP est indisponible.",
  erp_sync_evidence_storage_budget_unavailable: "Le budget de stockage des preuves ERP est indisponible.",
  erp_sync_evidence_storage_reservation_exceeded: "La réservation de stockage des preuves ERP est dépassée.",
  erp_sync_operations_database_unavailable: "L’état opérationnel ERP ne peut pas être lu depuis SQL.",
  erp_sync_sensitive_authority_required: "Cette action exige les autorisations ERP sensibles.",
  permission_denied: "Vous n’avez pas l’autorisation requise pour cette action.",
};

function messageFor(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return ERROR_MESSAGES[reason.code ?? ""] ?? (
      reason.message + (reason.code ? " (" + reason.code + ")" : "")
    );
  }
  return reason instanceof Error ? reason.message : fallback;
}

function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatAge(seconds: number | null) {
  if (seconds === null) return "Inconnue";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? hours + " h " + minutes + " min" : minutes + " min";
}

function statusClass(value: string) {
  const normalized = value.toLocaleLowerCase("fr-CA").replace(/[^a-z0-9]+/g, "-");
  return "erp-sync-status is-" + normalized;
}

function AttemptSummary({
  title,
  attempt,
}: {
  title: string;
  attempt: ErpSyncOperationsSnapshot["last_attempt"];
}) {
  return (
    <article className="erp-sync-card">
      <span className="eyebrow">{title}</span>
      {attempt ? (
        <>
          <strong>{attempt.status} · {attempt.phase}</strong>
          <small>Tentative {attempt.id}</small>
          <small>{formatDateTime(attempt.finished_at ?? attempt.started_at)}</small>
          <div className="erp-sync-metrics">
            <span>Projets reçus <b>{attempt.projects_received}</b></span>
            <span>Tâches reçues <b>{attempt.tasks_received ?? "—"}</b></span>
            <span>Rejets tâches <b>{attempt.tasks_rejected ?? "—"}</b></span>
          </div>
          {attempt.error_code && <code>{attempt.error_code}</code>}
        </>
      ) : (
        <strong>Aucune tentative</strong>
      )}
    </article>
  );
}

export default function ErpSyncOperationsPage() {
  const { can } = useAuth();
  const sensitive = can("sync_projects") && can("admin_settings");
  const [operations, setOperations] = useState<ErpSyncOperationsSnapshot | null>(null);
  const [baselines, setBaselines] = useState<ErpSyncBaselines | null>(null);
  const [review, setReview] = useState<ErpSyncPlausibilityReview | null>(null);
  const [preview, setPreview] = useState<ErpSyncCompensationPreview | null>(null);
  const [reason, setReason] = useState("Revue opérationnelle ERP");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextOperations, nextBaselines] = await Promise.all([
        getErpSyncOperations(signal),
        getErpSyncBaselines(signal),
      ]);
      setOperations(nextOperations);
      setBaselines(nextBaselines);
      if (
        nextOperations.current_attempt_id
        && nextOperations.current_phase === "PLAUSIBILITY_REVIEW_REQUIRED"
      ) {
        try {
          setReview(await getErpSyncPlausibilityReview(nextOperations.current_attempt_id, signal));
        } catch (reason: unknown) {
          if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
          setReview(null);
        }
      } else {
        setReview(null);
      }
      setError(null);
    } catch (reason: unknown) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setError(messageFor(reason, "Impossible de charger l’état opérationnel ERP."));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!operations?.current_attempt_id) return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [operations?.current_attempt_id, refresh]);

  const retryAttempt = operations?.last_partial_or_failed_attempt ?? null;
  const compensationAttemptId = (
    operations?.recovery_state.attempt_id
    ?? operations?.last_partial_or_failed_attempt?.id
    ?? null
  );
  const newWorkBlocked = operations?.recovery_state.state === "REQUIRED";
  const currentActive = Boolean(operations?.current_attempt_id);

  const projectProblems = useMemo(
    () => operations?.projects.filter((project) => (
      project.quality_state !== "VALID"
      || project.result_status === "rejected"
      || Boolean(project.error_code)
    )) ?? [],
    [operations?.projects],
  );

  async function command(label: string, action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(success);
      setPreview(null);
      await refresh();
    } catch (reason: unknown) {
      setError(messageFor(reason, "La commande ERP a échoué."));
      await refresh();
    } finally {
      setBusy(null);
    }
  }

  async function runNow() {
    await command(
      "run-now",
      () => runErpSyncNow(),
      "RUN_NOW accepté par le contrôle ERP.",
    );
  }

  async function retry() {
    if (!retryAttempt) return;
    if (!window.confirm(
      "Relancer la tentative " + retryAttempt.id
      + " ? Une RETRY crée une nouvelle tentative et relit les sources requises.",
    )) return;
    await command(
      "retry",
      () => retryErpSync(retryAttempt.occurrence_id),
      "RETRY acceptée; une nouvelle tentative a été demandée.",
    );
  }

  async function approveBaseline(manifestId: string, sourceType: string) {
    if (!window.confirm(
      "Approuver la source " + sourceType
      + " comme baseline de plausibilité ? Cette décision est auditée.",
    )) return;
    await command(
      "baseline-" + manifestId,
      () => approveErpBaseline(manifestId, reason),
      "Baseline " + sourceType + " approuvée.",
    );
  }

  async function approveException(manifestId: string, scope: string) {
    if (!review) return;
    if (!window.confirm(
      "Autoriser l’exception de plausibilité " + scope
      + " pour cette tentative et cette source scellée ?",
    )) return;
    await command(
      "exception-" + manifestId + "-" + scope,
      () => approveErpPlausibilityException(review.attempt_id, {
        manifest_id: manifestId,
        threshold_scope: scope,
        reason,
      }),
      "Exception " + scope + " approuvée pour la tentative courante.",
    );
  }

  async function continueReview() {
    if (!review) return;
    if (!window.confirm(
      "Continuer la tentative " + review.attempt_id
      + " avec les décisions de revue actuellement approuvées ?",
    )) return;
    await command(
      "review-continue",
      () => continueErpPlausibilityReview(review.attempt_id),
      "La même tentative ERP reprend à partir de sa source scellée.",
    );
  }

  async function declineReview() {
    if (!review) return;
    if (!window.confirm(
      "Abandonner la tentative " + review.attempt_id
      + " ? Elle deviendra terminale; une RETRY ultérieure créera une nouvelle tentative et relira les sources.",
    )) return;
    await command(
      "review-decline",
      () => declineErpPlausibilityReview(review.attempt_id, reason),
      "La revue a été refusée et l’ancienne tentative est maintenant terminale.",
    );
  }

  async function buildPreview() {
    if (!compensationAttemptId || busy) return;
    setBusy("compensation-preview");
    setError(null);
    setNotice(null);
    try {
      setPreview(await previewErpCompensation(compensationAttemptId, reason));
    } catch (reason: unknown) {
      setPreview(null);
      setError(messageFor(reason, "La prévisualisation de compensation a échoué."));
    } finally {
      setBusy(null);
    }
  }

  async function confirmCompensationAction() {
    if (!preview?.can_confirm) return;
    if (!window.confirm(
      "Confirmer la compensation de " + preview.eligible_records
      + " enregistrement(s) pour " + preview.attempt_id
      + " ? Cette action modifie les champs ERP contrôlés et sera auditée.",
    )) return;
    await command(
      "compensation-confirm",
      () => confirmErpCompensation(
        preview.attempt_id,
        preview.preview_hash,
        reason,
      ),
      "Compensation ERP confirmée.",
    );
  }

  if (loading && !operations) {
    return (
      <section className="erp-sync-page">
        <div className="erp-sync-card">Chargement des opérations ERP…</div>
      </section>
    );
  }

  if (!operations) {
    return (
      <section className="erp-sync-page">
        <div className="error-panel">
          <strong>Opérations ERP indisponibles</strong>
          <span>{error}</span>
        </div>
      </section>
    );
  }

  return (
    <section className="erp-sync-page">
      <header className="erp-sync-heading">
        <div>
          <span className="eyebrow">Acumatica</span>
          <h2>Opérations ERP</h2>
          <p>
            État et contrôles manuels du pipeline ERP. Les états, la fraîcheur et l’admission
            proviennent du backend; cette page ne recalcule pas l’autorité SQL.
          </p>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={Boolean(busy)}>
          Actualiser
        </button>
      </header>

      {error && (
        <div className="error-panel" role="alert">
          <strong>Action refusée ou indisponible</strong>
          <span>{error}</span>
        </div>
      )}
      {notice && <div className="erp-sync-notice" role="status">{notice}</div>}

      <div className="erp-sync-status-grid">
        <article className="erp-sync-card">
          <span className="eyebrow">Scheduler</span>
          <strong className={statusClass(operations.scheduler_status)}>{operations.scheduler_status}</strong>
          <small>Observation : {operations.scheduler_observation_status}</small>
          <small>Prochain run : {formatDateTime(operations.next_scheduled_run)}</small>
        </article>
        <article className="erp-sync-card">
          <span className="eyebrow">Automatisation</span>
          <strong>{operations.automation_enabled ? "Configurée active" : "Configurée désactivée"}</strong>
          <small>Capacité automatique : {operations.automatic_execution_available ? "disponible" : "indisponible"}</small>
          <small>Le mode manuel reste soumis à l’admission backend.</small>
        </article>
        <article className="erp-sync-card">
          <span className="eyebrow">Fraîcheur</span>
          <strong className={statusClass(operations.freshness_status)}>{operations.freshness_status}</strong>
          <small>Âge du dernier succès complet : {formatAge(operations.freshness_age_seconds)}</small>
          <small>
            Échecs consécutifs : {operations.consecutive_failures}
            {operations.repeated_failure ? " · signal répété" : ""}
          </small>
        </article>
        <article className="erp-sync-card">
          <span className="eyebrow">Recovery</span>
          <strong className={statusClass(operations.recovery_state.state)}>{operations.recovery_state.state}</strong>
          <small>{operations.recovery_state.reason_code ?? "Aucun motif bloquant"}</small>
          <small>{operations.recovery_state.pinned ? "Preuve épinglée" : "Preuve non épinglée"}</small>
        </article>
        <article className="erp-sync-card">
          <span className="eyebrow">Preuves / stockage</span>
          <strong className={statusClass(operations.evidence.status)}>{operations.evidence.status}</strong>
          <small>{operations.evidence.source_manifests} source(s) · {operations.evidence.checkpoint_manifests} checkpoint(s)</small>
          <small>{operations.evidence.manifests_pinned} épinglée(s) · {operations.evidence.manifests_expired} expirée(s)</small>
        </article>
        <article className="erp-sync-card">
          <span className="eyebrow">Compatibilité</span>
          <strong className={statusClass(operations.compatibility.status)}>{operations.compatibility.status}</strong>
          <small>Protocole : {String(operations.compatibility.protocol_compatible)}</small>
          <small>Configuration : {String(operations.compatibility.configuration_compatible)}</small>
        </article>
      </div>

      {operations.readiness_blockers.length > 0 && (
        <div className="erp-sync-card erp-sync-blockers">
          <strong>Bloqueurs opérationnels</strong>
          <ul>
            {operations.readiness_blockers.map((item) => <li key={item}><code>{item}</code></li>)}
          </ul>
        </div>
      )}

      <div className="erp-sync-attempt-grid">
        <AttemptSummary title="Dernière tentative" attempt={operations.last_attempt} />
        <AttemptSummary title="Dernier succès complet" attempt={operations.last_complete_success} />
        <AttemptSummary title="Dernier partiel / échec" attempt={operations.last_partial_or_failed_attempt} />
      </div>

      <article className="erp-sync-card">
        <div className="erp-sync-section-heading">
          <div>
            <span className="eyebrow">Commandes manuelles</span>
            <h3>RUN_NOW et RETRY</h3>
          </div>
          {operations.current_attempt_id && (
            <span className={statusClass("IN_PROGRESS")}>
              {operations.current_phase ?? "Tentative active"} · {operations.current_attempt_id}
            </span>
          )}
        </div>
        <p>
          Le backend reste l’autorité d’admission. Un scheduler automatique désactivé n’autorise
          ni n’interdit à lui seul une commande manuelle.
        </p>
        <div className="erp-sync-actions">
          <button
            className="primary-button"
            type="button"
            disabled={Boolean(busy) || newWorkBlocked || currentActive}
            onClick={() => void runNow()}
          >
            {busy === "run-now" ? "Admission…" : "Lancer RUN_NOW"}
          </button>
          <button
            type="button"
            disabled={Boolean(busy) || !retryAttempt || newWorkBlocked || currentActive}
            onClick={() => void retry()}
          >
            {busy === "retry" ? "Admission…" : "RETRY explicite"}
          </button>
        </div>
      </article>

      <article className="erp-sync-card">
        <div className="erp-sync-section-heading">
          <div>
            <span className="eyebrow">Plausibilité</span>
            <h3>Baselines et revue</h3>
          </div>
          <span>{sensitive ? "Autorité sensible disponible" : "Lecture seulement"}</span>
        </div>
        <div className="erp-sync-baselines">
          {(["PROJECTS", "TASKS"] as const).map((sourceType) => {
            const baseline = baselines?.sources[sourceType] ?? null;
            return (
              <div key={sourceType}>
                <strong>{sourceType}</strong>
                <span>
                  {baseline
                    ? "Baseline approuvée · " + baseline.row_count + " lignes"
                    : "Aucune baseline approuvée"}
                </span>
                {baseline && (
                  <small>
                    Approuvée par {baseline.approved_by} · {formatDateTime(baseline.approved_at)}
                  </small>
                )}
              </div>
            );
          })}
        </div>

        {review ? (
          <>
            <label className="erp-sync-reason">
              Raison auditée
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={1000}
              />
            </label>
            <div className="erp-sync-review-sources">
              {review.sources.map((source) => {
                const sourceKey = source.source_type as "PROJECTS" | "TASKS";
                const baseline = baselines?.sources[sourceKey] ?? null;
                return (
                  <div key={source.manifest_id} className="erp-sync-review-source">
                    <strong>{source.source_type} · {source.row_count} lignes</strong>
                    <small>Manifest {source.manifest_id}</small>
                    {source.diagnostics.map((item) => <code key={item}>{item}</code>)}
                    {sensitive && !baseline && (
                      <button
                        type="button"
                        disabled={Boolean(busy) || !reason.trim()}
                        onClick={() => void approveBaseline(source.manifest_id, source.source_type)}
                      >
                        Approuver comme baseline
                      </button>
                    )}
                    {sensitive && review.allowed_exception_scopes
                      .filter((scope) => scope.manifest_id === source.manifest_id)
                      .map((scope) => (
                        <button
                          type="button"
                          key={scope.manifest_id + "-" + scope.threshold_scope}
                          disabled={Boolean(busy) || !reason.trim()}
                          onClick={() => void approveException(scope.manifest_id, scope.threshold_scope)}
                        >
                          Autoriser l’exception {scope.threshold_scope}
                        </button>
                      ))}
                  </div>
                );
              })}
            </div>
            {sensitive ? (
              <div className="erp-sync-actions">
                <button
                  className="primary-button"
                  type="button"
                  disabled={Boolean(busy)}
                  onClick={() => void continueReview()}
                >
                  Continuer la même tentative
                </button>
                <button
                  className="danger-button"
                  type="button"
                  disabled={Boolean(busy) || !reason.trim()}
                  onClick={() => void declineReview()}
                >
                  Refuser / abandonner la tentative
                </button>
              </div>
            ) : (
              <p>Les décisions de baseline, exception et revue exigent également <code>admin_settings</code>.</p>
            )}
          </>
        ) : (
          <p>Aucune tentative n’attend actuellement une revue de plausibilité.</p>
        )}
      </article>

      <article className="erp-sync-card">
        <div className="erp-sync-section-heading">
          <div>
            <span className="eyebrow">Recovery</span>
            <h3>Compensation contrôlée</h3>
          </div>
          <span>{operations.recovery_state.state}</span>
        </div>
        <p>
          La prévisualisation est en lecture. La confirmation exige un hash encore valide,
          une autorité sensible et une confirmation explicite.
        </p>
        {operations.recovery_state.state === "REQUIRED" && (
          <div className="error-panel">
            <strong>Nouveau travail bloqué</strong>
            <span>{operations.recovery_state.reason_code ?? "Une récupération contrôlée est requise."}</span>
          </div>
        )}
        {operations.recovery_state.state === "EXPIRED" && (
          <div className="error-panel">
            <strong>Preuve expirée</strong>
            <span>Le matériel de récupération n’est plus utilisable pour cette compensation.</span>
          </div>
        )}
        <label className="erp-sync-reason">
          Raison auditée
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={1000}
          />
        </label>
        {sensitive ? (
          <div className="erp-sync-actions">
            <button
              type="button"
              disabled={Boolean(busy) || !compensationAttemptId || !reason.trim()}
              onClick={() => void buildPreview()}
            >
              Prévisualiser la compensation
            </button>
            <button
              className="danger-button"
              type="button"
              disabled={Boolean(busy) || !preview?.can_confirm}
              onClick={() => void confirmCompensationAction()}
            >
              Confirmer la compensation
            </button>
          </div>
        ) : (
          <p>La compensation exige <code>sync_projects</code> et <code>admin_settings</code>.</p>
        )}
        {preview && (
          <div className="erp-sync-preview">
            <strong>{preview.can_confirm ? "Prévisualisation confirmable" : "Prévisualisation bloquée"}</strong>
            <span>{preview.eligible_records} enregistrement(s) admissible(s)</span>
            <span>{preview.conflict_count} conflit(s)</span>
            {preview.conflicts.map((item) => <code key={item}>{item}</code>)}
            <small>Hash de confirmation : {preview.preview_hash}</small>
          </div>
        )}
      </article>

      <article className="erp-sync-card">
        <span className="eyebrow">Projets à surveiller</span>
        <h3>Qualité / échecs</h3>
        {projectProblems.length === 0 ? (
          <p>Aucun projet en état stale, quarantiné ou rejeté dans la projection courante.</p>
        ) : (
          <div className="erp-sync-project-list">
            {projectProblems.map((project) => (
              <div key={project.project_number}>
                <strong>{project.project_number}</strong>
                <span>
                  {project.quality_state}
                  {project.result_status ? " · " + project.result_status : ""}
                </span>
                <code>{project.error_code ?? project.reason_code ?? "diagnostic indisponible"}</code>
              </div>
            ))}
          </div>
        )}
      </article>
    </section>
  );
}
