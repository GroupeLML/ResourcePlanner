import { resourceDisplayName } from "./resourceLabels";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { createClientId } from "./clientId";
import { syncAcumaticaEmployees } from "./acumaticaIntegrationApi";
import {
  ApiError,
  AvailabilityRuleWrite,
  BusinessContactReadModel,
  CompetencyReadModel,
  ContactLinkReadModel,
  AvailabilityType,
  ResourceAvailabilityRuleReadModel,
  ResourceReadModel,
  ResourceWrite,
  createAvailabilityRule,
  createResource,
  deactivateAvailabilityRule,
  deactivateResource,
  getAvailabilityRules,
  getBusinessContacts,
  getCompetencies,
  getResourceBusinessContacts,
  getResources,
  reactivateResource,
  setResourceCoordinatorContact,
  updateAvailabilityRule,
  updateResource,
} from "./api";
import AssetCatalogPanel from "./AssetCatalogPanel";
import CompetencyCatalogPanel from "./CompetencyCatalogPanel";
import CompetencyPicker from "./CompetencyPicker";
import { ContactSelect } from "./BusinessContactUi";
import { ResourceClassOptionReadModel, getResourceClassOptions } from "./resourceClassesApi";

const WEEKDAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"] as const;
const DEFAULT_WEEKDAYS = "Lun,Mar,Mer,Jeu,Ven";

function mutationKey(prefix: string) {
  return `${prefix}-${createClientId()}`;
}

function apiMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function nullable(value: string) {
  const trimmed = value.trim();
  return trimmed || null;
}

function shortTime(value: string | null) {
  return value ? value.slice(0, 5) : "";
}

function resourceDraft(resource?: ResourceReadModel | null): ResourceWrite {
  return {
    name: resource?.name ?? "",
    email: resource?.email ?? null,
    resource_class: resource?.resource_class ?? null,
    competencies: resource?.competencies ?? null,
    competency_ids: resource?.competency_ids ?? [],
    note: resource?.note ?? null,
    active: resource?.active ?? true,
    sort_order: resource?.sort_order ?? 0,
    external_id: resource?.external_id ?? null,
  };
}

type RuleEditorProps = {
  resourceId: string | null;
  rule?: ResourceAvailabilityRuleReadModel | null;
  forcedType?: AvailabilityType;
  resourceClasses: ResourceClassOptionReadModel[];
  onSaved: () => void;
  onCancel: () => void;
};

