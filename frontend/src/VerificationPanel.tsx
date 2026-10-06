import { FormEvent, useEffect, useId, useMemo, useState } from "react";

import { ApiError } from "./api";
import { useAuth } from "./AuthContext";
import {
  StoryVerificationDecisionPayload,
  VerificationAction,
  VerificationPackageReadModel,
  VerificationPhase,
  VerificationRequirementReadModel,
  VerificationResult,
  addVerificationEvidenceLink,
  assignVerificationExecutor,
  recordHistoricalStoryVerificationDecision,
  recordVerificationExecution,
  requestVerificationRetest,
  unassignVerificationExecutor,
} from "./verificationApi";

const PHASES: Array<{ value: VerificationPhase; label: string }> = [
  { value: "FAT", label: "FAT" },
  { value: "SAT", label: "SAT" },
  { value: "COMMISSIONING", label: "Commissioning" },
];

const STATUS_LABELS: Record<string, string> = {
  NOT_RUN: "À exécuter",
  PASS: "Réussi",
  FAIL: "Échec",
  BLOCKED: "Bloqué",
};

function formatDateTime(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("fr-CA", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(date);
}

function percentage(value: number | null) {
  return value === null ? "—" : `${Math.round(value * 100)} %`;
}

function messageFor(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

export type VerificationDecisionEditorProps = {
  storyTitle: string;
  existingRequirements?: VerificationRequirementReadModel[];
  disabled?: boolean;
  historical?: boolean;
  onSubmit: (decision: StoryVerificationDecisionPayload) => Promise<void> | void;
  onCancel: () => void;
};

export function VerificationDecisionEditor({
  storyTitle,
  existingRequirements = [],
  disabled = false,
  historical = false,
  onSubmit,
  onCancel,
}: VerificationDecisionEditorProps) {
  const activeExisting = useMemo(
    () => existingRequirements.filter((requirement) => requirement.state === "ACTIVE"),
    [existingRequirements],
  );
  const radioName = useId();
  const [kind, setKind] = useState<"NO_TEST_REQUIRED" | "TESTS_DEFINED">("TESTS_DEFINED");
  const [justification, setJustification] = useState("");
  const [selectedExisting, setSelectedExisting] = useState<string[]>(
    activeExisting.map((requirement) => requirement.id),
  );
  const [phases, setPhases] = useState<VerificationPhase[]>(
    activeExisting.length > 0 ? [] : ["FAT"],
  );
  const [objective, setObjective] = useState("");
  const [method, setMethod] = useState("");
  const [expectedResult, setExpectedResult] = useState("");
  const [prerequisites, setPrerequisites] = useState("");
  const [criticality, setCriticality] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const wantsNewRequirements = phases.length > 0;
  const testsDefinedValid = selectedExisting.length > 0 || (
    wantsNewRequirements
    && objective.trim()
    && method.trim()
    && expectedResult.trim()
  );
  const valid = kind === "NO_TEST_REQUIRED"
    ? Boolean(justification.trim())
    : Boolean(testsDefinedValid);

  function togglePhase(phase: VerificationPhase) {
    setPhases((current) => current.includes(phase)
      ? current.filter((value) => value !== phase)
      : [...current, phase]);
  }

  function toggleExisting(requirementId: string) {
    setSelectedExisting((current) => current.includes(requirementId)
      ? current.filter((value) => value !== requirementId)
      : [...current, requirementId]);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || submitting) return;
    const decision: StoryVerificationDecisionPayload = kind === "NO_TEST_REQUIRED"
      ? {
          kind,
          justification: justification.trim(),
          existing_requirement_ids: [],
          new_requirements: [],
        }
      : {
          kind,
          justification: null,
          existing_requirement_ids: selectedExisting,
          new_requirements: phases.map((phase) => ({
            phase,
            objective: objective.trim(),
            method: method.trim(),
            expected_result: expectedResult.trim(),
            prerequisites: prerequisites
              .split("\n")
              .map((value) => value.trim())
              .filter(Boolean),
            criticality: criticality.trim() || null,
          })),
        };
    setSubmitting(true);
    try {
      await onSubmit(decision);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="verification-decision-editor" onSubmit={(event) => void submit(event)}>
      <div className="verification-decision-heading">
        <div>
          <span className="eyebrow">{historical ? "Reprise historique" : "Fermeture Story"}</span>
          <strong>{storyTitle}</strong>
        </div>
        <button type="button" className="secondary-button" onClick={onCancel} disabled={submitting}>
          Annuler
        </button>
      </div>
      <p>
        {historical
          ? "Documentez explicitement la décision Verification manquante sans modifier l'état Delivery."
          : "La Story peut être terminée avant les essais, mais la décision de vérification doit être explicite."}
      </p>
      <div className="verification-decision-kind">
        <label>
          <input
            type="radio"
            name={radioName}
            checked={kind === "TESTS_DEFINED"}
            disabled={disabled || submitting}
            onChange={() => setKind("TESTS_DEFINED")}
          />
          Tests définis
        </label>
        <label>
          <input
            type="radio"
            name={radioName}
            checked={kind === "NO_TEST_REQUIRED"}
            disabled={disabled || submitting}
            onChange={() => setKind("NO_TEST_REQUIRED")}
          />
          Aucun test requis
        </label>
      </div>

      {kind === "NO_TEST_REQUIRED" ? (
        <label>
          <span>Justification obligatoire</span>
          <textarea
            value={justification}
            disabled={disabled || submitting}
            placeholder="Pourquoi aucun essai FAT/SAT/commissioning n'est requis…"
            onChange={(event) => setJustification(event.target.value)}
          />
        </label>
      ) : (
        <>
          {activeExisting.length > 0 && (
            <fieldset className="verification-existing-requirements">
              <legend>Exigences existantes à reconfirmer</legend>
              {activeExisting.map((requirement) => (
                <label key={requirement.id}>
                  <input
                    type="checkbox"
                    checked={selectedExisting.includes(requirement.id)}
                    disabled={disabled || submitting}
                    onChange={() => toggleExisting(requirement.id)}
                  />
                  <span>
                    {requirement.phase} · {requirement.current_revision?.objective ?? requirement.id}
                  </span>
                </label>
              ))}
            </fieldset>
          )}

          <fieldset className="verification-new-requirement">
            <legend>Nouvelle exigence {activeExisting.length > 0 ? "(facultative)" : ""}</legend>
            <div className="verification-phase-picker">
              {PHASES.map((phase) => (
                <label key={phase.value}>
                  <input
                    type="checkbox"
                    checked={phases.includes(phase.value)}
                    disabled={disabled || submitting}
                    onChange={() => togglePhase(phase.value)}
                  />
                  {phase.label}
                </label>
              ))}
            </div>
            {wantsNewRequirements && (
              <div className="verification-definition-grid">
                <label>
                  <span>Objet du test</span>
                  <input
                    value={objective}
                    disabled={disabled || submitting}
                    onChange={(event) => setObjective(event.target.value)}
                  />
                </label>
                <label>
                  <span>Méthode / action</span>
                  <textarea
                    value={method}
                    disabled={disabled || submitting}
                    onChange={(event) => setMethod(event.target.value)}
                  />
                </label>
                <label>
                  <span>Résultat attendu</span>
                  <textarea
                    value={expectedResult}
                    disabled={disabled || submitting}
                    onChange={(event) => setExpectedResult(event.target.value)}
                  />
                </label>
                <label>
                  <span>Prérequis (un par ligne)</span>
                  <textarea
                    value={prerequisites}
                    disabled={disabled || submitting}
                    onChange={(event) => setPrerequisites(event.target.value)}
                  />
                </label>
                <label>
                  <span>Criticité (facultative)</span>
                  <input
                    value={criticality}
                    disabled={disabled || submitting}
                    placeholder="HIGH, MEDIUM…"
                    onChange={(event) => setCriticality(event.target.value)}
                  />
                </label>
              </div>
            )}
          </fieldset>
        </>
      )}

      <button type="submit" disabled={disabled || submitting || !valid}>
        {historical ? "Enregistrer la décision historique" : "Terminer la Story"}
      </button>
    </form>
  );
}

type RequirementCardProps = {
  requirement: VerificationRequirementReadModel;
  verificationVersion: number;
  actions: VerificationAction[];
  currentUserId: string | null;
  disabled: boolean;
  onReload: () => Promise<void>;
  onMutationFailure: (reason: unknown, fallback: string) => Promise<void> | void;
};

function RequirementCard({
  requirement,
  verificationVersion,
  actions,
  currentUserId,
  disabled,
  onReload,
  onMutationFailure,
}: RequirementCardProps) {
  const [executorId, setExecutorId] = useState("");
  const [unassignReason, setUnassignReason] = useState("");
  const [result, setResult] = useState<VerificationResult>("PASS");
  const [executedAt, setExecutedAt] = useState("");
  const [measurements, setMeasurements] = useState("{}");
  const [comments, setComments] = useState("");
  const [retestReason, setRetestReason] = useState("");
  const [evidenceExecutionId, setEvidenceExecutionId] = useState("");
  const [evidenceUrl, setEvidenceUrl] = useState("");
  const [evidenceLabel, setEvidenceLabel] = useState("");
  const [pending, setPending] = useState(false);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    setEvidenceExecutionId(
      requirement.latest_execution_id
        ?? requirement.executions[requirement.executions.length - 1]?.id
        ?? "",
    );
  }, [requirement.executions, requirement.latest_execution_id]);

  const assignedToCurrentUser = Boolean(
    currentUserId && requirement.assigned_executor_ids.includes(currentUserId),
  );
  const canManageAssignments = actions.includes("ASSIGN_EXECUTORS");
  const canRetest = actions.includes("REQUEST_RETEST");
  const canRecord = actions.includes("RECORD_RESULT") && assignedToCurrentUser;
  const canEvidence = actions.includes("ADD_EVIDENCE") && assignedToCurrentUser;

  async function mutate(operation: () => Promise<unknown>, fallback: string) {
    setPending(true);
    setLocalError("");
    try {
      await operation();
      await onReload();
    } catch (reason) {
      setLocalError(messageFor(reason, fallback));
      await onMutationFailure(reason, fallback);
    } finally {
      setPending(false);
    }
  }

  async function assign(event: FormEvent) {
    event.preventDefault();
    if (!executorId.trim()) return;
    const value = executorId.trim();
    await mutate(
      () => assignVerificationExecutor(requirement.id, value, verificationVersion),
      "Impossible d'affecter l'exécutant.",
    );
    setExecutorId("");
  }

  async function unassign(executorUserId: string) {
    if (!unassignReason.trim()) {
      setLocalError("Un motif de retrait est requis.");
      return;
    }
    await mutate(
      () => unassignVerificationExecutor(
        requirement.id,
        executorUserId,
        unassignReason.trim(),
        verificationVersion,
      ),
      "Impossible de retirer l'exécutant.",
    );
    setUnassignReason("");
  }

  async function execute(event: FormEvent) {
    event.preventDefault();
    let parsedMeasurements: Record<string, string | number | boolean | null>;
    try {
      const parsed = JSON.parse(measurements || "{}") as unknown;
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("Les mesures doivent être un objet JSON.");
      }
      parsedMeasurements = parsed as Record<string, string | number | boolean | null>;
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : "Mesures JSON invalides.");
      return;
    }
    await mutate(
      () => recordVerificationExecution(requirement.id, {
        result,
        executed_at: executedAt ? new Date(executedAt).toISOString() : null,
        measurements: parsedMeasurements,
        comments: comments.trim() || null,
        expected_verification_version: verificationVersion,
      }),
      "Impossible d'enregistrer le résultat.",
    );
    setComments("");
    setMeasurements("{}");
  }

  async function retest(event: FormEvent) {
    event.preventDefault();
    if (!retestReason.trim()) return;
    await mutate(
      () => requestVerificationRetest(
        requirement.id,
        retestReason.trim(),
        verificationVersion,
      ),
      "Impossible de demander le retest.",
    );
    setRetestReason("");
  }

  async function addEvidence(event: FormEvent) {
    event.preventDefault();
    if (!evidenceExecutionId || !evidenceUrl.trim()) return;
    await mutate(
      () => addVerificationEvidenceLink(evidenceExecutionId, {
        url: evidenceUrl.trim(),
        label: evidenceLabel.trim() || null,
        provenance: "external_https",
        expected_verification_version: verificationVersion,
      }),
      "Impossible d'ajouter la preuve.",
    );
    setEvidenceUrl("");
    setEvidenceLabel("");
  }

  const revision = requirement.current_revision;

  return (
    <article className="verification-requirement-card" data-status={requirement.projected_status ?? "WITHDRAWN"}>
      <header>
        <div>
          <span>{requirement.phase}</span>
          <strong>{revision?.objective ?? "Exigence Verification"}</strong>
        </div>
        <span className="verification-status">
          {requirement.state === "WITHDRAWN"
            ? "Retirée"
            : STATUS_LABELS[requirement.projected_status ?? "NOT_RUN"]}
        </span>
      </header>

      {revision && (
        <dl className="verification-definition">
          <div>
            <dt>Méthode</dt>
            <dd>{revision.method}</dd>
          </div>
          <div>
            <dt>Résultat attendu</dt>
            <dd>{revision.expected_result}</dd>
          </div>
          <div>
            <dt>Prérequis</dt>
            <dd>{revision.prerequisites.length > 0 ? revision.prerequisites.join(" · ") : "—"}</dd>
          </div>
          <div>
            <dt>Révision</dt>
            <dd>v{revision.revision_number}{revision.criticality ? ` · ${revision.criticality}` : ""}</dd>
          </div>
        </dl>
      )}

      <div className="verification-assignments">
        <strong>Exécutants</strong>
        {requirement.assigned_executor_ids.length === 0 ? (
          <span>Aucun exécutant affecté</span>
        ) : (
          requirement.assigned_executor_ids.map((executor) => (
            <div key={executor}>
              <code>{executor}</code>
              {canManageAssignments && requirement.state === "ACTIVE" && (
                <button
                  type="button"
                  className="secondary-button"
                  disabled={disabled || pending || !unassignReason.trim()}
                  onClick={() => void unassign(executor)}
                >
                  Retirer
                </button>
              )}
            </div>
          ))
        )}
        {canManageAssignments && requirement.state === "ACTIVE" && (
          <>
            <form onSubmit={(event) => void assign(event)}>
              <label>
                <span>AppUser à affecter</span>
                <input
                  value={executorId}
                  disabled={disabled || pending}
                  onChange={(event) => setExecutorId(event.target.value)}
                />
              </label>
              <button type="submit" disabled={disabled || pending || !executorId.trim()}>
                Affecter
              </button>
            </form>
            {requirement.assigned_executor_ids.length > 0 && (
              <label>
                <span>Motif de retrait</span>
                <input
                  value={unassignReason}
                  disabled={disabled || pending}
                  placeholder="Requis pour retirer un exécutant"
                  onChange={(event) => setUnassignReason(event.target.value)}
                />
              </label>
            )}
          </>
        )}
      </div>

      {canRecord && requirement.state === "ACTIVE" && (
        <form className="verification-execution-form" onSubmit={(event) => void execute(event)}>
          <strong>Nouvelle exécution</strong>
          <div className="verification-execution-grid">
            <label>
              <span>Résultat</span>
              <select
                value={result}
                disabled={disabled || pending}
                onChange={(event) => setResult(event.target.value as VerificationResult)}
              >
                <option value="PASS">PASS</option>
                <option value="FAIL">FAIL</option>
                <option value="BLOCKED">BLOCKED</option>
              </select>
            </label>
            <label>
              <span>Date / heure métier</span>
              <input
                type="datetime-local"
                value={executedAt}
                disabled={disabled || pending}
                onChange={(event) => setExecutedAt(event.target.value)}
              />
            </label>
            <label className="verification-wide-field">
              <span>Mesures (objet JSON)</span>
              <textarea
                value={measurements}
                disabled={disabled || pending}
                placeholder={'{"pression_bar": 5.4, "alarme": false}'}
                onChange={(event) => setMeasurements(event.target.value)}
              />
            </label>
            <label className="verification-wide-field">
              <span>Commentaires</span>
              <textarea
                value={comments}
                disabled={disabled || pending}
                onChange={(event) => setComments(event.target.value)}
              />
            </label>
          </div>
          <button type="submit" disabled={disabled || pending}>Enregistrer le résultat</button>
        </form>
      )}

      {canRetest && requirement.state === "ACTIVE" && requirement.executions.length > 0 && (
        <form className="verification-inline-form" onSubmit={(event) => void retest(event)}>
          <label>
            <span>Motif du retest</span>
            <input
              value={retestReason}
              disabled={disabled || pending}
              onChange={(event) => setRetestReason(event.target.value)}
            />
          </label>
          <button type="submit" disabled={disabled || pending || !retestReason.trim()}>
            Demander un retest
          </button>
        </form>
      )}

      {canEvidence && requirement.executions.length > 0 && (
        <form className="verification-evidence-form" onSubmit={(event) => void addEvidence(event)}>
          <strong>Ajouter une preuve HTTPS</strong>
          <label>
            <span>Exécution</span>
            <select
              value={evidenceExecutionId}
              disabled={disabled || pending}
              onChange={(event) => setEvidenceExecutionId(event.target.value)}
            >
              {requirement.executions.map((execution) => (
                <option key={execution.id} value={execution.id}>
                  #{execution.sequence} · {execution.result}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>URL HTTPS</span>
            <input
              type="url"
              value={evidenceUrl}
              disabled={disabled || pending}
              placeholder="https://…"
              onChange={(event) => setEvidenceUrl(event.target.value)}
            />
          </label>
          <label>
            <span>Libellé</span>
            <input
              value={evidenceLabel}
              disabled={disabled || pending}
              onChange={(event) => setEvidenceLabel(event.target.value)}
            />
          </label>
          <button type="submit" disabled={disabled || pending || !evidenceUrl.trim()}>
            Ajouter la preuve
          </button>
        </form>
      )}

      <details className="verification-history" open={requirement.executions.length > 0}>
        <summary>Historique · {requirement.executions.length} exécution(s)</summary>
        {requirement.executions.length === 0 ? (
          <p>Aucune exécution enregistrée.</p>
        ) : (
          <ol>
            {[...requirement.executions].reverse().map((execution) => (
              <li key={execution.id}>
                <div>
                  <strong>#{execution.sequence} · {execution.result}</strong>
                  <span>
                    {formatDateTime(execution.executed_at ?? execution.recorded_at)}
                    {" · "}<code>{execution.executor_user_id}</code>
                  </span>
                </div>
                {execution.comments && <p>{execution.comments}</p>}
                {Object.keys(execution.measurements).length > 0 && (
                  <pre>{JSON.stringify(execution.measurements, null, 2)}</pre>
                )}
                {execution.evidence_links.length > 0 && (
                  <ul>
                    {execution.evidence_links.map((evidence) => (
                      <li key={evidence.id}>
                        <a href={evidence.url} target="_blank" rel="noreferrer">
                          {evidence.label || evidence.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        )}
      </details>
      {localError && <div className="verification-local-error">{localError}</div>}
    </article>
  );
}

export type VerificationPanelProps = {
  workPackageId: string;
  packageModel: VerificationPackageReadModel | null;
  storyTitles: Map<string, string>;
  deliveryVersion: number;
  disabled?: boolean;
  inaccessible?: boolean;
  onReload: () => Promise<void>;
  onMutationFailure: (reason: unknown, fallback: string) => Promise<void> | void;
};

export default function VerificationPanel({
  workPackageId,
  packageModel,
  storyTitles,
  deliveryVersion,
  disabled = false,
  inaccessible = false,
  onReload,
  onMutationFailure,
}: VerificationPanelProps) {
  const { principal } = useAuth();
  const [historicalStoryId, setHistoricalStoryId] = useState("");
  const [historicalPending, setHistoricalPending] = useState(false);
  const [historicalError, setHistoricalError] = useState("");

  if (!packageModel) {
    return (
      <section className="verification-panel" aria-label="Verification">
        <div className="delivery-section-heading">
          <div>
            <span className="eyebrow">Verification</span>
            <h3>FAT, SAT et commissioning</h3>
          </div>
        </div>
        <div className="verification-empty">
          {inaccessible
            ? "Le package Verification n'est pas consultable avec ce périmètre. La fermeture d'une Story autorisée reste disponible depuis sa carte."
            : "Aucun package Verification n'est encore matérialisé pour ce WorkPackage."}
        </div>
      </section>
    );
  }

  const version = packageModel.verification_version;
  const canMutate = version !== null;
  const undocumented = packageModel.story_decision_coverage.undocumented_done_story_ids;

  async function recordHistorical(decision: StoryVerificationDecisionPayload) {
    if (!historicalStoryId) return;
    setHistoricalPending(true);
    setHistoricalError("");
    try {
      await recordHistoricalStoryVerificationDecision(historicalStoryId, {
        expected_delivery_version: deliveryVersion,
        expected_verification_version: packageModel.verification_version,
        decision,
      });
      setHistoricalStoryId("");
      await onReload();
    } catch (reason) {
      setHistoricalError(messageFor(reason, "Impossible d'enregistrer la décision historique."));
      await onMutationFailure(reason, "Impossible d'enregistrer la décision historique.");
    } finally {
      setHistoricalPending(false);
    }
  }

  return (
    <section className="verification-panel" aria-label="Verification">
      <div className="delivery-section-heading">
        <div>
          <span className="eyebrow">Verification</span>
          <h3>FAT, SAT et commissioning</h3>
        </div>
        <span>
          {version === null ? "Périmètre non créé" : `Version Verification ${version}`}
        </span>
      </div>

      <div className="verification-summary" aria-label="Résumé Verification">
        <article>
          <span>Décisions Stories</span>
          <strong>{percentage(packageModel.story_decision_coverage.coverage_rate)}</strong>
          <small>
            {packageModel.story_decision_coverage.documented_done_story_count}
            {" / "}{packageModel.story_decision_coverage.done_story_count} terminée(s)
          </small>
        </article>
        <article>
          <span>Réussite essais actifs</span>
          <strong>{percentage(packageModel.pass_rate)}</strong>
          <small>{packageModel.active_requirement_count} exigence(s) active(s)</small>
        </article>
        {PHASES.map((phase) => {
          const counts = packageModel.phases[phase.value];
          return (
            <article key={phase.value}>
              <span>{phase.label}</span>
              <strong>{counts.PASS} / {counts.active}</strong>
              <small>
                {counts.NOT_RUN} à faire · {counts.FAIL} échec · {counts.BLOCKED} bloqué
              </small>
            </article>
          );
        })}
      </div>

      {undocumented.length > 0 && (
        <div className="verification-history-recovery">
          <div>
            <strong>{undocumented.length} Story(s) terminée(s) sans décision documentée</strong>
            <span>La reprise historique ne modifie pas Delivery.</span>
          </div>
          {!historicalStoryId && (
            <label>
              <span>Story à documenter</span>
              <select
                value=""
                disabled={disabled || historicalPending}
                onChange={(event) => setHistoricalStoryId(event.target.value)}
              >
                <option value="">Choisir…</option>
                {undocumented.map((storyId) => (
                  <option key={storyId} value={storyId}>
                    {storyTitles.get(storyId) ?? storyId}
                  </option>
                ))}
              </select>
            </label>
          )}
          {historicalStoryId && (
            <VerificationDecisionEditor
              storyTitle={storyTitles.get(historicalStoryId) ?? historicalStoryId}
              existingRequirements={packageModel.requirements.filter(
                (requirement) => requirement.story_id === historicalStoryId,
              )}
              disabled={disabled || historicalPending}
              historical
              onSubmit={recordHistorical}
              onCancel={() => setHistoricalStoryId("")}
            />
          )}
          {historicalError && <div className="verification-local-error">{historicalError}</div>}
        </div>
      )}

      {packageModel.requirements.length === 0 ? (
        <div className="verification-empty">
          Aucun test défini. Un package sans exigence n'est pas présenté comme 100 % réussi.
        </div>
      ) : (
        <div className="verification-requirements">
          {packageModel.requirements.map((requirement) => (
            <RequirementCard
              key={requirement.id}
              requirement={requirement}
              verificationVersion={version ?? 1}
              actions={packageModel.allowed_actions}
              currentUserId={principal?.local_user_id ?? null}
              disabled={disabled || !canMutate}
              onReload={onReload}
              onMutationFailure={onMutationFailure}
            />
          ))}
        </div>
      )}

      <small className="verification-scope-reference">
        WorkPackage {workPackageId} · périmètre {packageModel.verification_scope_id ?? "non matérialisé"}
        {packageModel.withdrawn_requirement_count > 0
          ? ` · ${packageModel.withdrawn_requirement_count} exigence(s) retirée(s) conservée(s)`
          : ""}
      </small>
    </section>
  );
}
