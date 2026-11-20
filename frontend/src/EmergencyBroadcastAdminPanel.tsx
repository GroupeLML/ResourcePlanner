import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";
import {
  EmergencyAcknowledgmentHistoryItem,
  EmergencyAdminStatus,
  EmergencyBroadcastAdminRecord,
  createEmergencyBroadcast,
  endEmergencyBroadcast,
  getEmergencyAcknowledgmentHistory,
  getEmergencyAdminStatus,
  getEmergencyBroadcastHistory,
} from "./emergencyBroadcastApi";

const ADMIN_POLL_MIN_MS = 8_000;
const ADMIN_POLL_MAX_MS = 12_000;

function randomAdminDelay() {
  return Math.floor(ADMIN_POLL_MIN_MS + Math.random() * (ADMIN_POLL_MAX_MS - ADMIN_POLL_MIN_MS));
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function utf16Length(value: string) {
  return value.length;
}

function message(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function commandId() {
  return crypto.randomUUID();
}

export default function EmergencyBroadcastAdminPanel() {
  const [status, setStatus] = useState<EmergencyAdminStatus | null>(null);
  const [history, setHistory] = useState<EmergencyBroadcastAdminRecord[]>([]);
  const [historyCursor, setHistoryCursor] = useState<number | null>(0);
  const [selectedHistory, setSelectedHistory] = useState<EmergencyBroadcastAdminRecord | null>(null);
  const [receipts, setReceipts] = useState<EmergencyAcknowledgmentHistoryItem[]>([]);
  const [receiptCursor, setReceiptCursor] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [broadcastMessage, setBroadcastMessage] = useState("");
  const [createCommandId, setCreateCommandId] = useState(commandId);
  const [creating, setCreating] = useState(false);
  const [ending, setEnding] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const refreshStatus = useCallback(async (signal?: AbortSignal) => {
    const next = await getEmergencyAdminStatus(signal);
    setStatus(next);
    setStale(false);
  }, []);

  const refreshHistory = useCallback(async (signal?: AbortSignal) => {
    const page = await getEmergencyBroadcastHistory(0, 25, signal);
    setHistory(page.items);
    setHistoryCursor(page.next_cursor);
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setRefreshing(true);
    try {
      await Promise.all([refreshStatus(signal), refreshHistory(signal)]);
      setError(null);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setStale(true);
      setError(message(reason, "Impossible de confirmer l’état courant des diffusions."));
    } finally {
      setRefreshing(false);
    }
  }, [refreshHistory, refreshStatus]);

  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;

    const loop = async () => {
      if (stopped) return;
      try {
        await refreshStatus(controller.signal);
        setError(null);
      } catch (reason) {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setStale(true);
        setError(message(reason, "Impossible de confirmer l’état courant des diffusions."));
      } finally {
        if (!stopped) {
          timerRef.current = window.setTimeout(loop, randomAdminDelay());
        }
      }
    };

    void refreshHistory(controller.signal).catch((reason) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setStale(true);
        setError(message(reason, "Impossible de charger l’historique des diffusions."));
      }
    });
    void loop();

    return () => {
      stopped = true;
      controller.abort();
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, [refreshHistory, refreshStatus]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (creating) return;

    const normalizedTitle = title.trim();
    const normalizedMessage = broadcastMessage.trim();
    if (!normalizedTitle || !normalizedMessage) {
      setError("Le titre et le message sont requis.");
      return;
    }
    if (utf16Length(normalizedTitle) > 160) {
      setError("Le titre doit contenir au maximum 160 unités UTF-16.");
      return;
    }
    if (utf16Length(normalizedMessage) > 4000) {
      setError("Le message doit contenir au maximum 4000 unités UTF-16.");
      return;
    }
    if (!window.confirm("Diffuser ce message maintenant à tous les utilisateurs connectés?")) {
      return;
    }

    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      await createEmergencyBroadcast({
        creation_command_id: createCommandId,
        title: normalizedTitle,
        message: normalizedMessage,
        confirmed: true,
      });
      setTitle("");
      setBroadcastMessage("");
      setCreateCommandId(commandId());
      setNotice("La diffusion d’urgence est active.");
      await refresh();
    } catch (reason) {
      setError(message(
        reason,
        reason instanceof ApiError && reason.code === "emergency_broadcast_already_active"
          ? "Une autre diffusion est déjà active."
          : "Impossible de créer la diffusion.",
      ));
    } finally {
      setCreating(false);
    }
  }

  async function endActive() {
    const active = status?.broadcast;
    if (!active || ending) return;
    if (!window.confirm("Terminer cette diffusion d’urgence maintenant?")) return;

    setEnding(true);
    setError(null);
    setNotice(null);
    try {
      await endEmergencyBroadcast(active.id);
      setNotice("La diffusion est terminée.");
      await refresh();
    } catch (reason) {
      setStale(true);
      setError(message(reason, "Impossible de confirmer la fin de la diffusion."));
    } finally {
      setEnding(false);
    }
  }

  async function loadMoreHistory() {
    if (historyCursor === null) return;
    try {
      const page = await getEmergencyBroadcastHistory(historyCursor, 25);
      setHistory((current) => [...current, ...page.items]);
      setHistoryCursor(page.next_cursor);
    } catch (reason) {
      setError(message(reason, "Impossible de charger la suite de l’historique."));
    }
  }

  async function openReceipts(record: EmergencyBroadcastAdminRecord) {
    setSelectedHistory(record);
    setReceipts([]);
    setReceiptCursor(null);
    try {
      const page = await getEmergencyAcknowledgmentHistory(record.id, 0, 100);
      setReceipts(page.items);
      setReceiptCursor(page.next_cursor);
    } catch (reason) {
      setError(message(reason, "Impossible de charger les acquittements."));
    }
  }

  async function loadMoreReceipts() {
    if (!selectedHistory || receiptCursor === null) return;
    try {
      const page = await getEmergencyAcknowledgmentHistory(
        selectedHistory.id,
        receiptCursor,
        100,
      );
      setReceipts((current) => [...current, ...page.items]);
      setReceiptCursor(page.next_cursor);
    } catch (reason) {
      setError(message(reason, "Impossible de charger la suite des acquittements."));
    }
  }

  const active = status?.broadcast?.state === "ACTIVE" ? status.broadcast : null;

  return (
    <section className="emergency-admin-panel" aria-labelledby="emergency-admin-title">
      <header className="configuration-section-heading">
        <div>
          <span className="eyebrow">Diffusion d’urgence</span>
          <h3 id="emergency-admin-title">Avis global aux utilisateurs</h3>
          <p>
            Les présences sont éphémères et indicatives. Une liste incomplète ne constitue jamais
            une preuve que personne n’attend un acquittement.
          </p>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={refreshing}>
          {refreshing ? "Actualisation…" : "Actualiser"}
        </button>
      </header>

      {stale && (
        <div className="emergency-admin-stale" role="status">
          Données affichées possiblement périmées — l’état courant ne peut pas être confirmé.
        </div>
      )}
      {error && <div className="error-panel"><strong>Erreur</strong><span>{error}</span></div>}
      {notice && <div className="configuration-notice">{notice}</div>}

      <div className="emergency-admin-grid">
        <form className="configuration-card emergency-create-form" onSubmit={submit}>
          <span className="eyebrow">Nouvelle diffusion</span>
          <label>
            Titre
            <input
              value={title}
              maxLength={160}
              onChange={(event) => setTitle(event.target.value)}
              disabled={Boolean(active) || creating}
            />
            <small>{utf16Length(title)} / 160 unités UTF-16</small>
          </label>
          <label>
            Message
            <textarea
              value={broadcastMessage}
              maxLength={4000}
              rows={7}
              onChange={(event) => setBroadcastMessage(event.target.value)}
              disabled={Boolean(active) || creating}
            />
            <small>{utf16Length(broadcastMessage)} / 4000 unités UTF-16</small>
          </label>
          <button type="submit" disabled={Boolean(active) || creating || stale}>
            {creating ? "Diffusion…" : "Diffuser maintenant"}
          </button>
          {active && <small>Terminez la diffusion active avant d’en créer une autre.</small>}
        </form>

        <div className="configuration-card emergency-active-card">
          <span className="eyebrow">Diffusion active</span>
          {active ? (
            <>
              <h4>{active.title}</h4>
              <p className="emergency-admin-message">{active.message}</p>
              <small>Activée : {formatDateTime(active.activated_at)}</small>
              <div className="emergency-admin-counts">
                <span><strong>{status?.active_user_count ?? 0}</strong> actifs observés</span>
                <span><strong>{status?.active_pending_count ?? 0}</strong> actifs en attente</span>
                <span><strong>{status?.active_acknowledged_count ?? 0}</strong> actifs acquittés</span>
              </div>
              <button type="button" onClick={() => void endActive()} disabled={ending || stale}>
                {ending ? "Fin en cours…" : "Terminer la diffusion"}
              </button>
            </>
          ) : (
            <p>Aucune diffusion active confirmée.</p>
          )}
          <small>
            Présence : {status?.presence_warming
              ? "réchauffement après démarrage"
              : status?.presence_complete
                ? "suivi complet pour la fenêtre observée"
                : "suivi incomplet"}
          </small>
          <small>Époque : {status?.observation_epoch ?? "—"}</small>
          <small>Généré : {formatDateTime(status?.generated_at)}</small>
        </div>
      </div>

      <div className="configuration-card">
        <div className="configuration-section-heading">
          <div>
            <span className="eyebrow">Présence / acquittement</span>
            <h4>Utilisateurs observés</h4>
          </div>
        </div>
        <div className="emergency-admin-table-wrap">
          <table className="emergency-admin-table">
            <thead>
              <tr>
                <th>Utilisateur</th>
                <th>Présence</th>
                <th>Dernière observation</th>
                <th>Acquittement</th>
              </tr>
            </thead>
            <tbody>
              {(status?.users ?? []).map((user) => (
                <tr key={user.user_id}>
                  <td>{user.display_name || user.user_id}</td>
                  <td>
                    {user.active
                      ? user.pending ? "Actif · en attente" : "Actif"
                      : user.presence_state === "recent_inactive" ? "Récent · inactif" : "Inconnue"}
                  </td>
                  <td>{formatDateTime(user.last_seen_at)}</td>
                  <td>{user.acknowledged_at ? formatDateTime(user.acknowledged_at) : "Non acquitté"}</td>
                </tr>
              ))}
              {!status?.users?.length && (
                <tr><td colSpan={4}>Aucune observation disponible.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="configuration-card">
        <span className="eyebrow">Historique</span>
        <div className="emergency-history-list">
          {history.map((record) => (
            <button
              type="button"
              key={record.id}
              className={selectedHistory?.id === record.id ? "active" : ""}
              onClick={() => void openReceipts(record)}
            >
              <strong>{record.title}</strong>
              <span>{record.state} · {formatDateTime(record.activated_at)}</span>
            </button>
          ))}
        </div>
        {historyCursor !== null && (
          <button type="button" onClick={() => void loadMoreHistory()}>Charger la suite</button>
        )}

        {selectedHistory && (
          <div className="emergency-receipt-history">
            <h4>Acquittements — {selectedHistory.title}</h4>
            <ul>
              {receipts.map((receipt) => (
                <li key={receipt.user_id}>
                  <span>{receipt.display_name || receipt.user_id}</span>
                  <span>
                    {formatDateTime(receipt.acknowledged_at)}
                    {receipt.late ? " · tardif" : ""}
                  </span>
                </li>
              ))}
            </ul>
            {!receipts.length && <p>Aucun acquittement enregistré.</p>}
            {receiptCursor !== null && (
              <button type="button" onClick={() => void loadMoreReceipts()}>
                Charger la suite
              </button>
            )}
          </div>
        )}
      </div>

      <p className="emergency-admin-operator-note">
        Ces indicateurs n’autorisent jamais automatiquement une maintenance ou un déploiement.
        La décision demeure humaine et opérateur.
      </p>
    </section>
  );
}