function RuleEditor({ resourceId, rule, forcedType, resourceClasses, onSaved, onCancel }: RuleEditorProps) {
  const initialType = forcedType ?? rule?.availability_type ?? "Horaire standard";
  const [kind, setKind] = useState<AvailabilityType>(initialType);
  const [startDate, setStartDate] = useState(rule?.start_date ?? "");
  const [endDate, setEndDate] = useState(rule?.end_date ?? "");
  const [startTime, setStartTime] = useState(shortTime(rule?.start_time ?? null) || "07:00");
  const [endTime, setEndTime] = useState(shortTime(rule?.end_time ?? null) || "15:30");
  const [weekdays, setWeekdays] = useState(
    () => new Set((rule?.weekdays ?? DEFAULT_WEEKDAYS).split(",").map((item) => item.trim()).filter(Boolean)),
  );
  const [note, setNote] = useState(rule?.note ?? "");
  const [resourceClassCodes, setResourceClassCodes] = useState<string[]>(
    rule?.resource_class_codes ?? [],
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const idempotencyKey = useRef(mutationKey("availability"));

  const effectiveType = forcedType ?? kind;
  const isStandard = effectiveType === "Horaire standard";
  const isHoliday = effectiveType === "Jour férié";
  const holidayClassOptions = resourceClasses
    .filter((row) => row.active || resourceClassCodes.includes(row.code))
    .sort((left, right) => left.label.localeCompare(right.label, "fr-CA"));

  function toggleResourceClass(code: string) {
    setResourceClassCodes((current) => (
      current.includes(code)
        ? current.filter((item) => item !== code)
        : [...current, code]
    ));
  }

  function toggleWeekday(day: string) {
    setWeekdays((current) => {
      const next = new Set(current);
      if (next.has(day)) next.delete(day);
      else next.add(day);
      return next;
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setError(null);

    if (!isHoliday && !resourceId) {
      setError("Une ressource doit être sélectionnée.");
      return;
    }
    if ((effectiveType === "Vacances" || isHoliday) && !startDate) {
      setError("La date de début est requise.");
      return;
    }
    if (startDate && endDate && endDate < startDate) {
      setError("La date de fin ne peut pas précéder la date de début.");
      return;
    }
    if (isStandard && (!startTime || !endTime)) {
      setError("Les heures de début et de fin sont requises pour un horaire standard.");
      return;
    }

    const payload: AvailabilityRuleWrite = {
      availability_type: effectiveType,
      resource_id: isHoliday ? null : resourceId,
      start_date: startDate || null,
      end_date: endDate || null,
      weekdays: isStandard ? WEEKDAYS.filter((day) => weekdays.has(day)).join(",") || null : null,
      start_time: isStandard ? startTime : null,
      end_time: isStandard ? endTime : null,
      note: nullable(note),
      active: rule?.active ?? true,
      ...(isHoliday ? { resource_class_codes: resourceClassCodes } : {}),
    };

    setPending(true);
    try {
      if (rule) await updateAvailabilityRule(rule.id, payload);
      else await createAvailabilityRule(payload, idempotencyKey.current);
      idempotencyKey.current = mutationKey("availability");
      onSaved();
    } catch (reason) {
      setError(apiMessage(reason, "Impossible d'enregistrer la disponibilité."));
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="admin-editor" onSubmit={submit}>
      <div className="editor-heading">
        <div>
          <span className="eyebrow">{rule ? "Modifier" : "Ajouter"}</span>
          <h3>{forcedType ?? "Disponibilité"}</h3>
        </div>
        <button className="quiet-button" type="button" onClick={onCancel}>Fermer</button>
      </div>

      {!forcedType && (
        <label>
          Type
          <select value={kind} onChange={(event) => setKind(event.target.value as AvailabilityType)}>
            <option>Horaire standard</option>
            <option>Vacances</option>
          </select>
        </label>
      )}

      <div className="form-grid two-columns">
        <label>
          Date de début {isStandard ? "(optionnelle)" : ""}
          <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
        </label>
        <label>
          Date de fin {isStandard ? "(optionnelle)" : ""}
          <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
        </label>
      </div>

      {isStandard && (
        <>
          <div className="weekday-picker" aria-label="Jours de l'horaire standard">
            {WEEKDAYS.map((day) => (
              <label key={day} className={weekdays.has(day) ? "selected" : ""}>
                <input
                  type="checkbox"
                  checked={weekdays.has(day)}
                  onChange={() => toggleWeekday(day)}
                />
                {day}
              </label>
            ))}
          </div>
          <div className="form-grid two-columns">
            <label>
              Début
              <input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} />
            </label>
            <label>
              Fin
              <input type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} />
            </label>
          </div>
        </>
      )}

      {isHoliday && (
        <fieldset className="weekday-picker" aria-label="Classes visées par le jour férié">
          <legend>Classes visées</legend>
          {holidayClassOptions.map((row) => (
            <label key={row.code} className={resourceClassCodes.includes(row.code) ? "selected" : ""}>
              <input
                type="checkbox"
                checked={resourceClassCodes.includes(row.code)}
                onChange={() => toggleResourceClass(row.code)}
              />
              {row.label}{row.active ? "" : " (inactive)"}
            </label>
          ))}
          <small>
            Aucune classe sélectionnée = jour férié global, incluant les ressources non classées.
          </small>
        </fieldset>
      )}

      <label>
        Note
        <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Optionnel" />
      </label>

      {error && <div className="inline-error">{error}</div>}
      <div className="editor-actions">
        <button className="primary-button" type="submit" disabled={pending}>
          {pending ? "Enregistrement…" : rule ? "Enregistrer" : "Ajouter"}
        </button>
      </div>
    </form>
  );
}

export default function ResourcesPage() {
  const [resources, setResources] = useState<ResourceReadModel[]>([]);
  const [competencies, setCompetencies] = useState<CompetencyReadModel[]>([]);
  const [resourceClasses, setResourceClasses] = useState<ResourceClassOptionReadModel[]>([]);
  const [contacts, setContacts] = useState<BusinessContactReadModel[]>([]);
  const [resourceContactLink, setResourceContactLink] = useState<ContactLinkReadModel | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rules, setRules] = useState<ResourceAvailabilityRuleReadModel[]>([]);
  const [holidays, setHolidays] = useState<ResourceAvailabilityRuleReadModel[]>([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"active" | "inactive" | "all">("active");
  const [profile, setProfile] = useState<ResourceWrite>(resourceDraft());
  const [creatingResource, setCreatingResource] = useState(false);
  const [editingRule, setEditingRule] = useState<ResourceAvailabilityRuleReadModel | null>(null);
  const [ruleEditorOpen, setRuleEditorOpen] = useState(false);
  const [editingHoliday, setEditingHoliday] = useState<ResourceAvailabilityRuleReadModel | null>(null);
  const [holidayEditorOpen, setHolidayEditorOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingProfile, setPendingProfile] = useState(false);
  const [pendingCoordinator, setPendingCoordinator] = useState(false);
  const [pendingEmployeeSync, setPendingEmployeeSync] = useState(false);
  const [employeeSyncNotice, setEmployeeSyncNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const profileIdempotency = useRef(mutationKey("resource"));

  const selected = useMemo(
    () => resources.find((resource) => resource.id === selectedId) ?? null,
    [resources, selectedId],
  );
  const activeResourceClasses = useMemo(
    () => resourceClasses
      .filter((row) => row.active)
      .sort((left, right) => left.label.localeCompare(right.label, "fr-CA")),
    [resourceClasses],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getResources(false, controller.signal),
      getAvailabilityRules(null, true, false, controller.signal),
      getCompetencies("", false, controller.signal),
      getResourceClassOptions(controller.signal),
      getBusinessContacts(false, controller.signal),
    ])
      .then(([resourceRows, availabilityRows, competencyRows, classRows, contactRows]) => {
        setResources(resourceRows);
        setCompetencies(competencyRows);
        setResourceClasses(classRows);
        setContacts(contactRows);
        setHolidays(
          availabilityRows.filter(
            (rule) => rule.resource_id === null && rule.availability_type === "Jour férié",
          ),
        );
        if (!creatingResource) {
          setSelectedId((current) => {
            if (current && resourceRows.some((row) => row.id === current)) return current;
            return resourceRows.find((row) => row.active)?.id ?? resourceRows[0]?.id ?? null;
          });
        }
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(apiMessage(reason, "Impossible de charger les ressources."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refreshKey, creatingResource]);

  useEffect(() => {
    if (!selectedId) {
      setRules([]);
      setResourceContactLink(null);
      return;
    }
    const controller = new AbortController();
    Promise.all([
      getAvailabilityRules(selectedId, false, false, controller.signal),
      getResourceBusinessContacts(selectedId, controller.signal),
    ])
      .then(([ruleRows, contactLink]) => {
        setRules(ruleRows);
        setResourceContactLink(contactLink);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(apiMessage(reason, "Impossible de charger les disponibilités ou le coordonnateur."));
      });
    return () => controller.abort();
  }, [selectedId, refreshKey]);

  useEffect(() => {
    if (creatingResource) return;
    setProfile(resourceDraft(selected));
  }, [selected, creatingResource]);

  const visibleResources = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("fr-CA");
    return resources.filter((resource) => {
      if (statusFilter === "active" && !resource.active) return false;
      if (statusFilter === "inactive" && resource.active) return false;
      if (!query) return true;
      return [resource.name, resource.email, resource.resource_class, resource.competencies]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase("fr-CA")
        .includes(query);
    });
  }, [resources, search, statusFilter]);

  function selectResource(resource: ResourceReadModel) {
    setCreatingResource(false);
    setSelectedId(resource.id);
    setEditingRule(null);
    setRuleEditorOpen(false);
  }

  function startCreateResource() {
    setCreatingResource(true);
    setSelectedId(null);
    setRules([]);
    setProfile(resourceDraft());
    setEditingRule(null);
    setRuleEditorOpen(false);
    profileIdempotency.current = mutationKey("resource");
  }

  async function synchronizeEmployees() {
    if (pendingEmployeeSync) return;
    setPendingEmployeeSync(true);
    setError(null);
    setEmployeeSyncNotice(null);
    try {
      const result = await syncAcumaticaEmployees();
      setEmployeeSyncNotice(
        `RP_Employees : ${result.received} reçus, ${result.created} créés, ${result.updated} modifiés, ${result.unchanged} inchangés, ${result.errors} erreurs.`,
      );
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible de synchroniser RP_Employees."));
    } finally {
      setPendingEmployeeSync(false);
    }
  }

  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    if (pendingProfile) return;
    if (!profile.name.trim()) {
      setError("Le nom de la ressource est requis.");
      return;
    }
    setPendingProfile(true);
    setError(null);
    try {
      const payload: ResourceWrite = {
        ...profile,
        name: profile.name.trim(),
        email: nullable(profile.email ?? ""),
        resource_class: nullable(profile.resource_class ?? ""),
        competencies: profile.competencies,
        competency_ids: profile.competency_ids,
        note: nullable(profile.note ?? ""),
        external_id: nullable(profile.external_id ?? ""),
        sort_order: Number(profile.sort_order || 0),
      };
      if (creatingResource) {
        const result = await createResource(payload, profileIdempotency.current);
        profileIdempotency.current = mutationKey("resource");
        setCreatingResource(false);
        setSelectedId(result.resource_id);
      } else if (selectedId) {
        await updateResource(selectedId, payload);
      }
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible d'enregistrer la ressource."));
    } finally {
      setPendingProfile(false);
    }
  }

  async function disableResource() {
    if (!selected || pendingProfile) return;
    if (!window.confirm(`Désactiver ${selected.name} ? Son historique sera conservé.`)) return;
    setPendingProfile(true);
    setError(null);
    try {
      await deactivateResource(selected.id);
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible de désactiver la ressource."));
    } finally {
      setPendingProfile(false);
    }
  }

  async function enableResource() {
    if (!selected || pendingProfile) return;
    setPendingProfile(true);
    setError(null);
    try {
      await reactivateResource(selected.id);
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible de réactiver la ressource."));
    } finally {
      setPendingProfile(false);
    }
  }

  async function changeCoordinator(contactId: string | null) {
    if (!selectedId || pendingCoordinator) return;
    setPendingCoordinator(true);
    setError(null);
    try {
      const link = await setResourceCoordinatorContact(selectedId, contactId);
      setResourceContactLink(link);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible d'enregistrer le coordonnateur."));
    } finally {
      setPendingCoordinator(false);
    }
  }

  async function disableRule(rule: ResourceAvailabilityRuleReadModel) {
    if (!window.confirm("Désactiver cette règle de disponibilité ?")) return;
    setError(null);
    try {
      await deactivateAvailabilityRule(rule.id);
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(apiMessage(reason, "Impossible de désactiver la règle."));
    }
  }

  function profileField<K extends keyof ResourceWrite>(field: K, value: ResourceWrite[K]) {
    setProfile((current) => ({ ...current, [field]: value }));
  }

  const standardRules = rules.filter((rule) => rule.availability_type === "Horaire standard");
  const vacationRules = rules.filter((rule) => rule.availability_type === "Vacances");
  const activeResources = resources.filter((resource) => resource.active).length;

  return (
    <section className="resources-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Administration</span>
          <h1>Ressources & disponibilités</h1>
          <p>Profils, compétences, horaires standards, absences et jours fériés utilisés par le moteur de capacité.</p>
        </div>
        <button className="primary-button" type="button" onClick={startCreateResource}>
          + Nouvelle ressource
        </button>
      </div>

      <div className="admin-card">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Acumatica</span>
            <h2>RP_Employees</h2>
            <p>Synchronise l’annuaire ERP sans activer automatiquement les ressources dans RessourcePlanner.</p>
          </div>
          <button
            className="secondary-button"
            type="button"
            disabled={pendingEmployeeSync}
            onClick={() => void synchronizeEmployees()}
          >
            {pendingEmployeeSync ? "Synchronisation…" : "Synchroniser RP_Employees"}
          </button>
        </div>
        {employeeSyncNotice && <div className="subtle-status" aria-live="polite">{employeeSyncNotice}</div>}
      </div>

      <div className="metric-grid resource-metrics">
        <article><span>Ressources actives</span><strong>{activeResources}</strong><small>Planifiables selon leur horaire</small></article>
        <article><span>Ressources inactives</span><strong>{resources.length - activeResources}</strong><small>Historique conservé</small></article>
        <article><span>Jours fériés actifs</span><strong>{holidays.filter((rule) => rule.active).length}</strong><small>Règles de capacité</small></article>
      </div>

      {error && <div className="error-panel">{error}</div>}

      <div className="resource-admin-layout">
        <aside className="resource-list-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">Équipe</span><h2>Ressources</h2></div>
            {loading && <span className="subtle-status">Chargement…</span>}
          </div>
          <div className="resource-filters">
            <input
              type="search"
              placeholder="Rechercher nom, classe, compétence…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}>
              <option value="active">Actives</option>
              <option value="inactive">Inactives</option>
              <option value="all">Toutes</option>
            </select>
          </div>
          <div className="resource-list">
            {!loading && visibleResources.length === 0 && <div className="empty-admin-state">Aucune ressource pour ce filtre.</div>}
            {visibleResources.map((resource) => (
              <button
                type="button"
                key={resource.id}
                className={`resource-list-item ${selectedId === resource.id ? "selected" : ""} ${resource.active ? "" : "inactive"}`}
                onClick={() => selectResource(resource)}
              >
                <span className="resource-list-main"><strong>{resourceDisplayName(resource.name)}</strong><small>{resource.resource_class || "Non classé"}</small></span>
                <span className="resource-order">#{resource.sort_order}</span>
              </button>
            ))}
          </div>
        </aside>

        <div className="resource-detail-panel">
          {(creatingResource || selected) ? (
            <>
              <form className="admin-card resource-profile-card" onSubmit={saveProfile}>
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">Profil</span>
                    <h2>{creatingResource ? "Nouvelle ressource" : resourceDisplayName(selected?.name)}</h2>
                  </div>
                  {!creatingResource && selected && <span className={`status-chip ${selected.active ? "active" : "inactive"}`}>{selected.active ? "Active" : "Inactive"}</span>}
                </div>

                {!creatingResource && selected?.external_id && (
                  <div className="form-grid two-columns">
                    <label>EmployeID<input value={selected.external_id} readOnly /></label>
                    <label>Statut ERP<input value={selected.erp_status ?? (selected.erp_active ? "Actif" : "Inactif")} readOnly /></label>
                    <label>Département<input value={selected.erp_department_description ?? selected.erp_department_code ?? "—"} readOnly /></label>
                    <label>Division / succursale<input value={selected.erp_branch_code ?? "—"} readOnly /></label>
                  </div>
                )}

                <div className="form-grid two-columns">
                  <label>Nom<input value={resourceDisplayName(profile.name)} onChange={(event) => profileField("name", event.target.value)} required /></label>
                  <label>Courriel<input type="email" value={profile.email ?? ""} onChange={(event) => profileField("email", event.target.value)} /></label>
                  <label>
                    Classe
                    <select
                      value={profile.resource_class ?? ""}
                      onChange={(event) => profileField("resource_class", event.target.value)}
                      disabled={pendingProfile}
                    >
                      <option value="">Aucune classe</option>
                      {profile.resource_class && !activeResourceClasses.some((row) => row.code === profile.resource_class) && (
                        <option value={profile.resource_class}>{profile.resource_class} — historique/inactive</option>
                      )}
                      {activeResourceClasses.map((row) => (
                        <option value={row.code} key={row.code}>{row.code} · {row.label}</option>
                      ))}
                    </select>
                  </label>
                  <label>Ordre<input type="number" min="0" value={profile.sort_order} onChange={(event) => profileField("sort_order", Number(event.target.value))} /></label>
                </div>
                <CompetencyPicker
                  competencies={competencies}
                  selectedIds={profile.competency_ids}
                  onChange={(ids) => profileField("competency_ids", ids)}
                  disabled={pendingProfile}
                  label="Compétences"
                  placeholder="Rechercher PLC, SCADA, MES…"
                />
                {profile.competency_ids.length === 0 && profile.competencies && (
                  <small className="legacy-competency-note">
                    Valeur historique à convertir au catalogue : {profile.competencies}
                  </small>
                )}
                <label>Note<textarea value={profile.note ?? ""} onChange={(event) => profileField("note", event.target.value)} rows={2} /></label>

                <div className="editor-actions split-actions">
                  {!creatingResource && selected?.active && (
                    <button className="danger-button" type="button" disabled={pendingProfile} onClick={disableResource}>Désactiver</button>
                  )}
                  {!creatingResource && selected && !selected.active && (
                    <button className="quiet-button" type="button" disabled={pendingProfile} onClick={enableResource}>Réactiver</button>
                  )}
                  <span />
                  {creatingResource && <button className="quiet-button" type="button" onClick={() => setCreatingResource(false)}>Annuler</button>}
                  <button className="primary-button" type="submit" disabled={pendingProfile}>{pendingProfile ? "Enregistrement…" : "Enregistrer le profil"}</button>
                </div>
              </form>

              {!creatingResource && selected && (
                <div className="admin-card">
                  <div className="panel-heading">
                    <div>
                      <span className="eyebrow">Coordination métier</span>
                      <h2>Coordonnateur de la ressource</h2>
                      <p>Prioritaire sur le coordonnateur de la tâche pour les lignes qui proposent ou utilisent cette ressource.</p>
                    </div>
                  </div>
                  <label>
                    Coordonnateur
                    <ContactSelect
                      contacts={contacts}
                      value={resourceContactLink?.coordinator_contact_id ?? null}
                      onChange={(value) => void changeCoordinator(value)}
                      disabled={pendingCoordinator}
                      inheritLabel="Aucun coordonnateur de ressource · utiliser la tâche"
                    />
                  </label>
                </div>
              )}

              {!creatingResource && selected && (
                <div className="admin-card">
                  <div className="panel-heading">
                    <div><span className="eyebrow">Capacité</span><h2>Horaire & absences</h2></div>
                    <button className="secondary-button" type="button" onClick={() => { setEditingRule(null); setRuleEditorOpen(true); }}>+ Ajouter</button>
                  </div>

                  {ruleEditorOpen && (
                    <RuleEditor
                      key={editingRule?.id ?? "new-resource-rule"}
                      resourceId={selected.id}
                      rule={editingRule}
                      resourceClasses={resourceClasses}
                      onCancel={() => { setRuleEditorOpen(false); setEditingRule(null); }}
                      onSaved={() => { setRuleEditorOpen(false); setEditingRule(null); setRefreshKey((value) => value + 1); }}
                    />
                  )}

                  <div className="rule-groups">
                    <div>
                      <h3>Horaire standard</h3>
                      {standardRules.length === 0 && <p className="empty-admin-state">Aucun horaire standard. Cette ressource ne sera pas planifiable.</p>}
                      {standardRules.map((rule) => (
                        <article className={`availability-rule ${rule.active ? "" : "inactive"}`} key={rule.id}>
                          <div><strong>{rule.weekdays || "Tous les jours"}</strong><span>{shortTime(rule.start_time)} → {shortTime(rule.end_time)}</span><small>{rule.start_date || "Sans début"} → {rule.end_date || "Sans fin"}{rule.note ? ` · ${rule.note}` : ""}</small></div>
                          <div className="rule-actions"><button type="button" onClick={() => { setEditingRule(rule); setRuleEditorOpen(true); }}>Modifier</button>{rule.active && <button type="button" onClick={() => disableRule(rule)}>Désactiver</button>}</div>
                        </article>
                      ))}
                    </div>
                    <div>
                      <h3>Vacances / absences</h3>
                      {vacationRules.length === 0 && <p className="empty-admin-state">Aucune absence enregistrée.</p>}
                      {vacationRules.map((rule) => (
                        <article className={`availability-rule ${rule.active ? "" : "inactive"}`} key={rule.id}>
                          <div><strong>{rule.start_date || "—"} → {rule.end_date || rule.start_date || "—"}</strong><small>{rule.note || "Vacances"}</small></div>
                          <div className="rule-actions"><button type="button" onClick={() => { setEditingRule(rule); setRuleEditorOpen(true); }}>Modifier</button>{rule.active && <button type="button" onClick={() => disableRule(rule)}>Désactiver</button>}</div>
                        </article>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="admin-card empty-admin-state">Sélectionne une ressource ou crée-en une nouvelle.</div>
          )}
        </div>
      </div>

      <AssetCatalogPanel competencies={competencies} />

      <CompetencyCatalogPanel
        competencies={competencies}
        onChanged={() => setRefreshKey((value) => value + 1)}
      />

      <div className="admin-card holiday-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">Calendrier de disponibilité</span><h2>Jours fériés</h2><p>Un jour férié peut être global ou viser une ou plusieurs classes de ressources.</p></div>
          <button className="secondary-button" type="button" onClick={() => { setEditingHoliday(null); setHolidayEditorOpen(true); }}>+ Jour férié</button>
        </div>

        {holidayEditorOpen && (
          <RuleEditor
            key={editingHoliday?.id ?? "new-holiday"}
            resourceId={null}
            rule={editingHoliday}
            forcedType="Jour férié"
            resourceClasses={resourceClasses}
            onCancel={() => { setHolidayEditorOpen(false); setEditingHoliday(null); }}
            onSaved={() => { setHolidayEditorOpen(false); setEditingHoliday(null); setRefreshKey((value) => value + 1); }}
          />
        )}

        <div className="holiday-list">
          {holidays.length === 0 && <p className="empty-admin-state">Aucun jour férié enregistré.</p>}
          {holidays.map((rule) => {
            const scope = rule.resource_class_codes.length === 0
              ? "Toutes les classes"
              : rule.resource_class_codes.map((code) => {
                const option = resourceClasses.find((row) => row.code === code);
                return option ? `${option.label}${option.active ? "" : " (inactive)"}` : code;
              }).join(", ");
            return (
            <article className={`availability-rule ${rule.active ? "" : "inactive"}`} key={rule.id}>
              <div><strong>{rule.start_date || "—"}{rule.end_date && rule.end_date !== rule.start_date ? ` → ${rule.end_date}` : ""}</strong><span>{rule.note || "Jour férié"}</span><small>{scope}</small></div>
              <div className="rule-actions"><button type="button" onClick={() => { setEditingHoliday(rule); setHolidayEditorOpen(true); }}>Modifier</button>{rule.active && <button type="button" onClick={() => disableRule(rule)}>Désactiver</button>}</div>
            </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
