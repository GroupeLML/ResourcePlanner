import { useMemo } from "react";

import {
  CompetencyReadModel,
  DemandLineReadModel,
  DemandLineWrite,
  ResourceReadModel,
  TaskCatalogItemReadModel,
  WorkPackageReadModel,
} from "./api";
import CompetencyPicker from "./CompetencyPicker";
import { AssetCatalogItem, AssetTypeCatalogItem } from "./assetApi";
import { ResourceClassOptionReadModel } from "./resourceClassesApi";

export type DemandLineDraft = {
  key: string;
  id?: string;
  kind: "WORKFORCE" | "ASSET";
  required_resource_class: string;
  required_competency_ids: string[];
  desired_start: string;
  desired_end: string;
  desired_active_days: string;
  estimated_hours: string;
  work_package_ref: string;
  task_code: string;
  proposed_resource_id: string;
  asset_type_id: string;
  proposed_asset_id: string;
  confirmation: "Tentative" | "Confirmée";
  description: string;
  estimated_hours_source?: string | null;
};

export type DemandLineDefaults = Omit<DemandLineDraft, "key" | "id" | "estimated_hours_source">;

function draftKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `request-line-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function optionalNumber(value: string) {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized.replace(",", "."));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

export function newDemandLine(defaults: DemandLineDefaults): DemandLineDraft {
  return { key: draftKey(), ...defaults };
}

export function demandLineDraftFromReadModel(line: DemandLineReadModel): DemandLineDraft {
  return {
    key: line.line_id || draftKey(),
    id: line.line_id || undefined,
    kind: line.kind === "ASSET" ? "ASSET" : "WORKFORCE",
    required_resource_class: line.required_resource_class ?? "",
    required_competency_ids: line.required_competency_ids ?? [],
    desired_start: line.desired_start ?? "",
    desired_end: line.desired_end ?? "",
    desired_active_days: line.desired_active_days == null ? "" : String(line.desired_active_days),
    estimated_hours: line.estimated_hours_source === "DEFAULT_8H"
      ? ""
      : line.estimated_hours == null ? "" : String(line.estimated_hours),
    work_package_ref: line.work_package_ref ?? "",
    task_code: line.task_code ?? "",
    proposed_resource_id: line.proposed_resource_id ?? "",
    asset_type_id: line.asset_type_id ?? "",
    proposed_asset_id: line.proposed_asset_id ?? "",
    confirmation: line.confirmation === "Tentative" ? "Tentative" : "Confirmée",
    description: line.description ?? "",
    estimated_hours_source: line.estimated_hours_source,
  };
}

export function demandLineWrite(line: DemandLineDraft, position: number): DemandLineWrite {
  const activeDays = optionalNumber(line.desired_active_days);
  const hours = optionalNumber(line.estimated_hours);
  const shared = {
    ...(line.id ? { id: line.id } : {}),
    position,
    kind: line.kind,
    desired_start: line.desired_start || null,
    desired_end: line.desired_end || null,
    estimated_hours: hours == null || Number.isNaN(hours) ? null : hours,
    work_package_ref: line.work_package_ref || null,
    task_code: line.task_code || null,
    confirmation: line.confirmation,
    description: line.description.trim() || null,
  } satisfies Partial<DemandLineWrite>;

  if (line.kind === "ASSET") {
    return {
      ...shared,
      kind: "ASSET",
      required_resource_class: null,
      required_competency_ids: [],
      desired_active_days: null,
      proposed_resource_id: null,
      asset_type_id: line.asset_type_id || null,
      proposed_asset_id: line.proposed_asset_id || null,
    } as DemandLineWrite;
  }

  return {
    ...shared,
    kind: "WORKFORCE",
    required_resource_class: line.required_resource_class.trim() || null,
    required_competency_ids: line.required_competency_ids,
    desired_active_days: activeDays == null || Number.isNaN(activeDays) ? null : activeDays,
    proposed_resource_id: line.proposed_resource_id || null,
    asset_type_id: null,
    proposed_asset_id: null,
  } as DemandLineWrite;
}

export function lineValidationMessage(line: DemandLineDraft, position: number): string | null {
  const number = position + 1;
  if (!line.desired_start) return `Ligne ${number} : la date de début est requise.`;
  if (line.desired_end && line.desired_end < line.desired_start) {
    return `Ligne ${number} : la date de fin ne peut pas précéder la date de début.`;
  }

  const hours = optionalNumber(line.estimated_hours);
  if (Number.isNaN(hours)) {
    return `Ligne ${number} : les heures doivent être numériques.`;
  }
  if (hours != null && hours <= 0) {
    return `Ligne ${number} : les heures doivent être supérieures à zéro.`;
  }

  if (line.kind === "ASSET") {
    if (!line.asset_type_id) {
      return `Ligne ${number} : sélectionne un type d’actif.`;
    }
    return null;
  }

  const days = optionalNumber(line.desired_active_days);
  if (Number.isNaN(days)) {
    return `Ligne ${number} : les jours doivent être numériques.`;
  }
  if (days != null && (!Number.isInteger(days) || days < 1)) {
    return `Ligne ${number} : les jours actifs doivent être un entier supérieur ou égal à 1.`;
  }
  if (days == null && hours == null) {
    return `Ligne ${number} : indique les heures ou le nombre de jours actifs.`;
  }
  if (days != null) {
    const end = line.desired_end || line.desired_start;
    const available = Math.floor((Date.parse(end) - Date.parse(line.desired_start)) / 86_400_000) + 1;
    if (days > available) {
      return `Ligne ${number} : la cible de ${days} jours actifs dépasse les ${available} dates de la fenêtre.`;
    }
  }
  return null;
}

function lineProjectedHours(line: DemandLineDraft): number {
  if (line.kind === "ASSET") return 0;
  const hours = optionalNumber(line.estimated_hours);
  if (hours != null && !Number.isNaN(hours)) return hours;
  const days = optionalNumber(line.desired_active_days);
  if (days != null && !Number.isNaN(days)) return days * 8;
  return 0;
}

function lineProjectedDays(line: DemandLineDraft): number {
  if (line.kind === "ASSET") return 0;
  const days = optionalNumber(line.desired_active_days);
  return days != null && !Number.isNaN(days) ? days : 0;
}

export default function DemandLinesEditor({
  lines,
  onChange,
  defaults,
  generationCount,
  onGenerationCountChange,
  competencies,
  resourceClasses,
  resources,
  workPackages,
  tasks,
  assetTypes,
  assets,
  disabled = false,
}: {
  lines: DemandLineDraft[];
  onChange: (lines: DemandLineDraft[]) => void;
  defaults: DemandLineDefaults;
  generationCount: string;
  onGenerationCountChange: (value: string) => void;
  competencies: CompetencyReadModel[];
  resourceClasses: ResourceClassOptionReadModel[];
  resources: ResourceReadModel[];
  workPackages: WorkPackageReadModel[];
  tasks: TaskCatalogItemReadModel[];
  assetTypes: AssetTypeCatalogItem[];
  assets: AssetCatalogItem[];
  disabled?: boolean;
}) {
  const activeResourceClasses = useMemo(
    () => resourceClasses
      .filter((row) => row.active)
      .sort((left, right) => left.label.localeCompare(right.label, "fr-CA")),
    [resourceClasses],
  );
  const totalHours = useMemo(
    () => lines.reduce((sum, line) => sum + lineProjectedHours(line), 0),
    [lines],
  );
  const totalDays = useMemo(
    () => lines.reduce((sum, line) => sum + lineProjectedDays(line), 0),
    [lines],
  );
  const workforceCount = lines.filter((line) => line.kind === "WORKFORCE").length;
  const assetCount = lines.length - workforceCount;

  function updateLine(index: number, patch: Partial<DemandLineDraft>) {
    onChange(lines.map((line, lineIndex) => (
      lineIndex === index ? { ...line, ...patch } : line
    )));
  }

  function addLine() {
    onChange([...lines, newDemandLine(defaults)]);
  }

  function duplicateLine(index: number) {
    const source = lines[index];
    onChange([
      ...lines.slice(0, index + 1),
      { ...source, key: draftKey(), id: undefined, estimated_hours_source: undefined },
      ...lines.slice(index + 1),
    ]);
  }

  function removeLine(index: number) {
    if (lines.length <= 1) return;
    onChange(lines.filter((_line, lineIndex) => lineIndex !== index));
  }

  function generateLines() {
    const count = Number(generationCount);
    if (!Number.isInteger(count) || count < 1) return;
    const template = lines[0] ?? newDemandLine(defaults);
    onChange(Array.from({ length: count }, () => ({
      ...template,
      key: draftKey(),
      id: undefined,
      estimated_hours_source: undefined,
    })));
  }

  return (
    <section className="request-lines-editor" aria-label="Lignes planifiables">
      <div className="request-lines-heading">
        <div>
          <span className="eyebrow">Besoins planifiables</span>
          <h3>Lignes planifiables</h3>
          <p>Chaque ligne représente soit un besoin de main-d’œuvre, soit un actif physique. Les actifs utilisent une occupation par unité/jour et ne sont jamais convertis en fausses heures.</p>
        </div>
        <div className="request-lines-actions">
          <label>
            <span>Quantité</span>
            <input
              type="number"
              min="1"
              step="1"
              value={generationCount}
              onChange={(event) => onGenerationCountChange(event.target.value)}
              disabled={disabled}
              aria-label="Quantité de lignes à générer"
            />
          </label>
          <button type="button" className="secondary-button" onClick={generateLines} disabled={disabled}>
            Générer les lignes
          </button>
          <button type="button" className="secondary-button" onClick={addLine} disabled={disabled}>
            + Ajouter une ligne
          </button>
        </div>
      </div>

      <div className="request-lines-summary" aria-label="Récapitulatif des lignes">
        <div><strong>{workforceCount}</strong><span>ligne(s) main-d’œuvre</span></div>
        <div><strong>{assetCount}</strong><span>ligne(s) actif</span></div>
        <div><strong>{Number(totalHours.toFixed(2))}</strong><span>heure(s) humaines projetées</span></div>
        <small>{totalDays} jour(s) actif(s) humain(s). Les heures projetées utilisent 8 h/j uniquement pour l’aperçu humain; le backend demeure autoritaire sur la valeur persistée. Un éventuel budget d’usage d’un actif reste distinct des heures de main-d’œuvre.</small>
      </div>

      <div className="request-lines-grid">
        {lines.map((line, index) => (
          <article className="request-line-card" data-line-index={index} key={line.key}>
            <div className="request-line-title">
              <div>
                <strong>Ligne {index + 1}</strong>
                {line.id && <small>ID {line.id}</small>}
              </div>
              <div className="request-line-row-actions">
                <button type="button" className="text-button" onClick={() => duplicateLine(index)} disabled={disabled}>
                  Dupliquer
                </button>
                <button type="button" className="text-button danger" onClick={() => removeLine(index)} disabled={disabled || lines.length <= 1}>
                  Retirer
                </button>
              </div>
            </div>

            {lineValidationMessage(line, index) && (
              <div className="request-line-validation" role="status">
                {lineValidationMessage(line, index)}
              </div>
            )}

            <div className="request-line-fields">
              <label>
                <span>Type de besoin</span>
                <select
                  value={line.kind}
                  onChange={(event) => {
                    const kind = event.target.value === "ASSET" ? "ASSET" : "WORKFORCE";
                    updateLine(index, kind === "ASSET" ? {
                      kind,
                      required_resource_class: "",
                      required_competency_ids: [],
                      desired_active_days: "",
                      proposed_resource_id: "",
                    } : {
                      kind,
                      asset_type_id: "",
                      proposed_asset_id: "",
                    });
                  }}
                  disabled={disabled}
                  aria-label={`Type de besoin — ligne ${index + 1}`}
                >
                  <option value="WORKFORCE">Main-d’œuvre</option>
                  <option value="ASSET">Actif physique</option>
                </select>
              </label>

              <label>
                <span>WorkPackage</span>
                <select
                  value={line.work_package_ref}
                  onChange={(event) => updateLine(index, { work_package_ref: event.target.value })}
                  disabled={disabled}
                >
                  <option value="">Aucun WorkPackage</option>
                  {line.work_package_ref && !workPackages.some((row) => row.reference === line.work_package_ref) && (
                    <option value={line.work_package_ref}>{line.work_package_ref} — historique</option>
                  )}
                  {workPackages.map((item) => (
                    <option value={item.reference} key={item.id}>
                      {item.code ? `${item.code} — ` : ""}{item.name}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                <span>Tâche ERP</span>
                <select
                  value={line.task_code}
                  onChange={(event) => updateLine(index, { task_code: event.target.value })}
                  disabled={disabled}
                >
                  <option value="">Aucune tâche</option>
                  {line.task_code && !tasks.some((row) => row.code === line.task_code) && (
                    <option value={line.task_code}>{line.task_code} — historique</option>
                  )}
                  {tasks.filter((row) => row.active || row.code === line.task_code).map((task) => (
                    <option value={task.code} key={`${task.project_number}:${task.code}`}>
                      {task.code} — {task.label}
                    </option>
                  ))}
                </select>
              </label>

              <div className="request-line-date-group" data-testid={`request-line-dates-${index}`}>
                <label>
                  <span>Début</span>
                  <input
                    type="date"
                    value={line.desired_start}
                    onChange={(event) => updateLine(index, { desired_start: event.target.value })}
                    disabled={disabled}
                  />
                </label>

                <label>
                  <span>Fin</span>
                  <input
                    type="date"
                    min={line.desired_start || undefined}
                    value={line.desired_end}
                    onChange={(event) => updateLine(index, { desired_end: event.target.value })}
                    disabled={disabled}
                  />
                </label>
              </div>

              <label className="request-line-compact-field">
                <span>Confirmation</span>
                <select
                  value={line.confirmation}
                  onChange={(event) => updateLine(index, { confirmation: event.target.value === "Tentative" ? "Tentative" : "Confirmée" })}
                  disabled={disabled}
                >
                  <option value="Confirmée">Confirmée</option>
                  <option value="Tentative">Tentative</option>
                </select>
              </label>

              {line.kind === "ASSET" ? (
                <>
                  <label>
                    <span>Type d’actif</span>
                    <select
                      value={line.asset_type_id}
                      onChange={(event) => updateLine(index, {
                        asset_type_id: event.target.value,
                        proposed_asset_id: assets.some((asset) => (
                          asset.id === line.proposed_asset_id
                          && asset.asset_type_id === event.target.value
                        )) ? line.proposed_asset_id : "",
                      })}
                      disabled={disabled}
                      aria-label={`Type d’actif — ligne ${index + 1}`}
                    >
                      <option value="">Sélectionner un type…</option>
                      {line.asset_type_id && !assetTypes.some((row) => row.id === line.asset_type_id) && (
                        <option value={line.asset_type_id}>{line.asset_type_id} — historique</option>
                      )}
                      {assetTypes
                        .filter((row) => row.active || row.id === line.asset_type_id)
                        .map((row) => (
                          <option value={row.id} key={row.id}>{row.code} — {row.label}</option>
                        ))}
                    </select>
                  </label>

                  <label>
                    <span>Unité proposée</span>
                    <select
                      value={line.proposed_asset_id}
                      onChange={(event) => updateLine(index, { proposed_asset_id: event.target.value })}
                      disabled={disabled || !line.asset_type_id}
                      aria-label={`Unité proposée — ligne ${index + 1}`}
                    >
                      <option value="">Aucune — décision au planning</option>
                      {line.proposed_asset_id && !assets.some((row) => row.id === line.proposed_asset_id) && (
                        <option value={line.proposed_asset_id}>{line.proposed_asset_id} — historique</option>
                      )}
                      {assets
                        .filter((row) => (
                          row.asset_type_id === line.asset_type_id
                          && (row.active || row.id === line.proposed_asset_id)
                        ))
                        .map((row) => (
                          <option value={row.id} key={row.id}>{row.code} — {row.label}</option>
                        ))}
                    </select>
                    <small>Suggestion seulement : aucune unité n’est affectée automatiquement.</small>
                  </label>

                  <label>
                    <span>Budget d’usage (h, optionnel)</span>
                    <input
                      type="number"
                      min="0.25"
                      step="0.25"
                      value={line.estimated_hours}
                      onChange={(event) => updateLine(index, { estimated_hours: event.target.value, estimated_hours_source: event.target.value ? "EXPLICIT" : null })}
                      disabled={disabled}
                      placeholder="Aucun budget si vide"
                    />
                    <small>Ce budget d’usage reste distinct de la capacité humaine.</small>
                  </label>
                </>
              ) : (
                <>
                <label className="request-line-compact-field">
                  <span>Classe de ressource</span>
                  <select
                    value={line.required_resource_class}
                    onChange={(event) => updateLine(index, { required_resource_class: event.target.value })}
                    disabled={disabled}
                  >
                    <option value="">Aucune classe imposée</option>
                    {line.required_resource_class && !activeResourceClasses.some((row) => row.code === line.required_resource_class) && (
                      <option value={line.required_resource_class}>{line.required_resource_class} — historique/inactive</option>
                    )}
                    {activeResourceClasses.map((row) => (
                      <option value={row.code} key={row.code}>{row.code} · {row.label}</option>
                    ))}
                  </select>
                </label>
  
                <CompetencyPicker
                  competencies={competencies}
                  selectedIds={line.required_competency_ids}
                  onChange={(ids) => updateLine(index, { required_competency_ids: ids })}
                  disabled={disabled}
                  label={`Compétences requises — ligne ${index + 1}`}
                  placeholder="Rechercher une compétence…"
                />
  
                <label>
                  <span>Ressource proposée</span>
                  <select
                    value={line.proposed_resource_id}
                    onChange={(event) => updateLine(index, { proposed_resource_id: event.target.value })}
                    disabled={disabled}
                  >
                    <option value="">Aucune ressource proposée</option>
                    {line.proposed_resource_id && !resources.some((row) => row.id === line.proposed_resource_id) && (
                      <option value={line.proposed_resource_id}>{line.proposed_resource_id} — inactive/historique</option>
                    )}
                    {resources.map((resource) => (
                      <option value={resource.id} key={resource.id}>
                        {resource.name}{resource.resource_class ? ` — ${resource.resource_class}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
  
                <label>
                  <span>Jours actifs</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={line.desired_active_days}
                    onChange={(event) => updateLine(index, { desired_active_days: event.target.value })}
                    disabled={disabled}
                    placeholder="Ex. 3"
                  />
                </label>
  
                <label>
                  <span>Heures</span>
                  <input
                    type="number"
                    min="0.25"
                    step="0.25"
                    value={line.estimated_hours}
                    onChange={(event) => updateLine(index, { estimated_hours: event.target.value, estimated_hours_source: event.target.value ? "EXPLICIT" : null })}
                    disabled={disabled}
                    placeholder="8 h/j si vide"
                  />
                  <small>
                    {line.estimated_hours
                      ? "Heures explicites."
                      : "Vide : le backend applique et persiste 8 h par jour actif."}
                  </small>
                </label>
  
  
                </>
              )}

              <label className="span-2">
                <span>Description spécifique</span>
                <textarea
                  rows={2}
                  value={line.description}
                  onChange={(event) => updateLine(index, { description: event.target.value })}
                  disabled={disabled}
                  placeholder="Optionnel — précise seulement ce qui diffère du contexte de la demande."
                />
              </label>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
