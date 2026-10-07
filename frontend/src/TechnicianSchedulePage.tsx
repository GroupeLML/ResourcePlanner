import { resourceDisplayName } from "./resourceLabels";
import { useEffect, useMemo, useState } from "react";

import { ApiError, ShiftReadModel } from "./api";
import { addDays, formatDay, formatWeekRange, parseIsoDate, startOfWeek, toIsoDate } from "./dates";
import { getMySchedule, TechnicianScheduleReadModel } from "./technicianScheduleApi";

type ScheduleMode = "today" | "tomorrow" | "week";

function hours(value: number) {
  return new Intl.NumberFormat("fr-CA", {
    maximumFractionDigits: 1,
    minimumFractionDigits: value % 1 ? 1 : 0,
  }).format(value);
}

function confirmationLabel(value: string | null) {
  const normalized = (value ?? "").trim().toLocaleLowerCase("fr-CA");
  if (normalized.startsWith("confirm")) return "Confirmée";
  if (normalized.startsWith("tent")) return "Tentative";
  return value || "Non précisée";
}

function confirmationClass(value: string | null) {
  const normalized = (value ?? "").trim().toLocaleLowerCase("fr-CA");
  if (normalized.startsWith("confirm")) return "confirmed";
  if (normalized.startsWith("tent")) return "tentative";
  return "unknown";
}

function ShiftCard({ shift }: { shift: ShiftReadModel }) {
  const details = [
    shift.allocation_type,
    shift.outside_standard_hours ? "Hors horaire" : null,
    shift.demand_number ? `Demande ${shift.demand_number}` : null,
  ].filter(Boolean);

  return (
    <article className="my-shift-card">
      <div className="my-shift-card__heading">
        <div>
          <span>{shift.project_number || "Projet"}</span>
          <strong>{shift.project_name || shift.project_number || "Quart planifié"}</strong>
        </div>
        <strong>{hours(shift.hours)} h</strong>
      </div>
      <div className="my-shift-card__badges">
        <span className={`my-shift-badge my-shift-badge--${confirmationClass(shift.confirmation)}`}>
          {confirmationLabel(shift.confirmation)}
        </span>
        {details.map((detail) => <span key={String(detail)}>{detail}</span>)}
      </div>
      {shift.note && <p>{shift.note}</p>}
      {(shift.project_manager || shift.requester) && (
        <small>
          {shift.project_manager ? `Chargé de projet : ${shift.project_manager}` : ""}
          {shift.project_manager && shift.requester ? " · " : ""}
          {shift.requester ? `Demandeur : ${shift.requester}` : ""}
        </small>
      )}
    </article>
  );
}

