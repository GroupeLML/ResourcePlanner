import { useEffect, useRef, useState } from 'react'
import MarkdownDocument from './MarkdownDocument'
import type {
  ArchitectureDetail,
  ChatConversationStatus,
  CommitDetail,
  Dashboard,
  DetailDocument,
  IssueDetail,
  Job,
  PipelineStep,
  RoadmapDetail,
  RoadmapItem,
  RoleConfig,
  Run,
} from './types'

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('fr-CA', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value))
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

function latestRun(dashboard: Dashboard): Run | null {
  if (!dashboard.active_work) return null
  return (
    dashboard.active_work.primary_pr?.runs?.[0] ??
    dashboard.active_work.active_runs?.[0] ??
    null
  )
}

function PipelineUnavailable({ dashboard }: { dashboard: Dashboard }) {
  const invalid = !dashboard.pipeline.valid
  return (
    <section className="role-detail-section">
      <div className="role-detail-section-title">
        {invalid ? `Pipeline #${dashboard.pipeline_source.number} invalide` : 'Aucune étape active'}
      </div>
      <p className="role-detail-muted">{dashboard.next_action}</p>
      {invalid && dashboard.pipeline.errors.length > 0 && (
        <div className="role-detail-warning-list">
          {dashboard.pipeline.errors.map((error) => (
            <div key={error}>⚠ {error}</div>
          ))}
        </div>
      )}
    </section>
  )
}

async function fetchDetail<T>(url: string): Promise<T> {
  const response = await fetch(url)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload.detail || `Erreur HTTP ${response.status}`)
  }
  return payload as T
}

function detailUrl(path: string, repo: string): string {
  return `${path}?repo=${encodeURIComponent(repo)}`
}

function LoadingDetail({ label = 'Lecture de GitHub…' }: { label?: string }) {
  return <div className="role-detail-loading">{label}</div>
}

function DetailError({ value }: { value: string }) {
  return <div className="role-detail-error">⚠ {value}</div>
}

function JobSummary({ job }: { job: Job }) {
  const label =
    job.status === 'completed'
      ? job.conclusion || 'completed'
      : job.status || 'unknown'
  return (
    <a
      className="role-detail-job"
      href={job.url || undefined}
      target="_blank"
      rel="noreferrer"
    >
      <span>{job.name}</span>
      <strong>{label}</strong>
    </a>
  )
}

function ChatStatusBlock({
  role,
  chatStatus,
}: {
  role: RoleConfig
  chatStatus: ChatConversationStatus | null
}) {
  return (
    <section className="role-detail-section">
      <div className="role-detail-section-title">Conversation ChatGPT</div>
      {role.chat_url ? (
        <>
          <dl className="role-detail-facts compact">
            <div>
              <dt>État</dt>
              <dd>{chatStatus?.effective_state ?? 'offline'}</dd>
            </div>
            <div>
              <dt>Dernier heartbeat</dt>
              <dd>{formatAge(chatStatus?.last_seen_seconds)}</dd>
            </div>
            <div>
              <dt>Visible</dt>
              <dd>{chatStatus ? (chatStatus.page_visible ? 'oui' : 'non') : '—'}</dd>
            </div>
            <div>
              <dt>Focus</dt>
              <dd>{chatStatus ? (chatStatus.page_focused ? 'oui' : 'non') : '—'}</dd>
            </div>
          </dl>
          <a
            className="button role-detail-primary-link"
            href={role.chat_url}
            target="_blank"
            rel="noreferrer"
          >
            Ouvrir la conversation ↗
          </a>
        </>
      ) : (
        <p className="role-detail-muted">Aucune conversation n'est associée à ce rôle.</p>
      )}
    </section>
  )
}

function DocumentAccordion({
  document,
  open = false,
  badge,
}: {
  document: DetailDocument
  open?: boolean
  badge?: string
}) {
  return (
    <details className="role-document-accordion" open={open}>
      <summary>
        <div>
          <strong>{document.title}</strong>
          <small>{document.path}</small>
        </div>
        <div className="role-document-summary-meta">
          {badge && <span className="role-document-badge">{badge}</span>}
          {document.status && <span>{document.status}</span>}
        </div>
      </summary>
      <div className="role-document-body">
        <MarkdownDocument markdown={document.content} />
        <a
          className="role-detail-inline-link"
          href={document.url}
          target="_blank"
          rel="noreferrer"
        >
          Ouvrir sur GitHub ↗
        </a>
      </div>
    </details>
  )
}

