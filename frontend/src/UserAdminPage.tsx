import { FormEvent, useEffect, useMemo, useState } from "react";

import { ApiError } from "./api";
import { accountStateLabel, oidcStateLabel } from "./identityAdmin";
import ErpUserDirectoryPanel from "./ErpUserDirectoryPanel";
import { useAuth } from "./AuthContext";
import {
  UserAdminCreate,
  UserAdminReadModel,
  UserRoleDefinition,
  createAdminUser,
  getAdminRoleCatalog,
  getAdminUsers,
  updateAdminUser,
} from "./userAdminApi";
import "./user-admin.css";


type Draft = {
  issuer: string;
  subject: string;
  display_name: string;
  email: string;
  phone: string;
  roles: string[];
  active: boolean;
};

function blankDraft(): Draft {
  return {
    issuer: "",
    subject: "",
    display_name: "",
    email: "",
    phone: "",
    roles: [],
    active: true,
  };
}

function draftFrom(user: UserAdminReadModel): Draft {
  return {
    issuer: user.issuer ?? "",
    subject: user.subject ?? "",
    display_name: user.display_name,
    email: user.email ?? "",
    phone: user.phone ?? "",
    roles: [...user.roles],
    active: user.active,
  };
}

function message(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

export default function UserAdminPage() {
  const { principal } = useAuth();
  const [users, setUsers] = useState<UserAdminReadModel[]>([]);
  const [roleCatalog, setRoleCatalog] = useState<UserRoleDefinition[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Draft>(blankDraft());
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"active" | "inactive" | "all">("active");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const selected = useMemo(
    () => users.find((user) => user.user_id === selectedId) ?? null,
    [users, selectedId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getAdminUsers(controller.signal),
      getAdminRoleCatalog(controller.signal),
    ])
      .then(([userRows, roles]) => {
        setUsers(userRows);
        setRoleCatalog(roles);
        if (!creating) {
          setSelectedId((current) => {
            if (current && userRows.some((user) => user.user_id === current)) return current;
            return userRows.find((user) => user.active)?.user_id ?? userRows[0]?.user_id ?? null;
          });
        }
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(message(reason, "Impossible de charger les utilisateurs."));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [refreshKey, creating]);

  useEffect(() => {
    if (creating) return;
    if (selected) setDraft(draftFrom(selected));
  }, [selected, creating]);

  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("fr");
    return users.filter((user) => {
      if (statusFilter === "active" && !user.active) return false;
      if (statusFilter === "inactive" && user.active) return false;
      if (!query) return true;
      return [
        user.display_name,
        user.email ?? "",
        user.erp_user_id ?? "",
        user.employee_external_id ?? "",
        user.roles.join(" "),
      ].some((value) => value.toLocaleLowerCase("fr").includes(query));
    });
  }, [users, search, statusFilter]);

  function choose(user: UserAdminReadModel) {
    setCreating(false);
    setSelectedId(user.user_id);
    setDraft(draftFrom(user));
    setError(null);
    setNotice(null);
  }

  function startCreate() {
    setCreating(true);
    setSelectedId(null);
    setDraft(blankDraft());
    setError(null);
    setNotice(null);
  }

  function toggleRole(role: string) {
    setDraft((current) => ({
      ...current,
      roles: current.roles.includes(role)
        ? current.roles.filter((item) => item !== role)
        : [...current.roles, role],
    }));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setError(null);
    setNotice(null);

    if (!draft.display_name.trim()) {
      setError("Le nom d’affichage est requis.");
      return;
    }
    if (creating && (!draft.issuer.trim() || !draft.subject.trim())) {
      setError("Issuer et subject sont requis pour provisionner l’identité externe.");
      return;
    }
    if (draft.roles.length === 0) {
      setError("Sélectionne au moins un rôle RessourcePlanner.");
      return;
    }

    setPending(true);
    try {
      let saved: UserAdminReadModel;
      if (creating) {
        const payload: UserAdminCreate = {
          issuer: draft.issuer.trim(),
          subject: draft.subject.trim(),
          display_name: draft.display_name.trim(),
          email: draft.email.trim() || null,
          phone: draft.phone.trim() || null,
          roles: draft.roles,
          active: draft.active,
        };
        saved = await createAdminUser(payload);
        setNotice("Utilisateur provisionné.");
      } else if (selected) {
        saved = await updateAdminUser(selected.user_id, {
          display_name: draft.display_name.trim(),
          email: draft.email.trim() || null,
          phone: draft.phone.trim() || null,
          roles: draft.roles,
          active: draft.active,
        });
        setNotice("Utilisateur mis à jour.");
      } else {
        return;
      }
      setCreating(false);
      setSelectedId(saved.user_id);
      setDraft(draftFrom(saved));
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(message(reason, "Impossible d’enregistrer l’utilisateur."));
    } finally {
      setPending(false);
    }
  }

  const isSelf = Boolean(selected && principal?.local_user_id === selected.user_id);

  return (
    <section className="user-admin-page">
      <header className="user-admin-heading">
        <div>
          <span className="eyebrow">Sécurité</span>
          <h2>Utilisateurs et rôles</h2>
          <p>
            Les identités OIDC et leurs profils métier sont administrés ici. Les permissions restent définies et appliquées par FastAPI.
          </p>
        </div>
        <button className="primary-button" type="button" onClick={startCreate}>
          Nouvel utilisateur
        </button>
      </header>

      {error && <div className="inline-error">{error}</div>}
      {notice && <div className="user-admin-notice">{notice}</div>}

      <ErpUserDirectoryPanel roleCatalog={roleCatalog} />

      <div className="user-admin-layout">
        <aside className="user-admin-list-panel">
          <div className="user-admin-filters">
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Rechercher un utilisateur"
              aria-label="Rechercher un utilisateur"
            />
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}>
              <option value="active">Actifs</option>
              <option value="inactive">Inactifs</option>
              <option value="all">Tous</option>
            </select>
          </div>

          {loading ? (
            <p className="user-admin-empty">Chargement…</p>
          ) : filtered.length === 0 ? (
            <p className="user-admin-empty">Aucun utilisateur ne correspond aux filtres.</p>
          ) : (
            <div className="user-admin-user-list">
              {filtered.map((user) => (
                <button
                  type="button"
                  key={user.user_id}
                  className={user.user_id === selectedId && !creating ? "selected" : ""}
                  onClick={() => choose(user)}
                >
                  <span>
                    <strong>{user.display_name}</strong>
                    <small>{user.email || user.erp_user_id || "Compte RessourcePlanner"}</small>
                  </span>
                  <span className="user-status-stack">
                    <span className={user.active ? "user-status active" : "user-status inactive"}>
                      Compte · {accountStateLabel(user.active)}
                    </span>
                    <span className={`user-status oidc-${user.oidc_state}`}>
                      OIDC · {oidcStateLabel(user.oidc_state)}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </aside>

        <form className="user-admin-editor" onSubmit={submit}>
          <div className="editor-heading">
            <div>
              <span className="eyebrow">{creating ? "Provisionnement" : "Compte local"}</span>
              <h3>{creating ? "Nouvel utilisateur" : selected?.display_name ?? "Sélectionne un utilisateur"}</h3>
            </div>
            {isSelf && <span className="user-admin-self">Votre compte</span>}
          </div>

          {(creating || selected) ? (
            <>
              {selected && (
                <div className="identity-status-grid" data-testid="app-user-identity-status">
                  <div className="identity-status-card">
                    <span>Compte</span>
                    <strong>{accountStateLabel(selected.active)}</strong>
                  </div>
                  <div className={`identity-status-card oidc-${selected.oidc_state}`}>
                    <span>OIDC</span>
                    <strong>{oidcStateLabel(selected.oidc_state)}</strong>
                  </div>
                  <div className="identity-status-card identity-reference">
                    <span>AppUser</span>
                    <strong><code>{selected.app_user_id}</code></strong>
                  </div>
                  {selected.erp_user_id && (
                    <div className="identity-status-card identity-reference">
                      <span>UserID ERP</span>
                      <strong><code>{selected.erp_user_id}</code></strong>
                    </div>
                  )}
                </div>
              )}

              <div className="form-grid two-columns">
                <label>
                  Issuer OIDC (technique)
                  <input
                    value={draft.issuer}
                    readOnly={!creating}
                    onChange={(event) => setDraft((current) => ({ ...current, issuer: event.target.value }))}
                    placeholder={creating ? "https://…" : "Non lié"}
                  />
                </label>
                <label>
                  Subject OIDC (technique)
                  <input
                    value={draft.subject}
                    readOnly={!creating}
                    onChange={(event) => setDraft((current) => ({ ...current, subject: event.target.value }))}
                    placeholder={creating ? "Identifiant subject exact" : "Non lié"}
                  />
                </label>
              </div>

              <div className="form-grid two-columns">
                <label>
                  Nom d’affichage
                  <input
                    value={draft.display_name}
                    onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))}
                  />
                </label>
                <label>
                  Courriel
                  <input
                    type="email"
                    value={draft.email}
                    onChange={(event) => setDraft((current) => ({ ...current, email: event.target.value }))}
                    placeholder="Optionnel"
                  />
                </label>
                <label>
                  Téléphone
                  <input
                    value={draft.phone}
                    onChange={(event) => setDraft((current) => ({ ...current, phone: event.target.value }))}
                    placeholder="Optionnel"
                  />
                </label>
              </div>

              <fieldset className="user-role-fieldset">
                <legend>Rôles RessourcePlanner</legend>
                <div className="user-role-grid">
                  {roleCatalog.map((definition) => {
                    const checked = draft.roles.includes(definition.role);
                    const selfAdmin = isSelf && definition.role === "ADMIN";
                    return (
                      <label key={definition.role} className={checked ? "selected" : ""}>
                        <span className="user-role-choice">
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={selfAdmin}
                            onChange={() => toggleRole(definition.role)}
                          />
                          <strong>{definition.label}</strong>
                        </span>
                        <small>{definition.permissions.join(" · ")}</small>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              <label className="user-active-toggle">
                <input
                  type="checkbox"
                  checked={draft.active}
                  disabled={isSelf}
                  onChange={(event) => setDraft((current) => ({ ...current, active: event.target.checked }))}
                />
                <span>
                  <strong>Compte actif</strong>
                  <small>Un compte inactif ne peut plus résoudre une session OIDC, même si son cookie existe encore.</small>
                </span>
              </label>

              <div className="editor-actions">
                {creating && (
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => {
                      setCreating(false);
                      setSelectedId(users.find((user) => user.active)?.user_id ?? users[0]?.user_id ?? null);
                    }}
                  >
                    Annuler
                  </button>
                )}
                <button className="primary-button" type="submit" disabled={pending}>
                  {pending ? "Enregistrement…" : creating ? "Provisionner" : "Enregistrer"}
                </button>
              </div>
            </>
          ) : (
            <p className="user-admin-empty">Sélectionne un utilisateur ou crée-en un nouveau.</p>
          )}
        </form>
      </div>
    </section>
  );
}
