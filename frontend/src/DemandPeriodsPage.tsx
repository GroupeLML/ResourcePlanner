import { resourceDisplayName } from "./resourceLabels";
import { useEffect, useMemo, useState } from "react";

import { createClientId } from "./clientId";
import {
  ApiError,
  type DemandPeriodReadModel,
  type DemandPeriodWrite,
  type DemandReadModel,
  type DemandLineReadModel,
  type ResourceReadModel,
  type ViewScope,
  getDemand,
  getDemandLinePeriods,
  getDemandPeriods,
  getDemands,
  getResources,
  replaceDemandLinePeriods,
  replaceDemandPeriods,
  selectDemandAlternative,
  selectDemandLineAlternative,
} from "./api";

type PeriodDraft = DemandPeriodWrite & {
  selected: boolean;
  proposed_resource_id: string | null;
  inheritance_contract_version: number | null;
  confirmation_provenance: string | null;
  proposed_resource_provenance: string | null;
  resource_count_provenance: string | null;
  same_as_root_period_id: string | null;
  same_as_state: string | null;
};

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiError) return reason.message;
  if (reason instanceof Error) return reason.message;
  return "Une erreur inattendue est survenue.";
}

function newPeriodId(): string {
  return `PER-WEB-${createClientId()}`;
}

function fromRead(row: DemandPeriodReadModel): PeriodDraft {
  return {
    period_id: row.period_id,
    start_date: row.start_date,
    end_date: row.end_date,
    hours: row.hours,
    kind: row.kind,
    alternative_group: row.alternative_group,
    confirmation: row.confirmation_explicit ?? row.confirmation,
    confirmation_mode: row.confirmation_mode ?? "EXPLICIT",
    proposed_resource: row.proposed_resource_explicit ?? (row.proposed_resource_mode === "EXPLICIT" ? row.proposed_resource : null),
    proposed_resource_id: row.proposed_resource_mode === "EXPLICIT" ? row.proposed_resource_id : null,
    proposed_resource_mode: row.proposed_resource_mode ?? "EXPLICIT",
    same_as_period_id: row.same_as_period_id,
    resource_count: row.resource_count,
    desired_active_days: row.desired_active_days ?? null,
    note: row.note ?? "",
    selected: row.selected,
    inheritance_contract_version: row.inheritance_contract_version,
    confirmation_provenance: row.confirmation_provenance,
    proposed_resource_provenance: row.proposed_resource_provenance,
    resource_count_provenance: row.resource_count_provenance,
    same_as_root_period_id: row.same_as_root_period_id,
    same_as_state: row.same_as_state,
  };
}

function provenanceLabel(value: string | null): string {
  if (value === "MASTER") return "dérivée du besoin maître";
  if (value === "EXPLICIT") return "surcharge propre à la période";
  if (value === "SAME_AS_PERIOD") return "contrainte de même personne";
  if (value === "OPERATIONAL_OVERRIDE") return "choix opérationnel";
  if (value === "LEGACY") return "valeur historique conservée";
  return "valeur résolue par le serveur";
}

function baseDates(
  demand: DemandReadModel | null,
  line: DemandLineReadModel | null,
): { start: string; end: string } {
  const start = line?.desired_start ?? demand?.desired_start ?? new Date().toISOString().slice(0, 10);
  return { start, end: line?.desired_end ?? demand?.desired_end ?? start };
}

function defaultActiveDays(
  demand: DemandReadModel | null,
  line: DemandLineReadModel | null,
): number | null {
  const value = line?.desired_active_days ?? demand?.estimated_days;
  return value != null && Number.isInteger(value) && value > 0 ? value : null;
}

