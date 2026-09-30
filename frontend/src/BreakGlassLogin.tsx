import { FormEvent, useState } from "react";

import { useAuth } from "./AuthContext";

export default function BreakGlassLogin() {
  const { breakGlassLogin } = useAuth();
  const [loginName, setLoginName] = useState("");
  const [secret, setSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setLocalError(null);
    try {
      await breakGlassLogin(loginName, secret);
      setSecret("");
    } catch {
      setLocalError("Connexion de secours refusée.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <details className="break-glass-login">
      <summary>Accès administrateur de secours</summary>
      <p>
        À utiliser uniquement lorsque l’authentification Acumatica/OIDC est indisponible.
      </p>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Identifiant break-glass
          <input
            type="text"
            autoComplete="username"
            value={loginName}
            onChange={(event) => setLoginName(event.target.value)}
            required
          />
        </label>
        <label>
          Secret
          <input
            type="password"
            autoComplete="current-password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            required
          />
        </label>
        {localError && <div className="error-panel">{localError}</div>}
        <button type="submit" disabled={submitting || !loginName || !secret}>
          {submitting ? "Connexion…" : "Ouvrir la session de secours"}
        </button>
      </form>
    </details>
  );
}
