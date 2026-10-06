import { useEffect, useMemo, useState } from "react";

import {
  CommunicationBatch,
  ProjectCommunicationDraft,
  ProjectCommunicationPreview,
  ProjectCommunicationReview,
  getProjectCommunicationPreview,
  listProjectCommunicationBatches,
  prepareProjectCommunicationBatch,
  projectCommunicationBatchAction,
} from "./communicationsApi";

function nextMondayIso() {
  const today = new Date();
  const day = today.getDay();
  const daysUntilMonday = day === 0 ? 1 : 8 - day;
  const next = new Date(today);
  next.setDate(today.getDate() + daysUntilMonday);
  return next.toISOString().slice(0, 10);
}

function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function kindLabel(kind: string) {
  return kind === "project_confirmation" ? "Confirmation par projet" : "Modification de planning";
}

type DraftState = ProjectCommunicationReview & { source: ProjectCommunicationDraft };
type BatchAction = "approve" | "create-drafts" | "send-smtp" | "cancel" | "mark-communicated";

export default function CommunicationsPage() {
  const [weekStart, setWeekStart] = useState(nextMondayIso);
  const [batches, setBatches] = useState<CommunicationBatch[]>([]);
  const [preview, setPreview] = useState<ProjectCommunicationPreview | null>(null);
  const [drafts, setDrafts] = useState<DraftState[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const hasBlockingDraft = useMemo(
    () => drafts.some((draft) => !draft.source.approvable),
    [drafts],
  );

  async function reloadBatches() {
    setBatches(await listProjectCommunicationBatches(weekStart));
  }

  useEffect(() => {
    setPreview(null);
    setDrafts([]);
    setError(null);
    void reloadBatches().catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "Impossible de charger les communications.");
    });
  }, [weekStart]);

  async function runPreview() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const row = await getProjectCommunicationPreview(weekStart);
      setPreview(row);
      setDrafts(
        row.drafts.map((draft) => ({
          message_key: draft.message_key,
          include: true,
          subject: draft.subject,
          body: draft.body,
          source: draft,
        })),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Prévisualisation impossible.");
    } finally {
      setBusy(false);
    }
  }

  async function prepare() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await prepareProjectCommunicationBatch(
        preview.week_start,
        preview.snapshot_fingerprint,
        drafts.map(({ source: _source, ...review }) => review),
      );
      setNotice("Lot projet préparé. Aucun message n’a été envoyé.");
      setPreview(null);
      setDrafts([]);
      await reloadBatches();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Préparation impossible.");
    } finally {
      setBusy(false);
    }
  }

  async function batchAction(batch: CommunicationBatch, action: BatchAction) {
    if (action === "create-drafts") {
      const included = batch.messages.filter((message) => message.included).length;
      const confirmed = window.confirm(
        `Créer ${included} brouillon(s) dans Microsoft 365 ?\n\n` +
        "Aucun courriel ne sera envoyé. Les brouillons devront encore être vérifiés dans Outlook/M365.",
      );
      if (!confirmed) return;
    }
    if (action === "send-smtp") {
      const pending = batch.messages.filter((message) => (
        message.included
        && !message.deliveries.some((delivery) => delivery.provider === "SMTP" && delivery.status === "SENT")
      )).length;
      const confirmed = window.confirm(
        `Envoyer ${pending} courriel(s) maintenant via SMTP ?\n\n`
        + "Cette action est irréversible. Les messages déjà marqués SENT ne seront pas renvoyés.",
      );
      if (!confirmed) return;
    }

    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await projectCommunicationBatchAction(batch.id, action);
      setNotice(
        action === "approve"
          ? "Lot projet approuvé. Aucun message n’a été envoyé."
          : action === "create-drafts"
            ? `${updated.drafts_created_count} brouillon(s) M365 créé(s). Aucun courriel n’a été envoyé.`
            : action === "send-smtp"
              ? (() => {
                  const deliveries = updated.messages
                    .filter((message) => message.included)
                    .flatMap((message) => message.deliveries.filter((delivery) => delivery.provider === "SMTP"));
                  const sent = deliveries.filter((delivery) => delivery.status === "SENT").length;
                  const failed = deliveries.filter((delivery) => delivery.status === "FAILED").length;
                  return updated.status === "COMMUNICATED"
                    ? `Envoi SMTP complété : ${sent} message(s) envoyé(s). Le lot est communiqué.`
                    : `Envoi SMTP partiel : ${sent} envoyé(s), ${failed} en échec. Un retry ne renverra pas les messages déjà SENT.`;
                })()
            : action === "mark-communicated"
              ? "Lot projet confirmé comme communiqué."
              : "Lot projet annulé.",
      );
      await reloadBatches();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Action impossible.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="communications-page">
      <header className="communications-header">
        <div>
          <span className="eyebrow">Communication par projet</span>
          <h2>Communications de planification</h2>
          <p>
            Un message est préparé par projet : le chargé principal ERP et les co-chargés RP
            effectifs sont en To; les ressources réellement affectées restent en CC.
          </p>
        </div>
        <label>
          Semaine du
          <input
            type="date"
            value={weekStart}
            onChange={(event) => setWeekStart(event.target.value)}
          />
        </label>
      </header>

      {error && <div className="error-panel"><strong>Erreur</strong><span>{error}</span></div>}
      {notice && <div className="communications-notice">{notice}</div>}

      <section className="communications-card communications-source-card">
        <div>
          <span className="eyebrow">Référentiel métier</span>
          <strong>Destinataires gérés dans Utilisateurs</strong>
        </div>
        <p>
          Les adresses ne sont jamais déduites d’un nom. Corrigez un courriel ou un téléphone dans
          le profil utilisateur correspondant; la prévisualisation signalera toute donnée manquante.
        </p>
      </section>

      <section className="communications-card preview-card">
        <div className="communications-card-heading">
          <div>
            <span className="eyebrow">Planning courant</span>
            <h3>Prévisualisation par projet</h3>
          </div>
          <button type="button" disabled={busy} onClick={() => void runPreview()}>
            Générer la prévisualisation
          </button>
        </div>

        {!preview ? (
          <p className="communications-empty">Aucune prévisualisation générée.</p>
        ) : (
          <>
            <div className="preview-summary">
              <strong>{kindLabel(preview.mode)}</strong>
              <span>{preview.drafts.length} projet(s) à communiquer</span>
              <span>
                {preview.has_communicated_baseline
                  ? "Snapshot communiqué trouvé"
                  : "Première communication de cette semaine"}
              </span>
            </div>

            {drafts.length === 0 ? (
              <p className="communications-empty">
                Aucun changement à communiquer pour cette version du planning.
              </p>
            ) : (
              <div className="draft-list">
                {drafts.map((draft, index) => {
                  const source = draft.source;
                  const blocking = source.diagnostics.filter((row) => row.severity === "BLOCKING");
                  const warnings = source.diagnostics.filter((row) => row.severity === "WARNING");
                  const toRecipients = source.to_recipients.length > 0
                    ? source.to_recipients
                    : [source.to_recipient];
                  return (
                    <article
                      className={`draft-card ${source.approvable ? "" : "is-blocked"}`}
                      key={source.message_key}
                      data-message-key={source.message_key}
                    >
                      <div className="draft-heading">
                        <div>
                          <strong>Projet {source.project_number}</strong>
                          <small>{source.message_key}</small>
                        </div>
                        <span className={source.approvable ? "draft-ready" : "draft-blocked"}>
                          {source.approvable ? "Prêt à préparer" : "Destinataire bloquant"}
                        </span>
                      </div>

                      <div className="draft-recipients">
                        <div>
                          <span>To</span>
                          <div className="recipient-chips">
                            {toRecipients.map((recipient) => (
                              <span
                                key={
                                  recipient.contact_id
                                  ?? recipient.user_id
                                  ?? recipient.email
                                  ?? recipient.display_name
                                }
                                className={recipient.email ? "" : "recipient-missing"}
                              >
                                {recipient.display_name} · {recipient.email ?? "Courriel manquant"}
                              </span>
                            ))}
                          </div>
                        </div>
                        <div>
                          <span>CC</span>
                          <div className="recipient-chips">
                            {source.cc_recipients.length === 0 ? (
                              <small>Aucune ressource avec courriel explicite</small>
                            ) : source.cc_recipients.map((recipient) => (
                              <span key={recipient.email ?? recipient.contact_id ?? recipient.display_name}>
                                {recipient.display_name} · {recipient.email}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>

                      {(blocking.length > 0 || warnings.length > 0) && (
                        <div className="draft-diagnostics" aria-label={`Diagnostics ${source.project_number}`}>
                          {blocking.map((diagnostic) => (
                            <div className="diagnostic-blocking" key={`${diagnostic.code}:${diagnostic.entity_id}`}>
                              <strong>Blocage</strong>
                              <span>{diagnostic.message}</span>
                            </div>
                          ))}
                          {warnings.map((diagnostic) => (
                            <div className="diagnostic-warning" key={`${diagnostic.code}:${diagnostic.entity_id}`}>
                              <strong>Anomalie</strong>
                              <span>{diagnostic.message}</span>
                            </div>
                          ))}
                        </div>
                      )}

                      <label className="draft-field">
                        <span>Sujet</span>
                        <input
                          className="draft-subject"
                          value={draft.subject}
                          onChange={(event) => setDrafts((rows) => rows.map((row, rowIndex) =>
                            rowIndex === index ? { ...row, subject: event.target.value } : row
                          ))}
                        />
                      </label>
                      <label className="draft-field">
                        <span>Corps du message</span>
                        <textarea
                          rows={11}
                          value={draft.body}
                          onChange={(event) => setDrafts((rows) => rows.map((row, rowIndex) =>
                            rowIndex === index ? { ...row, body: event.target.value } : row
                          ))}
                        />
                      </label>
                    </article>
                  );
                })}
              </div>
            )}

            <div className="communications-actions">
              <button
                type="button"
                disabled={
                  busy
                  || drafts.length === 0
                  || hasBlockingDraft
                }
                onClick={() => void prepare()}
              >
                Préparer le lot
              </button>
              <small>
                {hasBlockingDraft
                  ? "Un projet contient un blocage de destinataire."
                  : "Cette action persiste la révision. Elle n’envoie rien."}
              </small>
            </div>
          </>
        )}
      </section>

      <section className="communications-card batches-card">
        <div className="communications-card-heading">
          <div>
            <span className="eyebrow">Audit SQL</span>
            <h3>Lots projet de la semaine</h3>
          </div>
          <span>{batches.length} lot(s)</span>
        </div>

        {batches.length === 0 ? (
          <p className="communications-empty">Aucun lot projet préparé pour cette semaine.</p>
        ) : (
          <div className="batch-list">
            {batches.map((batch) => {
              const included = batch.messages.filter((message) => message.included).length;
              return (
                <article className={`batch-row status-${batch.status.toLowerCase()}`} key={batch.id}>
                  <div className="batch-summary">
                    <strong>{kindLabel(batch.kind)}</strong>
                    <span>{batch.status} · {included}/{batch.messages.length} projet(s) inclus</span>
                    <small>Préparé {formatDateTime(batch.prepared_at)} par {batch.prepared_by ?? "—"}</small>
                    {batch.drafts_created_at && (
                      <small>
                        Brouillons M365 : {batch.drafts_created_count} créé(s) {formatDateTime(batch.drafts_created_at)}
                        {batch.drafts_created_by ? ` par ${batch.drafts_created_by}` : ""}
                      </small>
                    )}
                    {batch.stale && <small className="stale-label">Le planning a changé depuis ce lot.</small>}
                    <div className="batch-message-list">
                      {batch.messages.map((message) => (
                        <div key={message.id}>
                          <strong>{message.message_key ?? message.project_id ?? message.id}</strong>
                          <span>Sujet : {message.subject}</span>
                          <span>
                            To : {
                              message.to_emails.length > 0
                                ? message.to_emails.join(", ")
                                : message.recipient_email ?? "courriel manquant"
                            }
                          </span>
                          <span>
                            CC : {message.cc_emails.length > 0 ? message.cc_emails.join(", ") : "—"}
                          </span>
                          {message.deliveries
                            .filter((delivery) => delivery.provider === "SMTP")
                            .map((delivery) => (
                              <span
                                key={delivery.id}
                                className={`delivery-status delivery-${delivery.status.toLowerCase()}`}
                              >
                                SMTP : {delivery.status}
                                {delivery.attempt_count > 0 ? ` · tentative ${delivery.attempt_count}` : ""}
                                {delivery.error_detail ? ` · ${delivery.error_detail}` : ""}
                              </span>
                            ))}
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="batch-actions">
                    {batch.status === "PREPARED" && (
                      <button
                        type="button"
                        disabled={busy || batch.stale}
                        onClick={() => void batchAction(batch, "approve")}
                      >
                        Approuver
                      </button>
                    )}
                    {(batch.status === "PREPARED" || batch.status === "APPROVED") && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy}
                        onClick={() => void batchAction(batch, "cancel")}
                      >
                        Annuler
                      </button>
                    )}
                    {batch.status === "APPROVED" && !batch.drafts_created_at && (
                      <button
                        type="button"
                        disabled={busy || batch.stale}
                        onClick={() => void batchAction(batch, "create-drafts")}
                      >
                        Créer brouillons M365
                      </button>
                    )}
                    {batch.status === "APPROVED" && (
                      <button
                        type="button"
                        className="smtp-send-button"
                        disabled={busy || batch.stale}
                        onClick={() => void batchAction(batch, "send-smtp")}
                      >
                        Envoyer par SMTP
                      </button>
                    )}
                    {batch.status === "APPROVED" && (
                      <button
                        type="button"
                        disabled={busy || batch.stale}
                        onClick={() => void batchAction(batch, "mark-communicated")}
                      >
                        Confirmer communiqué
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}

        <p className="communications-help">
          « Créer brouillons M365 » crée seulement des brouillons. « Envoyer par SMTP » transmet
          réellement les messages après approbation et marque automatiquement le lot communiqué si
          toutes les livraisons réussissent. Les échecs restent auditables et peuvent être retentés.
        </p>
      </section>
    </section>
  );
}
