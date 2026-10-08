import { sortProjectsRecentFirst } from "./projectRecency";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { getProjects, type ProjectReadModel } from "./api";
import {
  createResourceClass,
  createTaskClassStandard,
  getProjectTaskClassRules,
  getResourceClasses,
  getTaskClassStandards,
  removeProjectTaskClassOverride,
  setProjectTaskClassOverride,
  type ProjectTaskClassRuleReadModel,
  type ResourceClassConfigReadModel,
  type TaskClassStandardReadModel,
  updateResourceClass,
  updateTaskClassStandard,
} from "./resourceClassesApi";
import "./resource-classes.css";


function message(reason: unknown) {
  return reason instanceof Error
    ? reason.message
    : "Administration des classes workforce indisponible.";
}

function sortClasses(rows: ResourceClassConfigReadModel[]) {
  return [...rows].sort((left, right) => left.code.localeCompare(right.code, "fr-CA"));
}

function sortStandards(rows: TaskClassStandardReadModel[]) {
  return [...rows].sort((left, right) => left.task_code.localeCompare(right.task_code, "fr-CA"));
}

export default function ResourceClassesPanel() {
  const [classes, setClasses] = useState<ResourceClassConfigReadModel[]>([]);
  const [standards, setStandards] = useState<TaskClassStandardReadModel[]>([]);
  const [projects, setProjects] = useState<ProjectReadModel[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [rules, setRules] = useState<ProjectTaskClassRuleReadModel[]>([]);

  const [classCode, setClassCode] = useState("");
  const [classLabel, setClassLabel] = useState("");
  const [classCost, setClassCost] = useState("");

  const [standardTaskCode, setStandardTaskCode] = useState("");
  const [standardClassCode, setStandardClassCode] = useState("");

  const [exceptionTaskCode, setExceptionTaskCode] = useState("");
  const [exceptionValue, setExceptionValue] = useState("__EXCLUDE__");

  const [pending, setPending] = useState<string | null>(null);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      getResourceClasses(controller.signal),
      getTaskClassStandards(controller.signal),
      getProjects(true, controller.signal),
    ])
      .then(([classRows, standardRows, projectRows]) => {
        setClasses(sortClasses(classRows));
        setStandards(sortStandards(standardRows));
        setProjects(sortProjectsRecentFirst(projectRows));
        setStandardClassCode((current) => current || classRows.find((row) => row.active)?.code || "");
        setSelectedProjectId((current) => current || projectRows[0]?.id || "");
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(message(reason));
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedProjectId) {
      setRules([]);
      return;
    }
    const controller = new AbortController();
    setRulesLoading(true);
    getProjectTaskClassRules(selectedProjectId, controller.signal)
      .then(setRules)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(message(reason));
        }
      })
      .finally(() => setRulesLoading(false));
    return () => controller.abort();
  }, [selectedProjectId]);

  const classLabels = useMemo(
    () => new Map(classes.map((row) => [row.code, row.label])),
    [classes],
  );
  const activeClasses = useMemo(
    () => classes.filter((row) => row.active),
    [classes],
  );

  function replaceClass(next: ResourceClassConfigReadModel) {
    setClasses((rows) => sortClasses(rows.map((row) => row.code === next.code ? next : row)));
  }

  function replaceStandard(next: TaskClassStandardReadModel) {
    setStandards((rows) => sortStandards(rows.map((row) => (
      row.task_code === next.task_code ? next : row
    ))));
  }

  async function refreshRules() {
    if (!selectedProjectId) return;
    setRules(await getProjectTaskClassRules(selectedProjectId));
  }

  async function submitClass(event: FormEvent) {
    event.preventDefault();
    if (pending || !classCode.trim() || !classLabel.trim() || !classCost.trim()) return;
    setPending("class:create");
    setError(null);
    setNotice(null);
    try {
      const created = await createResourceClass({
        code: classCode.trim(),
        label: classLabel.trim(),
        average_hourly_cost_cad: classCost.trim(),
        active: true,
      });
      setClasses((rows) => sortClasses([...rows, created]));
      setClassCode("");
      setClassLabel("");
      setClassCost("");
      setStandardClassCode((current) => current || created.code);
      setNotice(`Classe ${created.code} créée.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function saveClass(row: ResourceClassConfigReadModel) {
    setPending(`class:${row.code}`);
    setError(null);
    setNotice(null);
    try {
      const next = await updateResourceClass(row.code, row.version, {
        label: row.label.trim(),
        average_hourly_cost_cad: row.average_hourly_cost_cad?.trim() || null,
        active: row.active,
      });
      replaceClass(next);
      setNotice(`Classe ${next.code} mise à jour.`);
      if (selectedProjectId) await refreshRules();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function submitStandard(event: FormEvent) {
    event.preventDefault();
    if (pending || !standardTaskCode.trim() || !standardClassCode) return;
    setPending("standard:create");
    setError(null);
    setNotice(null);
    try {
      const created = await createTaskClassStandard({
        task_code: standardTaskCode.trim(),
        resource_class_code: standardClassCode,
        active: true,
      });
      setStandards((rows) => sortStandards([...rows, created]));
      setStandardTaskCode("");
      setNotice(`Standard TaskCD ${created.task_code} créé.`);
      if (selectedProjectId) await refreshRules();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function saveStandard(row: TaskClassStandardReadModel) {
    setPending(`standard:${row.task_code}`);
    setError(null);
    setNotice(null);
    try {
      const next = await updateTaskClassStandard(row.task_code, row.version, {
        resource_class_code: row.resource_class_code,
        active: row.active,
      });
      replaceStandard(next);
      setNotice(`Standard TaskCD ${next.task_code} mis à jour.`);
      if (selectedProjectId) await refreshRules();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function changeException(
    rule: ProjectTaskClassRuleReadModel,
    value: string,
  ) {
    if (!selectedProjectId || pending) return;
    setPending(`override:${rule.task_code}`);
    setError(null);
    setNotice(null);
    try {
      if (value === "__STANDARD__") {
        if (rule.override) {
          await removeProjectTaskClassOverride(
            selectedProjectId,
            rule.task_code,
            rule.override.version,
          );
        }
      } else if (value === "__EXCLUDE__") {
        await setProjectTaskClassOverride(selectedProjectId, rule.task_code, {
          expected_version: rule.override?.version,
          excluded: true,
        });
      } else {
        await setProjectTaskClassOverride(selectedProjectId, rule.task_code, {
          expected_version: rule.override?.version,
          resource_class_code: value,
          excluded: false,
        });
      }
      await refreshRules();
      setNotice(`Exception TaskCD ${rule.task_code} mise à jour.`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  async function submitException(event: FormEvent) {
    event.preventDefault();
    if (!selectedProjectId || pending || !exceptionTaskCode.trim()) return;
    setPending("override:create");
    setError(null);
    setNotice(null);
    try {
      await setProjectTaskClassOverride(selectedProjectId, exceptionTaskCode.trim(), {
        excluded: exceptionValue === "__EXCLUDE__",
        resource_class_code: exceptionValue === "__EXCLUDE__" ? null : exceptionValue,
      });
      setExceptionTaskCode("");
      await refreshRules();
      setNotice("Exception projet créée.");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="configuration-card resource-classes-panel" data-testid="resource-classes-admin">
      <div className="configuration-section-heading">
        <div>
          <span className="eyebrow">#454 · Référentiel workforce</span>
          <h3>Classes, coûts moyens et standards TaskCD</h3>
          <small>
            La classe décrit la nature du besoin workforce. Elle reste strictement distincte
            des périmètres d’approbation de #276. La résolution effective affichée ici vient
            du backend.
          </small>
        </div>
      </div>

      {error && <div className="error-panel"><strong>Erreur</strong><span>{error}</span></div>}
      {notice && <div className="configuration-notice">{notice}</div>}

      <div className="resource-class-section">
        <div className="resource-class-section-heading">
          <div>
            <h4>Classes et coûts</h4>
            <small>Coût moyen de planification en CAD/h — jamais un salaire individuel.</small>
          </div>
        </div>

        <form className="resource-class-create" onSubmit={submitClass}>
          <label>
            Classe
            <input
              value={classCode}
              onChange={(event) => setClassCode(event.target.value)}
              placeholder="PROGRAMMEUR"
            />
          </label>
          <label>
            Libellé
            <input
              value={classLabel}
              onChange={(event) => setClassLabel(event.target.value)}
              placeholder="Programmeur"
            />
          </label>
          <label>
            Coût moyen CAD/h
            <input
              inputMode="decimal"
              value={classCost}
              onChange={(event) => setClassCost(event.target.value)}
              placeholder="125.00"
            />
          </label>
          <button
            className="primary-button"
            type="submit"
            disabled={pending !== null || !classCode.trim() || !classLabel.trim() || !classCost.trim()}
          >
            Créer la classe
          </button>
        </form>

        <div className="resource-class-table-scroll">
          <table className="resource-class-table">
            <thead>
              <tr>
                <th>Classe</th>
                <th>Libellé</th>
                <th>Coût moyen CAD/h</th>
                <th>Active</th>
                <th>Version</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {classes.map((row) => (
                <tr key={row.code}>
                  <td><code>{row.code}</code></td>
                  <td>
                    <input
                      value={row.label}
                      onChange={(event) => setClasses((items) => items.map((item) => (
                        item.code === row.code ? { ...item, label: event.target.value } : item
                      )))}
                    />
                  </td>
                  <td>
                    <input
                      inputMode="decimal"
                      value={row.average_hourly_cost_cad ?? ""}
                      onChange={(event) => setClasses((items) => items.map((item) => (
                        item.code === row.code
                          ? { ...item, average_hourly_cost_cad: event.target.value || null }
                          : item
                      )))}
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={row.active}
                      onChange={(event) => setClasses((items) => items.map((item) => (
                        item.code === row.code ? { ...item, active: event.target.checked } : item
                      )))}
                    />
                  </td>
                  <td>{row.version}</td>
                  <td>
                    <button
                      className="quiet-button"
                      type="button"
                      disabled={pending !== null}
                      onClick={() => void saveClass(row)}
                    >
                      Enregistrer
                    </button>
                  </td>
                </tr>
              ))}
              {classes.length === 0 && (
                <tr><td colSpan={6}>Aucune classe configurée.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="resource-class-section">
        <div className="resource-class-section-heading">
          <div>
            <h4>Standards TaskCD</h4>
            <small>Mapping global administrable — aucune règle TaskCD n’est codée dans React.</small>
          </div>
        </div>

        <form className="resource-class-create standards-create" onSubmit={submitStandard}>
          <label>
            TaskCD
            <input
              value={standardTaskCode}
              onChange={(event) => setStandardTaskCode(event.target.value)}
              placeholder="216"
            />
          </label>
          <label>
            Classe par défaut
            <select
              value={standardClassCode}
              onChange={(event) => setStandardClassCode(event.target.value)}
            >
              <option value="">Choisir…</option>
              {activeClasses.map((row) => (
                <option key={row.code} value={row.code}>{row.code} · {row.label}</option>
              ))}
            </select>
          </label>
          <button
            className="primary-button"
            type="submit"
            disabled={pending !== null || !standardTaskCode.trim() || !standardClassCode}
          >
            Créer le standard
          </button>
        </form>

        <div className="resource-class-table-scroll">
          <table className="resource-class-table">
            <thead>
              <tr>
                <th>TaskCD</th>
                <th>Classe par défaut</th>
                <th>Active</th>
                <th>Version</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {standards.map((row) => (
                <tr key={row.task_code}>
                  <td><code>{row.task_code}</code></td>
                  <td>
                    <select
                      value={row.resource_class_code}
                      onChange={(event) => setStandards((items) => items.map((item) => (
                        item.task_code === row.task_code
                          ? { ...item, resource_class_code: event.target.value }
                          : item
                      )))}
                    >
                      {classes.map((resourceClass) => (
                        <option key={resourceClass.code} value={resourceClass.code}>
                          {resourceClass.code} · {resourceClass.label}
                          {resourceClass.active ? "" : " (inactive)"}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={row.active}
                      onChange={(event) => setStandards((items) => items.map((item) => (
                        item.task_code === row.task_code ? { ...item, active: event.target.checked } : item
                      )))}
                    />
                  </td>
                  <td>{row.version}</td>
                  <td>
                    <button
                      className="quiet-button"
                      type="button"
                      disabled={pending !== null}
                      onClick={() => void saveStandard(row)}
                    >
                      Enregistrer
                    </button>
                  </td>
                </tr>
              ))}
              {standards.length === 0 && (
                <tr><td colSpan={5}>Aucun standard configuré.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="resource-class-section">
        <div className="resource-class-section-heading">
          <div>
            <h4>Exceptions projet</h4>
            <small>Remplacer la classe, EXCLUDE ou revenir au standard global.</small>
          </div>
          <label className="resource-project-select">
            Projet
            <select
              value={selectedProjectId}
              onChange={(event) => setSelectedProjectId(event.target.value)}
            >
              <option value="">Choisir…</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.number} · {project.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {selectedProjectId && (
          <form className="resource-class-create exception-create" onSubmit={submitException}>
            <label>
              TaskCD
              <input
                value={exceptionTaskCode}
                onChange={(event) => setExceptionTaskCode(event.target.value)}
                placeholder="216"
              />
            </label>
            <label>
              Exception projet
              <select
                value={exceptionValue}
                onChange={(event) => setExceptionValue(event.target.value)}
              >
                <option value="__EXCLUDE__">EXCLUDE</option>
                {activeClasses.map((row) => (
                  <option key={row.code} value={row.code}>{row.code} · {row.label}</option>
                ))}
              </select>
            </label>
            <button
              className="primary-button"
              type="submit"
              disabled={pending !== null || !exceptionTaskCode.trim()}
            >
              Ajouter l’exception
            </button>
          </form>
        )}

        <div className="resource-class-table-scroll">
          <table className="resource-class-table">
            <thead>
              <tr>
                <th>TaskCD</th>
                <th>Standard</th>
                <th>Exception projet</th>
                <th>Résolution effective</th>
              </tr>
            </thead>
            <tbody>
              {!selectedProjectId ? (
                <tr><td colSpan={4}>Sélectionnez un projet.</td></tr>
              ) : rulesLoading ? (
                <tr><td colSpan={4}>Chargement…</td></tr>
              ) : rules.length === 0 ? (
                <tr><td colSpan={4}>Aucun standard ni exception pour ce projet.</td></tr>
              ) : rules.map((rule) => {
                const overrideValue = rule.override?.excluded
                  ? "__EXCLUDE__"
                  : rule.override?.resource_class_code || "__STANDARD__";
                return (
                  <tr key={rule.task_code}>
                    <td><code>{rule.task_code}</code></td>
                    <td>
                      {rule.standard
                        ? `${rule.standard.resource_class_code}${rule.standard.active ? "" : " (inactif)"}`
                        : "Aucun"}
                    </td>
                    <td>
                      <select
                        value={overrideValue}
                        disabled={pending !== null}
                        onChange={(event) => void changeException(rule, event.target.value)}
                      >
                        <option value="__STANDARD__">Revenir au standard</option>
                        <option value="__EXCLUDE__">EXCLUDE</option>
                        {classes.map((row) => (
                          <option key={row.code} value={row.code}>
                            {row.code} · {row.label}{row.active ? "" : " (inactive)"}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <strong>
                        {rule.resolution.status === "CLASS"
                          ? rule.resolution.resource_class_code
                          : rule.resolution.status}
                      </strong>
                      {rule.resolution.resource_class_code && (
                        <small>{classLabels.get(rule.resolution.resource_class_code) ?? ""}</small>
                      )}
                      {rule.resolution.diagnostics.length > 0 && (
                        <small>{rule.resolution.diagnostics.join(" · ")}</small>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
