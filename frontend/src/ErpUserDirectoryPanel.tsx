import { useEffect, useMemo, useState } from "react";

import { ApiError } from "./api";
import {
  ErpUserDirectoryReadModel,
  getErpUsers,
  syncErpUsers,
  updateErpUser,
} from "./erpUserAdminApi";
import { UserRoleDefinition } from "./userAdminApi";


function oidcLabel(state: string) {
  if (state === "linked") return "Résolu";
  if (state === "conflict") return "Conflit";
  return "En attente";
}

function message(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

export default function ErpUserDirectoryPanel({
  roleCatalog,
}: {
  roleCatalog: UserRoleDefinition[];
}) {
  const [users, setUsers] = useState<ErpUserDirectoryReadModel[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [active, setActive] = useState(false);
  const [roles, setRoles] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const selected = useMemo(
    () => users.find((user) => user.user_id === selectedId) ?? null,
    [users, selectedId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    getErpUsers(controller.signal)
      .then((rows) => {
        setUsers(rows);
        setSelectedId((current) =>
          current && rows.some((row) => row.user_id === current)
            ? current
            : rows[0]?.user_id ?? null,
        );
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(message(reason, "Impossible de charger l’annuaire ERP."));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [refreshKey]);

  useEffect(() => {
    if (!selected) return;
    setActive(selected.local_active);
    setRoles([...selected.roles]);
  }, [selected]);

  function toggleRole(role: string) {
    setRoles((current) =>
      current.includes(role)
        ? current.filter((item) => item !== role)
        : [...current, role],
    );
  }

  async function synchronize() {
    if (pending) return;
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      const result = await syncErpUsers();
      setNotice(
        `RP_Users : ${result.received} reçus, ${result.created} créés, ${result.updated} modifiés, ${result.unchanged} inchangés, ${result.errors} erreurs.`,
      );
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(message(reason, "Impossible de synchroniser RP_Users."));
    } finally {
      setPending(false);
    }
  }

  async function save() {
    if (!selected || pending) return;
    if (active && roles.length === 0) {
      setError("Au moins un rôle local est requis pour activer cet utilisateur.");
      return;
    }
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await updateErpUser(selected.user_id, { active, roles });
      setUsers((current) =>
        current.map((row) => (row.user_id === saved.user_id ? saved : row)),
      );
      setNotice("Activation et rôles locaux enregistrés.");
    } catch (reason) {
      setError(message(reason, "Impossible de mettre à jour l’utilisateur ERP."));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="erp-user-directory">
      <div className="erp-user-directory-heading">
        <div>
          <span className="eyebrow">Acumatica</span>
          <h3>Annuaire RP_Users</h3>
          <p>
            UserID et EmployeID proviennent de l’ERP. Le login OIDC utilise
            preferred_username → UserID; l’activation et les rôles restent locaux.
          </p>
        </div>
        <button className="quiet-button" type="button" onClick={synchronize} disabled={pending}>
          Synchroniser RP_Users
        </button>
      </div>

      {error && <div className="inline-error">{error}</div>}
      {notice && <div className="user-admin-notice">{notice}</div>}

      <div className="erp-user-directory-layout">
        <div className="erp-user-directory-table-wrap">
          {loading ? (
            <p className="user-admin-empty">Chargement…</p>
          ) : (
            <table className="erp-user-directory-table">
              <thead>
                <tr>
                  <th>Utilisateur</th>
                  <th>UserID</th>
                  <th>EmployeID / ressource</th>
                  <th>ERP User</th>
                  <th>ERP Employé</th>
                  <th>OIDC</th>
                  <th>RP</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr
                    key={user.user_id}
                    className={user.user_id === selectedId ? "selected" : ""}
                    onClick={() => setSelectedId(user.user_id)}
                  >
                    <td>{user.display_name}</td>
                    <td><code>{user.user_id}</code></td>
                    <td>
                      <code>{user.employee_external_id}</code>
                      <small>{user.resource_name ?? "Ressource non résolue"}</small>
                    </td>
                    <td>{user.erp_user_active ? "Actif" : "Inactif"}</td>
                    <td>{user.employee_status ?? "—"}</td>
                    <td>{oidcLabel(user.oidc_state)}</td>
                    <td>{user.local_active ? "Activé" : "Désactivé"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {selected && (
          <aside className="erp-user-directory-editor">
            <h4>{selected.display_name}</h4>
            <dl>
              <div><dt>UserID</dt><dd>{selected.user_id}</dd></div>
              <div><dt>EmployeID</dt><dd>{selected.employee_external_id}</dd></div>
              <div><dt>Ressource</dt><dd>{selected.resource_name ?? "Non résolue"}</dd></div>
              <div><dt>OIDC</dt><dd>{oidcLabel(selected.oidc_state)}</dd></div>
            </dl>

            <fieldset className="user-role-fieldset">
              <legend>Rôles RessourcePlanner</legend>
              <div className="user-role-grid">
                {roleCatalog.map((definition) => (
                  <label
                    key={definition.role}
                    className={roles.includes(definition.role) ? "selected" : ""}
                  >
                    <span className="user-role-choice">
                      <input
                        type="checkbox"
                        checked={roles.includes(definition.role)}
                        onChange={() => toggleRole(definition.role)}
                      />
                      <strong>{definition.label}</strong>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <label className="user-active-toggle">
              <input
                type="checkbox"
                checked={active}
                onChange={(event) => setActive(event.target.checked)}
              />
              <span>
                <strong>Activation RessourcePlanner</strong>
                <small>
                  Cette activation autorise le provisionnement contrôlé au premier login;
                  aucun rôle n’est dérivé des claims OIDC ou de l’ERP.
                </small>
              </span>
            </label>
            {!selected.source_admissible && (
              <p className="user-admin-warning">
                L’état ERP actuel n’est pas admissible; l’activation locale ne donnera pas accès.
              </p>
            )}
            <button className="primary-button" type="button" onClick={save} disabled={pending}>
              {pending ? "Enregistrement…" : "Enregistrer"}
            </button>
          </aside>
        )}
      </div>
    </section>
  );
}