function inclusiveCalendarDays(start: string, end: string): number {
  if (!start || !end) return 0;
  return Math.floor((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
}

function nextAlternativeGroup(periods: PeriodDraft[]): string {
  const existing = new Set(
    periods
      .map((row) => row.alternative_group?.trim())
      .filter((value): value is string => Boolean(value)),
  );
  let sequence = 1;
  while (existing.has(`ALT-${sequence}`)) sequence += 1;
  return `ALT-${sequence}`;
}

function validatePeriods(periods: PeriodDraft[]): string | null {
  const groupCounts = new Map<string, number>();
  for (const period of periods) {
    if (!period.period_id.trim()) return "Chaque période doit avoir un identifiant stable.";
    if (!period.start_date || !period.end_date) return "Chaque période doit avoir une date de début et de fin.";
    if (period.end_date < period.start_date) return "La fin d'une période ne peut pas précéder son début.";
    if (!Number.isFinite(period.hours) || period.hours <= 0) return "Les heures de chaque période doivent être supérieures à zéro.";
    if (period.proposed_resource_mode === "SAME_AS_PERIOD" && !period.same_as_period_id) {
      return `La période ${period.period_id} doit cibler une autre période cumulative.`;
    }
    if (period.desired_active_days != null) {
      if (!Number.isInteger(period.desired_active_days) || period.desired_active_days < 1) {
        return "Les jours actifs souhaités doivent être un entier supérieur ou égal à 1.";
      }
      const windowDays = inclusiveCalendarDays(period.start_date, period.end_date);
      if (period.desired_active_days > windowDays) {
        return `La cible de ${period.desired_active_days} jours actifs dépasse les ${windowDays} dates de la période ${period.period_id}.`;
      }
    }
    if (period.kind === "ALTERNATIVE") {
      const group = period.alternative_group?.trim();
      if (!group) return "Chaque option alternative doit appartenir à un groupe.";
      groupCounts.set(group, (groupCounts.get(group) ?? 0) + 1);
    }
  }
  for (const [group, count] of groupCounts) {
    if (count < 2) return `Le groupe ${group} doit contenir au moins deux options.`;
  }
  return null;
}

function PeriodFields({
  period,
  periods,
  resources,
  disabled,
  masterConfirmation,
  masterProposedResource,
  onChange,
  onRemove,
}: {
  period: PeriodDraft;
  periods: PeriodDraft[];
  resources: ResourceReadModel[];
  disabled: boolean;
  masterConfirmation: "Tentative" | "Confirmée";
  masterProposedResource: string | null;
  onChange: (next: PeriodDraft) => void;
  onRemove: () => void;
}) {
  const change = <K extends keyof PeriodDraft>(field: K, value: PeriodDraft[K]) => {
    onChange({ ...period, [field]: value });
  };
  const sameAsTargets = periods.filter(
    (candidate) => candidate.kind === "CUMULATIVE" && candidate.period_id !== period.period_id,
  );
  const confirmationMode = period.confirmation_mode ?? "EXPLICIT";
  const resourceMode = period.proposed_resource_mode ?? "EXPLICIT";

  const changeResourceMode = (mode: DemandPeriodWrite["proposed_resource_mode"]) => {
    onChange({
      ...period,
      proposed_resource_mode: mode,
      proposed_resource: mode === "EXPLICIT" ? period.proposed_resource : null,
      proposed_resource_id: mode === "EXPLICIT" ? period.proposed_resource_id : null,
      proposed_resource_provenance: null,
      same_as_period_id: mode === "SAME_AS_PERIOD" ? period.same_as_period_id : null,
      same_as_root_period_id: null,
      same_as_state: null,
    });
  };

  return (
    <div className={`period-card ${period.kind === "ALTERNATIVE" ? "alternative" : "cumulative"}`}>
      <div className="period-card-heading">
        <div>
          <strong>{period.kind === "ALTERNATIVE" ? "Option alternative" : "Période cumulative"}</strong>
          <span>{period.period_id}</span>
        </div>
        <button type="button" className="period-remove" onClick={onRemove} disabled={disabled}>
          Retirer
        </button>
      </div>

      {period.inheritance_contract_version == null && (
        <div className="period-authority-note">
          Période historique : les valeurs existantes restent explicites si cette définition est enregistrée à nouveau.
        </div>
      )}

      <div className="period-form-grid">
        <label>
          <span>Début</span>
          <input type="date" value={period.start_date} disabled={disabled} onChange={(event) => change("start_date", event.target.value)} />
        </label>
        <label>
          <span>Fin</span>
          <input type="date" min={period.start_date} value={period.end_date} disabled={disabled} onChange={(event) => change("end_date", event.target.value)} />
        </label>
        <label>
          <span>Heures totales</span>
          <input type="number" min="0.25" step="0.25" value={period.hours} disabled={disabled} onChange={(event) => change("hours", Number(event.target.value))} />
          <small>Volume total de main-d’œuvre; la quantité de personnes est dérivée du besoin maître.</small>
        </label>
        <div className="period-effective-field">
          <span>Quantité effective</span>
          <strong>{period.resource_count} ressource(s)</strong>
          <small>{provenanceLabel(period.resource_count_provenance)}</small>
        </div>
        <label>
          <span>Jours actifs souhaités</span>
          <input
            type="number"
            min="1"
            step="1"
            value={period.desired_active_days ?? ""}
            disabled={disabled}
            onChange={(event) => change("desired_active_days", event.target.value ? Number(event.target.value) : null)}
            placeholder="Optionnel"
          />
          <small>Cible de répartition; la capacité peut imposer davantage de jours.</small>
        </label>
        <label>
          <span>Mode de confirmation</span>
          <select
            value={confirmationMode}
            disabled={disabled}
            onChange={(event) => change("confirmation_mode", event.target.value as DemandPeriodWrite["confirmation_mode"])}
          >
            <option value="INHERIT_MASTER">Dérivée de la demande maître</option>
            <option value="EXPLICIT">Valeur propre à la période</option>
          </select>
          <small>
            {confirmationMode === "INHERIT_MASTER"
              ? `Valeur maître actuelle : ${masterConfirmation}.`
              : `Valeur effective : ${period.confirmation} · ${provenanceLabel(period.confirmation_provenance)}.`}
          </small>
        </label>
        {confirmationMode === "EXPLICIT" && (
          <label>
            <span>Confirmation propre</span>
            <select value={period.confirmation} disabled={disabled} onChange={(event) => change("confirmation", event.target.value as PeriodDraft["confirmation"])}>
              <option value="Tentative">Tentative</option>
              <option value="Confirmée">Confirmée</option>
            </select>
          </label>
        )}
        <label className="span-2">
          <span>Mode de ressource</span>
          <select
            value={resourceMode}
            disabled={disabled}
            onChange={(event) => changeResourceMode(event.target.value as DemandPeriodWrite["proposed_resource_mode"])}
          >
            <option value="INHERIT_MASTER">Dérivée de la demande maître</option>
            <option value="EXPLICIT">Ressource spécifique</option>
            {period.kind === "CUMULATIVE" && period.resource_count === 1 && (
              <option value="SAME_AS_PERIOD">Même ressource qu’une autre période</option>
            )}
          </select>
          <small>
            {resourceMode === "INHERIT_MASTER"
              ? `Suggestion maître actuelle : ${masterProposedResource ?? "aucune"}.`
              : resourceMode === "SAME_AS_PERIOD"
                ? "Contrainte réelle : les quarts des périodes liées doivent utiliser la même personne."
                : `Suggestion effective : ${period.proposed_resource ?? "aucune"} · ${provenanceLabel(period.proposed_resource_provenance)}.`}
          </small>
        </label>
        {resourceMode === "EXPLICIT" && (
          <label className="span-2">
            <span>Ressource spécifique</span>
            <select
              value={period.proposed_resource_id ?? ""}
              disabled={disabled}
              onChange={(event) => {
                const selected = resources.find((resource) => resource.id === event.target.value) ?? null;
                onChange({
                  ...period,
                  proposed_resource_id: selected?.id ?? null,
                  proposed_resource: selected?.name ?? null,
                  same_as_period_id: null,
                });
              }}
            >
              <option value="">Aucune</option>
              {resources.map((resource) => (
                <option value={resource.id} key={resource.id}>{resourceDisplayName(resource.name)}{resource.resource_class ? ` — ${resource.resource_class}` : ""}</option>
              ))}
            </select>
            <small>Le sélecteur utilise l’identifiant stable de la ressource; la suggestion n’est pas une affectation réelle.</small>
          </label>
        )}
        {resourceMode === "SAME_AS_PERIOD" && (
          <label className="span-2">
            <span>Même ressource que</span>
            <select
              value={period.same_as_period_id ?? ""}
              disabled={disabled}
              onChange={(event) => onChange({
                ...period,
                same_as_period_id: event.target.value || null,
                same_as_root_period_id: null,
                same_as_state: null,
              })}
            >
              <option value="">Sélectionner une période cumulative…</option>
              {sameAsTargets.map((target) => (
                <option value={target.period_id} key={target.period_id}>
                  {target.period_id} — {target.start_date} → {target.end_date}
                </option>
              ))}
            </select>
            <small>
              {period.same_as_period_id
                ? `Même personne obligatoire que ${period.same_as_period_id}${period.same_as_root_period_id && period.same_as_root_period_id !== period.same_as_period_id ? ` · racine ${period.same_as_root_period_id}` : ""}. État serveur : ${period.same_as_state ?? "sera validé à l’enregistrement"}.`
                : "La cible est une identité logique de période; les cycles sont refusés par le backend."}
            </small>
          </label>
        )}
        {period.kind === "ALTERNATIVE" && (
          <label className="span-2">
            <span>Groupe alternatif</span>
            <input value={period.alternative_group ?? ""} disabled={disabled} onChange={(event) => change("alternative_group", event.target.value)} placeholder="Ex. ALT-1" />
          </label>
        )}
        <label className="span-2">
          <span>Note</span>
          <input value={period.note} disabled={disabled} onChange={(event) => change("note", event.target.value)} placeholder="Contrainte, préférence ou contexte de cette période" />
        </label>
      </div>
    </div>
  );
}

type DemandPeriodsPageProps = {
  demandNumber?: string;
  viewScope?: ViewScope;
  canonicalDemand?: DemandReadModel | null;
  embedded?: boolean;
  onChanged?: () => void | Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  canEdit?: boolean;
};

export default function DemandPeriodsPage({
  demandNumber,
  viewScope,
  canonicalDemand,
  embedded = false,
  onChanged,
  onDirtyChange,
  canEdit = true,
}: DemandPeriodsPageProps = {}) {
  const [demands, setDemands] = useState<DemandReadModel[]>([]);
  const [resources, setResources] = useState<ResourceReadModel[]>([]);
  const [selectedNumber, setSelectedNumber] = useState("");
  const [selectedLineId, setSelectedLineId] = useState("");
  const [periods, setPeriods] = useState<PeriodDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [periodLoading, setPeriodLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selectedDemand = useMemo(
    () => demands.find((row) => row.number === selectedNumber) ?? null,
    [demands, selectedNumber],
  );

  const selectedLine = useMemo(
    () =>
      selectedDemand?.line_mode
        ? selectedDemand.lines.find((row) => row.active && row.line_id === selectedLineId) ?? null
        : null,
    [selectedDemand, selectedLineId],
  );

  useEffect(() => {
    if (!selectedDemand?.line_mode) {
      setSelectedLineId("");
      return;
    }
    const activeLines = selectedDemand.lines.filter((row) => row.active);
    setSelectedLineId((current) =>
      activeLines.some((row) => row.line_id === current)
        ? current
        : activeLines[0]?.line_id ?? "",
    );
  }, [selectedDemand?.number, selectedDemand?.line_mode, selectedDemand?.lines]);

  useEffect(() => {
    const controller = new AbortController();
    const demandRequest = canonicalDemand
      ? Promise.resolve([canonicalDemand])
      : demandNumber
        ? getDemand(demandNumber, controller.signal, viewScope).then((row) => [row])
        : getDemands(controller.signal);
    const resourceRequest = canEdit
      ? getResources(true, controller.signal)
      : Promise.resolve<ResourceReadModel[]>([]);
    Promise.all([demandRequest, resourceRequest])
      .then(([demandRows, resourceRows]) => {
        setDemands(demandRows);
        setResources(resourceRows);
        setSelectedNumber((current) =>
          demandNumber || current || demandRows[0]?.number || "",
        );
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demandNumber, canonicalDemand, viewScope, canEdit]);

  useEffect(() => {
    onDirtyChange?.(dirty);
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!selectedNumber || (selectedDemand?.line_mode && !selectedLineId)) {
      setPeriods([]);
      return;
    }
    const controller = new AbortController();
    setPeriodLoading(true);
    setError(null);
    setNotice(null);
    const request = selectedDemand?.line_mode
      ? getDemandLinePeriods(selectedNumber, selectedLineId, controller.signal, viewScope)
      : getDemandPeriods(selectedNumber, controller.signal, viewScope);
    request
      .then((rows) => {
        setPeriods(rows.map(fromRead));
        setDirty(false);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPeriodLoading(false);
      });
    return () => controller.abort();
  }, [selectedNumber, selectedDemand?.line_mode, selectedLineId, viewScope]);

  const cumulative = periods.filter((row) => row.kind === "CUMULATIVE");
  const alternativeGroups = useMemo(() => {
    const grouped = new Map<string, PeriodDraft[]>();
    for (const period of periods) {
      if (period.kind !== "ALTERNATIVE") continue;
      const group = period.alternative_group?.trim() || "Sans groupe";
      const rows = grouped.get(group) ?? [];
      rows.push(period);
      grouped.set(group, rows);
    }
    return [...grouped.entries()];
  }, [periods]);

  function replacePeriod(index: number, next: PeriodDraft) {
    setPeriods((current) => current.map((row, position) => (position === index ? next : row)));
    setDirty(true);
    setNotice(null);
  }

  function removePeriod(periodId: string) {
    setPeriods((current) => current.filter((row) => row.period_id !== periodId));
    setDirty(true);
    setNotice(null);
  }

  function addCumulative() {
    const { start, end } = baseDates(selectedDemand, selectedLine);
    setPeriods((current) => [
      ...current,
      {
        period_id: newPeriodId(),
        start_date: start,
        end_date: end,
        hours: 8,
        kind: "CUMULATIVE",
        alternative_group: null,
        confirmation: ((selectedLine?.confirmation ?? selectedDemand?.confirmation) === "Confirmée" ? "Confirmée" : "Tentative"),
        confirmation_mode: "INHERIT_MASTER",
        proposed_resource: null,
        proposed_resource_id: null,
        proposed_resource_mode: "INHERIT_MASTER",
        same_as_period_id: null,
        resource_count: selectedLine?.slot_count ?? selectedDemand?.resource_count ?? 1,
        desired_active_days: defaultActiveDays(selectedDemand, selectedLine),
        note: "",
        selected: false,
        inheritance_contract_version: 1,
        confirmation_provenance: "MASTER",
        proposed_resource_provenance: "MASTER",
        resource_count_provenance: "MASTER",
        same_as_root_period_id: null,
        same_as_state: null,
      },
    ]);
    setDirty(true);
    setNotice(null);
  }

  function addAlternativeGroup() {
    const { start, end } = baseDates(selectedDemand, selectedLine);
    const group = nextAlternativeGroup(periods);
    const base: Omit<PeriodDraft, "period_id"> = {
      start_date: start,
      end_date: end,
      hours: 8,
      kind: "ALTERNATIVE",
      alternative_group: group,
      confirmation: ((selectedLine?.confirmation ?? selectedDemand?.confirmation) === "Confirmée" ? "Confirmée" : "Tentative"),
      confirmation_mode: "INHERIT_MASTER",
      proposed_resource: null,
      proposed_resource_id: null,
      proposed_resource_mode: "INHERIT_MASTER",
      same_as_period_id: null,
      resource_count: selectedLine?.slot_count ?? selectedDemand?.resource_count ?? 1,
      desired_active_days: defaultActiveDays(selectedDemand, selectedLine),
      note: "",
      selected: false,
      inheritance_contract_version: 1,
      confirmation_provenance: "MASTER",
      proposed_resource_provenance: "MASTER",
      resource_count_provenance: "MASTER",
      same_as_state: null,
    };
    setPeriods((current) => [
      ...current,
      { ...base, period_id: newPeriodId() },
      { ...base, period_id: newPeriodId() },
    ]);
    setDirty(true);
    setNotice(null);
  }

  function addAlternativeOption(group: string) {
    const existing = periods.find((row) => row.kind === "ALTERNATIVE" && row.alternative_group === group);
    const { start, end } = baseDates(selectedDemand, selectedLine);
    setPeriods((current) => [
      ...current,
      {
        period_id: newPeriodId(),
        start_date: existing?.start_date ?? start,
        end_date: existing?.end_date ?? end,
        hours: existing?.hours ?? 8,
        kind: "ALTERNATIVE",
        alternative_group: group,
        confirmation: existing?.confirmation ?? ((selectedLine?.confirmation ?? selectedDemand?.confirmation) === "Confirmée" ? "Confirmée" : "Tentative"),
        confirmation_mode: existing?.confirmation_mode ?? "INHERIT_MASTER",
        proposed_resource: existing?.proposed_resource_mode === "EXPLICIT" ? existing.proposed_resource : null,
        proposed_resource_id: existing?.proposed_resource_mode === "EXPLICIT" ? existing.proposed_resource_id : null,
        proposed_resource_mode: existing?.proposed_resource_mode === "EXPLICIT" ? "EXPLICIT" : "INHERIT_MASTER",
        same_as_period_id: null,
        resource_count: selectedLine?.slot_count ?? selectedDemand?.resource_count ?? existing?.resource_count ?? 1,
        desired_active_days: existing?.desired_active_days ?? defaultActiveDays(selectedDemand, selectedLine),
        note: "",
        selected: false,
        inheritance_contract_version: 1,
        confirmation_provenance: existing?.confirmation_provenance ?? "MASTER",
        proposed_resource_provenance: existing?.proposed_resource_provenance ?? "MASTER",
        resource_count_provenance: "MASTER",
        same_as_root_period_id: null,
        same_as_state: null,
      },
    ]);
    setDirty(true);
    setNotice(null);
  }

  async function savePeriods() {
    if (!canEdit || !selectedDemand || saving) return;
    const validation = validatePeriods(periods);
    if (validation) {
      setError(validation);
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: DemandPeriodWrite[] = periods.map(({
        selected: _selected,
        proposed_resource_id: _proposedResourceId,
        inheritance_contract_version: _contractVersion,
        confirmation_provenance: _confirmationProvenance,
        proposed_resource_provenance: _resourceProvenance,
        resource_count_provenance: _countProvenance,
        same_as_root_period_id: _sameAsRootPeriodId,
        same_as_state: _sameAsState,
        ...row
      }) => ({
        ...row,
        proposed_resource: row.proposed_resource_mode === "EXPLICIT" ? row.proposed_resource : null,
        same_as_period_id: row.proposed_resource_mode === "SAME_AS_PERIOD" ? row.same_as_period_id : null,
        resource_count: selectedLine ? 1 : row.resource_count,
        alternative_group: row.kind === "ALTERNATIVE" ? row.alternative_group?.trim() || null : null,
        note: row.note.trim(),
      }));
      const result = selectedLine
        ? await replaceDemandLinePeriods(selectedDemand.number, selectedLine.line_id, payload, selectedDemand.version)
        : await replaceDemandPeriods(selectedDemand.number, payload, selectedDemand.version);
      const refreshed = selectedLine
        ? await getDemandLinePeriods(selectedDemand.number, selectedLine.line_id, undefined, viewScope)
        : await getDemandPeriods(selectedDemand.number, undefined, viewScope);
      if (onChanged) {
        await onChanged();
      } else {
        const demandRows = demandNumber
          ? [await getDemand(demandNumber, undefined, viewScope)]
          : await getDemands();
        setDemands(demandRows);
      }
      setPeriods(refreshed.map(fromRead));
      setDirty(false);
      setNotice(
        result.reapproval_required
          ? "Périodes enregistrées. L'enveloppe ayant changé, la demande doit être approuvée de nouveau; le plan approuvé précédent reste inchangé jusque-là."
          : "Périodes enregistrées.",
      );
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  async function chooseAlternative(group: string, periodId: string) {
    if (!canEdit || !selectedDemand || saving || dirty) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      if (selectedLine) {
        await selectDemandLineAlternative(
          selectedDemand.number,
          selectedLine.line_id,
          group,
          periodId,
        );
      } else {
        await selectDemandAlternative(selectedDemand.number, group, periodId);
      }
      const refreshed = selectedLine
        ? await getDemandLinePeriods(selectedDemand.number, selectedLine.line_id, undefined, viewScope)
        : await getDemandPeriods(selectedDemand.number, undefined, viewScope);
      if (onChanged) {
        await onChanged();
      } else {
        const demandRows = demandNumber
          ? [await getDemand(demandNumber, undefined, viewScope)]
          : await getDemands();
        setDemands(demandRows);
      }
      setPeriods(refreshed.map(fromRead));
      setNotice(`Option ${periodId} retenue pour ${group}. Les autres options du groupe restent alternatives et ne sont pas matérialisées en parallèle.`);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="demand-periods-page">
      {!embedded && (
        <div className="page-heading periods-heading">
          <div>
            <span className="eyebrow">Demandes</span>
            <h1>Périodes de travail</h1>
            <p>Répartis un besoin sur plusieurs périodes ou définis plusieurs fenêtres possibles sans dupliquer le budget.</p>
          </div>
        </div>
      )}

      {error && <div className="error-panel"><strong>Action impossible.</strong><span>{error}</span></div>}
      {notice && <div className="demand-notice" role="status">{notice}</div>}

      {!embedded && <div className="period-demand-picker">
        <label>
          <span>Demande</span>
          <select
            value={selectedNumber}
            disabled={loading || saving || dirty}
            onChange={(event) => {
              setSelectedNumber(event.target.value);
              setSelectedLineId("");
            }}
          >
            <option value="">Sélectionner une demande…</option>
            {demands.map((demand) => (
              <option value={demand.number} key={demand.number}>{demand.number} — {demand.project_number || "Projet"} — {demand.status}</option>
            ))}
          </select>
        </label>
        {selectedDemand?.line_mode && (
          <label>
            <span>Ligne de demande</span>
            <select
              aria-label="Ligne de demande"
              value={selectedLineId}
              disabled={loading || saving || dirty}
              onChange={(event) => setSelectedLineId(event.target.value)}
            >
              {selectedDemand.lines.filter((line) => line.active).map((line) => (
                <option value={line.line_id} key={line.line_id}>
                  Ligne {line.position + 1} — {line.description || line.required_resource_class || line.task_code || "Besoin"}
                </option>
              ))}
            </select>
          </label>
        )}
        {dirty && <span className="period-dirty-warning">Enregistre ou recharge avant de changer de demande.</span>}
      </div>}

      {embedded && selectedDemand?.line_mode && (
        <div className="period-demand-picker embedded-line-picker">
          <label>
            <span>Besoin / ligne</span>
            <select
              aria-label="Ligne de demande"
              value={selectedLineId}
              disabled={loading || saving || dirty}
              onChange={(event) => setSelectedLineId(event.target.value)}
            >
              {selectedDemand.lines.filter((line) => line.active).map((line) => (
                <option value={line.line_id} key={line.line_id}>
                  Ligne {line.position + 1} — {line.description || line.required_resource_class || line.task_code || "Besoin"}
                </option>
              ))}
            </select>
          </label>
          {dirty && <span className="period-dirty-warning">Enregistre les périodes avant de changer de besoin.</span>}
        </div>
      )}

      {selectedDemand && (
        <div className="period-demand-summary">
          <div><span>Projet</span><strong>{selectedDemand.project_number} — {selectedDemand.project_name || "Projet"}</strong></div>
          <div><span>Statut</span><strong>{selectedDemand.status}</strong></div>
          <div><span>Portée périodes</span><strong>{selectedLine ? `Ligne ${selectedLine.position + 1}` : "Demande générale"}</strong></div>
          <div><span>Confirmation</span><strong>{selectedLine?.confirmation ?? selectedDemand.confirmation ?? "Confirmée"}</strong></div>
          <div><span>Heures</span><strong>{selectedLine?.estimated_hours ?? selectedDemand.estimated_hours ?? "—"} h totales</strong></div>
          <div><span>Jours</span><strong>{selectedLine?.desired_active_days ?? selectedDemand.estimated_days ?? "—"} jour(s) actif(s)</strong></div>
          <div><span>Ressources</span><strong>{selectedLine?.slot_count ?? selectedDemand.resource_count ?? 1} simultanée(s) · maître</strong></div>
          <div><span>Plage moyen terme</span><strong>{selectedDemand.work_package_name || selectedDemand.work_package_ref || "Aucune"}</strong></div>
        </div>
      )}

      {selectedDemand && (
        <div className="period-toolbar">
          <div>
            <button type="button" className="secondary-button" onClick={addCumulative} disabled={!canEdit || saving || periodLoading || (selectedDemand.line_mode && !selectedLine)}>+ Période cumulative</button>
            <button type="button" className="secondary-button" onClick={addAlternativeGroup} disabled={!canEdit || saving || periodLoading || (selectedDemand.line_mode && !selectedLine)}>+ Groupe alternatif</button>
          </div>
          <button type="button" className="primary-button" onClick={savePeriods} disabled={!canEdit || saving || periodLoading || !dirty}>
            {saving ? "Enregistrement…" : "Enregistrer les périodes"}
          </button>
        </div>
      )}

      {periodLoading && <div className="period-empty">Chargement des périodes…</div>}

      {!periodLoading && selectedDemand && periods.length === 0 && (
        <div className="period-empty">
          <strong>Aucune période détaillée.</strong>
          <span>La demande utilise encore son enveloppe générale. Ajoute une période cumulative ou un groupe alternatif pour la détailler.</span>
        </div>
      )}

      {!periodLoading && cumulative.length > 0 && (
        <section className="period-section">
          <div className="period-section-heading">
            <div><span className="eyebrow">Additionnées</span><h2>Travail en plusieurs périodes</h2></div>
            <p>Ces périodes représentent du travail distinct et peuvent donc toutes contribuer au besoin.</p>
          </div>
          <div className="period-card-grid">
            {cumulative.map((period) => {
              const index = periods.findIndex((row) => row.period_id === period.period_id);
              return (
                <PeriodFields
                  key={period.period_id}
                  period={period}
                  resources={resources}
                  disabled={!canEdit || saving}
                  periods={periods}
                  masterConfirmation={(selectedLine?.confirmation ?? selectedDemand.confirmation) === "Confirmée" ? "Confirmée" : "Tentative"}
                  masterProposedResource={selectedLine?.proposed_resource ?? selectedDemand.proposed_resource ?? null}
                  onChange={(next) => replacePeriod(index, next)}
                  onRemove={() => removePeriod(period.period_id)}
                />
              );
            })}
          </div>
        </section>
      )}

      {!periodLoading && alternativeGroups.length > 0 && (
        <section className="period-section">
          <div className="period-section-heading">
            <div><span className="eyebrow">Une seule fenêtre retenue</span><h2>Travail possible dans l’une de ces fenêtres</h2></div>
            <p>Une seule fenêtre d’un même groupe est retenue. Les possibilités restent exclusives et ne sont jamais additionnées.</p>
          </div>
          <div className="alternative-groups">
            {alternativeGroups.map(([group, rows]) => (
              <article className="alternative-group" key={group}>
                <div className="alternative-group-heading">
                  <div>
                    <strong>{group}</strong>
                    <span>{rows.length} option(s) · {rows.some((row) => row.selected) ? "option sélectionnée" : "aucune option sélectionnée"}</span>
                  </div>
                  <button type="button" className="secondary-button" onClick={() => addAlternativeOption(group)} disabled={!canEdit || saving}>+ Option</button>
                </div>
                {dirty && <div className="alternative-selection-note">Enregistre les définitions avant de sélectionner l'option à matérialiser.</div>}
                <div className="period-card-grid">
                  {rows.map((period) => {
                    const index = periods.findIndex((row) => row.period_id === period.period_id);
                    return (
                      <div className={`alternative-option ${period.selected ? "selected" : ""}`} key={period.period_id}>
                        <PeriodFields
                          period={period}
                          resources={resources}
                          disabled={!canEdit || saving}
                          periods={periods}
                          masterConfirmation={(selectedLine?.confirmation ?? selectedDemand.confirmation) === "Confirmée" ? "Confirmée" : "Tentative"}
                          masterProposedResource={selectedLine?.proposed_resource ?? selectedDemand.proposed_resource ?? null}
                          onChange={(next) => replacePeriod(index, next)}
                          onRemove={() => removePeriod(period.period_id)}
                        />
                        <button
                          type="button"
                          className={period.selected ? "selected-option-button" : "secondary-button"}
                          disabled={!canEdit || saving || dirty || period.selected}
                          onClick={() => chooseAlternative(group, period.period_id)}
                        >
                          {period.selected ? "Option retenue" : "Retenir cette option"}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {!selectedDemand && !loading && <div className="period-empty">Aucune demande disponible.</div>}
    </section>
  );
}
