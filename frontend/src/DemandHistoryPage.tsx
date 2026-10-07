import { useEffect, useMemo, useState } from "react";

import { DemandReadModel, type ViewScope, getDemand, getDemands } from "./api";
import { DemandHistoryReadModel, getDemandHistory } from "./demandHistoryApi";

function formatDateTime(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function statusTransition(event: DemandHistoryReadModel) {
  if (event.previous_status && event.status && event.previous_status !== event.status) {
    return `${event.previous_status} → ${event.status}`;
  }
  return event.status || event.previous_status || null;
}

type DemandHistoryPageProps = {
  demandNumber?: string;
  viewScope?: ViewScope;
  embedded?: boolean;
  refreshToken?: number;
};

export default function DemandHistoryPage({
  demandNumber,
  viewScope,
  embedded = false,
  refreshToken = 0,
}: DemandHistoryPageProps = {}) {
  const [demands, setDemands] = useState<DemandReadModel[]>([]);
  const [selectedNumber, setSelectedNumber] = useState("");
  const [history, setHistory] = useState<DemandHistoryReadModel[]>([]);
  const [loadingDemands, setLoadingDemands] = useState(true);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoadingDemands(true);
    const request = demandNumber
      ? getDemand(demandNumber, controller.signal, viewScope).then((row) => [row])
      : getDemands(controller.signal);
    request
      .then((rows) => {
        setDemands(rows);
        const nextNumber = demandNumber || rows[0]?.number || "";
        setSelectedNumber(nextNumber);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Impossible de charger les demandes.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDemands(false);
      });
    return () => controller.abort();
  }, [demandNumber, viewScope]);

  useEffect(() => {
    if (!selectedNumber) {
      setHistory([]);
      return;
    }
    const controller = new AbortController();
    setLoadingHistory(true);
    setError(null);
    getDemandHistory(selectedNumber, controller.signal, viewScope)
      .then(setHistory)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Impossible de charger l’historique.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingHistory(false);
      });
    return () => controller.abort();
  }, [selectedNumber, refreshToken, viewScope]);

  const selectedDemand = useMemo(
    () => demands.find((row) => row.number === selectedNumber) ?? null,
    [demands, selectedNumber],
  );

  return (
    <section className="demand-history-page">
      {!embedded && (      <header className="demand-history-header">
        <div>
          <span className="eyebrow">Audit</span>
          <h2>Historique des demandes</h2>
          <p>Chronologie enregistrée par le backend pour chaque demande de main-d’œuvre.</p>
        </div>
        <label className="demand-history-selector">
          <span>Demande</span>
          <select
            value={selectedNumber}
            onChange={(event) => setSelectedNumber(event.target.value)}
            disabled={loadingDemands || demands.length === 0}
          >
            {demands.length === 0 ? (
              <option value="">Aucune demande</option>
            ) : (
              demands.map((demand) => (
                <option key={demand.number} value={demand.number}>
                  {demand.number} — {demand.project_number || "Sans projet"}
                </option>
              ))
            )}
          </select>
        </label>
      </header>)}

      {selectedDemand && (
        <div className="demand-history-summary">
          <strong>{selectedDemand.number}</strong>
          <span>{selectedDemand.project_name || selectedDemand.project_number || "Projet non renseigné"}</span>
          <span className="demand-history-status">{selectedDemand.status}</span>
        </div>
      )}

      {error && <div className="error-panel">{error}</div>}

      {loadingHistory ? (
        <div className="demand-history-empty">Chargement de l’historique…</div>
      ) : selectedNumber && history.length === 0 ? (
        <div className="demand-history-empty">
          Aucun événement d’historique n’est enregistré pour cette demande.
        </div>
      ) : (
        <ol className="demand-history-timeline">
          {history.map((event, index) => {
            const transition = statusTransition(event);
            return (
              <li key={`${event.occurred_at}-${event.action}-${index}`}>
                <div className="demand-history-marker" aria-hidden="true" />
                <article className="demand-history-event">
                  <div className="demand-history-event-heading">
                    <div>
                      <strong>{event.action}</strong>
                      {transition && <span className="demand-history-transition">{transition}</span>}
                    </div>
                    <time>{formatDateTime(event.occurred_at)}</time>
                  </div>
                  <div className="demand-history-meta">
                    <span>{event.actor_name || "Acteur non enregistré"}</span>
                  </div>
                  {event.comment && <p>{event.comment}</p>}
                  {event.details && <pre>{event.details}</pre>}
                </article>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
