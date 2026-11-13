import { useEffect, useMemo, useState } from 'react'
import RoleDetailPanel from './RoleDetailPanel'
import type {
  ChatConversationStatus,
  ChatStatusResponse,
  Dashboard,
  RoleConfig,
  RolesConfig,
} from './types'

const AVATAR_LABELS: Record<RoleConfig['avatar'], string> = {
  'product-owner': '🧑‍💼',
  developer: '🤖',
  architect: '🧠',
  reviewer: '🔎',
  generic: '💬',
}

const AVATAR_OPTIONS: Array<{ value: RoleConfig['avatar']; label: string }> = [
  { value: 'product-owner', label: 'Product Owner' },
  { value: 'developer', label: 'Developer / chatbot' },
  { value: 'architect', label: 'Architecte' },
  { value: 'reviewer', label: 'Reviewer' },
  { value: 'generic', label: 'Générique' },
]

function minutesLabel(value: number | null | undefined): string {
  if (value == null) return 'un moment'
  if (value < 60) return `${value} min`
  const hours = Math.floor(value / 60)
  const minutes = value % 60
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`
}

function normalizeChatUrl(value: string): string | null {
  if (!value.trim()) return null
  try {
    const parsed = new URL(value.trim())
    if (
      parsed.protocol !== 'https:' ||
      !['chatgpt.com', 'www.chatgpt.com'].includes(parsed.hostname)
    ) {
      return null
    }
    const path = parsed.pathname.replace(/\/$/, '') || '/'
    return `https://chatgpt.com${path}`
  } catch {
    return null
  }
}

function chatStatusForRole(
  role: RoleConfig,
  statuses: ChatStatusResponse | null,
): ChatConversationStatus | null {
  const key = normalizeChatUrl(role.chat_url)
  if (!key || !statuses) return null
  return statuses.conversations.find((row) => row.conversation_url === key) ?? null
}

function secondsLabel(value: number | null | undefined): string {
  if (value == null) return 'un moment'
  if (value < 60) return `${value} s`
  return minutesLabel(Math.floor(value / 60))
}

function companionBubble(status: ChatConversationStatus | null): string | null {
  if (!status) return null
  if (status.effective_state === 'working') {
    return 'ChatGPT répond présentement…'
  }
  if (status.effective_state === 'possible_stall') {
    return `Conversation possiblement interrompue · aucun heartbeat depuis ${secondsLabel(status.last_seen_seconds)}.`
  }
  if (
    status.effective_state === 'idle' &&
    status.last_completed_seconds != null &&
    status.last_completed_seconds <= 30
  ) {
    return `Réponse ChatGPT terminée il y a ${secondsLabel(status.last_completed_seconds)}.`
  }
  return null
}

function developerBubble(dashboard: Dashboard | null): string {
  if (!dashboard) return 'En attente des données GitHub.'
  if (!dashboard.pipeline.valid) {
    return `Pipeline #${dashboard.pipeline_source.number} invalide · aucune tranche DEV déduite.`
  }
  if (!dashboard.active_work) return 'Aucune tranche DEV active déclarée.'
  const active = dashboard.active_work
  const states = new Set(active.states)
  const key = active.subitem_key || `#${active.issue_number}`
  const silence = minutesLabel(active.stalled_details?.last_activity_minutes)

  if (states.has('STALLED_CONFIRMED')) {
    return `CI rouge abandonnée · aucune reprise visible depuis ${silence}.`
  }
  if (states.has('STALLED')) {
    return `Aucune activité GitHub pertinente depuis ${silence}.`
  }
  if (states.has('POSSIBLE_STALL')) {
    return `Travail possiblement interrompu · silence depuis ${silence}.`
  }
  if (states.has('CI_RUNNING')) return `CI en cours pour ${key}.`
  if (states.has('CI_RED')) return `CI rouge sur ${key}.`
  if (states.has('MERGEABLE')) return `PR verte et mergeable pour ${key}.`
  if (states.has('DONE')) return `Le bloc ${key} semble terminé.`
  if (states.has('IN_PROGRESS')) {
    const branch = active.active_branch?.name
    return branch ? `${key} en cours sur ${branch}.` : `${key} est en cours.`
  }
  return `${key} est READY.`
}

