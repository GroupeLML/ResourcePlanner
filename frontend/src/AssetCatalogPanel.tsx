import { FormEvent, useEffect, useMemo, useState } from "react";

import { ApiError, CompetencyReadModel } from "./api";
import {
  AssetCatalog,
  AssetCatalogItem,
  AssetPlanningState,
  AssetTypeCatalogItem,
  addAssetUnavailability,
  createAsset,
  createAssetType,
  getAssetCatalog,
  getAssetPlanningState,
  removeAssetUnavailability,
  setAssetActive,
  setAssetApprover,
  setAssetTypeActive,
  setAssetTypeQualification,
  updateAsset,
  updateAssetType,
} from "./assetApi";
import { useAuth } from "./AuthContext";
import CompetencyPicker from "./CompetencyPicker";

const CATEGORIES = [
  ["VEHICLE", "Véhicule"],
  ["EQUIPMENT", "Équipement"],
  ["TOOL", "Outil"],
  ["WORKCENTER", "Poste de travail"],
] as const;

const STALE_CODES = new Set(["planning_version_conflict", "planning_version_stale"]);

type TypeDraft = {
  code: string;
  label: string;
  category: string;
  competency_ids: string[];
};

type AssetDraft = {
  code: string;
  label: string;
  asset_type_id: string;
};

function typeDraft(row?: AssetTypeCatalogItem | null): TypeDraft {
  return {
    code: row?.code ?? "",
    label: row?.label ?? "",
    category: row?.category ?? "EQUIPMENT",
    competency_ids: row?.required_competencies.map((item) => item.id) ?? [],
  };
}

function assetDraft(row?: AssetCatalogItem | null, typeId = ""): AssetDraft {
  return {
    code: row?.code ?? "",
    label: row?.label ?? "",
    asset_type_id: row?.asset_type_id ?? typeId,
  };
}

function messageFromError(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return reason.message + (reason.code ? " (" + reason.code + ")" : "");
  }
  return reason instanceof Error ? reason.message : fallback;
}

function sameIds(left: string[], right: string[]) {
  return [...left].sort().join("|") === [...right].sort().join("|");
}

