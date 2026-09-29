import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  type ApprovalScopeReadModel,
  createApprovalScope,
  getApprovalScopes,
  setApprovalScopeApprover,
  setApprovalScopeResourceClass,
  updateApprovalScope,
} from "./approvalScopesApi";
import {
  getResourceClasses,
  type ResourceClassConfigReadModel,
} from "./resourceClassesApi";
import {
  getAdminRoleCatalog,
  getAdminUsers,
  type UserAdminReadModel,
  type UserRoleDefinition,
} from "./userAdminApi";
import "./approval-scopes.css";

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : "Administration des périmètres indisponible.";
}

export default function ApprovalScopesPanel() {
  const [scopes, setScopes] = useState<ApprovalScopeReadModel[]>([]);
  const [users, setUsers] = useState<UserAdminReadModel[]>([]);
  const [roles, setRoles] = useState<UserRoleDefinition[]>([]);
  const [resourceClasses, setResourceClasses] = useState<ResourceClassConfigReadModel[]>([]);
  const [code, setCode] = useState("");
  const [label, setLabel] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      getApprovalScopes(controller.signal),
      getAdminUsers(controller.signal),
      getAdminRoleCatalog(controller.signal),
      getResourceClasses(controller.signal),
    ])
      .then(([scopeRows, userRows, roleRows, classRows]) => {
        setScopes(scopeRows);
        setUsers(userRows);
        setRoles(roleRows);
        setResourceClasses(classRows);
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(message(reason));
        }
      });
    return () => controller.abort();
  }, []);

  const approvingRoles = useMemo(
    () => new Set(
      roles
        .filter((role) => role.permissions.includes("approve_demands"))
        .map((role) => role.role),
    ),
    [roles],
  );
  const eligibleUsers = useMemo(
    () => users.filter(
      (user) => user.active && user.roles.some((role) => approvingRoles.has(role)),
    ),
    [users, approvingRoles],
  );

  function replaceScope(next: ApprovalScopeReadModel) {
    setScopes((rows) => rows.map((row) => row.id === next.id ? next : row));
  }

  async function createScope(event: FormEvent) {
    event.preventDefault();
    if (pending || !code.trim() || !label.trim()) return;
    setPending("create");
    setError(null);
    setNotice(null);
    try {
      const created = await createApprovalScope(code.trim(), label.trim());
      setScopes((rows) => [...rows, created].sort((a, b) => a.code.localeCompare(b.code)));
      setCode("");
      setLabel("");
      setNotice(`Périmètre ${created.code} créé.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function toggleActive(scope: ApprovalScopeReadModel, active: boolean) {
    setPending(`active:${scope.id}`);
    setError(null);
    setNotice(null);
    try {
      const next = await updateApprovalScope(scope.id, scope.version, { active });
      replaceScope(next);
      setNotice(`Périmètre ${next.code} mis à jour.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function toggleResourceClass(
    scope: ApprovalScopeReadModel,
    resourceClass: ResourceClassConfigReadModel,
    assigned: boolean,
  ) {
    setPending(`resource-class:${scope.id}:${resourceClass.code}`);
    setError(null);
    setNotice(null);
    try {
      const next = await setApprovalScopeResourceClass(
        scope.id,
        resourceClass.code,
        assigned,
        scope.version,
      );
      replaceScope(next);
      setNotice(`Classes couvertes de ${next.code} mises à jour.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function toggleApprover(
    scope: ApprovalScopeReadModel,
    user: UserAdminReadModel,
    assigned: boolean,
  ) {
    setPending(`approver:${scope.id}:${user.user_id}`);
    setError(null);
    setNotice(null);
    try {
      const next = await setApprovalScopeApprover(
        scope.id,
        user.user_id,
        assigned,
        scope.version,
      );
      replaceScope(next);
      setNotice(`Approbateurs de ${next.code} mis à jour.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="configuration-card approval-scopes-panel" data-testid="approval-scopes-admin">
      <div className="configuration-section-heading">
        <div>
          <span className="eyebrow">#276 · Routage d’approbation</span>
          <h3>Périmètres et approbateurs</h3>
          <small>
            Les associations actives sont figées au moment de la soumission. Modifier ce référentiel
            ne réécrit jamais un cycle déjà ouvert.
          </small>
        </div>
      </div>

      {error && <div className="error-panel"><strong>Erreur</strong><span>{error}</span></div>}
      {notice && <div className="configuration-notice">{notice}</div>}

      <form className="approval-scope-create" onSubmit={createScope}>
        <label>
          Code
          <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="AUTOMATION" />
        </label>
        <label>
          Libellé
          <input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Automatisation" />
        </label>
        <button className="primary-button" type="submit" disabled={pending !== null || !code.trim() || !label.trim()}>
          Créer le périmètre
        </button>
      </form>

      <div className="approval-scope-list">
        {scopes.map((scope) => (
          <article className="approval-scope-card" data-testid={`approval-scope-${scope.code}`} key={scope.id}>
            <div className="approval-scope-heading">
              <div>
                <strong>{scope.code} · {scope.label}</strong>
                <small>Version {scope.version}</small>
              </div>
              <label className="configuration-toggle">
                <input
                  type="checkbox"
                  checked={scope.active}
                  disabled={pending !== null}
                  onChange={(event) => void toggleActive(scope, event.target.checked)}
                />
                Actif
              </label>
            </div>

            <div className="approval-scope-approvers approval-scope-classes">
              <span>Classes couvertes</span>
              {resourceClasses.length === 0 ? (
                <small>Aucune classe du catalogue canonique #454.</small>
              ) : resourceClasses.map((resourceClass) => {
                const assigned = scope.resource_class_codes.includes(resourceClass.code);
                return (
                  <label key={resourceClass.code}>
                    <input
                      type="checkbox"
                      checked={assigned}
                      disabled={pending !== null || (!resourceClass.active && !assigned)}
                      onChange={(event) => void toggleResourceClass(
                        scope,
                        resourceClass,
                        event.target.checked,
                      )}
                    />
                    {resourceClass.label}
                    <small>
                      {resourceClass.code}{resourceClass.active ? "" : " · inactive"}
                    </small>
                  </label>
                );
              })}
            </div>

            <div className="approval-scope-approvers">
              <span>Approbateurs admissibles</span>
              {eligibleUsers.length === 0 ? (
                <small>Aucun AppUser actif avec la permission approve_demands.</small>
              ) : eligibleUsers.map((user) => (
                <label key={user.user_id}>
                  <input
                    type="checkbox"
                    checked={scope.approver_user_ids.includes(user.user_id)}
                    disabled={pending !== null}
                    onChange={(event) => void toggleApprover(scope, user, event.target.checked)}
                  />
                  {user.display_name}
                  <small>{user.roles.join(" · ")}</small>
                </label>
              ))}
            </div>

            <div className="approval-scope-approvers approval-scope-overrides">
              <span>Overrides de tâches</span>
              <small>
                Associations exceptionnelles : {scope.task_catalog_item_ids.length
                  ? scope.task_catalog_item_ids.join(", ")
                  : "aucune"}
              </small>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
