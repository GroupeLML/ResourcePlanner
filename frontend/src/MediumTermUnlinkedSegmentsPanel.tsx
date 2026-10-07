import { resourceDisplayName } from "./resourceLabels";
import { useMemo, useState } from "react";

import {
  ApiError,
  MediumTermUnlinkedSegmentReadModel,
  WorkPackageReadModel,
  linkDemandToWorkPackage,
} from "./api";
import { updateSegment } from "./segments-api";

type FilterMode = "all" | "anomaly" | "adhoc";

function hours(value: number) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 1 }).format(value);
}

function classificationLabel(row: MediumTermUnlinkedSegmentReadModel) {
  switch (row.classification) {
    case "REQUEST_UNLINKED":
      return "Demande sans WorkPackage";
    case "AD_HOC_ALLOWED":
      return "Ad hoc légitime";
    case "BROKEN_REFERENCE":
      return "Référence WorkPackage invalide";
    default:
      return "Segment orphelin";
  }
}

export default function MediumTermUnlinkedSegmentsPanel({
  rows,
  workPackages,
  loading,
  onOpenDemands,
  onOpenSegment,
  onLinked,
}: {
  rows: MediumTermUnlinkedSegmentReadModel[];
  workPackages: WorkPackageReadModel[];
  loading: boolean;
  onOpenDemands: () => void;
  onOpenSegment: (segmentId: string) => void;
  onLinked: () => void;
}) {
  const [filter, setFilter] = useState<FilterMode>("all");
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const visible = useMemo(
    () => rows.filter((row) => (
      filter === "all"
      || (filter === "anomaly" && row.anomaly)
      || (filter === "adhoc" && row.classification === "AD_HOC_ALLOWED")
    )),
    [rows, filter],
  );

  const anomalyCount = rows.filter((row) => row.anomaly).length;
  const adHocCount = rows.filter((row) => row.classification === "AD_HOC_ALLOWED").length;

  async function link(row: MediumTermUnlinkedSegmentReadModel) {
    const workPackageRef = targets[row.segment_id] || "";
    if (!workPackageRef || busy) return;
    setBusy(row.segment_id);
    setError(null);
    try {
      if (row.link_target === "DEMAND") {
        if (!row.demand_number) throw new Error("La demande source est introuvable.");
        await linkDemandToWorkPackage(row.demand_number, workPackageRef);
      } else {
        await updateSegment(row.segment_id, { source_effort_id: workPackageRef });
      }
      setTargets((current) => {
        const next = { ...current };
        delete next[row.segment_id];
        return next;
      });
      onLinked();
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de rattacher le segment.");
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="mt-unlinked-panel" aria-label="Segments sans WorkPackage">
      <div className="mt-unlinked-heading">
        <div>
          <span className="eyebrow">À classer au moyen terme</span>
          <strong>Segments sans WorkPackage</strong>
          <small>
            Les Quick Shifts/ad hoc peuvent rester sans WorkPackage. Les demandes classiques sont signalées comme anomalies.
          </small>
        </div>
        <div className="mt-unlinked-summary">
          <span>{rows.length} visible(s)</span>
          <span className={anomalyCount ? "is-warning" : ""}>{anomalyCount} anomalie(s)</span>
          <span>{adHocCount} ad hoc</span>
        </div>
      </div>

      <div className="mt-unlinked-filter">
        <button type="button" className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>Tous</button>
        <button type="button" className={filter === "anomaly" ? "active" : ""} onClick={() => setFilter("anomaly")}>Anomalies</button>
        <button type="button" className={filter === "adhoc" ? "active" : ""} onClick={() => setFilter("adhoc")}>Ad hoc légitimes</button>
      </div>

      {error && <div className="error-panel"><span>{error}</span></div>}

      {loading ? (
        <div className="mt-unlinked-empty">Chargement des segments sans WorkPackage…</div>
      ) : visible.length === 0 ? (
        <div className="mt-unlinked-empty">Aucun segment ne correspond à ce filtre.</div>
      ) : (
        <div className="mt-unlinked-list">
          {visible.map((row) => {
            const candidates = workPackages.filter((item) => item.project_number === row.project_number);
            const task = [row.task_code, row.task_label].filter(Boolean).join(" — ");
            return (
              <article className={`mt-unlinked-card ${row.anomaly ? "is-anomaly" : "is-adhoc"}`} key={row.segment_id}>
                <div className="mt-unlinked-card-heading">
                  <div>
                    <strong>{row.project_number} — {row.project_name}</strong>
                    <span>{classificationLabel(row)}</span>
                  </div>
                  <strong>{hours(row.planned_hours)} h</strong>
                </div>

                <div className="mt-unlinked-meta">
                  <span>{row.segment_id}</span>
                  {row.demand_number && <span>Demande {row.demand_number}</span>}
                  {task && <span>{task}</span>}
                  <span>{row.start_date} → {row.end_date}</span>
                  {row.resource_name && <span>Cible automatique : {resourceDisplayName(row.resource_name)}</span>}
                  <span>{row.status}</span>
                </div>

                {row.description && <p>{row.description}</p>}

                {row.reapproval_on_link && (
                  <div className="mt-link-warning">
                    Le rattachement modifiera une demande déjà approuvée et déclenchera une nouvelle approbation selon les règles actuelles.
                  </div>
                )}

                <div className="mt-unlinked-actions">
                  <button type="button" onClick={() => onOpenSegment(row.segment_id)}>Ouvrir le segment</button>
                  {row.demand_number && <button type="button" onClick={onOpenDemands}>Voir la demande</button>}
                  <select
                    aria-label={`WorkPackage cible pour ${row.segment_id}`}
                    value={targets[row.segment_id] || ""}
                    onChange={(event) => setTargets((current) => ({ ...current, [row.segment_id]: event.target.value }))}
                    disabled={busy === row.segment_id || candidates.length === 0}
                  >
                    <option value="">Rattacher à un WorkPackage…</option>
                    {candidates.map((item) => (
                      <option value={item.reference} key={item.id}>
                        {item.code || item.reference} — {item.name}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="primary-action"
                    disabled={!targets[row.segment_id] || busy === row.segment_id}
                    onClick={() => void link(row)}
                  >
                    {busy === row.segment_id ? "Rattachement…" : "Rattacher"}
                  </button>
                </div>

                {candidates.length === 0 && (
                  <small className="mt-no-target">Aucun WorkPackage actif pour ce projet dans le référentiel actuel.</small>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
