import { MediumTermWeekReadModel } from "./api";
import { formatWeekRange, parseIsoDate } from "./dates";

const WEEK_DIAGNOSTIC_LABELS: Record<string, string> = {
  WORK_PACKAGE_LOAD_INCOMPLETE: "Charge WorkPackage non disponible",
  WORKFORCE_CAPACITY_ZERO: "Capacité workforce nulle",
};

const PROJECTION_DIAGNOSTIC_LABELS: Record<string, string> = {
  WEEKLY_LOAD_INCOMPLETE: "Une ou plusieurs répartitions WorkPackage sont incomplètes.",
  WORKFORCE_CAPACITY_ZERO: "Une ou plusieurs semaines ont une capacité workforce nulle.",
};

function hours(value: number | null) {
  if (value == null) return "Charge non disponible";
  return `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(value)} h`;
}

function percent(value: number | null) {
  if (value == null) return "Non calculable";
  return `${new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(value)} %`;
}

function diagnosticLabel(code: string) {
  return WEEK_DIAGNOSTIC_LABELS[code] || code;
}

export default function MediumTermCapacityPanel({
  weeks,
  diagnostics,
  loading,
}: {
  weeks: MediumTermWeekReadModel[];
  diagnostics: string[];
  loading: boolean;
}) {
  const template = `170px repeat(${Math.max(weeks.length, 1)}, minmax(96px, 1fr))`;

  return (
    <section className={`mt-capacity-panel ${loading ? "is-loading" : ""}`}>
      <header className="mt-capacity-heading">
        <div>
          <span className="eyebrow">Capacité hebdomadaire</span>
          <h2>Charge WorkPackage / capacité workforce</h2>
        </div>
        <p>
          Valeurs projetées par FastAPI avant déduction des Shift. Le filtre projet s’applique à
          la charge WorkPackage, pas à la capacité workforce globale.
        </p>
      </header>

      {diagnostics.length > 0 && (
        <div className="mt-capacity-diagnostics" role="status">
          {diagnostics.map((code) => (
            <span key={code}>⚑ {PROJECTION_DIAGNOSTIC_LABELS[code] || code}</span>
          ))}
        </div>
      )}

      {!loading && weeks.length === 0 ? (
        <div className="mt-capacity-empty">Aucune semaine projetée pour cette fenêtre.</div>
      ) : (
        <div className="mt-capacity-scroll">
          <div className="mt-capacity-grid" style={{ gridTemplateColumns: template }}>
            <div className="mt-capacity-corner">Indicateur</div>
            {weeks.map((week) => (
              <div className="mt-capacity-week" key={week.week_start}>
                <strong>{formatWeekRange(parseIsoDate(week.week_start))}</strong>
              </div>
            ))}

            <div className="mt-capacity-label">
              <strong>Capacité</strong>
              <span>Workforce disponible</span>
            </div>
            {weeks.map((week) => (
              <div
                className={`mt-capacity-cell ${week.diagnostics.includes("WORKFORCE_CAPACITY_ZERO") ? "is-warning" : ""}`}
                key={`capacity-${week.week_start}`}
              >
                <div className="mt-capacity-main"><strong>{hours(week.capacity_hours)}</strong></div>
                {week.diagnostics.includes("WORKFORCE_CAPACITY_ZERO") && (
                  <small>⚑ Capacité workforce nulle</small>
                )}
              </div>
            ))}

            <div className="mt-capacity-label">
              <strong>Charge WP</strong>
              <span>Répartition persistée</span>
            </div>
            {weeks.map((week) => (
              <div
                className={`mt-capacity-cell ${week.work_package_hours == null ? "is-warning" : ""}`}
                key={`load-${week.week_start}`}
              >
                <div className="mt-capacity-main">
                  <strong>{hours(week.work_package_hours)}</strong>
                </div>
                {week.work_package_hours == null && (
                  <small>⚑ Charge inconnue — pas 0 h</small>
                )}
              </div>
            ))}

            <div className="mt-capacity-label">
              <strong>Utilisation</strong>
              <span>Valeur backend</span>
            </div>
            {weeks.map((week) => (
              <div
                className={`mt-capacity-cell ${week.utilization == null ? "is-unavailable" : ""}`}
                key={`utilization-${week.week_start}`}
              >
                <div className="mt-capacity-main">
                  <strong>{percent(week.utilization)}</strong>
                </div>
                {week.diagnostics.length > 0 && (
                  <div className="mt-capacity-detail">
                    {week.diagnostics.map((code) => (
                      <span key={code}>{diagnosticLabel(code)}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