function RoadmapIssueAccordion({
  item,
  repo,
  active,
}: {
  item: RoadmapItem
  repo: string
  active: boolean
}) {
  const [detail, setDetail] = useState<IssueDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    if (detail || loading || !item.issue_number) return
    setLoading(true)
    setError(null)
    try {
      setDetail(
        await fetchDetail<IssueDetail>(
          detailUrl(`/api/details/issues/${item.issue_number}`, repo),
        ),
      )
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
    } finally {
      setLoading(false)
    }
  }

  return (
    <details
      className={`roadmap-issue-accordion ${active ? 'active' : ''}`}
      onToggle={(event) => {
        if (event.currentTarget.open) void load()
      }}
    >
      <summary>
        <span className={`roadmap-issue-mark ${item.done ? 'done' : active ? 'active' : ''}`}>
          {item.done ? '✓' : active ? '→' : '·'}
        </span>
        <div>
          <strong>#{item.key} · {item.title}</strong>
          <small>
            {item.done ? 'terminée' : active ? 'active' : item.marker || 'à venir'}
          </small>
        </div>
      </summary>
      <div className="roadmap-issue-content">
        {loading && <LoadingDetail label="Chargement de l'issue…" />}
        {error && <DetailError value={error} />}
        {detail && (
          <>
            <div className="roadmap-issue-meta">
              <span>État GitHub : {detail.state}</span>
              <span>Mis à jour : {formatDate(detail.updated_at)}</span>
              <a href={detail.url} target="_blank" rel="noreferrer">Issue #{detail.number} ↗</a>
            </div>
            <MarkdownDocument markdown={detail.body} />
            {detail.documents.length > 0 && (
              <div className="role-inline-documents">
                <div className="role-detail-subtitle">
                  Documentation référencée · {detail.documents.length}
                </div>
                {detail.documents.map((document) => (
                  <DocumentAccordion key={document.path} document={document} />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </details>
  )
}

function pipelineKindLabel(kind: PipelineStep['kind']): string {
  if (kind === 'ARCHITECTURE_GATE') return 'ARCH'
  if (kind === 'ENVIRONMENT_GATE') return 'ENV'
  return 'DEV'
}

function pipelineStepLabel(step: PipelineStep): string {
  if (step.kind !== 'WORK') return step.title
  const clean = step.title.replace(/^#?\d+[A-Z]?\s*/, '').trim()
  return clean ? `#${step.key} · ${clean}` : `#${step.key}`
}

function PipelineStepAccordion({
  step,
  repo,
  current = false,
}: {
  step: PipelineStep
  repo: string
  current?: boolean
}) {
  const [detail, setDetail] = useState<IssueDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    if (detail || loading || !step.issue_number) return
    setLoading(true)
    setError(null)
    try {
      setDetail(
        await fetchDetail<IssueDetail>(
          detailUrl(`/api/details/issues/${step.issue_number}`, repo),
        ),
      )
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
    } finally {
      setLoading(false)
    }
  }

  const exactSection =
    detail?.sections.find((section) => section.work_key === step.key) ?? null

  return (
    <details
      className={`pipeline-detail-accordion ${current ? 'current' : ''}`}
      onToggle={(event) => {
        if (event.currentTarget.open) void load()
      }}
    >
      <summary>
        <span className={`pipeline-kind ${step.kind.toLowerCase()}`}>
          {pipelineKindLabel(step.kind)}
        </span>
        <div>
          <strong>{pipelineStepLabel(step)}</strong>
          <small>
            {step.done
              ? 'terminé'
              : current
                ? 'étape courante'
                : step.status || 'à venir'}
          </small>
        </div>
      </summary>
      <div className="pipeline-detail-body">
        <dl className="role-detail-facts compact">
          <div>
            <dt>Type</dt>
            <dd>{step.kind}</dd>
          </div>
          <div>
            <dt>État machine</dt>
            <dd>{step.status || (step.done ? 'terminé' : 'non terminé')}</dd>
          </div>
          <div>
            <dt>Issue cible</dt>
            <dd>{step.issue_number ? `#${step.issue_number}` : '—'}</dd>
          </div>
        </dl>

        {step.rationale && (
          <div className="pipeline-rationale">
            <strong>Pourquoi / dépendance</strong>
            <p>{step.rationale}</p>
          </div>
        )}

        {loading && <LoadingDetail label="Chargement du détail GitHub…" />}
        {error && <DetailError value={error} />}

        {detail && exactSection && (
          <div className="pipeline-step-source">
            <div className="role-detail-subtitle">{exactSection.title}</div>
            <MarkdownDocument markdown={exactSection.content} />
          </div>
        )}

        {detail && !exactSection && step.kind === 'WORK' && (
          <div className="pipeline-step-source">
            <div className="role-detail-subtitle">
              #{detail.number} · {detail.title}
            </div>
            <MarkdownDocument markdown={detail.body} />
          </div>
        )}

        {detail && step.kind !== 'WORK' && (
          <details className="role-document-accordion pipeline-gate-source">
            <summary>
              <div>
                <strong>Contexte de l'issue #{detail.number}</strong>
                <small>{detail.title}</small>
              </div>
            </summary>
            <div className="role-document-body">
              <MarkdownDocument markdown={detail.body} />
            </div>
          </details>
        )}

        {detail && detail.documents.length > 0 && (
          <div className="role-inline-documents">
            <div className="role-detail-subtitle">
              Documentation associée · {detail.documents.length}
            </div>
            {detail.documents.map((document) => (
              <DocumentAccordion key={document.path} document={document} />
            ))}
          </div>
        )}
      </div>
    </details>
  )
}

function PipelineHorizonGroup({
  label,
  steps,
  repo,
  currentKey,
  defaultOpen = false,
}: {
  label: string
  steps: PipelineStep[]
  repo: string
  currentKey?: string | null
  defaultOpen?: boolean
}) {
  return (
    <details className="pipeline-horizon-group" open={defaultOpen}>
      <summary>
        <strong>{label}</strong>
        <span>{steps.length} étape(s)</span>
      </summary>
      <div className="pipeline-horizon-content">
        {steps.length ? (
          steps.map((step) => (
            <PipelineStepAccordion
              key={`${step.kind}-${step.key}`}
              step={step}
              repo={repo}
              current={step.key === currentKey}
            />
          ))
        ) : (
          <p className="role-detail-muted">Aucune étape dans cet horizon.</p>
        )}
      </div>
    </details>
  )
}

function ProductOwnerDetails({ dashboard }: { dashboard: Dashboard | null }) {
  const [roadmap, setRoadmap] = useState<RoadmapDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!dashboard?.active_work) return
    let cancelled = false
    setRoadmap(null)
    setError(null)
    void fetchDetail<RoadmapDetail>(
      detailUrl('/api/details/roadmap', dashboard.repo),
    )
      .then((value) => {
        if (!cancelled) setRoadmap(value)
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
        }
      })
    return () => {
      cancelled = true
    }
  }, [dashboard?.repo])

  if (!dashboard) {
    return <p className="role-detail-muted">Données GitHub indisponibles.</p>
  }
  if (!dashboard.pipeline.valid || !dashboard.active_work) {
    return <PipelineUnavailable dashboard={dashboard} />
  }

  return (
    <>
      <section className="role-detail-hero">
        <span>Décision produit actuelle</span>
        <strong>{dashboard.roadmap.effective_active}</strong>
        <p>{dashboard.active_work.title || dashboard.active_work.issue.title}</p>
        <div className="role-detail-next-action">{dashboard.next_action}</div>
      </section>

      {dashboard.pipeline.steps.length > 0 && (
        <section className="role-detail-section product-pipeline-section">
          <div className="role-detail-section-title">
            Trajectoire produit · {dashboard.pipeline.completed_count}/{dashboard.pipeline.steps.length} franchie(s)
          </div>
          <p className="role-detail-muted">
            Pipeline déterministe lu depuis la source machine #${dashboard.pipeline_source.number}. Ouvre une tranche pour
            afficher son contexte humain #${dashboard.roadmap.number} puis son détail GitHub.
          </p>
          <div className="pipeline-horizon-stack">
            <PipelineHorizonGroup
              label="Maintenant"
              steps={dashboard.pipeline.now ? [dashboard.pipeline.now] : []}
              repo={dashboard.repo}
              currentKey={dashboard.pipeline.now?.key}
              defaultOpen
            />
            <PipelineHorizonGroup
              label="En parallèle"
              steps={dashboard.pipeline.parallel}
              repo={dashboard.repo}
              currentKey={dashboard.pipeline.now?.key}
            />
            <PipelineHorizonGroup
              label="Ensuite"
              steps={dashboard.pipeline.next}
              repo={dashboard.repo}
              currentKey={dashboard.pipeline.now?.key}
            />
            <PipelineHorizonGroup
              label="Plus tard"
              steps={dashboard.pipeline.later}
              repo={dashboard.repo}
              currentKey={dashboard.pipeline.now?.key}
            />
          </div>
        </section>
      )}

      <section className="role-detail-section">
        <div className="role-detail-section-title">
          Roadmap complet · ouvrir une issue pour lire sa spécification
        </div>
        {!roadmap && !error && <LoadingDetail label="Chargement du roadmap complet…" />}
        {error && <DetailError value={error} />}
        {roadmap && (
          <>
            <div className="roadmap-detail-meta">
              <span>#{roadmap.number} · {roadmap.title}</span>
              <span>Mis à jour {formatDate(roadmap.updated_at)}</span>
            </div>
            <div className="roadmap-issue-accordions">
              {roadmap.items.map((item) => (
                <RoadmapIssueAccordion
                  key={item.key}
                  item={item}
                  repo={dashboard.repo}
                  active={String(item.issue_number) === String(dashboard.roadmap.active_issue)}
                />
              ))}
            </div>
            <details className="role-document-accordion roadmap-source">
              <summary>
                <div>
                  <strong>Roadmap humain #{roadmap.number}</strong>
                  <small>Contenu complet du roadmap GitHub</small>
                </div>
              </summary>
              <div className="role-document-body">
                <MarkdownDocument markdown={roadmap.body} />
              </div>
            </details>
          </>
        )}
      </section>

      {dashboard.warnings.length > 0 && (
        <section className="role-detail-section">
          <div className="role-detail-section-title">Décalages détectés</div>
          <div className="role-detail-warning-list">
            {dashboard.warnings.map((warning) => (
              <div key={warning}>⚠ {warning}</div>
            ))}
          </div>
        </section>
      )}
    </>
  )
}

function DeveloperDetails({ dashboard }: { dashboard: Dashboard | null }) {
  const [copied, setCopied] = useState(false)
  const [issue, setIssue] = useState<IssueDetail | null>(null)
  const [commit, setCommit] = useState<CommitDetail | null>(null)
  const [issueError, setIssueError] = useState<string | null>(null)
  const [commitError, setCommitError] = useState<string | null>(null)

  useEffect(() => {
    if (!dashboard?.active_work) return
    let cancelled = false
    setIssue(null)
    setCommit(null)
    setIssueError(null)
    setCommitError(null)

    void fetchDetail<IssueDetail>(
      detailUrl(
        `/api/details/issues/${dashboard.active_work.issue_number}`,
        dashboard.repo,
      ),
    )
      .then((value) => {
        if (!cancelled) setIssue(value)
      })
      .catch((caught) => {
        if (!cancelled) {
          setIssueError(caught instanceof Error ? caught.message : 'Erreur inconnue')
        }
      })

    const sha = dashboard.active_work.last_commit?.sha
    if (sha) {
      void fetchDetail<CommitDetail>(
        detailUrl(`/api/details/commits/${sha}`, dashboard.repo),
      )
        .then((value) => {
          if (!cancelled) setCommit(value)
        })
        .catch((caught) => {
          if (!cancelled) {
            setCommitError(caught instanceof Error ? caught.message : 'Erreur inconnue')
          }
        })
    }

    return () => {
      cancelled = true
    }
  }, [
    dashboard?.repo,
    dashboard?.active_work?.issue_number,
    dashboard?.active_work?.last_commit?.sha,
  ])

  if (!dashboard) {
    return <p className="role-detail-muted">Données GitHub indisponibles.</p>
  }
  if (!dashboard.pipeline.valid || !dashboard.active_work) {
    return <PipelineUnavailable dashboard={dashboard} />
  }

  const work = dashboard.active_work
  const run = latestRun(dashboard)
  const prompt = dashboard.dev_prompt
  const activeSection = work.subitem_key
    ? issue?.sections.find((section) => section.work_key === work.subitem_key) ?? null
    : null

  const byDocumentPath = new Map<string, DetailDocument>()
  for (const document of issue?.documents ?? []) {
    byDocumentPath.set(document.path, document)
  }
  for (const document of commit?.documentation ?? []) {
    byDocumentPath.set(document.path, document)
  }
  const documents = [...byDocumentPath.values()]

  async function copyPrompt() {
    if (!prompt) return
    await navigator.clipboard.writeText(prompt)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <>
      <section className="role-detail-hero">
        <span>Tâche réellement en cours</span>
        <strong>{work.subitem_key || `#${work.issue_number}`}</strong>
        <p>{work.title || work.issue.title}</p>
        <div className="role-detail-chips">
          {work.states.map((state) => (
            <span key={state}>{state}</span>
          ))}
        </div>
      </section>

      <section className="role-detail-section task-specification">
        <div className="role-detail-section-title">
          Spécification documentée de la tâche
        </div>
        {!issue && !issueError && <LoadingDetail label="Lecture de l'issue active…" />}
        {issueError && <DetailError value={issueError} />}
        {issue && activeSection && (
          <>
            <div className="role-detail-subtitle">{activeSection.title}</div>
            <MarkdownDocument markdown={activeSection.content} />
          </>
        )}
        {issue && !activeSection && (
          <>
            <p className="role-detail-muted">
              Aucune section dédiée à {work.subitem_key || work.key} n'a été détectée;
              le contenu complet de l'issue est affiché.
            </p>
            <MarkdownDocument markdown={issue.body} />
          </>
        )}
        {issue && activeSection && (
          <details className="role-document-accordion parent-issue-source">
            <summary>
              <div>
                <strong>Contexte complet de l'issue #{issue.number}</strong>
                <small>{issue.title}</small>
              </div>
            </summary>
            <div className="role-document-body">
              <MarkdownDocument markdown={issue.body} />
            </div>
          </details>
        )}
      </section>

      <section className="role-detail-section">
        <div className="role-detail-section-title">HEAD de la branche active</div>
        {commitError && <DetailError value={commitError} />}
        {!work.last_commit && (
          <p className="role-detail-muted">Aucun commit de travail actif détecté.</p>
        )}
        {work.last_commit && !commit && !commitError && (
          <LoadingDetail label="Lecture du commit courant…" />
        )}
        {commit && (
          <>
            <div className="commit-detail-head">
              <div>
                <a href={commit.url} target="_blank" rel="noreferrer">
                  <strong>{commit.short_sha}</strong>
                </a>
                <span>{commit.message}</span>
              </div>
              <div className="commit-detail-stats">
                <span>+{commit.stats.additions ?? 0}</span>
                <span>−{commit.stats.deletions ?? 0}</span>
                <span>{commit.files.length} fichier(s)</span>
              </div>
            </div>
            <dl className="role-detail-facts compact">
              <div>
                <dt>Branche</dt>
                <dd>
                  {work.active_branch ? (
                    <a href={work.active_branch.url} target="_blank" rel="noreferrer">
                      {work.active_branch.name}
                    </a>
                  ) : '—'}
                </dd>
              </div>
              <div>
                <dt>Auteur</dt>
                <dd>{commit.author || '—'}</dd>
              </div>
              <div>
                <dt>Date</dt>
                <dd>{formatDate(commit.date)}</dd>
              </div>
              <div>
                <dt>SHA</dt>
                <dd className="mono">{commit.sha}</dd>
              </div>
            </dl>
            <div className="commit-file-list">
              {commit.files.map((changed) => (
                <a
                  key={changed.filename}
                  href={changed.url || undefined}
                  target="_blank"
                  rel="noreferrer"
                  className="commit-file-row"
                >
                  <span className={`commit-file-status ${changed.status || ''}`}>
                    {changed.status || 'changed'}
                  </span>
                  <span className="commit-file-name">{changed.filename}</span>
                  <span className="commit-file-delta">
                    +{changed.additions ?? 0} −{changed.deletions ?? 0}
                  </span>
                </a>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="role-detail-section">
        <div className="role-detail-section-title">
          Documentation associée à l'issue / au commit
        </div>
        {!issue && !issueError && <LoadingDetail label="Résolution de la documentation…" />}
        {documents.length ? (
          <div className="role-document-stack">
            {documents.map((document, index) => (
              <DocumentAccordion
                key={document.path}
                document={document}
                open={index === 0}
                badge={
                  dashboard.architecture.referenced_adrs.includes(document.name)
                    ? 'ADR du bloc'
                    : commit?.documentation.some((item) => item.path === document.path)
                      ? 'modifié par HEAD'
                      : undefined
                }
              />
            ))}
          </div>
        ) : (
          issue && (
            <p className="role-detail-muted">
              Aucun document Markdown explicitement référencé par l'issue ou modifié par le HEAD.
            </p>
          )
        )}
      </section>

      <section className="role-detail-section">
        <div className="role-detail-section-title">PR / CI</div>
        <dl className="role-detail-facts compact">
          <div>
            <dt>PR</dt>
            <dd>
              {work.primary_pr ? (
                <a href={work.primary_pr.url} target="_blank" rel="noreferrer">
                  #{work.primary_pr.number} · {work.primary_pr.title}
                </a>
              ) : 'Aucune PR associée'}
            </dd>
          </div>
          <div>
            <dt>Issue</dt>
            <dd>
              <a href={work.issue.url} target="_blank" rel="noreferrer">
                #{work.issue.number} · {work.issue.title}
              </a>
            </dd>
          </div>
        </dl>
        {run ? (
          <>
            <div className="role-detail-run-head">
              <a href={run.url || undefined} target="_blank" rel="noreferrer">
                {run.name} #{run.run_number ?? ''}
              </a>
              <strong>
                {run.status === 'completed' ? run.conclusion || 'completed' : run.status}
              </strong>
            </div>
            {run.jobs.length > 0 && (
              <div className="role-detail-jobs">
                {run.jobs.map((job) => (
                  <JobSummary key={job.id ?? job.name} job={job} />
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="role-detail-muted">Aucun workflow associé au travail actif.</p>
        )}
      </section>

      {work.stalled && (
        <section className="role-detail-section danger">
          <div className="role-detail-section-title">Diagnostic de stall</div>
          <dl className="role-detail-facts compact">
            <div>
              <dt>Niveau</dt>
              <dd>{work.stall_level || '—'}</dd>
            </div>
            <div>
              <dt>Dernière activité</dt>
              <dd>
                {work.stalled_details.last_activity_minutes == null
                  ? '—'
                  : `${work.stalled_details.last_activity_minutes} min`}
              </dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>{work.stalled_details.last_activity_source || '—'}</dd>
            </div>
            <div>
              <dt>Workflow actif</dt>
              <dd>{work.stalled_details.no_active_workflow ? 'non' : 'oui'}</dd>
            </div>
          </dl>
        </section>
      )}

      <section className="role-detail-section">
        <div className="role-detail-section-title">Prompt de reprise</div>
        <pre className="role-detail-prompt">{prompt}</pre>
        <button className="primary" type="button" onClick={() => void copyPrompt()}>
          {copied ? '✓ Copié' : 'Copier le prompt Dev'}
        </button>
      </section>
    </>
  )
}

function ArchitectDetails({ dashboard }: { dashboard: Dashboard | null }) {
  const [architecture, setArchitecture] = useState<ArchitectureDetail | null>(null)
  const [issue, setIssue] = useState<IssueDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!dashboard?.active_work) return
    let cancelled = false
    setArchitecture(null)
    setIssue(null)
    setError(null)

    void Promise.all([
      fetchDetail<ArchitectureDetail>(
        detailUrl('/api/details/architecture', dashboard.repo),
      ),
      fetchDetail<IssueDetail>(
        detailUrl(
          `/api/details/issues/${dashboard.active_work.issue_number}`,
          dashboard.repo,
        ),
      ),
    ])
      .then(([architectureValue, issueValue]) => {
        if (!cancelled) {
          setArchitecture(architectureValue)
          setIssue(issueValue)
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
        }
      })

    return () => {
      cancelled = true
    }
  }, [dashboard?.repo, dashboard?.active_work?.issue_number])

  if (!dashboard) {
    return <p className="role-detail-muted">Données GitHub indisponibles.</p>
  }
  if (!dashboard.pipeline.valid || !dashboard.active_work) {
    return <PipelineUnavailable dashboard={dashboard} />
  }

  const work = dashboard.active_work
  const referenced = new Set(dashboard.architecture.referenced_adrs)
  const overview = architecture?.documents.find((document) => document.name === 'README.md')
  const adrs = architecture?.documents.filter((document) => document.name.startsWith('ADR-')) ?? []
  const relevantAdrs = adrs.filter((document) => referenced.has(document.name))
  const otherAdrs = adrs.filter((document) => !referenced.has(document.name))
  const activeSection = work.subitem_key
    ? issue?.sections.find(
        (section) => section.work_key === work.subitem_key,
      ) ?? null
    : null

  return (
    <>
      <section className="role-detail-hero">
        <span>Architecture du bloc actif</span>
        <strong>
          {dashboard.active_work.subitem_key || `#${dashboard.active_work.issue_number}`}
        </strong>
        <p>{dashboard.active_work.title || dashboard.active_work.issue.title}</p>
      </section>

      {error && <DetailError value={error} />}
      {!architecture && !error && <LoadingDetail label="Chargement de l'architecture et des ADR…" />}

      {activeSection && (
        <section className="role-detail-section">
          <div className="role-detail-section-title">Contexte documenté du bloc actif</div>
          <div className="role-detail-subtitle">{activeSection.title}</div>
          <MarkdownDocument markdown={activeSection.content} />
        </section>
      )}

      {overview && (
        <section className="role-detail-section">
          <div className="role-detail-section-title">Architecture de l'application</div>
          <DocumentAccordion document={overview} open />
        </section>
      )}

      {architecture && (
        <section className="role-detail-section">
          <div className="role-detail-section-title">
            ADR pertinents pour le bloc · {relevantAdrs.length}
          </div>
          {relevantAdrs.length ? (
            <div className="role-document-stack">
              {relevantAdrs.map((document, index) => (
                <DocumentAccordion
                  key={document.path}
                  document={document}
                  open={index === 0}
                  badge="référencé"
                />
              ))}
            </div>
          ) : (
            <p className="role-detail-muted">
              Aucun ADR n'est explicitement référencé par l'issue ou le bloc actif.
            </p>
          )}
        </section>
      )}

      {architecture && otherAdrs.length > 0 && (
        <section className="role-detail-section">
          <div className="role-detail-section-title">
            Autres ADR disponibles · {otherAdrs.length}
          </div>
          <div className="role-document-stack">
            {otherAdrs.map((document) => (
              <DocumentAccordion key={document.path} document={document} />
            ))}
          </div>
          <a
            className="role-detail-inline-link"
            href={architecture.url}
            target="_blank"
            rel="noreferrer"
          >
            Ouvrir docs/architecture ↗
          </a>
        </section>
      )}
    </>
  )
}

function ReviewerDetails({ dashboard }: { dashboard: Dashboard | null }) {
  if (!dashboard) {
    return <p className="role-detail-muted">Données GitHub indisponibles.</p>
  }
  if (!dashboard.pipeline.valid || !dashboard.active_work) {
    return <PipelineUnavailable dashboard={dashboard} />
  }
  return (
    <>
      <section className="role-detail-hero">
        <span>Revue active</span>
        <strong>{dashboard.active_work.subitem_key || dashboard.active_work.key}</strong>
        <p>{dashboard.active_work.title || dashboard.active_work.issue.title}</p>
      </section>
      <section className="role-detail-section">
        <div className="role-detail-section-title">Jobs en échec</div>
        {dashboard.active_work.failed_jobs.length ? (
          <div className="role-detail-warning-list">
            {dashboard.active_work.failed_jobs.map((job) => (
              <div key={job}>✕ {job}</div>
            ))}
          </div>
        ) : (
          <p className="role-detail-muted">Aucun job CI en échec sur le travail actif.</p>
        )}
      </section>
      <section className="role-detail-section">
        <div className="role-detail-section-title">PR ouvertes</div>
        <div className="role-detail-link-list">
          {dashboard.open_prs.length ? (
            dashboard.open_prs.map((pr) => (
              <a key={pr.number} href={pr.url} target="_blank" rel="noreferrer">
                #{pr.number} · {pr.title}
              </a>
            ))
          ) : (
            <span className="role-detail-muted">Aucune PR ouverte.</span>
          )}
        </div>
      </section>
    </>
  )
}

function MissionBlock({
  role,
  dashboard,
}: {
  role: RoleConfig
  dashboard: Dashboard | null
}) {
  const [copied, setCopied] = useState(false)
  const mission = dashboard?.execution.missions[role.avatar] ?? null
  const pack = dashboard?.handoff.packs[role.avatar] ?? null
  if (!mission || !pack) return null
  const handoffPrompt = pack.prompt

  function handoff() {
    void navigator.clipboard.writeText(handoffPrompt)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
    if (role.chat_url) {
      window.open(role.chat_url, '_blank', 'noopener,noreferrer')
    }
  }

  return (
    <section className={`role-detail-section mission-block ${mission.state}`}>
      <div className="role-detail-section-title">Mission courante</div>
      <div className="mission-heading">
        <strong>{mission.title}</strong>
        <span>{mission.state}</span>
      </div>
      <p className="role-detail-muted">{mission.detail}</p>

      <div className="handoff-summary">
        <div>
          <span className="section-label">HANDOFF PACK</span>
          <strong className={`handoff-confidence ${pack.confidence.toLowerCase()}`}>
            {pack.confidence}
          </strong>
        </div>
        {pack.missing.length > 0 && (
          <ul className="handoff-missing">
            {pack.missing.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
        {pack.sources.length > 0 && (
          <div className="handoff-sources">
            {pack.sources.map((source) => (
              <a key={`${source.kind}:${source.url}`} href={source.url} target="_blank" rel="noreferrer">
                {source.label} ↗
              </a>
            ))}
          </div>
        )}
      </div>

      <details className="handoff-preview">
        <summary>Voir le pack de reprise</summary>
        <pre className="mission-prompt">{handoffPrompt}</pre>
      </details>

      <div className="mission-actions">
        <button type="button" className="primary" onClick={handoff}>
          {copied
            ? 'Handoff copié ✓'
            : role.chat_url
              ? 'Préparer la reprise + ouvrir ChatGPT ↗'
              : 'Préparer la reprise'}
        </button>
        {mission.primary_link && (
          <a
            className="button"
            href={mission.primary_link.url}
            target="_blank"
            rel="noreferrer"
          >
            {mission.primary_link.label} ↗
          </a>
        )}
      </div>
    </section>
  )
}

export default function RoleDetailPanel({
  role,
  dashboard,
  chatStatus,
  onClose,
}: {
  role: RoleConfig
  dashboard: Dashboard | null
  chatStatus: ChatConversationStatus | null
  onClose: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])

  let details
  if (role.avatar === 'product-owner') {
    details = <ProductOwnerDetails dashboard={dashboard} />
  } else if (role.avatar === 'developer') {
    details = <DeveloperDetails dashboard={dashboard} />
  } else if (role.avatar === 'architect') {
    details = <ArchitectDetails dashboard={dashboard} />
  } else if (role.avatar === 'reviewer') {
    details = <ReviewerDetails dashboard={dashboard} />
  } else {
    details = (
      <section className="role-detail-section">
        <div className="role-detail-section-title">Rôle personnalisé</div>
        <p className="role-detail-muted">
          Ce rôle n'a pas encore de vue métier spécialisée.
        </p>
      </section>
    )
  }

  return (
    <div className="role-detail-backdrop" onMouseDown={onClose}>
      <aside
        className="role-detail-drawer rich"
        role="dialog"
        aria-modal="true"
        aria-labelledby="role-detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="role-detail-header">
          <div>
            <span className="panel-kicker">DÉTAIL DU RÔLE</span>
            <h2 id="role-detail-title">{role.name}</h2>
          </div>
          <button
            ref={closeRef}
            className="role-detail-close"
            type="button"
            onClick={onClose}
            aria-label="Fermer le panneau"
          >
            ×
          </button>
        </header>

        <div className="role-detail-content">
          <MissionBlock role={role} dashboard={dashboard} />
          {details}
          <ChatStatusBlock role={role} chatStatus={chatStatus} />
        </div>

        <footer className="role-detail-footer">
          <span>Dashboard {formatDate(dashboard?.generated_at)}</span>
          <span>Les détails sont chargés à la demande depuis GitHub.</span>
        </footer>
      </aside>
    </div>
  )
}
