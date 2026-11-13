import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import RoleCards from './RoleCards'
import type {
  AttentionCenter,
  AttentionItem,
  CockpitConfig,
  Dashboard,
  HandoffProjection,
  ExecutionControl,
  FlowAnalyticsReport,
  Job,
  PipelineReconciliation,
  RoadmapWritebackPreview,
  PipelineStep,
  PullRequest,
  Run,
} from './types'

function latestRun(pr: PullRequest | null): Run | null {
  return pr?.runs?.[0] ?? null
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('fr-CA', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value))
}

function formatMinutes(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 60) return `${value} min`
  const hours = Math.floor(value / 60)
  const minutes = value % 60
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`
}

function StateBadge({ value }: { value: string }) {
  const tone =
    value === 'CI_RED' ||
    value === 'BLOCKED' ||
    value === 'STALLED' ||
    value === 'STALLED_CONFIRMED' ||
    value === 'POSSIBLE_STALL'
      ? 'danger'
      : value === 'MERGEABLE' || value === 'DONE'
        ? 'success'
        : value === 'CI_RUNNING'
          ? 'progress'
          : 'neutral'

  return <span className={`state-badge ${tone}`}>{value}</span>
}

function PipelineRoadmapRow({
  step,
  active = false,
  parallel = false,
}: {
  step: PipelineStep
  active?: boolean
  parallel?: boolean
}) {
  const displayKey =
    step.kind === 'WORK' && /^\d/.test(step.key) ? `#${step.key}` : step.key
  const title = step.title
    .replace(/^En\s+parall[eè]le[^:]*:\s*/i, '')
    .replace(
      step.kind === 'WORK'
        ? new RegExp(`^#${step.key}\\s*(?:✅\\s*)?(?:[—–-]\\s*)?`, 'i')
        : /$^/,
      '',
    )
    .trim()

  return (
    <div className={`roadmap-row ${active ? 'active' : ''}`}>
      <span className={`roadmap-mark ${active || parallel ? 'current' : ''}`}>
        {active ? '→' : parallel ? '↗' : '·'}
      </span>
      <div>
        <strong>{displayKey}</strong>
        <small>{title || step.title}</small>
      </div>
    </div>
  )
}

