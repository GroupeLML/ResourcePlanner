import { MediumTermClassWeekReadModel, MediumTermWeekReadModel } from "./api";
import { formatWeekRange, parseIsoDate } from "./dates";

const WEEK_DIAGNOSTIC_LABELS: Record<string, string> = {
  WORK_PACKAGE_LOAD_INCOMPLETE: "Charge WorkPackage non disponible",
  WORKFORCE_CAPACITY_ZERO: "Capacité workforce nulle",
};

const PROJECTION_DIAGNOSTIC_LABELS: Record<string, string> = {
  WEEKLY_LOAD_INCOMPLETE: "Une ou plusieurs répartitions WorkPackage sont incomplètes.",
  WORKFORCE_CAPACITY_ZERO: "Une ou plusieurs semaines ont une capacité workforce nulle.",
};

const STATE_LABELS: Record<MediumTermClassWeekReadModel["state"], string> = {
  available: "Disponible",
  warning: "Attention",
  overloaded: "Surchargé",
  unavailable: "Indisponible",
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

function classKey(row: MediumTermClassWeekReadModel) {
  return row.resource_class_code || "__UNCLASSIFIED__";
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
  const classes = new Map<string, { code: string | null; label: string }>();
  weeks.forEach((week) => {
    week.classes.forEach((row) => {
      classes.set(classKey(row), {
        code: row.resource_class_code,
        label: row.resource_class_label,
      });
    });
  });
  const classRows = [...classes.entries()];

  return (
    <section className={`mt-capacity-panel ${loading ? "is-loading" : ""}`}>
      <header className="mt-capacity-heading">
        <div>
          <span className="eyebrow">Capacité hebdomadaire par classe</span>
          <h2>Charge WorkPackage / capacité workforce</h2>
        </div>
        <p>
          Valeurs projetées par FastAPI avant déduction des Shift. Projet et tâche réduisent la
          charge WorkPackage; la capacité reste organisationnelle. Le filtre classe sélectionne
          la tranche workforce correspondante.
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
      ) : !loading && classRows.length === 0 ? (
        <div className="mt-capacity-empty">Aucune classe de capacité à afficher pour cette fenêtre.</div>
      ) : (
        <div className="mt-capacity-scroll">
          {classRows.map(([key, resourceClass]) => {
            const template = `170px repeat(${Math.max(weeks.length, 1)}, minmax(96px, 1fr))`;
            return (
              <section className="mt-capacity-class" key={key}>
                <header className="mt-capacity-class-heading">
                  <strong>{resourceClass.label}</strong>
                  {resourceClass.code == null && (
                    <span>Capacité et charge non qualifiées — aucune classe canonique déduite</span>
                  )}
                </header>
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
                  {weeks.map((week) => {
                    const bucket = week.classes.find((row) => classKey(row) === key);
                    return (
                      <div
                        className={`mt-capacity-cell ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                        key={`capacity-${key}-${week.week_start}`}
                      >
                        <div className="mt-capacity-main">
                          <strong>{bucket ? hours(bucket.capacity_hours) : "—"}</strong>
                        </div>
                      </div>
                    );
                  })}

                  <div className="mt-capacity-label">
                    <strong>Charge WP</strong>
                    <span>Répartition persistée</span>
                  </div>
                  {weeks.map((week) => {
                    const bucket = week.classes.find((row) => classKey(row) === key);
                    return (
                      <div
                        className={`mt-capacity-cell ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                        key={`load-${key}-${week.week_start}`}
                      >
                        <div className="mt-capacity-main">
                          <strong>{bucket ? hours(bucket.work_package_hours) : "—"}</strong>
                        </div>
                        {bucket?.work_package_hours == null && bucket && (
                          <small>⚑ Charge inconnue — pas 0 h</small>
                        )}
                      </div>
                    );
                  })}

                  <div className="mt-capacity-label">
                    <strong>Utilisation</strong>
                    <span>Valeur backend</span>
                  </div>
                  {weeks.map((week) => {
                    const bucket = week.classes.find((row) => classKey(row) === key);
                    return (
                      <div
                        className={`mt-capacity-cell ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                        key={`utilization-${key}-${week.week_start}`}
                      >
                        <div className="mt-capacity-main">
                          <strong>{bucket ? percent(bucket.utilization) : "—"}</strong>
                        </div>
                        {bucket && bucket.diagnostics.length > 0 && (
                          <div className="mt-capacity-detail">
                            {bucket.diagnostics.map((code) => (
                              <span key={code}>{diagnosticLabel(code)}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}

                  <div className="mt-capacity-label">
                    <strong>État</strong>
                    <span>Politique backend</span>
                  </div>
                  {weeks.map((week) => {
                    const bucket = week.classes.find((row) => classKey(row) === key);
                    return (
                      <div
                        className={`mt-capacity-cell ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                        key={`state-${key}-${week.week_start}`}
                      >
                        <div className="mt-capacity-main">
                          <strong>{bucket ? STATE_LABELS[bucket.state] : "Indisponible"}</strong>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}
