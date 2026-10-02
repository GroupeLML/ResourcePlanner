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

function number(value: number) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(value);
}

function loadHours(value: number | null) {
  if (value == null) return "Charge inconnue";
  return `${number(value)} h`;
}

function capacityHours(value: number) {
  return `${number(value)} h`;
}

function percent(value: number | null) {
  if (value == null) return "Non calculable";
  return `${number(value)} %`;
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
  const template = `minmax(190px, 1.15fr) repeat(${Math.max(weeks.length, 1)}, minmax(132px, 1fr))`;

  return (
    <section className={`mt-capacity-panel ${loading ? "is-loading" : ""}`}>
      <header className="mt-capacity-heading">
        <div>
          <span className="eyebrow">Capacité hebdomadaire par classe</span>
          <h2>Charge / capacité · utilisation</h2>
        </div>
        <p>
          Une ligne par classe. Charge, capacité, utilisation, état et diagnostics proviennent
          directement de FastAPI; React ne recalcule aucune règle de capacité.
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
          <div className="mt-capacity-grid is-compact" style={{ gridTemplateColumns: template }}>
            <div className="mt-capacity-corner">Classe</div>
            {weeks.map((week) => (
              <div className="mt-capacity-week" key={week.week_start}>
                <strong>{formatWeekRange(parseIsoDate(week.week_start))}</strong>
              </div>
            ))}

            {classRows.map(([key, resourceClass]) => (
              <>
                <div className="mt-capacity-label is-class" key={`label-${key}`}>
                  <strong>{resourceClass.label}</strong>
                  {resourceClass.code == null && (
                    <span>Non classé · aucune classe canonique déduite</span>
                  )}
                </div>
                {weeks.map((week) => {
                  const bucket = week.classes.find((row) => classKey(row) === key);
                  const diagnosticsText = bucket?.diagnostics.map(diagnosticLabel).join(" · ") || "";
                  return (
                    <div
                      className={`mt-capacity-cell is-compact ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                      key={`${key}-${week.week_start}`}
                      title={diagnosticsText || undefined}
                    >
                      {bucket ? (
                        <>
                          <div className="mt-capacity-main">
                            <strong>
                              {loadHours(bucket.work_package_hours)} / {capacityHours(bucket.capacity_hours)}
                            </strong>
                            <span>· {percent(bucket.utilization)}</span>
                          </div>
                          <small>
                            {STATE_LABELS[bucket.state]}
                            {bucket.work_package_hours == null ? " · ⚑ Charge inconnue — pas 0 h" : ""}
                          </small>
                          {bucket.diagnostics.length > 0 && (
                            <div className="mt-capacity-detail">
                              {bucket.diagnostics.map((code) => (
                                <span key={code}>{diagnosticLabel(code)}</span>
                              ))}
                            </div>
                          )}
                        </>
                      ) : (
                        <div className="mt-capacity-main">
                          <strong>Indisponible</strong>
                          <span>· Non calculable</span>
                        </div>
                      )}
                    </div>
                  );
                })}
              </>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
