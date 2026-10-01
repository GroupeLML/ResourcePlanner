import { useEffect, useMemo, useState } from "react";

import { ResourceReadModel, ShiftReadModel } from "./api";
import {
  OverallocationContext,
  OverallocationPolicy,
  PlanningDropAction,
  PlanningDropEvaluation,
} from "./manualOverallocationApi";

export type PlanningDropExecutionRequest = {
  code: string;
  transferHours: number | null;
  outsideStandardHours: boolean;
  overallocationPolicy: OverallocationPolicy | null;
};

function hours(value: number | null | undefined) {
  return new Intl.NumberFormat("fr-CA", { maximumFractionDigits: 2 }).format(Number(value ?? 0));
}

function authorizationLabel(value: string) {
  if (value === "WITHIN_APPROVED_ENTRY") return "Dans l’entrée approuvée active";
  if (value === "WINDOW_EXTENSION_REAPPROVAL_REQUIRED") return "Extension hors enveloppe approuvée";
  if (value === "NOT_APPLICABLE") return "Besoin autonome — approbation non applicable";
  if (value === "APPROVAL_REFERENCE_UNKNOWN") return "Référence d’approbation inconnue";
  return value || "Décision backend non précisée";
}

function actionNeedsOverallocationChoice(
  action: PlanningDropAction,
  choiceRequired: boolean,
) {
  if (!choiceRequired) return false;
  return ["MOVE", "SPLIT", "DUPLICATE", "EXTEND_AND_MOVE"].includes(action.code);
}