function roleBubble(
  role: RoleConfig,
  dashboard: Dashboard | null,
  chatStatus: ChatConversationStatus | null,
): string {
  const mission = dashboard?.execution.missions[role.avatar]
  if (mission) return `${mission.title} · ${mission.detail}`
  const companion = companionBubble(chatStatus)
  if (companion) return companion
  if (role.avatar === 'developer') return developerBubble(dashboard)
  if (!dashboard) {
    return role.chat_url
      ? 'Conversation associée prête à ouvrir.'
      : 'Aucune conversation associée.'
  }

  if (role.avatar === 'product-owner') {
    if (!dashboard.pipeline.valid) {
      return `Roadmap #${dashboard.roadmap.number} · pipeline canonique invalide.`
    }
    return `Roadmap #${dashboard.roadmap.number} · actif: ${dashboard.roadmap.effective_active ?? 'aucun'}.`
  }
  if (role.avatar === 'architect') {
    const referenced = dashboard.architecture.referenced_adrs.length
    return referenced
      ? `${referenced} ADR référencé(s) par le bloc actif.`
      : 'Consulter les ADR applicables au besoin.'
  }
  if (role.avatar === 'reviewer') {
    const failed = dashboard.active_work?.failed_jobs.length ?? 0
    return failed
      ? `${failed} job(s) CI en échec à examiner.`
      : 'Aucun job CI en échec sur le travail actif.'
  }
  return role.chat_url
    ? 'Conversation associée prête à ouvrir.'
    : 'Aucune conversation associée.'
}

function isDeveloperWorking(role: RoleConfig, dashboard: Dashboard | null): boolean {
  if (role.avatar !== 'developer' || !dashboard?.active_work) return false
  const states = new Set(dashboard.active_work.states)
  if (
    states.has('STALLED') ||
    states.has('STALLED_CONFIRMED') ||
    states.has('POSSIBLE_STALL')
  ) {
    return false
  }
  return states.has('IN_PROGRESS') || states.has('CI_RUNNING')
}

function normalizeOrder(roles: RoleConfig[]): RoleConfig[] {
  return roles.map((role, index) => ({ ...role, order: (index + 1) * 10 }))
}