function RoadmapReconciliationPanel({
  reconciliation,
  repo,
  pipelineIssue,
  onApplied,
}: {
  reconciliation: PipelineReconciliation
  repo: string
  pipelineIssue: number
  onApplied: () => Promise<void>
}) {
  const [preview, setPreview] = useState<RoadmapWritebackPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [applyLoading, setApplyLoading] = useState(false)
  const [writeError, setWriteError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [applied, setApplied] = useState(false)

  useEffect(() => {
    setPreview(null)
    setWriteError(null)
    setApplied(false)
  }, [reconciliation.status, reconciliation.proposal?.pipeline_block])

  if (reconciliation.status === 'legacy') return null

  const tone =
    reconciliation.status === 'coherent'
      ? 'coherent'
      : reconciliation.status === 'stale'
        ? 'stale'
        : reconciliation.status === 'invalid'
          ? 'invalid'
          : 'attention'
  const label =
    reconciliation.status === 'coherent'
      ? 'COHÉRENT'
      : reconciliation.status === 'stale'
        ? 'ROADMAP STALE'
        : reconciliation.status === 'invalid'
          ? 'INVALID'
          : 'ATTENTION'

  function apiError(payload: unknown, fallback: string): string {
    if (
      payload &&
      typeof payload === 'object' &&
      'detail' in payload
    ) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string') return detail
      if (
        detail &&
        typeof detail === 'object' &&
        'message' in detail &&
        typeof (detail as { message?: unknown }).message === 'string'
      ) {
        const typed = detail as { code?: unknown; message: string }
        return typeof typed.code === 'string'
          ? `${typed.code} — ${typed.message}`
          : typed.message
      }
    }
    return fallback
  }

  async function loadPreview() {
    setPreviewLoading(true)
    setWriteError(null)
    setApplied(false)
    try {
      const response = await fetch(
        `/api/roadmap-writeback/preview?repo=${encodeURIComponent(repo)}`,
        { method: 'POST' },
      )
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(apiError(payload, `Erreur HTTP ${response.status}`))
      }
      setPreview(payload as RoadmapWritebackPreview)
    } catch (caught) {
      setPreview(null)
      setWriteError(
        caught instanceof Error ? caught.message : 'Erreur de preview inconnue',
      )
    } finally {
      setPreviewLoading(false)
    }
  }

  async function applyWriteback() {
    if (!preview) return
    setApplyLoading(true)
    setWriteError(null)
    try {
      const response = await fetch(
        `/api/roadmap-writeback/apply?repo=${encodeURIComponent(repo)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expected_updated_at: preview.expected_updated_at,
            expected_body_sha256: preview.expected_body_sha256,
            expected_proposal_sha256: preview.proposal_sha256,
            confirm: true,
          }),
        },
      )
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(apiError(payload, `Erreur HTTP ${response.status}`))
      }
      setApplied(true)
      setPreview(null)
      await onApplied()
    } catch (caught) {
      setWriteError(
        caught instanceof Error ? caught.message : 'Erreur de writeback inconnue',
      )
      if (
        caught instanceof Error &&
        /ROADMAP_CHANGED|PROPOSAL_CHANGED|changé|changed|nouveau diff|recharge/i.test(caught.message)
      ) {
        setPreview(null)
      }
    } finally {
      setApplyLoading(false)
    }
  }

  async function copyProposal() {
    const block = preview?.proposed_block ?? reconciliation.proposal?.pipeline_block
    if (!block) return
    await navigator.clipboard.writeText(block)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  return (
    <section className={`reconciliation-panel ${tone}`}>
      <div className="reconciliation-header">
        <div>
          <span className="panel-kicker">COHÉRENCE ROADMAP</span>
          <h2>Contrat #{pipelineIssue} ↔ réalité GitHub</h2>
        </div>
        <span className={`reconciliation-status ${tone}`}>{label}</span>
      </div>

      <p className="reconciliation-summary">{reconciliation.summary}</p>

      {reconciliation.findings.length > 0 && (
        <div className="reconciliation-findings">
          {reconciliation.findings.map((finding) => (
            <article key={`${finding.code}:${finding.key}`} className={finding.severity}>
              <strong>{finding.key}</strong>
              <span>{finding.message}</span>
              {finding.evidence.length > 0 && (
                <div className="reconciliation-evidence">
                  {finding.evidence.map((evidence) =>
                    evidence.url ? (
                      <a
                        key={`${finding.code}:${evidence.label}`}
                        href={evidence.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {evidence.label} ↗
                      </a>
                    ) : (
                      <span key={`${finding.code}:${evidence.label}`}>
                        {evidence.label}
                      </span>
                    ),
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {reconciliation.proposal && (
        <div className="reconciliation-proposal">
          <div>
            <strong>Proposition déterministe</strong>
            <span>
              {reconciliation.proposal.changes
                .map((change) => `${change.key}: ${change.from} → ${change.to}`)
                .join(' · ')}
            </span>
          </div>
          <div className="actions">
            <button
              className="primary"
              type="button"
              onClick={() => void loadPreview()}
              disabled={previewLoading || applyLoading}
            >
              {previewLoading ? 'Préparation…' : preview ? 'Rafraîchir le diff' : 'Voir le diff'}
            </button>
            <button
              className="button"
              type="button"
              onClick={() => void copyProposal()}
              disabled={applyLoading}
            >
              {copied ? 'Bloc copié ✓' : 'Copier le bloc'}
            </button>
          </div>
        </div>
      )}

      {writeError && (
        <div className="writeback-error">
          <strong>Writeback non appliqué</strong>
          <span>{writeError}</span>
        </div>
      )}

      {applied && (
        <div className="writeback-success">
          ✓ #{pipelineIssue} a été mis à jour et le dashboard a été rechargé.
        </div>
      )}

      {preview && (
        <div className="writeback-preview">
          <div className="writeback-preview-header">
            <div>
              <span className="section-label">SAFE WRITEBACK · PREVIEW OBLIGATOIRE</span>
              <strong>
                {preview.changes
                  .map((change) => `${change.key}: ${change.from} → ${change.to}`)
                  .join(' · ')}
              </strong>
            </div>
            <span className="writeback-lock">CAS · updated_at + SHA-256</span>
          </div>

          <pre className="writeback-diff">{preview.diff}</pre>

          <div className="writeback-guards">
            <span>✓ uniquement COCKPIT_PIPELINE_V1</span>
            <span>✓ preuve Reconciler = stale</span>
            <span>✓ proposition recalculée avant PATCH</span>
            <span>✓ refus si #{pipelineIssue} change</span>
          </div>

          <div className="writeback-confirm">
            <div>
              <strong>Confirmation explicite requise</strong>
              <small>
                Le backend relira #{pipelineIssue} et recalculera la preuve GitHub avant toute écriture.
                Une modification concurrente produit ROADMAP_CHANGED et aucune écriture.
              </small>
            </div>
            <button
              className="danger-action"
              type="button"
              onClick={() => void applyWriteback()}
              disabled={applyLoading}
            >
              {applyLoading ? 'Validation GitHub…' : `Confirmer et appliquer à #${pipelineIssue}`}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

function AttentionCenterPanel({
  attention,
  handoff,
  copiedId,
  onPrepare,
}: {
  attention: AttentionCenter
  handoff: HandoffProjection
  copiedId: string | null
  onPrepare: (item: AttentionItem) => void
}) {
  const roleSummary = Object.entries(attention.roles).filter(
    ([, counts]) => counts.actions > 0 || counts.watches > 0,
  )

  return (
    <section className={`attention-center ${attention.status.toLowerCase()}`}>
      <div className="attention-header">
        <div>
          <span className="panel-kicker">ATTENTION CENTER</span>
          <h2>
            {attention.status === 'CLEAR'
              ? 'Tout roule'
              : `${attention.action_count} action(s) · ${attention.watch_count} attente(s)`}
          </h2>
          <p>{attention.summary}</p>
        </div>
        <span className={`attention-overall ${attention.status.toLowerCase()}`}>
          {attention.status}
        </span>
      </div>

      {roleSummary.length > 0 && (
        <div className="attention-role-summary">
          {roleSummary.map(([role, counts]) => (
            <span key={role}>
              {counts.label}: {counts.actions} action
              {counts.actions === 1 ? '' : 's'} · {counts.watches} attente
              {counts.watches === 1 ? '' : 's'}
            </span>
          ))}
        </div>
      )}

      {attention.items.length > 0 ? (
        <div className="attention-list">
          {attention.items.map((item) => {
            const pack = handoff.packs[item.role]
            return (
              <article key={item.id} className={item.level.toLowerCase()}>
                <div className="attention-item-main">
                  <div className="attention-item-title">
                    <span className={`attention-level ${item.level.toLowerCase()}`}>
                      {item.level}
                    </span>
                    <strong>
                      {item.key} · {item.title}
                    </strong>
                  </div>
                  <span className="attention-role">{item.role_label}</span>
                  <p>{item.detail}</p>
                  <div className="attention-action">
                    <span>Action</span>
                    <strong>{item.action}</strong>
                  </div>
                </div>

                <div className="attention-item-actions">
                  {pack?.prompt && (
                    <button
                      className="primary"
                      type="button"
                      onClick={() => onPrepare(item)}
                    >
                      {copiedId === item.id
                        ? 'Reprise copiée ✓'
                        : 'Préparer la reprise'}
                    </button>
                  )}
                  {item.primary_link && (
                    <a
                      className="button"
                      href={item.primary_link.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.primary_link.label} ↗
                    </a>
                  )}
                  {item.handoff_confidence && (
                    <span
                      className={`handoff-confidence ${item.handoff_confidence.toLowerCase()}`}
                    >
                      {item.handoff_confidence}
                    </span>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      ) : (
        <div className="attention-clear">
          <span>✓</span>
          <div>
            <strong>Aucune intervention requise</strong>
            <small>
              Les entrées apparaîtront automatiquement lorsqu'un signal GitHub
              nécessitera une action ou une surveillance.
            </small>
          </div>
        </div>
      )}
    </section>
  )
}

function ExecutionControllerPanel({
  execution,
  copied,
  onCopy,
}: {
  execution: ExecutionControl
  copied: boolean
  onCopy: () => void
}) {
  const tone =
    execution.phase === 'CI_RED' ||
    execution.phase === 'STALLED' ||
    execution.phase === 'PIPELINE_INVALID'
      ? 'danger'
      : execution.phase === 'ROADMAP_UPDATE_REQUIRED' ||
          execution.phase === 'DELIVERY_UNVERIFIED' ||
          execution.phase === 'POSSIBLE_STALL'
        ? 'warning'
        : execution.phase === 'READY_TO_MERGE'
          ? 'success'
          : execution.phase === 'CI_RUNNING'
            ? 'progress'
            : 'neutral'

  return (
    <section className={`execution-panel ${tone}`}>
      <div className="execution-main">
        <div className="execution-heading">
          <div>
            <span className="panel-kicker">CONTRÔLEUR D'EXÉCUTION</span>
            <h2>{execution.label}</h2>
          </div>
          <span className={`execution-phase ${tone}`}>{execution.phase}</span>
        </div>

        <p className="execution-summary">{execution.summary}</p>

        <div className="execution-next">
          <span>PROCHAINE ACTION · {execution.responsible_role}</span>
          <strong>{execution.next_action}</strong>
        </div>

        <pre className="prompt-preview execution-prompt">{execution.prompt}</pre>

        <div className="actions execution-actions">
          <button className="primary" type="button" onClick={onCopy}>
            {copied ? '✓ Prompt copié' : 'Copier le prompt de reprise'}
          </button>
          {execution.primary_link && (
            <a
              className="button"
              href={execution.primary_link.url}
              target="_blank"
              rel="noreferrer"
            >
              {execution.primary_link.label} ↗
            </a>
          )}
        </div>
      </div>

      <div className="execution-timeline">
        <div className="section-label">TIMELINE GITHUB</div>
        {execution.timeline.length ? (
          <div className="timeline-list">
            {execution.timeline.map((event) => (
              <article key={`${event.at}:${event.label}`}>
                <span className={`timeline-dot ${event.kind}`} />
                <div>
                  <strong>
                    {event.url ? (
                      <a href={event.url} target="_blank" rel="noreferrer">
                        {event.label}
                      </a>
                    ) : (
                      event.label
                    )}
                  </strong>
                  <small>{event.detail}</small>
                  <time>{formatDate(event.at)}</time>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty compact">Aucun événement GitHub pertinent.</div>
        )}
      </div>
    </section>
  )
}

function FlowAnalyticsPanel({ repo }: { repo: string }) {
  const [report, setReport] = useState<FlowAnalyticsReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setReport(null)
    setError(null)
  }, [repo])

  async function loadAnalytics() {
    if (!repo) return
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(
        `/api/flow-analytics?repo=${encodeURIComponent(repo)}&limit=12`,
      )
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail || `Erreur HTTP ${response.status}`)
      }
      setReport(payload as FlowAnalyticsReport)
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Erreur inconnue pendant l'analyse du flux",
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="flow-panel">
      <div className="flow-header">
        <div>
          <span className="panel-kicker">SANTÉ DU FLUX</span>
          <h2>Flow Analytics · Delivery History</h2>
          <p>
            Mesures reconstruites uniquement depuis les timestamps GitHub
            observables. Cette analyse est chargée séparément du polling du
            dashboard.
          </p>
        </div>
        <div className="flow-header-actions">
          {report && (
            <span className={`flow-status ${report.status}`}>
              {report.status.toUpperCase()}
            </span>
          )}
          <button type="button" onClick={() => void loadAnalytics()} disabled={loading}>
            {loading
              ? 'Analyse GitHub…'
              : report
                ? "Rafraîchir l'analyse"
                : "Charger l'analyse du flux"}
          </button>
        </div>
      </div>

      {error && (
        <div className="flow-error">
          <strong>Flow Analytics indisponible</strong>
          <span>{error}</span>
        </div>
      )}

      {!report && !error && !loading && (
        <div className="flow-placeholder">
          <strong>Chargement à la demande</strong>
          <span>
            Les workflows et commits historiques ne sont pas relus toutes les
            60 secondes. Lance l'analyse lorsque tu veux examiner le flux.
          </span>
        </div>
      )}

      {report?.status === 'unavailable' && (
        <div className="flow-placeholder">
          <strong>Historique non reconstructible</strong>
          <span>{report.reason || 'Aucune donnée exploitable.'}</span>
        </div>
      )}

      {report?.summary && (
        <>
          <div className="flow-kpis">
            <div>
              <span>Livraisons analysables</span>
              <strong>
                {report.summary.analyzable_count}/{report.summary.delivery_count}
              </strong>
            </div>
            <div>
              <span>Médiane totale observable</span>
              <strong>{formatMinutes(report.summary.median_total_observed_minutes)}</strong>
            </div>
            <div>
              <span>Commit → PR</span>
              <strong>{formatMinutes(report.summary.median_commit_to_pr_minutes)}</strong>
            </div>
            <div>
              <span>PR → CI verte</span>
              <strong>{formatMinutes(report.summary.median_pr_to_green_minutes)}</strong>
            </div>
            <div>
              <span>CI verte → merge</span>
              <strong>{formatMinutes(report.summary.median_green_to_merge_minutes)}</strong>
            </div>
            <div>
              <span>Tentatives validation</span>
              <strong>
                {report.summary.average_validation_attempts == null
                  ? '—'
                  : report.summary.average_validation_attempts}
              </strong>
              <small>
                rouges moy.{' '}
                {report.summary.average_red_attempts == null
                  ? '—'
                  : report.summary.average_red_attempts}
              </small>
            </div>
          </div>

          {report.summary.trend && (
            <div className="flow-trend">
              <strong>Tendance PR → CI verte</strong>
              <span>{report.summary.trend.description}</span>
            </div>
          )}

          <div className="flow-deliveries">
            {report.deliveries.map((delivery) => (
              <details className="flow-delivery" key={delivery.key}>
                <summary>
                  <div className="flow-delivery-title">
                    <span className={`flow-delivery-status ${delivery.status}`}>
                      {delivery.status}
                    </span>
                    <strong>{delivery.key}</strong>
                    <span>{delivery.title}</span>
                  </div>
                  <div className="flow-delivery-summary">
                    <strong>
                      {formatMinutes(delivery.metrics.total_observed_minutes)}
                    </strong>
                    <span>
                      {delivery.bottleneck
                        ? `ralentissement: ${delivery.bottleneck.label}`
                        : delivery.reason || 'données partielles'}
                    </span>
                  </div>
                </summary>

                <div className="flow-delivery-body">
                  <div className="flow-metric-grid">
                    <div>
                      <span>Commit → PR</span>
                      <strong>
                        {formatMinutes(delivery.metrics.commit_to_pr_minutes)}
                      </strong>
                    </div>
                    <div>
                      <span>PR → CI verte</span>
                      <strong>
                        {formatMinutes(delivery.metrics.pr_to_green_minutes)}
                      </strong>
                    </div>
                    <div>
                      <span>CI verte → merge</span>
                      <strong>
                        {formatMinutes(delivery.metrics.green_to_merge_minutes)}
                      </strong>
                    </div>
                    <div>
                      <span>Validation</span>
                      <strong>
                        {delivery.metrics.validation_attempts ?? '—'} tentative(s)
                      </strong>
                      <small>
                        {delivery.metrics.red_attempts ?? '—'} rouge(s)
                      </small>
                    </div>
                    <div>
                      <span>Récupération après rouge</span>
                      <strong>
                        {formatMinutes(delivery.metrics.red_recovery_minutes)}
                      </strong>
                    </div>
                    <div>
                      <span>PR → merge</span>
                      <strong>
                        {formatMinutes(delivery.metrics.pr_to_merge_minutes)}
                      </strong>
                    </div>
                  </div>

                  {delivery.pr && (
                    <div className="flow-pr-line">
                      <a href={delivery.pr.url} target="_blank" rel="noreferrer">
                        PR #{delivery.pr.number} · {delivery.pr.title} ↗
                      </a>
                      <span>
                        ouverte {formatDate(delivery.pr.created_at)} · fusionnée{' '}
                        {formatDate(delivery.pr.merged_at)}
                      </span>
                    </div>
                  )}

                  {delivery.attempts.length > 0 && (
                    <div className="flow-attempts">
                      <div className="section-label">CYCLES CI OBSERVABLES</div>
                      {delivery.attempts.map((attempt, index) => (
                        <article key={`${delivery.key}:${attempt.sha}`}>
                          <span className={`flow-attempt-state ${attempt.state}`}>
                            #{index + 1} {attempt.state}
                          </span>
                          <div>
                            <strong>{attempt.short_sha}</strong>
                            <small>
                              {formatDate(attempt.started_at)} →{' '}
                              {formatDate(attempt.completed_at)}
                            </small>
                            {attempt.failed_jobs.length > 0 && (
                              <small>
                                jobs rouges: {attempt.failed_jobs.join(', ')}
                              </small>
                            )}
                          </div>
                          {attempt.url && (
                            <a href={attempt.url} target="_blank" rel="noreferrer">
                              Run ↗
                            </a>
                          )}
                        </article>
                      ))}
                    </div>
                  )}

                  {delivery.timeline.length > 0 && (
                    <div className="flow-history">
                      <div className="section-label">TIMELINE DE LIVRAISON</div>
                      {delivery.timeline.map((event, index) => (
                        <div key={`${delivery.key}:${event.at}:${index}`}>
                          <span>{formatDate(event.at)}</span>
                          {event.url ? (
                            <a href={event.url} target="_blank" rel="noreferrer">
                              {event.label} ↗
                            </a>
                          ) : (
                            <strong>{event.label}</strong>
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {delivery.diagnostics.length > 0 && (
                    <div className="flow-diagnostics">
                      {delivery.diagnostics.map((diagnostic) => (
                        <span key={diagnostic}>• {diagnostic}</span>
                      ))}
                    </div>
                  )}
                </div>
              </details>
            ))}
          </div>

          <div className="flow-notes">
            {report.notes.map((note) => (
              <span key={note}>• {note}</span>
            ))}
          </div>
        </>
      )}
    </section>
  )
}

function JobLine({ job }: { job: Job }) {
  const running = job.status && job.status !== 'completed'
  const success = job.conclusion === 'success'
  const failed =
    Boolean(job.conclusion) &&
    job.conclusion !== 'success' &&
    job.conclusion !== 'skipped' &&
    job.conclusion !== 'neutral'

  const icon = running ? '…' : success ? '✓' : failed ? '✕' : '·'
  const tone = running ? 'running' : success ? 'good' : failed ? 'bad' : 'muted'

  return (
    <a
      className="job-line"
      href={job.url || undefined}
      target="_blank"
      rel="noreferrer"
    >
      <span className={`job-icon ${tone}`}>{icon}</span>
      <span>{job.name}</span>
      <span className="job-state">
        {job.status === 'completed' ? job.conclusion : job.status}
      </span>
    </a>
  )
}

export default function App() {
  const storedRepo = localStorage.getItem('dev-cockpit-repo') || ''
  const [repo, setRepo] = useState(storedRepo)
  const [repoDraft, setRepoDraft] = useState(storedRepo)
  const [data, setData] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [attentionCopiedId, setAttentionCopiedId] = useState<string | null>(null)

  async function load(targetRepo: string) {
    if (!targetRepo) return

    setLoading(true)
    setError(null)
    try {
      const response = await fetch(
        `/api/dashboard?repo=${encodeURIComponent(targetRepo)}`,
      )
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail || `Erreur HTTP ${response.status}`)
      }
      setData(payload as Dashboard)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    async function initialize() {
      try {
        const response = await fetch('/api/config')
        const config = (await response.json()) as CockpitConfig
        const initialRepo = storedRepo || config.repository
        setRepo(initialRepo)
        setRepoDraft(initialRepo)
      } catch {
        const fallback = storedRepo || 'tchi99/RessourcePlanner'
        setRepo(fallback)
        setRepoDraft(fallback)
      }
    }

    void initialize()
  }, [storedRepo])

  useEffect(() => {
    if (!repo) return
    void load(repo)
    const timer = window.setInterval(() => void load(repo), 60_000)
    return () => window.clearInterval(timer)
  }, [repo])

  function changeRepo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const value = repoDraft.trim()
    if (!value) return
    localStorage.setItem('dev-cockpit-repo', value)
    setRepo(value)
  }

  async function copyPrompt() {
    if (!data?.execution.prompt) return
    await navigator.clipboard.writeText(data.execution.prompt)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1400)
  }

  async function prepareAttention(item: AttentionItem) {
    const pack = data?.handoff.packs[item.role]
    if (!pack?.prompt) return
    await navigator.clipboard.writeText(pack.prompt)
    setAttentionCopiedId(item.id)
    window.setTimeout(() => setAttentionCopiedId(null), 1800)
  }

  const activeWork = data?.active_work ?? null
  const primaryPr = activeWork?.primary_pr ?? null
  const run = latestRun(primaryPr) ?? activeWork?.active_runs?.[0] ?? null
  const issueUrl = activeWork?.issue.url
  const stallDetails = activeWork?.stalled_details
  const stallTitle =
    activeWork?.stall_level === 'confirmed'
      ? '⚠ Dev probablement arrêté — CI rouge abandonnée'
      : activeWork?.stall_level === 'possible'
        ? '⚠ Travail possiblement interrompu'
        : '⚠ Dev probablement arrêté'

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">RESSOURCEPLANNER · DEV TOOL</p>
          <h1>Dev Cockpit</h1>
          <p className="subtitle">
            GitHub reste la source de vérité. Le cockpit observe, dérive et
            génère un prompt à copier.
          </p>
        </div>

        <form className="repo-form" onSubmit={changeRepo}>
          <label htmlFor="repo">Dépôt surveillé</label>
          <div className="repo-input-row">
            <input
              id="repo"
              value={repoDraft}
              onChange={(event) => setRepoDraft(event.target.value)}
              spellCheck={false}
            />
            <button type="submit">Charger</button>
            <button
              className="ghost"
              type="button"
              onClick={() => void load(repo)}
              disabled={loading}
              aria-label="Actualiser"
            >
              ↻
            </button>
          </div>
        </form>
      </header>

      {error && (
        <section className="error-panel">
          <strong>Configuration / GitHub</strong>
          <span>{error}</span>
        </section>
      )}

      {loading && !data && (
        <section className="loading-panel">Lecture de GitHub…</section>
      )}

      {data && (
        <AttentionCenterPanel
          attention={data.attention}
          handoff={data.handoff}
          copiedId={attentionCopiedId}
          onPrepare={(item) => void prepareAttention(item)}
        />
      )}

      <RoleCards dashboard={data} />

      {data && (
        <ExecutionControllerPanel
          execution={data.execution}
          copied={copied}
          onCopy={() => void copyPrompt()}
        />
      )}

      {data && <FlowAnalyticsPanel repo={repo} />}

      {data && !data.pipeline.valid && (
        <section className="error-panel pipeline-invalid">
          <strong>Pipeline #{data.pipeline_source.number} invalide</strong>
          <span>
            Le bloc COCKPIT_PIPELINE_V1 doit être corrigé avant de déterminer
            une étape active.
          </span>
          {data.pipeline.errors.map((message) => (
            <span key={message}>• {message}</span>
          ))}
        </section>
      )}

      {data && data.pipeline.valid && !data.active_work && (
        <section className="loading-panel">
          Aucune étape MAIN active n'est déclarée dans le pipeline canonique.
        </section>
      )}

      {data && data.pipeline.valid && data.active_work && (
        <>
          {data.warnings.length > 0 && (
            <div className="warnings">
              {data.warnings.map((warning) => (
                <div key={warning}>⚠ {warning}</div>
              ))}
            </div>
          )}

          <RoadmapReconciliationPanel
            reconciliation={data.reconciliation}
            repo={repo}
            pipelineIssue={data.pipeline_source.number}
            onApplied={() => load(repo)}
          />

          <section className="dashboard-grid">
            <article className="panel roadmap-panel">
              <div className="panel-header">
                <div>
                  <span className="panel-kicker">ROADMAP</span>
                  <h2>Maître #{data.roadmap.number}</h2>
                </div>
                <a href={data.roadmap.url} target="_blank" rel="noreferrer">
                  GitHub ↗
                </a>
              </div>

              {data.pipeline.steps.length > 0 ? (
                <div className="roadmap-list roadmap-pipeline">
                  <div className="roadmap-lane-label">MAINTENANT</div>
                  {data.pipeline.now ? (
                    <PipelineRoadmapRow step={data.pipeline.now} active />
                  ) : (
                    <div className="empty compact">Aucune étape principale restante.</div>
                  )}

                  {data.pipeline.parallel.length > 0 && (
                    <>
                      <div className="roadmap-lane-label">PARALLÈLE DISPONIBLE</div>
                      {data.pipeline.parallel.map((step) => (
                        <PipelineRoadmapRow
                          key={step.key}
                          step={step}
                          parallel
                        />
                      ))}
                    </>
                  )}

                  {data.pipeline.next.length > 0 && (
                    <>
                      <div className="roadmap-lane-label">ENSUITE</div>
                      {data.pipeline.next.map((step) => (
                        <PipelineRoadmapRow key={step.key} step={step} />
                      ))}
                    </>
                  )}
                </div>
              ) : (
                <div className="roadmap-list">
                  {data.roadmap.items.map((item) => {
                    const active = item.key === data.roadmap.effective_active
                    return (
                      <div
                        className={`roadmap-row ${active ? 'active' : ''}`}
                        key={item.key}
                      >
                        <span
                          className={`roadmap-mark ${item.done ? 'done' : active ? 'current' : ''}`}
                        >
                          {item.done ? '✓' : active ? '→' : '·'}
                        </span>
                        <div>
                          <strong>
                            {/^\d+$/.test(item.key) ? `#${item.key}` : item.key}
                          </strong>
                          <small>{item.title}</small>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              <div className="panel-foot">
                Issue active #{data.roadmap.active_issue}
                {data.pipeline.steps.length > 0
                  ? ` · pipeline #${data.pipeline_source.number} canonique`
                  : ` · sous-tranche ${data.active_work.subitem_key || '—'}`}
              </div>
            </article>

            <article className="panel active-panel">
              <div className="panel-header">
                <div>
                  <span className="panel-kicker">TRAVAIL ACTIF</span>
                  <h2>
                    #{data.active_work.issue_number}
                    {data.active_work.subitem_key
                      ? ` · ${data.active_work.subitem_key}`
                      : ''}
                  </h2>
                </div>
                <div className="state-stack">
                  {data.active_work.states.map((state) => (
                    <StateBadge key={state} value={state} />
                  ))}
                </div>
              </div>

              {data.active_work.stalled && stallDetails && (
                <div className="stalled-banner">
                  <strong>{stallTitle}</strong>
                  {stallDetails.ci_failed_minutes != null &&
                    data.active_work.stall_level === 'confirmed' && (
                      <span>
                        CI rouge depuis{' '}
                        {formatMinutes(stallDetails.ci_failed_minutes)}
                      </span>
                    )}
                  <span>
                    Dernière activité GitHub pertinente il y a{' '}
                    {formatMinutes(stallDetails.last_activity_minutes)}
                  </span>
                  <span>
                    {stallDetails.branch_present
                      ? 'Branche de travail détectée'
                      : 'Aucune branche associée'}
                    {' · '}
                    {stallDetails.pr_present ? 'PR détectée' : 'Aucune PR associée'}
                  </span>
                  <span>
                    {stallDetails.no_active_workflow
                      ? 'Aucun workflow actif'
                      : 'Workflow actif'}
                  </span>
                </div>
              )}

              <dl className="facts">
                <div>
                  <dt>Issue</dt>
                  <dd>
                    <a href={issueUrl} target="_blank" rel="noreferrer">
                      #{data.active_work.issue.number} ·{' '}
                      {data.active_work.issue.title}
                    </a>
                  </dd>
                </div>
                <div>
                  <dt>Sous-tranche</dt>
                  <dd>
                    {data.active_work.subitem_key || 'aucune sous-tranche explicite'}
                  </dd>
                </div>
                <div>
                  <dt>Enchaînement</dt>
                  <dd>
                    {data.active_work.can_chain_block
                      ? `autorisé dans ce bloc (${data.active_work.remaining_subitems.join(' → ')})`
                      : 'non déduit automatiquement'}
                  </dd>
                </div>
                <div>
                  <dt>PR</dt>
                  <dd>
                    {primaryPr ? (
                      <a href={primaryPr.url} target="_blank" rel="noreferrer">
                        #{primaryPr.number} · {primaryPr.title}
                      </a>
                    ) : (
                      'Aucune PR associée'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Branche</dt>
                  <dd>
                    {data.active_work.active_branch ? (
                      <a
                        className="mono"
                        href={data.active_work.active_branch.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {data.active_work.active_branch.name}
                      </a>
                    ) : (
                      '—'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Dernier commit</dt>
                  <dd>
                    {data.active_work.last_commit ? (
                      <a
                        href={data.active_work.last_commit.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span className="mono">
                          {data.active_work.last_commit.short_sha}
                        </span>{' '}
                        · {data.active_work.last_commit.message}
                      </a>
                    ) : (
                      '—'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Dernière activité</dt>
                  <dd>
                    {stallDetails?.last_activity_at
                      ? `${formatDate(stallDetails.last_activity_at)} · ${stallDetails.last_activity_source ?? 'GitHub'}`
                      : '—'}
                  </dd>
                </div>
                <div>
                  <dt>Mergeable</dt>
                  <dd>
                    {primaryPr
                      ? primaryPr.mergeable == null
                        ? 'calcul GitHub en attente'
                        : primaryPr.mergeable
                          ? 'oui'
                          : 'non'
                      : '—'}
                  </dd>
                </div>
                <div>
                  <dt>ADR</dt>
                  <dd>
                    <a
                      href={data.architecture.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {data.architecture.adrs.length} présent(s)
                    </a>
                    {data.architecture.referenced_adrs.length
                      ? ` · ${data.architecture.referenced_adrs.length} référencé(s) par le bloc`
                      : ''}
                  </dd>
                </div>
              </dl>
            </article>

            <article className="panel ci-panel">
              <div className="panel-header">
                <div>
                  <span className="panel-kicker">PR / CI</span>
                  <h2>
                    {run
                      ? `${run.name} #${run.run_number ?? ''}`
                      : 'Aucune CI associée'}
                  </h2>
                </div>
                {run?.url && (
                  <a href={run.url} target="_blank" rel="noreferrer">
                    Run ↗
                  </a>
                )}
              </div>

              <div className="ci-summary">
                <span>État CI</span>
                <strong>
                  {run
                    ? run.status === 'completed'
                      ? run.conclusion
                      : run.status
                    : '—'}
                </strong>
              </div>

              {data.active_work.failed_jobs.length > 0 && (
                <div className="failed-jobs">
                  <span>Jobs en échec</span>
                  <strong>{data.active_work.failed_jobs.join(', ')}</strong>
                </div>
              )}

              {run?.jobs?.length ? (
                <div className="jobs">
                  {run.jobs.map((job) => (
                    <JobLine key={job.id ?? job.name} job={job} />
                  ))}
                </div>
              ) : (
                <div className="empty">
                  Aucun job GitHub Actions associé à la PR active.
                </div>
              )}

              <div className="pr-list">
                <div className="section-label">
                  PR ouvertes · {data.open_prs.length}
                </div>
                {data.open_prs.length ? (
                  data.open_prs.map((pr) => {
                    const prRun = latestRun(pr)
                    return (
                      <a
                        className="pr-row"
                        key={pr.number}
                        href={pr.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span>#{pr.number}</span>
                        <span>{pr.title}</span>
                        <span>
                          {prRun
                            ? prRun.status === 'completed'
                              ? prRun.conclusion
                              : prRun.status
                            : '·'}
                        </span>
                      </a>
                    )
                  })
                ) : (
                  <div className="empty compact">Aucune PR ouverte.</div>
                )}
              </div>
            </article>
          </section>

          <footer>
            <span>
              Dernier commit de main (référence) :{' '}
              {data.latest_commit ? (
                <a
                  href={data.latest_commit.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="mono">{data.latest_commit.short_sha}</span> ·{' '}
                  {data.latest_commit.message}
                </a>
              ) : (
                '—'
              )}
            </span>
            <span>
              Actualisé {formatDate(data.generated_at)} · auto-refresh 60 s ·
              STALLED ≥ {data.config.stalled_after_minutes} min
            </span>
          </footer>
        </>
      )}
    </main>
  )
}