export default function PlanningDropDialog({
  evaluation,
  shift,
  targetResource,
  busy,
  error,
  overallocationPrompt,
  onClose,
  onReevaluate,
  onExecute,
}: {
  evaluation: PlanningDropEvaluation;
  shift: ShiftReadModel;
  targetResource: ResourceReadModel;
  busy: boolean;
  error: string | null;
  overallocationPrompt: OverallocationContext | null;
  onClose: () => void;
  onReevaluate: (outsideStandardHours: boolean) => Promise<void>;
  onExecute: (request: PlanningDropExecutionRequest) => Promise<void>;
}) {
  const outsideWarning = evaluation.warnings.find(
    (warning) => warning.code === "OUTSIDE_STANDARD_HOURS_REQUIRED",
  );
  const targetKey = `${evaluation.allocation_id}:${evaluation.target_resource_id}:${evaluation.target_day}`;
  const [outsideStandardHours, setOutsideStandardHours] = useState(false);
  const [outsideOptionPresented, setOutsideOptionPresented] = useState(Boolean(outsideWarning));
  const [overallocationPolicy, setOverallocationPolicy] = useState<OverallocationPolicy | null>(null);
  const [transferHours, setTransferHours] = useState(() => (
    Math.max(Number(shift.hours || 0) / 2, 0.01).toFixed(2)
  ));

  useEffect(() => {
    setOutsideStandardHours(false);
    setOutsideOptionPresented(false);
    setOverallocationPolicy(null);
    setTransferHours(Math.max(Number(shift.hours || 0) / 2, 0.01).toFixed(2));
  }, [targetKey, shift.hours]);

  useEffect(() => {
    if (outsideWarning) setOutsideOptionPresented(true);
  }, [outsideWarning]);

  const executionActions = useMemo(
    () => evaluation.actions.filter((action) => action.code !== "CANCEL"),
    [evaluation.actions],
  );
  const evaluationOverallocationWarning = evaluation.warnings.find(
    (warning) => warning.code === "OVERALLOCATION_CHOICE_REQUIRED",
  );
  const overallocationChoiceRequired = Boolean(
    overallocationPrompt || evaluationOverallocationWarning,
  );
  const parsedTransferHours = Number(transferHours.replace(",", "."));
  const validTransfer = (
    Number.isFinite(parsedTransferHours)
    && parsedTransferHours > 0
    && parsedTransferHours < Number(shift.hours)
  );

  async function setOutside(value: boolean) {
    setOutsideStandardHours(value);
    await onReevaluate(value);
  }

  function canExecute(action: PlanningDropAction) {
    if (!action.enabled || busy) return false;
    if (action.code === "SPLIT" && !validTransfer) return false;
    if (
      actionNeedsOverallocationChoice(action, overallocationChoiceRequired)
      && !overallocationPolicy
    ) return false;
    return true;
  }

  return (
    <div
      className="planning-drop-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target && !busy) onClose();
      }}
    >
      <section
        className="planning-drop-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Choisir l’action du déplacement"
        data-allocation-id={evaluation.allocation_id}
      >
        <header className="planning-drop-dialog-header">
          <div>
            <span className="eyebrow">Drag-and-drop contextuel</span>
            <h2>Choisir l’action</h2>
            <p>
              {shift.project_number || shift.project_name || evaluation.segment_id} · {hours(shift.hours)} h ·
              {" "}{shift.resource_name} → {targetResource.name}, {evaluation.target_day}
            </p>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Annuler le déplacement">×</button>
        </header>

        <div className="planning-drop-dialog-body">
          <div className="planning-drop-summary">
            <article>
              <span>Fenêtre actuelle</span>
              <strong>{evaluation.current_window.start} → {evaluation.current_window.end}</strong>
            </article>
            <article>
              <span>Fenêtre proposée</span>
              <strong>{evaluation.proposed_window.start} → {evaluation.proposed_window.end}</strong>
            </article>
            <article>
              <span>Autorisation</span>
              <strong>{authorizationLabel(evaluation.authorization_decision)}</strong>
            </article>
            <article>
              <span>Capacité standard cible</span>
              <strong>{hours(evaluation.availability_hours)} h</strong>
            </article>
          </div>

          <div className="planning-drop-impact" data-testid="planning-drop-impact">
            <span>Budget : <strong>{hours(evaluation.planned_hours)} h</strong></span>
            <span>Verrouillé : <strong>{hours(evaluation.current_locked_hours)} h</strong></span>
            <span>Projeté : <strong>{hours(evaluation.projected_locked_hours)} h</strong></span>
            <span>Excédent projeté : <strong>{hours(evaluation.projected_excess_hours)} h</strong></span>
          </div>

          {evaluation.approved_window && (
            <p className="planning-drop-approved-window">
              Entrée approuvée : {evaluation.approved_window.start} → {evaluation.approved_window.end}
              {evaluation.period_key ? ` · période ${evaluation.period_key}` : ""}
            </p>
          )}

          {evaluation.warnings.length > 0 && (
            <div className="planning-drop-warnings" aria-label="Avertissements du backend">
              {evaluation.warnings.map((warning) => (
                <p key={warning.code}>
                  <strong>{warning.code}</strong>
                  <span>{warning.message}</span>
                </p>
              ))}
            </div>
          )}

          {outsideOptionPresented && (
            <label className="planning-drop-option">
              <input
                type="checkbox"
                checked={outsideStandardHours}
                disabled={busy}
                onChange={(event) => void setOutside(event.target.checked)}
              />
              <span>Autoriser explicitement le quart hors horaire standard pour cette action</span>
            </label>
          )}

          {overallocationChoiceRequired && (
            <fieldset className="planning-drop-policy">
              <legend>Décision de surallocation requise</legend>
              <p>
                {overallocationPrompt
                  ? `Le backend projette ${hours(overallocationPrompt.excess_hours)} h d’excédent.`
                  : evaluationOverallocationWarning?.message}
              </p>
              <label>
                <input
                  type="radio"
                  name="drop-overallocation-policy"
                  checked={overallocationPolicy === "KEEP_EXCEPTION"}
                  onChange={() => setOverallocationPolicy("KEEP_EXCEPTION")}
                />
                <span>Conserver la surallocation comme dérogation</span>
              </label>
              <label>
                <input
                  type="radio"
                  name="drop-overallocation-policy"
                  checked={overallocationPolicy === "INCREASE_PLANNED"}
                  onChange={() => setOverallocationPolicy("INCREASE_PLANNED")}
                />
                <span>Augmenter les heures prévues si l’autorisation le permet</span>
              </label>
            </fieldset>
          )}

          {executionActions.some((action) => action.code === "SPLIT") && (
            <label className="planning-drop-split-hours">
              <span>Heures à transférer si « Partager »</span>
              <input
                type="number"
                min="0.01"
                max={Math.max(Number(shift.hours) - 0.01, 0.01)}
                step="0.25"
                value={transferHours}
                disabled={busy}
                onChange={(event) => setTransferHours(event.target.value)}
              />
              {!validTransfer && <small>Le transfert doit être supérieur à 0 et inférieur à {hours(shift.hours)} h.</small>}
            </label>
          )}

          {error && <div className="planning-drop-error" role="alert">{error}</div>}

          <div className="planning-drop-actions">
            {executionActions.map((action) => (
              <div className="planning-drop-action" key={action.code}>
                <button
                  type="button"
                  data-drop-action={action.code}
                  disabled={!canExecute(action)}
                  onClick={() => void onExecute({
                    code: action.code,
                    transferHours: action.code === "SPLIT" ? parsedTransferHours : null,
                    outsideStandardHours,
                    overallocationPolicy,
                  })}
                >
                  {busy ? "Traitement…" : action.label}
                </button>
                {!action.enabled && (
                  <small>{action.reason || action.reason_code || "Action refusée par le backend."}</small>
                )}
              </div>
            ))}
            <button type="button" className="secondary-button" onClick={onClose} disabled={busy}>
              Annuler
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