export default function TechnicianSchedulePage() {
  const [mode, setMode] = useState<ScheduleMode>("today");
  const [weekStart, setWeekStart] = useState(() => startOfWeek(new Date()));
  const [schedule, setSchedule] = useState<TechnicianScheduleReadModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const window = useMemo(() => {
    const today = new Date();
    if (mode === "today") {
      const day = toIsoDate(today);
      return { start: day, end: day };
    }
    if (mode === "tomorrow") {
      const day = toIsoDate(addDays(today, 1));
      return { start: day, end: day };
    }
    return {
      start: toIsoDate(weekStart),
      end: toIsoDate(addDays(weekStart, 6)),
    };
  }, [mode, weekStart]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getMySchedule(window.start, window.end, controller.signal)
      .then(setSchedule)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        if (reason instanceof ApiError) {
          setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
          return;
        }
        setError(reason instanceof Error ? reason.message : "Impossible de charger votre horaire.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [window.start, window.end]);

  const groupedShifts = useMemo(() => {
    const groups = new Map<string, ShiftReadModel[]>();
    for (const shift of schedule?.shifts ?? []) {
      const rows = groups.get(shift.work_date) ?? [];
      rows.push(shift);
      groups.set(shift.work_date, rows);
    }
    return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [schedule]);

  const totalHours = (schedule?.shifts ?? []).reduce((sum, shift) => sum + Number(shift.hours || 0), 0);
  const title = mode === "today"
    ? "Aujourd’hui"
    : mode === "tomorrow"
      ? "Demain"
      : `Semaine du ${formatWeekRange(weekStart)}`;

  return (
    <section className="technician-schedule-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Mon horaire</span>
          <h1>{title}</h1>
          <p>Vos quarts sont déterminés par la ressource liée à votre identité RessourcePlanner.</p>
        </div>
        {mode === "week" && (
          <div className="week-navigation" role="group" aria-label="Navigation de mon horaire">
            <button type="button" onClick={() => setWeekStart((value) => addDays(value, -7))}>← Précédente</button>
            <button type="button" onClick={() => setWeekStart(startOfWeek(new Date()))}>Cette semaine</button>
            <button type="button" onClick={() => setWeekStart((value) => addDays(value, 7))}>Suivante →</button>
          </div>
        )}
      </div>

      <div className="my-schedule-tabs" role="tablist" aria-label="Période de mon horaire">
        <button type="button" className={mode === "today" ? "active" : ""} onClick={() => setMode("today")}>Aujourd’hui</button>
        <button type="button" className={mode === "tomorrow" ? "active" : ""} onClick={() => setMode("tomorrow")}>Demain</button>
        <button type="button" className={mode === "week" ? "active" : ""} onClick={() => setMode("week")}>Ma semaine</button>
      </div>

      {error && (
        <div className="error-panel">
          <strong>Votre horaire n’a pas pu être chargé.</strong>
          <span>{error}</span>
        </div>
      )}

      {!error && schedule?.link_status === "UNLINKED" && (
        <section className="my-schedule-state">
          <span className="eyebrow">Compte non lié</span>
          <h2>Aucune ressource n’est liée à votre compte.</h2>
          <p>Votre accès fonctionne, mais RessourcePlanner ne sait pas encore quel profil de ressource correspond à votre identité.</p>
        </section>
      )}

      {!error && schedule?.link_status === "RESOURCE_NOT_FOUND" && (
        <section className="my-schedule-state">
          <span className="eyebrow">Liaison à corriger</span>
          <h2>La ressource liée n’existe pas dans le planning.</h2>
          <p>L’identifiant employé est connu, mais aucun profil ressource correspondant n’est présent. Un administrateur doit corriger la synchronisation ou la liaison.</p>
        </section>
      )}

      {!error && schedule?.link_status === "LINKED" && schedule.resource && (
        <>
          <div className="my-schedule-summary">
            <article>
              <span>Ressource</span>
              <strong>{resourceDisplayName(schedule.resource.name)}</strong>
              <small>{schedule.resource.resource_class || schedule.resource.competencies || "Profil planifiable"}</small>
            </article>
            <article>
              <span>Heures affichées</span>
              <strong>{hours(totalHours)} h</strong>
              <small>{schedule.shifts.length} quart(s)</small>
            </article>
            <article>
              <span>État</span>
              <strong>{schedule.resource.active ? "Active" : "Inactive"}</strong>
              <small>{schedule.resource.active ? "Ressource disponible pour la planification" : "Historique conservé, nouvelles affectations à vérifier"}</small>
            </article>
          </div>

          {loading ? (
            <div className="my-schedule-state"><p>Chargement de votre horaire…</p></div>
          ) : groupedShifts.length === 0 ? (
            <div className="my-schedule-state">
              <h2>Aucun quart dans cette période.</h2>
              <p>Il n’y a actuellement aucune affectation planifiée entre {window.start} et {window.end}.</p>
            </div>
          ) : (
            <div className="my-schedule-days">
              {groupedShifts.map(([workDate, shifts]) => (
                <section className="my-schedule-day" key={workDate}>
                  <header>
                    <span>{formatDay(parseIsoDate(workDate))}</span>
                    <strong>{hours(shifts.reduce((sum, shift) => sum + Number(shift.hours || 0), 0))} h</strong>
                  </header>
                  <div className="my-schedule-day__shifts">
                    {shifts.map((shift) => <ShiftCard shift={shift} key={shift.allocation_id} />)}
                  </div>
                </section>
              ))}
            </div>
          )}
        </>
      )}

      {!error && loading && !schedule && (
        <div className="my-schedule-state"><p>Chargement de votre horaire…</p></div>
      )}
    </section>
  );
}
