import { FormEvent, useEffect, useState } from "react";

import {
  ApiError,
  CompetencyReadModel,
  CompetencyWrite,
  createCompetency,
  deactivateCompetency,
  setCompetencyResourceClass,
  updateCompetency,
} from "./api";
import {
  getResourceClassOptions,
  ResourceClassOptionReadModel,
} from "./resourceClassesApi";

function messageFromError(reason: unknown) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : "Impossible de modifier le catalogue.";
}

function draftFrom(row: CompetencyReadModel | null): CompetencyWrite {
  return {
    name: row?.name ?? "",
    description: row?.description ?? null,
    active: row?.active ?? true,
    sort_order: row?.sort_order ?? 0,
  };
}

export default function CompetencyCatalogPanel({
  competencies,
  onChanged,
}: {
  competencies: CompetencyReadModel[];
  onChanged: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<CompetencyWrite>(() => draftFrom(null));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resourceClasses, setResourceClasses] = useState<ResourceClassOptionReadModel[]>([]);

  const selected = competencies.find((row) => row.id === selectedId) ?? null;

  useEffect(() => {
    const controller = new AbortController();
    getResourceClassOptions(controller.signal)
      .then(setResourceClasses)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(messageFromError(reason));
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (creating) return;
    if (selected) {
      setDraft(draftFrom(selected));
      return;
    }
    setSelectedId(competencies[0]?.id ?? null);
    setDraft(draftFrom(competencies[0] ?? null));
  }, [competencies, selectedId, selected, creating]);

  function beginCreate() {
    setCreating(true);
    setSelectedId(null);
    setDraft(draftFrom(null));
    setError(null);
  }

  function edit(row: CompetencyReadModel) {
    setCreating(false);
    setSelectedId(row.id);
    setDraft(draftFrom(row));
    setError(null);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    const name = draft.name.trim();
    if (!name) {
      setError("Le nom de la compétence est requis.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (creating) {
        const result = await createCompetency({ ...draft, name });
        setSelectedId(result.competency_id);
        setCreating(false);
      } else if (selectedId) {
        await updateCompetency(selectedId, { ...draft, name });
      }
      onChanged();
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setSaving(false);
    }
  }

  async function changeResourceClass(value: string) {
    if (!selected || saving) return;
    setSaving(true);
    setError(null);
    try {
      await setCompetencyResourceClass(
        selected.id,
        value || null,
        selected.resource_class_version,
      );
      onChanged();
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setSaving(false);
    }
  }

  async function deactivate() {
    if (!selected || !selected.active || saving) return;
    if (!window.confirm(`Désactiver la compétence « ${selected.name} »? Les attributions existantes seront conservées.`)) return;
    setSaving(true);
    setError(null);
    try {
      await deactivateCompetency(selected.id);
      onChanged();
    } catch (reason: unknown) {
      setError(messageFromError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="admin-card competency-catalog-card">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Référentiel local</span>
          <h2>Catalogue de compétences</h2>
          <p>Les IDs restent stables même si un libellé est renommé. Une désactivation conserve les attributions historiques.</p>
        </div>
        <button type="button" className="secondary-button" onClick={beginCreate} disabled={saving}>
          + Compétence
        </button>
      </div>

      <div className="competency-catalog-layout">
        <div className="competency-catalog-list">
          {competencies.length === 0 && <div className="empty-admin-state">Aucune compétence.</div>}
          {competencies.map((row) => (
            <button
              type="button"
              className={`competency-catalog-row ${row.id === selectedId ? "selected" : ""} ${row.active ? "" : "inactive"}`}
              onClick={() => edit(row)}
              key={row.id}
            >
              <span><strong>{row.name}</strong><small>{row.description || "Sans description"}</small></span>
              <span>#{row.sort_order}{row.active ? "" : " · inactive"}</span>
            </button>
          ))}
        </div>

        {(creating || selected) && (
          <form className="competency-editor" onSubmit={save}>
            <label>
              <span>Nom</span>
              <input value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} disabled={saving} required />
            </label>
            <label>
              <span>Description</span>
              <textarea rows={3} value={draft.description ?? ""} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value || null }))} disabled={saving} />
            </label>
            <label>
              <span>Ordre</span>
              <input type="number" min="0" step="1" value={draft.sort_order} onChange={(event) => setDraft((current) => ({ ...current, sort_order: Math.max(Number(event.target.value) || 0, 0) }))} disabled={saving} />
            </label>
            {!creating && selected && (
              <label>
                <span>Regroupement par classe</span>
                <select
                  value={selected.resource_class_code ?? ""}
                  onChange={(event) => void changeResourceClass(event.target.value)}
                  disabled={saving}
                >
                  <option value="">Sans classe</option>
                  {resourceClasses.map((row) => (
                    <option
                      key={row.code}
                      value={row.code}
                      disabled={!row.active && row.code !== selected.resource_class_code}
                    >
                      {row.code} · {row.label}{row.active ? "" : " (inactive)"}
                    </option>
                  ))}
                </select>
                <small>
                  Regroupement analytique seulement : ce choix ne change ni la classe réelle
                  d’un technicien ni son admissibilité.
                </small>
              </label>
            )}
            {!creating && selected && (
              <label className="checkbox-field">
                <input type="checkbox" checked={draft.active} onChange={(event) => setDraft((current) => ({ ...current, active: event.target.checked }))} disabled={saving} />
                <span>Compétence active</span>
              </label>
            )}
            {error && <div className="inline-error">{error}</div>}
            <div className="editor-actions">
              {!creating && selected?.active && (
                <button type="button" className="danger-button" onClick={deactivate} disabled={saving}>Désactiver</button>
              )}
              <button type="submit" className="primary-button" disabled={saving}>
                {saving ? "Enregistrement…" : creating ? "Créer" : "Enregistrer"}
              </button>
            </div>
          </form>
        )}
      </div>
    </section>
  );
}
