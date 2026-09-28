import { FormEvent, useEffect, useState } from "react";

import ApprovalScopesPanel from "./ApprovalScopesPanel";
import ResourceClassesPanel from "./ResourceClassesPanel";
import {
  SmtpConfiguration,
  SmtpConfigurationUpdate,
  SmtpConnectionTestResult,
  getSmtpConfiguration,
  saveSmtpConfiguration,
  sendSmtpTestEmail,
  testSmtpConnection,
} from "./configurationApi";


type Draft = {
  host: string;
  port: string;
  security: "STARTTLS" | "SSL_TLS";
  username: string;
  password: string;
  clearPassword: boolean;
  fromEmail: string;
  fromName: string;
  replyTo: string;
  timeoutSeconds: string;
  enabled: boolean;
};

function fromConfiguration(row: SmtpConfiguration): Draft {
  return {
    host: row.host ?? "",
    port: String(row.port),
    security: row.security,
    username: row.username ?? "",
    password: "",
    clearPassword: false,
    fromEmail: row.from_email ?? "",
    fromName: row.from_name ?? "",
    replyTo: row.reply_to ?? "",
    timeoutSeconds: String(row.timeout_seconds),
    enabled: row.enabled,
  };
}

function formatDateTime(value: string | null) {
  if (!value) return "Jamais";
  return new Intl.DateTimeFormat("fr-CA", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function ConfigurationPage() {
  const [configuration, setConfiguration] = useState<SmtpConfiguration | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [smtpTest, setSmtpTest] = useState<SmtpConnectionTestResult | null>(null);
  const [testRecipient, setTestRecipient] = useState("");
  const [sendingTestEmail, setSendingTestEmail] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getSmtpConfiguration(controller.signal)
      .then((row) => {
        setConfiguration(row);
        setDraft(fromConfiguration(row));
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Configuration SMTP indisponible.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft || pending) return;
    setError(null);
    setNotice(null);

    const port = Number.parseInt(draft.port, 10);
    const timeout = Number.parseInt(draft.timeoutSeconds, 10);
    if (!draft.host.trim() || !draft.fromEmail.trim()) {
      setError("Le serveur SMTP et l’adresse d’expéditeur sont requis.");
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setError("Le port SMTP doit être compris entre 1 et 65535.");
      return;
    }
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 120) {
      setError("Le délai SMTP doit être compris entre 1 et 120 secondes.");
      return;
    }
    if (draft.password && !configuration?.encryption_available) {
      setError("La clé de chiffrement serveur doit être configurée avant d’enregistrer un mot de passe.");
      return;
    }

    const payload: SmtpConfigurationUpdate = {
      host: draft.host.trim(),
      port,
      security: draft.security,
      username: draft.username.trim() || null,
      password: draft.password || null,
      clear_password: draft.clearPassword,
      from_email: draft.fromEmail.trim(),
      from_name: draft.fromName.trim() || null,
      reply_to: draft.replyTo.trim() || null,
      timeout_seconds: timeout,
      enabled: draft.enabled,
    };

    setPending(true);
    try {
      const saved = await saveSmtpConfiguration(payload);
      setConfiguration(saved);
      setDraft(fromConfiguration(saved));
      setNotice("Configuration SMTP enregistrée. Le mot de passe n’est jamais retourné par l’API.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Impossible d’enregistrer la configuration.");
    } finally {
      setPending(false);
    }
  }

  async function testConnection() {
    if (testing) return;
    setTesting(true);
    setError(null);
    setNotice(null);
    setSmtpTest(null);
    try {
      const result = await testSmtpConnection();
      setSmtpTest(result);
      if (result.ok) {
        setNotice(result.message);
      } else {
        setError(result.message);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Le test SMTP a échoué.");
    } finally {
      setTesting(false);
    }
  }

  async function sendTestEmail() {
    if (sendingTestEmail) return;
    const recipient = testRecipient.trim();
    if (!recipient) {
      setError("Entrez l’adresse qui doit recevoir le courriel test.");
      return;
    }

    setSendingTestEmail(true);
    setError(null);
    setNotice(null);
    setSmtpTest(null);
    try {
      const result = await sendSmtpTestEmail(recipient);
      setSmtpTest(result);
      if (result.ok) {
        setNotice(result.message);
      } else {
        setError(result.message);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "L’envoi du courriel test a échoué.");
    } finally {
      setSendingTestEmail(false);
    }
  }

  if (loading || !draft || !configuration) {
    return (
      <section className="configuration-page">
        <div className="configuration-card">Chargement de la configuration…</div>
      </section>
    );
  }

  return (
    <section className="configuration-page">
      <header className="configuration-heading">
        <div>
          <span className="eyebrow">Administration</span>
          <h2>Configuration</h2>
          <p>
            Configurez le relais SMTP utilisé pour l’envoi explicite des communications approuvées.
            L’approbation seule n’envoie jamais de courriel.
          </p>
        </div>
      </header>

      {error && <div className="error-panel"><strong>Erreur</strong><span>{error}</span></div>}
      {notice && <div className="configuration-notice">{notice}</div>}

      <ResourceClassesPanel />

      <ApprovalScopesPanel />

      <div className="configuration-status-grid">
        <div className="configuration-card">
          <span className="eyebrow">Chiffrement</span>
          <strong>{configuration.encryption_available ? "Clé serveur disponible" : "Clé serveur absente"}</strong>
          <small>
            {configuration.encryption_available
              ? "Les nouveaux secrets peuvent être chiffrés avant stockage SQL."
              : "Définissez RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY dans le .env utilisé par le backend, puis recréez le conteneur backend."}
          </small>
          {!configuration.encryption_available && (
            <div className="configuration-key-help">
              <span>Générer une clé Fernet :</span>
              <code>
                python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
              </code>
              <span>Puis ajoutez la valeur au fichier <code>.env</code> :</span>
              <code>RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY=&lt;clé générée&gt;</code>
            </div>
          )}
        </div>
        <div className="configuration-card">
          <span className="eyebrow">Secret SMTP</span>
          <strong>{configuration.password_configured ? "Mot de passe configuré" : "Aucun mot de passe"}</strong>
          <small>La valeur du secret n’est jamais renvoyée au navigateur.</small>
        </div>
        <div className="configuration-card">
          <span className="eyebrow">Dernière modification</span>
          <strong>{formatDateTime(configuration.updated_at)}</strong>
          <small>{configuration.updated_by ?? "Aucun auteur enregistré"}</small>
        </div>
      </div>

      <form className="configuration-card smtp-form" onSubmit={submit}>
        <div className="configuration-section-heading">
          <div>
            <span className="eyebrow">Transport</span>
            <h3>Serveur SMTP</h3>
          </div>
          <label className="configuration-toggle">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => setDraft((row) => row && ({ ...row, enabled: event.target.checked }))}
            />
            Envoi SMTP activé
          </label>
        </div>

        <div className="configuration-form-grid">
          <label>
            Serveur SMTP
            <input
              value={draft.host}
              onChange={(event) => setDraft((row) => row && ({ ...row, host: event.target.value }))}
              placeholder="smtp.example.invalid"
            />
          </label>
          <label>
            Port
            <input
              inputMode="numeric"
              value={draft.port}
              onChange={(event) => setDraft((row) => row && ({ ...row, port: event.target.value }))}
            />
          </label>
          <label>
            Sécurité
            <select
              value={draft.security}
              onChange={(event) => setDraft((row) => row && ({
                ...row,
                security: event.target.value as Draft["security"],
              }))}
            >
              <option value="STARTTLS">STARTTLS</option>
              <option value="SSL_TLS">SSL/TLS</option>
            </select>
          </label>
          <label>
            Délai (secondes)
            <input
              inputMode="numeric"
              value={draft.timeoutSeconds}
              onChange={(event) => setDraft((row) => row && ({ ...row, timeoutSeconds: event.target.value }))}
            />
          </label>
        </div>

        <div className="configuration-form-grid">
          <label>
            Nom d’utilisateur
            <input
              value={draft.username}
              onChange={(event) => setDraft((row) => row && ({ ...row, username: event.target.value }))}
              autoComplete="off"
            />
          </label>
          <label>
            Mot de passe
            <input
              type="password"
              value={draft.password}
              onChange={(event) => setDraft((row) => row && ({
                ...row,
                password: event.target.value,
                clearPassword: false,
              }))}
              placeholder={configuration.password_configured ? "Déjà configuré — laisser vide pour conserver" : ""}
              autoComplete="new-password"
            />
          </label>
        </div>

        {configuration.password_configured && (
          <label className="configuration-toggle danger">
            <input
              type="checkbox"
              checked={draft.clearPassword}
              onChange={(event) => setDraft((row) => row && ({
                ...row,
                clearPassword: event.target.checked,
                password: event.target.checked ? "" : row.password,
              }))}
            />
            Effacer le mot de passe enregistré
          </label>
        )}

        <div className="configuration-form-grid">
          <label>
            Courriel expéditeur
            <input
              type="email"
              value={draft.fromEmail}
              onChange={(event) => setDraft((row) => row && ({ ...row, fromEmail: event.target.value }))}
            />
          </label>
          <label>
            Nom expéditeur
            <input
              value={draft.fromName}
              onChange={(event) => setDraft((row) => row && ({ ...row, fromName: event.target.value }))}
            />
          </label>
          <label>
            Reply-To
            <input
              type="email"
              value={draft.replyTo}
              onChange={(event) => setDraft((row) => row && ({ ...row, replyTo: event.target.value }))}
              placeholder="Optionnel"
            />
          </label>
        </div>

        <div className="configuration-actions">
          <button className="primary-button" type="submit" disabled={pending}>
            {pending ? "Enregistrement…" : "Enregistrer"}
          </button>
          <button
            className="quiet-button"
            type="button"
            disabled={testing || pending || !configuration.host || !configuration.from_email}
            onClick={() => void testConnection()}
          >
            {testing ? "Test en cours…" : "Tester la connexion enregistrée"}
          </button>
        </div>

        <div className="smtp-test-email">
          <div>
            <span className="eyebrow">Envoi réel de validation</span>
            <h4>Courriel test</h4>
            <small>
              Envoie un vrai courriel avec la configuration SMTP enregistrée. Cette action ne crée
              aucun lot de communication et peut être utilisée avant d’activer l’envoi SMTP général.
            </small>
          </div>
          <label>
            Destinataire du courriel test
            <input
              type="email"
              value={testRecipient}
              onChange={(event) => setTestRecipient(event.target.value)}
              placeholder={"vous" + String.fromCharCode(64) + "entreprise.ca"}
            />
          </label>
          <button
            className="quiet-button"
            type="button"
            disabled={
              sendingTestEmail
              || pending
              || !configuration.host
              || !configuration.from_email
              || !testRecipient.trim()
            }
            onClick={() => void sendTestEmail()}
          >
            {sendingTestEmail ? "Envoi en cours…" : "Envoyer un courriel test"}
          </button>
        </div>
      </form>

      {smtpTest && (
        <section className="configuration-card smtp-test-log" aria-live="polite">
          <div className="configuration-section-heading">
            <div>
              <span className="eyebrow">Diagnostic</span>
              <h3>Journal du dernier test SMTP</h3>
            </div>
            <strong className={smtpTest.ok ? "smtp-test-success" : "smtp-test-error"}>
              {smtpTest.ok ? "Succès" : "Échec"}
            </strong>
          </div>
          <div className="smtp-test-log-list">
            {smtpTest.log.map((entry, index) => (
              <div
                className={`smtp-test-log-entry level-${entry.level.toLowerCase()}`}
                key={`${entry.step}-${index}`}
              >
                <span>{entry.level}</span>
                <strong>{entry.step}</strong>
                <code>{entry.message}</code>
              </div>
            ))}
          </div>
          <small>
            Le journal masque le mot de passe SMTP. Il est également écrit dans les logs du backend
            sous la forme <code>smtp_test ...</code>.
          </small>
        </section>
      )}
    </section>
  );
}
