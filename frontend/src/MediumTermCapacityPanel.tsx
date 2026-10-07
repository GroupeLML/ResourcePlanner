import { Fragment, useState } from "react";
import {
  MediumTermClassWeekReadModel,
  MediumTermCompetencyWeekReadModel,
  MediumTermWeekReadModel,
} from "./api";
import { formatWeekRange, parseIsoDate } from "./dates";

const WEEK_DIAGNOSTIC_LABELS: Record<string, string> = {
  WORK_PACKAGE_LOAD_INCOMPLETE: "Charge WorkPackage non disponible",
  WORKFORCE_CAPACITY_ZERO: "Capacité workforce nulle",
};

const COMPETENCY_DIAGNOSTIC_LABELS: Record<string, string> = {
  COMPETENCY_REQUESTED_HOURS_UNAVAILABLE: "Heures demandées incomplètes",
  COMPETENCY_ALTERNATIVE_UNRESOLVED: "Alternative non sélectionnée : enveloppe prudente",
  COMPETENCY_REFERENCE_UNRESOLVED: "Référence de compétence historique non résolue",
  COMPETENCY_GROUP_CLASS_INACTIVE: "Classe de regroupement inactive",
  COMPETENCY_GROUP_CLASS_UNRESOLVED: "Classe de regroupement introuvable",
  COMPETENCY_CAPACITY_ZERO: "Capacité théorique nulle",
  COMPETENCY_CAPACITY_EXCEEDED: "Heures demandées supérieures à la capacité théorique",
  COMPETENCY_COMMON_QUALIFICATION_ZERO: "Aucune capacité avec toutes les compétences requises",
  COMPETENCY_COMMON_QUALIFICATION_EXCEEDED: "Qualification commune insuffisante",
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

function requestedHours(value: number | null) {
  if (value == null) return "Heures demandées inconnues";
  return `${number(value)} h demandées`;
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

function competencyDiagnosticLabel(code: string) {
  return COMPETENCY_DIAGNOSTIC_LABELS[code] || code;
}

function classKey(row: { resource_class_code: string | null }) {
  return row.resource_class_code || "__UNCLASSIFIED__";
}

type CapacityClassGroup = {
  key: string;
  code: string | null;
  label: string;
};

type CompetencyIdentity = {
  id: string;
  name: string;
  active: boolean;
};

export default function MediumTermCapacityPanel({
  weeks,
  diagnostics,
  loading,
}: {
  weeks: MediumTermWeekReadModel[];
  diagnostics: string[];
  loading: boolean;
}) {
  const [expandedClasses, setExpandedClasses] = useState<Set<string>>(() => new Set());

  const classes = new Map<string, CapacityClassGroup>();
  const competenciesByClass = new Map<string, Map<string, CompetencyIdentity>>();
  weeks.forEach((week) => {
    week.classes.forEach((row) => {
      const key = classKey(row);
      classes.set(key, {
        key,
        code: row.resource_class_code,
        label: row.resource_class_label,
      });
    });
    (week.competencies ?? []).forEach((row) => {
      const key = classKey(row);
      if (!classes.has(key)) {
        classes.set(key, {
          key,
          code: row.resource_class_code,
          label: row.resource_class_label || row.resource_class_code || "Sans classe de regroupement",
        });
      }
      const classCompetencies = competenciesByClass.get(key) ?? new Map<string, CompetencyIdentity>();
      classCompetencies.set(row.competency_id, {
        id: row.competency_id,
        name: row.competency_name,
        active: row.competency_active,
      });
      competenciesByClass.set(key, classCompetencies);
    });
  });
  const classRows = [...classes.values()];
  const template = `minmax(220px, 1.2fr) repeat(${Math.max(weeks.length, 1)}, minmax(150px, 1fr))`;

  function toggleClass(key: string) {
    setExpandedClasses((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <section className={`mt-capacity-panel ${loading ? "is-loading" : ""}`}>
      <header className="mt-capacity-heading">
        <div>
          <span className="eyebrow">Capacité hebdomadaire par classe</span>
          <h2>Charge WorkPackage / capacité · compétences</h2>
        </div>
        <p>
          Les classes comparent la charge WorkPackage à la capacité de classe. Ouvre une classe
          pour voir les heures demandées et la capacité théorique de chaque compétence.
        </p>
      </header>

      <div className="mt-competency-non-additive" role="note">
        <strong>Compétences non additives.</strong>
        <span>
          Une même demande et une même ressource peuvent apparaître dans plusieurs compétences.
          Ne somme pas les lignes de compétences pour obtenir une charge ou une capacité globale.
        </span>
      </div>

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
            <div className="mt-capacity-corner">Classe / compétence</div>
            {weeks.map((week) => (
              <div className="mt-capacity-week" key={week.week_start}>
                <strong>{formatWeekRange(parseIsoDate(week.week_start))}</strong>
              </div>
            ))}

            {classRows.map((resourceClass) => {
              const key = resourceClass.key;
              const expanded = expandedClasses.has(key);
              const competencies = [...(competenciesByClass.get(key)?.values() ?? [])]
                .sort((left, right) => left.name.localeCompare(right.name, "fr-CA"));

              return (
                <Fragment key={key}>
                  <div className="mt-capacity-label is-class" key={`label-${key}`}>
                    <button
                      className="mt-capacity-class-toggle"
                      type="button"
                      aria-expanded={expanded}
                      onClick={() => toggleClass(key)}
                    >
                      <strong>{expanded ? "▼" : "▶"} {resourceClass.label}</strong>
                      <span>
                        {competencies.length} compétence(s)
                        {resourceClass.code == null ? " · aucune classe canonique" : ""}
                      </span>
                    </button>
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
                                {loadHours(bucket.work_package_hours)} / {capacityHours(bucket.capacity_hours)} capacité de classe
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
                            <strong>Aucune mesure de classe</strong>
                            <span>· Non calculable</span>
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {expanded && competencies.length === 0 && (
                    <>
                      <div className="mt-capacity-label is-competency is-empty">
                        <span>Aucune compétence rattachée à cette classe.</span>
                      </div>
                      {weeks.map((week) => (
                        <div className="mt-capacity-cell is-competency is-unavailable" key={`${key}-empty-${week.week_start}`}>
                          <small>—</small>
                        </div>
                      ))}
                    </>
                  )}

                  {expanded && competencies.map((competency) => (
                    <Fragment key={`${key}-${competency.id}`}>
                      <div className="mt-capacity-label is-competency">
                        <strong>{competency.name}</strong>
                        <span>
                          Heures demandées / capacité théorique
                          {!competency.active ? " · compétence inactive" : ""}
                        </span>
                      </div>
                      {weeks.map((week) => {
                        const bucket = (week.competencies ?? []).find(
                          (row: MediumTermCompetencyWeekReadModel) => row.competency_id === competency.id,
                        );
                        const diagnosticsText = bucket?.diagnostics
                          .map(competencyDiagnosticLabel)
                          .join(" · ") || "";
                        return (
                          <div
                            className={`mt-capacity-cell is-competency ${bucket ? `is-${bucket.state}` : "is-unavailable"}`}
                            key={`${competency.id}-${week.week_start}`}
                            title={diagnosticsText || undefined}
                          >
                            {bucket ? (
                              <>
                                <div className="mt-capacity-main">
                                  <strong>
                                    {requestedHours(bucket.requested_hours)} / {capacityHours(bucket.capacity_hours)} capacité théorique
                                  </strong>
                                  <span>· {percent(bucket.utilization)}</span>
                                </div>
                                <small>
                                  {STATE_LABELS[bucket.state]} · {bucket.qualifying_resource_count} ressource(s) qualifiée(s)
                                  {bucket.non_additive ? " · non additive" : ""}
                                </small>
                                {bucket.diagnostics.length > 0 && (
                                  <div className="mt-capacity-detail">
                                    {bucket.diagnostics.map((code) => (
                                      <span key={code}>{competencyDiagnosticLabel(code)}</span>
                                    ))}
                                  </div>
                                )}
                              </>
                            ) : (
                              <div className="mt-capacity-main">
                                <strong>Aucune mesure de compétence</strong>
                                <span>· Non calculable</span>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </Fragment>
                  ))}
                </Fragment>
              );
            })}
          </div>
        </div>
      )}

      {weeks.some((week) => (
        (week.competency_diagnostics ?? []).length > 0
        || (week.competency_combinations ?? []).some((row) => row.diagnostics.length > 0)
      )) && (
        <div className="mt-competency-advisories" role="status">
          <strong>Diagnostics compétences</strong>
          {weeks.map((week) => {
            const weekDiagnostics = week.competency_diagnostics ?? [];
            const combinations = (week.competency_combinations ?? [])
              .filter((row) => row.diagnostics.length > 0);
            if (weekDiagnostics.length === 0 && combinations.length === 0) return null;
            return (
              <div className="mt-competency-advisory-week" key={`diagnostic-${week.week_start}`}>
                <span>{formatWeekRange(parseIsoDate(week.week_start))}</span>
                {weekDiagnostics.map((code) => (
                  <small key={code}>⚑ {competencyDiagnosticLabel(code)}</small>
                ))}
                {combinations.map((row) => (
                  <small key={`${row.competency_ids.join("-")}-${row.required_resource_class_code || "all"}`}>
                    ⚑ Qualification commune {row.competency_names.join(" + ")} :
                    {" "}{requestedHours(row.requested_hours)} / {capacityHours(row.common_capacity_hours)} capacité commune.
                    {" "}Diagnostic indicatif et non additif.
                  </small>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