export default function RoleCards({ dashboard }: { dashboard: Dashboard | null }) {
  const [roles, setRoles] = useState<RoleConfig[]>([])
  const [draft, setDraft] = useState<RoleConfig[]>([])
  const [editing, setEditing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [chatStatuses, setChatStatuses] = useState<ChatStatusResponse | null>(null)
  const [selectedRoleId, setSelectedRoleId] = useState<string | null>(null)
  const [missionCopiedId, setMissionCopiedId] = useState<string | null>(null)

  async function loadRoles() {
    setLoading(true)
    setError(null)
    try {
      const response = await fetch('/api/roles')
      const payload = (await response.json()) as RolesConfig & { detail?: string }
      if (!response.ok) {
        throw new Error(payload.detail || `Erreur HTTP ${response.status}`)
      }
      const ordered = [...payload.roles].sort((a, b) => a.order - b.order)
      setRoles(ordered)
      setDraft(ordered.map((role) => ({ ...role })))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadRoles()
  }, [])

  useEffect(() => {
    let cancelled = false

    async function loadChatStatuses() {
      try {
        const response = await fetch('/api/chat-status')
        if (!response.ok) return
        const payload = (await response.json()) as ChatStatusResponse
        if (!cancelled) setChatStatuses(payload)
      } catch {
        if (!cancelled) setChatStatuses(null)
      }
    }

    void loadChatStatuses()
    const timer = window.setInterval(() => void loadChatStatuses(), 3_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  const enabledRoles = useMemo(
    () => roles.filter((role) => role.enabled).sort((a, b) => a.order - b.order),
    [roles],
  )

  const selectedRole =
    roles.find((role) => role.id === selectedRoleId) ?? null
  const selectedChatStatus = selectedRole
    ? chatStatusForRole(selectedRole, chatStatuses)
    : null

  function beginEditing() {
    setDraft(roles.map((role) => ({ ...role })))
    setError(null)
    setEditing(true)
  }

  function updateRole(index: number, patch: Partial<RoleConfig>) {
    setDraft((current) =>
      current.map((role, roleIndex) =>
        roleIndex === index ? { ...role, ...patch } : role,
      ),
    )
  }

  function addRole() {
    setDraft((current) => [
      ...current,
      {
        id: `role-${Date.now()}`,
        name: 'Nouveau rôle',
        avatar: 'generic',
        chat_url: '',
        enabled: true,
        order: (current.length + 1) * 10,
      },
    ])
  }

  function removeRole(index: number) {
    setDraft((current) =>
      normalizeOrder(current.filter((_, roleIndex) => roleIndex !== index)),
    )
  }

  function moveRole(index: number, direction: -1 | 1) {
    setDraft((current) => {
      const target = index + direction
      if (target < 0 || target >= current.length) return current
      const next = [...current]
      ;[next[index], next[target]] = [next[target], next[index]]
      return normalizeOrder(next)
    })
  }

  function handoffRole(role: RoleConfig) {
    const pack = dashboard?.handoff.packs[role.avatar]
    if (!pack?.prompt) return

    void navigator.clipboard.writeText(pack.prompt)
    setMissionCopiedId(role.id)
    window.setTimeout(() => setMissionCopiedId(null), 1600)

    if (role.chat_url) {
      window.open(role.chat_url, '_blank', 'noopener,noreferrer')
    }
  }

  async function saveRoles() {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const normalized = normalizeOrder(draft)
      const response = await fetch('/api/roles', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roles: normalized }),
      })
      const payload = (await response.json()) as RolesConfig & { detail?: string }
      if (!response.ok) {
        throw new Error(payload.detail || `Erreur HTTP ${response.status}`)
      }
      const ordered = [...payload.roles].sort((a, b) => a.order - b.order)
      setRoles(ordered)
      setDraft(ordered.map((role) => ({ ...role })))
      setEditing(false)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1600)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Erreur inconnue')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="roles-section">
      <div className="roles-heading">
        <div>
          <span className="panel-kicker">ÉQUIPE DEV</span>
          <h2>Rôles et conversations</h2>
        </div>
        <div className="roles-heading-actions">
          {saved && <span className="roles-saved">✓ Enregistré</span>}
          <button type="button" onClick={editing ? () => setEditing(false) : beginEditing}>
            {editing ? 'Fermer' : '⚙ Gérer les rôles'}
          </button>
        </div>
      </div>

      {error && <div className="roles-error">{error}</div>}

      {loading ? (
        <div className="roles-empty">Chargement des rôles…</div>
      ) : enabledRoles.length ? (
        <div className="role-cards">
          {enabledRoles.map((role) => {
            const chatStatus = chatStatusForRole(role, chatStatuses)
            const mission = dashboard?.execution.missions[role.avatar] ?? null
            const handoffPack = dashboard?.handoff.packs[role.avatar] ?? null
            const chatWorking = chatStatus?.effective_state === 'working'
            const githubWorking = isDeveloperWorking(role, dashboard)
            const working = Boolean(chatWorking || githubWorking)
            const stalled = Boolean(
              chatStatus?.effective_state === 'possible_stall' ||
                (role.avatar === 'developer' && dashboard?.active_work?.stalled),
            )
            return (
              <article
                className={`role-card ${working ? 'is-working' : ''} ${stalled ? 'is-stalled' : ''}`}
                key={role.id}
              >
                <button
                  className="role-avatar-wrap role-avatar-button"
                  type="button"
                  onClick={() => setSelectedRoleId(role.id)}
                  aria-label={`Voir les détails de ${role.name}`}
                >
                  <div className="role-avatar" aria-hidden="true">
                    {AVATAR_LABELS[role.avatar]}
                  </div>
                  {working && <span className="role-working-dot" title="Activité détectée" />}
                </button>
                <div className="role-card-body">
                  <div className="role-name-row">
                    <button
                      className="role-name-button"
                      type="button"
                      onClick={() => setSelectedRoleId(role.id)}
                    >
                      {role.name}
                    </button>
                    {working && <span className="role-status">travaille</span>}
                    {stalled && <span className="role-status stalled">silencieux</span>}
                    {mission && (
                      <span className={`role-mission-state ${mission.state}`}>
                        {mission.state}
                      </span>
                    )}
                    {handoffPack && (
                      <span className={`handoff-confidence ${handoffPack.confidence.toLowerCase()}`}>
                        {handoffPack.confidence}
                      </span>
                    )}
                    {role.chat_url && (
                      <span
                        className={`chat-state ${chatStatus?.effective_state ?? 'unknown'}`}
                        title={
                          chatStatus
                            ? `Heartbeat il y a ${secondsLabel(chatStatus.last_seen_seconds)}`
                            : 'Aucun heartbeat du Firefox Companion'
                        }
                      >
                        ChatGPT · {chatStatus?.effective_state ?? 'offline'}
                      </span>
                    )}
                  </div>
                  <div className="role-bubble">
                    {roleBubble(role, dashboard, chatStatus)}
                  </div>
                  <div className="role-actions">
                    <button
                      className="role-details-button"
                      type="button"
                      onClick={() => setSelectedRoleId(role.id)}
                    >
                      Voir détails
                    </button>
                    {mission ? (
                      <button
                        className="button role-chat-link"
                        type="button"
                        onClick={() => handoffRole(role)}
                      >
                        {missionCopiedId === role.id
                          ? 'Handoff copié ✓'
                          : role.chat_url
                            ? 'Préparer la reprise + ouvrir ↗'
                            : 'Préparer la reprise'}
                      </button>
                    ) : role.chat_url ? (
                      <a
                        className="button role-chat-link"
                        href={role.chat_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Ouvrir ChatGPT ↗
                      </a>
                    ) : (
                      <button className="role-chat-link" type="button" onClick={beginEditing}>
                        Associer une discussion
                      </button>
                    )}
                  </div>
                </div>
              </article>
            )
          })}
        </div>
      ) : (
        <div className="roles-empty">
          Aucun rôle actif. Utilise « Gérer les rôles » pour en ajouter.
        </div>
      )}

      {selectedRole && (
        <RoleDetailPanel
          role={selectedRole}
          dashboard={dashboard}
          chatStatus={selectedChatStatus}
          onClose={() => setSelectedRoleId(null)}
        />
      )}

      {editing && (
        <div className="roles-editor">
          <div className="roles-editor-header">
            <div>
              <strong>Configuration locale</strong>
              <span>
                Cette configuration sert uniquement à l’affichage et aux liens ChatGPT.
                GitHub reste la source de vérité du développement.
              </span>
            </div>
            <button type="button" onClick={addRole}>+ Ajouter un rôle</button>
          </div>

          <div className="roles-editor-list">
            {draft.map((role, index) => (
              <div className="role-editor-row" key={role.id}>
                <div className="role-editor-order">
                  <button
                    type="button"
                    onClick={() => moveRole(index, -1)}
                    disabled={index === 0}
                    aria-label="Monter le rôle"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => moveRole(index, 1)}
                    disabled={index === draft.length - 1}
                    aria-label="Descendre le rôle"
                  >
                    ↓
                  </button>
                </div>

                <label>
                  Nom
                  <input
                    value={role.name}
                    onChange={(event) => updateRole(index, { name: event.target.value })}
                  />
                </label>

                <label>
                  Identifiant
                  <input
                    value={role.id}
                    onChange={(event) =>
                      updateRole(index, {
                        id: event.target.value
                          .toLowerCase()
                          .replace(/[^a-z0-9_-]+/g, '-')
                          .replace(/^-+/, ''),
                      })
                    }
                    spellCheck={false}
                  />
                </label>

                <label>
                  Avatar
                  <select
                    value={role.avatar}
                    onChange={(event) =>
                      updateRole(index, {
                        avatar: event.target.value as RoleConfig['avatar'],
                      })
                    }
                  >
                    {AVATAR_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {AVATAR_LABELS[option.value]} {option.label}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="role-url-field">
                  Discussion ChatGPT
                  <input
                    type="url"
                    placeholder="https://chatgpt.com/c/..."
                    value={role.chat_url}
                    onChange={(event) =>
                      updateRole(index, { chat_url: event.target.value })
                    }
                  />
                </label>

                <label className="role-enabled">
                  <input
                    type="checkbox"
                    checked={role.enabled}
                    onChange={(event) =>
                      updateRole(index, { enabled: event.target.checked })
                    }
                  />
                  Affiché
                </label>

                <button
                  className="role-delete"
                  type="button"
                  onClick={() => removeRole(index)}
                >
                  Supprimer
                </button>
              </div>
            ))}
          </div>

          <div className="roles-editor-footer">
            <button type="button" onClick={() => setEditing(false)}>
              Annuler
            </button>
            <button
              className="primary"
              type="button"
              onClick={() => void saveRoles()}
              disabled={saving}
            >
              {saving ? 'Enregistrement…' : 'Enregistrer les rôles'}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