export default function AssetCatalogPanel({
  competencies,
}: {
  competencies: CompetencyReadModel[];
}) {
  const { can } = useAuth();
  const canManagePlanning = can("manage_planning");
  const canManageResources = can("manage_resources");
  const canAdminSettings = can("admin_settings");
  const [catalog, setCatalog] = useState<AssetCatalog | null>(null);
  const [planningState, setPlanningState] = useState<AssetPlanningState | null>(null);
  const [selectedTypeId, setSelectedTypeId] = useState<string | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [creatingType, setCreatingType] = useState(false);
  const [creatingAsset, setCreatingAsset] = useState(false);
  const [typeForm, setTypeForm] = useState<TypeDraft>(() => typeDraft());
  const [assetForm, setAssetForm] = useState<AssetDraft>(() => assetDraft());
  const [unavailableStart, setUnavailableStart] = useState("");
  const [unavailableEnd, setUnavailableEnd] = useState("");
  const [unavailableReason, setUnavailableReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "info" | "success" | "error"; text: string } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getAssetCatalog(controller.signal),
      getAssetPlanningState(controller.signal),
    ])
      .then(([nextCatalog, nextState]) => {
        setCatalog(nextCatalog);
        setPlanningState(nextState);
        setSelectedTypeId((current) => {
          if (current && nextCatalog.types.some((row) => row.id === current)) return current;
          return nextCatalog.types.find((row) => row.active)?.id ?? nextCatalog.types[0]?.id ?? null;
        });
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(messageFromError(reason, "Impossible de charger le catalogue des actifs."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refreshKey]);

  const selectedType = useMemo(
    () => catalog?.types.find((row) => row.id === selectedTypeId) ?? null,
    [catalog, selectedTypeId],
  );
  const typeAssets = useMemo(
    () => (catalog?.assets ?? [])
      .filter((row) => row.asset_type_id === selectedTypeId)
      .sort((left, right) => left.code.localeCompare(right.code, "fr-CA")),
    [catalog, selectedTypeId],
  );
  const selectedAsset = useMemo(
    () => typeAssets.find((row) => row.id === selectedAssetId) ?? null,
    [typeAssets, selectedAssetId],
  );
  const staleApproverIds = useMemo(() => {
    if (!catalog || !selectedAsset) return [];
    const candidates = new Set(catalog.approver_candidates.map((row) => row.id));
    return selectedAsset.approver_user_ids.filter((id) => !candidates.has(id));
  }, [catalog, selectedAsset]);

  const selectedUnavailability = useMemo(
    () => (planningState?.unavailability ?? [])
      .filter((row) => row.asset_id === selectedAssetId)
      .sort((left, right) => left.start_date.localeCompare(right.start_date)),
    [planningState, selectedAssetId],
  );

  useEffect(() => {
    if (creatingType) return;
    setTypeForm(typeDraft(selectedType));
    setCreatingAsset(false);
    setSelectedAssetId((current) => {
      if (current && typeAssets.some((row) => row.id === current)) return current;
      return typeAssets.find((row) => row.active)?.id ?? typeAssets[0]?.id ?? null;
    });
  }, [selectedType, typeAssets, creatingType]);

  useEffect(() => {
    if (creatingAsset) return;
    setAssetForm(assetDraft(selectedAsset, selectedTypeId ?? ""));
  }, [selectedAsset, selectedTypeId, creatingAsset]);

  function refresh() {
    setRefreshKey((value) => value + 1);
  }

  function handleMutationFailure(reason: unknown, fallback: string) {
    if (reason instanceof ApiError && reason.code && STALE_CODES.has(reason.code)) {
      setNotice({
        tone: "info",
        text: "Le planning a changé depuis le chargement du catalogue. Les données sont actualisées; vérifie puis réessaie.",
      });
      refresh();
      return;
    }
    setNotice({ tone: "error", text: messageFromError(reason, fallback) });
  }

  function selectType(id: string) {
    setCreatingType(false);
    setSelectedTypeId(id);
    setCreatingAsset(false);
    setSelectedAssetId(null);
    setNotice(null);
  }

  function startType() {
    setCreatingType(true);
    setCreatingAsset(false);
    setSelectedAssetId(null);
    setTypeForm(typeDraft());
    setNotice(null);
  }

  async function saveType(event: FormEvent) {
    event.preventDefault();
    if (!catalog || pending) return;
    const code = typeForm.code.trim();
    const label = typeForm.label.trim();
    if (!code || !label) {
      setNotice({ tone: "error", text: "Le code et le libellé du type sont requis." });
      return;
    }
    setPending("type");
    setNotice(null);
    try {
      if (creatingType) {
        const created = await createAssetType({
          code,
          label,
          category: typeForm.category,
        });
        if (typeForm.competency_ids.length > 0) {
          await setAssetTypeQualification(created.id, {
            competency_ids: typeForm.competency_ids,
            qualification_policy: "ANY_ASSIGNED_WORKFORCE",
            expected_planning_version: catalog.planning_version,
          });
        }
        setSelectedTypeId(created.id);
        setCreatingType(false);
        setNotice({ tone: "success", text: "Type d’actif créé." });
      } else if (selectedType) {
        let version = catalog.planning_version;
        const fieldsChanged = (
          code !== selectedType.code
          || label !== selectedType.label
          || typeForm.category !== selectedType.category
        );
        if (fieldsChanged) {
          const updated = await updateAssetType(selectedType.id, {
            code,
            label,
            category: typeForm.category,
            expected_planning_version: version,
          });
          version = updated.planning_version;
        }
        const oldCompetencies = selectedType.required_competencies.map((item) => item.id);
        if (!sameIds(typeForm.competency_ids, oldCompetencies)) {
          await setAssetTypeQualification(selectedType.id, {
            competency_ids: typeForm.competency_ids,
            qualification_policy: selectedType.qualification_policy || "ANY_ASSIGNED_WORKFORCE",
            expected_planning_version: version,
          });
        }
        setNotice({
          tone: "success",
          text: fieldsChanged || !sameIds(typeForm.competency_ids, oldCompetencies)
            ? "Type d’actif enregistré."
            : "Aucune modification à enregistrer.",
        });
      }
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible d’enregistrer le type d’actif.");
    } finally {
      setPending(null);
    }
  }

  async function toggleType() {
    if (!catalog || !selectedType || pending) return;
    setPending("type-active");
    setNotice(null);
    try {
      await setAssetTypeActive(selectedType.id, !selectedType.active, catalog.planning_version);
      setNotice({
        tone: "success",
        text: selectedType.active ? "Type d’actif désactivé." : "Type d’actif réactivé.",
      });
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible de modifier l’état du type d’actif.");
    } finally {
      setPending(null);
    }
  }

  function startAsset() {
    if (!selectedType) return;
    setCreatingAsset(true);
    setSelectedAssetId(null);
    setAssetForm(assetDraft(null, selectedType.id));
    setNotice(null);
  }

  async function saveAsset(event: FormEvent) {
    event.preventDefault();
    if (!catalog || pending) return;
    const code = assetForm.code.trim();
    const label = assetForm.label.trim();
    if (!code || !label || !assetForm.asset_type_id) {
      setNotice({ tone: "error", text: "Le code, le libellé et le type de l’unité sont requis." });
      return;
    }
    setPending("asset");
    setNotice(null);
    try {
      if (creatingAsset) {
        const created = await createAsset({
          code,
          label,
          asset_type_id: assetForm.asset_type_id,
        });
        setSelectedTypeId(assetForm.asset_type_id);
        setSelectedAssetId(created.id);
        setCreatingAsset(false);
        setNotice({ tone: "success", text: "Unité physique créée." });
      } else if (selectedAsset) {
        const changed = (
          code !== selectedAsset.code
          || label !== selectedAsset.label
          || assetForm.asset_type_id !== selectedAsset.asset_type_id
        );
        if (changed) {
          await updateAsset(selectedAsset.id, {
            code,
            label,
            asset_type_id: assetForm.asset_type_id,
            expected_planning_version: catalog.planning_version,
          });
          setSelectedTypeId(assetForm.asset_type_id);
        }
        setNotice({
          tone: "success",
          text: changed ? "Unité physique enregistrée." : "Aucune modification à enregistrer.",
        });
      }
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible d’enregistrer l’unité physique.");
    } finally {
      setPending(null);
    }
  }

  async function toggleAsset() {
    if (!catalog || !selectedAsset || pending) return;
    setPending("asset-active");
    setNotice(null);
    try {
      await setAssetActive(selectedAsset.id, !selectedAsset.active, catalog.planning_version);
      setNotice({
        tone: "success",
        text: selectedAsset.active ? "Unité désactivée." : "Unité réactivée.",
      });
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible de modifier l’état de l’unité.");
    } finally {
      setPending(null);
    }
  }

  async function toggleAssetApprover(userId: string, assigned: boolean) {
    if (!selectedAsset || pending || !canAdminSettings) return;
    setPending("asset-approver");
    setNotice(null);
    try {
      await setAssetApprover(selectedAsset.id, userId, assigned);
      setNotice({
        tone: "success",
        text: assigned ? "Approbateur spécifique ajouté." : "Approbateur spécifique retiré.",
      });
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible de modifier les approbateurs de l’unité.");
    } finally {
      setPending(null);
    }
  }

  async function addUnavailability(event: FormEvent) {
    event.preventDefault();
    if (!catalog || !selectedAsset || pending || !canManagePlanning) return;
    if (!unavailableStart || !unavailableEnd) {
      setNotice({ tone: "error", text: "Les dates de début et de fin sont requises." });
      return;
    }
    setPending("unavailability");
    setNotice(null);
    try {
      await addAssetUnavailability(selectedAsset.id, {
        start_date: unavailableStart,
        end_date: unavailableEnd,
        reason: unavailableReason.trim() || null,
        expected_planning_version: catalog.planning_version,
      });
      setUnavailableReason("");
      setNotice({ tone: "success", text: "Indisponibilité ajoutée." });
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible d’ajouter l’indisponibilité.");
    } finally {
      setPending(null);
    }
  }

  async function removeUnavailability(id: string) {
    if (!catalog || !selectedAsset || pending || !canManagePlanning) return;
    setPending("unavailability");
    setNotice(null);
    try {
      await removeAssetUnavailability(
        selectedAsset.id,
        id,
        catalog.planning_version,
      );
      setNotice({ tone: "success", text: "Indisponibilité retirée." });
      refresh();
    } catch (reason: unknown) {
      handleMutationFailure(reason, "Impossible de retirer l’indisponibilité.");
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="admin-card asset-catalog-card">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Actifs réservables</span>
          <h2>Catalogue des actifs</h2>
          <p>Types et unités physiques restent distincts des ressources humaines. Les réservations demeurent autoritaires côté Planning.</p>
        </div>
        <button className="secondary-button" type="button" onClick={startType} disabled={Boolean(pending)}>
          + Type d’actif
        </button>
      </div>

      {loading && <div className="subtle-status">Actualisation du catalogue…</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}
      {notice && (
        <div className={"asset-catalog-notice is-" + notice.tone} aria-live="polite">
          {notice.text}
        </div>
      )}

      {!loading && !creatingType && (catalog?.types.length ?? 0) === 0 ? (
        <div className="empty-admin-state">Aucun type d’actif. Crée le premier type pour ajouter des unités physiques.</div>
      ) : (
        <div className="asset-catalog-layout">
          <aside className="asset-type-list" aria-label="Types d’actifs">
            {(catalog?.types ?? []).map((row) => (
              <button
                type="button"
                key={row.id}
                className={"asset-type-row " + (selectedTypeId === row.id && !creatingType ? "selected " : "") + (row.active ? "" : "inactive")}
                onClick={() => selectType(row.id)}
              >
                <span>
                  <strong>{row.label}</strong>
                  <small>{row.code} · {row.category}</small>
                </span>
                <span>{row.active ? "Actif" : "Inactif"}</span>
              </button>
            ))}
          </aside>

          <div className="asset-catalog-detail">
            {(creatingType || selectedType) && (
              <form className="asset-type-editor" onSubmit={saveType}>
                <div className="editor-heading">
                  <div>
                    <span className="eyebrow">Type</span>
                    <h3>{creatingType ? "Nouveau type d’actif" : selectedType?.label}</h3>
                  </div>
                  {!creatingType && selectedType && (
                    <span className={"status-chip " + (selectedType.active ? "active" : "inactive")}>
                      {selectedType.active ? "Actif" : "Inactif"}
                    </span>
                  )}
                </div>
                <div className="form-grid two-columns">
                  <label>
                    Code
                    <input value={typeForm.code} onChange={(event) => setTypeForm((current) => ({ ...current, code: event.target.value }))} required />
                  </label>
                  <label>
                    Libellé
                    <input value={typeForm.label} onChange={(event) => setTypeForm((current) => ({ ...current, label: event.target.value }))} required />
                  </label>
                  <label>
                    Catégorie
                    <select value={typeForm.category} onChange={(event) => setTypeForm((current) => ({ ...current, category: event.target.value }))}>
                      {CATEGORIES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                    </select>
                  </label>
                  {!creatingType && selectedType && (
                    <label>
                      Politique d’occupation
                      <input value={selectedType.occupancy_policy} readOnly />
                    </label>
                  )}
                </div>
                <CompetencyPicker
                  competencies={competencies}
                  selectedIds={typeForm.competency_ids}
                  onChange={(ids) => setTypeForm((current) => ({ ...current, competency_ids: ids }))}
                  disabled={Boolean(pending)}
                  label="Compétences / permis requis"
                  placeholder="Rechercher une compétence ou classe de permis…"
                />
                <div className="editor-actions split-actions">
                  {!creatingType && selectedType && (
                    <button
                      className={selectedType.active ? "danger-button" : "quiet-button"}
                      type="button"
                      disabled={Boolean(pending)}
                      onClick={() => void toggleType()}
                    >
                      {selectedType.active ? "Désactiver le type" : "Réactiver le type"}
                    </button>
                  )}
                  <span />
                  {creatingType && (
                    <button className="quiet-button" type="button" onClick={() => setCreatingType(false)} disabled={Boolean(pending)}>
                      Annuler
                    </button>
                  )}
                  <button className="primary-button" type="submit" disabled={Boolean(pending)}>
                    {pending === "type" ? "Enregistrement…" : "Enregistrer le type"}
                  </button>
                </div>
              </form>
            )}

            {!creatingType && selectedType && (
              <section className="asset-unit-section">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">Unités physiques</span>
                    <h3>{selectedType.label}</h3>
                    <p>Chaque unité est réservée individuellement; aucune unité n’est créée comme ressource humaine.</p>
                  </div>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={startAsset}
                    disabled={Boolean(pending) || !selectedType.active}
                  >
                    + Unité
                  </button>
                </div>

                <div className="asset-unit-layout">
                  <div className="asset-unit-list">
                    {typeAssets.length === 0 && !creatingAsset && (
                      <div className="empty-admin-state">Aucune unité physique pour ce type.</div>
                    )}
                    {typeAssets.map((row) => (
                      <button
                        type="button"
                        key={row.id}
                        className={"asset-unit-row " + (selectedAssetId === row.id && !creatingAsset ? "selected " : "") + (row.active ? "" : "inactive")}
                        onClick={() => {
                          setCreatingAsset(false);
                          setSelectedAssetId(row.id);
                          setNotice(null);
                        }}
                      >
                        <span>
                          <strong>{row.label}</strong>
                          <small>{row.code}</small>
                        </span>
                        <span>{row.active ? "Actif" : "Inactif"}</span>
                      </button>
                    ))}
                  </div>

                  {(creatingAsset || selectedAsset) && (
                    <form className="asset-unit-editor" onSubmit={saveAsset}>
                      <div className="editor-heading">
                        <div>
                          <span className="eyebrow">Unité</span>
                          <h3>{creatingAsset ? "Nouvelle unité physique" : selectedAsset?.label}</h3>
                        </div>
                        {!creatingAsset && selectedAsset && (
                          <span className={"status-chip " + (selectedAsset.active ? "active" : "inactive")}>
                            {selectedAsset.active ? "Active" : "Inactive"}
                          </span>
                        )}
                      </div>
                      <div className="form-grid two-columns">
                        <label>
                          Code
                          <input value={assetForm.code} onChange={(event) => setAssetForm((current) => ({ ...current, code: event.target.value }))} required />
                        </label>
                        <label>
                          Libellé
                          <input value={assetForm.label} onChange={(event) => setAssetForm((current) => ({ ...current, label: event.target.value }))} required />
                        </label>
                        <label>
                          Type
                          <select
                            value={assetForm.asset_type_id}
                            disabled={Boolean(pending) || (!creatingAsset && !canAdminSettings)}
                            onChange={(event) => setAssetForm((current) => ({ ...current, asset_type_id: event.target.value }))}
                          >
                            {(catalog?.types ?? []).filter((row) => row.active || row.id === assetForm.asset_type_id).map((row) => (
                              <option value={row.id} key={row.id}>{row.code} — {row.label}{row.active ? "" : " — inactif"}</option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <div className="editor-actions split-actions">
                        {!creatingAsset && selectedAsset && (
                          <button
                            className={selectedAsset.active ? "danger-button" : "quiet-button"}
                            type="button"
                            disabled={Boolean(pending)}
                            onClick={() => void toggleAsset()}
                          >
                            {selectedAsset.active ? "Désactiver l’unité" : "Réactiver l’unité"}
                          </button>
                        )}
                        <span />
                        {creatingAsset && (
                          <button className="quiet-button" type="button" onClick={() => setCreatingAsset(false)} disabled={Boolean(pending)}>
                            Annuler
                          </button>
                        )}
                        <button className="primary-button" type="submit" disabled={Boolean(pending)}>
                          {pending === "asset" ? "Enregistrement…" : "Enregistrer l’unité"}
                        </button>
                      </div>
                    </form>
                  )}
                </div>

                {!creatingAsset && selectedAsset && (
                  <div className="asset-unavailability-section" data-testid="asset-approvers-editor">
                    <div className="editor-heading">
                      <div>
                        <span className="eyebrow">Approbation</span>
                        <h3>Approbateurs spécifiques de {selectedAsset.label}</h3>
                        <small>Ces utilisateurs sont additifs au périmètre résolu depuis le type d’actif.</small>
                      </div>
                    </div>
                    {!canAdminSettings && (
                      <div className="subtle-status">La permission admin_settings est requise pour modifier les approbateurs.</div>
                    )}
                    <div className="approval-scope-approvers">
                      {(catalog?.approver_candidates ?? []).map((user) => (
                        <label key={user.id}>
                          <input
                            type="checkbox"
                            checked={selectedAsset.approver_user_ids.includes(user.id)}
                            disabled={Boolean(pending) || !canAdminSettings}
                            onChange={(event) => void toggleAssetApprover(user.id, event.target.checked)}
                          />
                          {user.display_name}
                          <small>{user.id}</small>
                        </label>
                      ))}
                      {staleApproverIds.map((userId) => (
                        <label key={userId}>
                          <input
                            type="checkbox"
                            checked
                            disabled={Boolean(pending) || !canAdminSettings}
                            onChange={(event) => void toggleAssetApprover(userId, event.target.checked)}
                          />
                          {userId}
                          <small>Association non admissible — retirer pour nettoyer le référentiel.</small>
                        </label>
                      ))}
                      {(catalog?.approver_candidates.length ?? 0) === 0 && staleApproverIds.length === 0 && (
                        <div className="empty-admin-state">Aucun AppUser actif avec approve_demands.</div>
                      )}
                    </div>
                  </div>
                )}

                {!creatingAsset && selectedAsset && (
                  <div className="asset-unavailability-section">
                    <div className="editor-heading">
                      <div>
                        <span className="eyebrow">Disponibilité</span>
                        <h3>Indisponibilités de {selectedAsset.label}</h3>
                      </div>
                    </div>
                    {!canManagePlanning && (
                      <div className="subtle-status">La consultation est permise, mais la permission manage_planning est requise pour modifier les indisponibilités.</div>
                    )}
                    <form className="asset-catalog-unavailability-form" onSubmit={addUnavailability}>
                      <label>
                        Début
                        <input type="date" value={unavailableStart} onChange={(event) => setUnavailableStart(event.target.value)} required />
                      </label>
                      <label>
                        Fin
                        <input type="date" value={unavailableEnd} onChange={(event) => setUnavailableEnd(event.target.value)} required />
                      </label>
                      <label>
                        Raison
                        <input value={unavailableReason} onChange={(event) => setUnavailableReason(event.target.value)} placeholder="Entretien, inspection…" />
                      </label>
                      <button className="secondary-button" type="submit" disabled={Boolean(pending) || !canManagePlanning}>
                        Ajouter l’indisponibilité
                      </button>
                    </form>
                    <div className="asset-catalog-unavailability-list">
                      {selectedUnavailability.length === 0 && (
                        <div className="empty-admin-state">Aucune indisponibilité enregistrée pour cette unité.</div>
                      )}
                      {selectedUnavailability.map((row) => (
                        <article key={row.id}>
                          <div>
                            <strong>{row.start_date} → {row.end_date}</strong>
                            <small>{row.reason || "Indisponible"}</small>
                          </div>
                          <button
                            type="button"
                            className="quiet-button"
                            disabled={Boolean(pending) || !canManagePlanning}
                            onClick={() => void removeUnavailability(row.id)}
                          >
                            Retirer
                          </button>
                        </article>
                      ))}
                    </div>
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
