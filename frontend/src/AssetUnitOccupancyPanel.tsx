import { createContext, Fragment, useContext, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type { PlanningSnapshotReadModel } from "./api";
import { resourceDisplayName } from "./resourceLabels";

const UNIT_SECTIONS = [
  { prefix: "GIR", label: "Skyjack" },
  { prefix: "LIF", label: "Chariots élévateurs" },
  { prefix: "MAT", label: "Véhicules" },
  { prefix: "PEP", label: "Pelles et pépines" },
  { prefix: "ROU", label: "Roulottes de chantier" },
  { prefix: "TAY", label: "Taylor Dunn" },
  { prefix: "TRA", label: "Remorques" },
  { prefix: "OTHER", label: "Autres / Non classés" },
] as const;

type UnitPrefix = (typeof UNIT_SECTIONS)[number]["prefix"];

const UnitSectionState = createContext<{
  expandedSections: ReadonlySet<UnitPrefix>;
  toggleSection: (prefix: UnitPrefix) => void;
} | null>(null);

// Planning clears its snapshot during week/scope transitions. Keep disclosure
// state above that boundary, separately for each normal or detached window.
export function AssetUnitOccupancySectionsProvider({ children }: { children: ReactNode }) {
  const [expandedSections, setExpandedSections] = useState<ReadonlySet<UnitPrefix>>(
    () => new Set(),
  );
  function toggleSection(prefix: UnitPrefix) {
    setExpandedSections((previous) => {
      const next = new Set(previous);
      if (next.has(prefix)) next.delete(prefix);
      else next.add(prefix);
      return next;
    });
  }
  return (
    <UnitSectionState.Provider value={{ expandedSections, toggleSection }}>
      {children}
    </UnitSectionState.Provider>
  );
}

export default function AssetUnitOccupancyPanel({
  snapshot,
}: {
  snapshot: PlanningSnapshotReadModel;
}) {
  const [expanded, setExpanded] = useState(true);
  const sectionState = useContext(UnitSectionState);
  const sectionId = useId();
  const typesById = useMemo(
    () => new Map(snapshot.asset_types.map((row) => [row.id, row])),
    [snapshot.asset_types],
  );
  const capacityByAssetDay = useMemo(
    () => new Map(snapshot.asset_capacity.map((row) => [`${row.asset_id}:${row.day}`, row])),
    [snapshot.asset_capacity],
  );
  const days = useMemo(() => {
    const result: string[] = [];
    const cursor = new Date(`${snapshot.start}T12:00:00`);
    const end = new Date(`${snapshot.end}T12:00:00`);
    while (cursor <= end) {
      result.push(cursor.toISOString().slice(0, 10));
      cursor.setDate(cursor.getDate() + 1);
    }
    return result;
  }, [snapshot.start, snapshot.end]);
  const visibleAssets = snapshot.assets.filter(
    (asset) => asset.active && asset.visible_in_unit_occupancy,
  );
  const sections = UNIT_SECTIONS.map((section) => ({
    ...section,
    assets: [] as PlanningSnapshotReadModel["assets"],
  }));
  for (const asset of visibleAssets) {
    const prefix = asset.code?.trim().slice(0, 3).toUpperCase();
    const section = sections.find((candidate) => candidate.prefix === prefix)
      ?? sections[sections.length - 1];
    section.assets.push(asset);
  }

  if (!sectionState) throw new Error("Asset unit occupancy requires its Planning section provider.");
  const { expandedSections, toggleSection } = sectionState;

  return (
    <div className="asset-capacity-panel" data-testid="asset-unit-occupancy-panel">
      <div className="asset-section-heading">
        <div>
          <strong>Occupation par unité</strong>
          <span>1 unité disponible ou occupée par jour — aucune conversion en heures humaines</span>
        </div>
        <button
          type="button"
          className="text-button"
          data-testid="asset-unit-occupancy-toggle"
          aria-expanded={expanded}
          aria-controls="asset-unit-occupancy-content"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Réduire" : "Développer"}
        </button>
      </div>
      {expanded && (
        <div
          className="asset-capacity-scroll"
          id="asset-unit-occupancy-content"
          data-testid="asset-unit-occupancy-content"
        >
          <div
            className="asset-capacity-grid"
            style={{
              gridTemplateColumns: `minmax(180px, 1.2fr) repeat(${days.length}, minmax(92px, 1fr))`,
            }}
          >
            <div className="asset-capacity-header">Actif</div>
            {days.map((day) => (
              <div className="asset-capacity-header" key={day}>{day.slice(5)}</div>
            ))}
            {sections.map(({ prefix, label, assets }) => {
              const sectionExpanded = expandedSections.has(prefix);
              const rowsId = `${sectionId}-${prefix}`;
              return (
                <Fragment key={prefix}>
                  <button
                    type="button"
                    className="asset-capacity-section-toggle"
                    data-testid={`asset-unit-section-${prefix}`}
                    aria-expanded={sectionExpanded}
                    aria-controls={rowsId}
                    aria-label={`${label} — ${assets.length} unité${assets.length === 1 ? "" : "s"}`}
                    onClick={() => toggleSection(prefix)}
                  >
                    <span><span aria-hidden="true">{sectionExpanded ? "▾" : "▸"}</span> {label}</span>
                    <span data-testid="asset-unit-section-count">{assets.length}</span>
                  </button>
                  <div
                    className="asset-capacity-section-rows"
                    id={rowsId}
                    data-testid={`asset-unit-section-rows-${prefix}`}
                    hidden={!sectionExpanded}
                  >
                    {sectionExpanded && assets.map((asset) => {
                      const type = typesById.get(asset.asset_type_id);
                      return (
                        <div className="asset-capacity-row" key={asset.id}>
                          <div className="asset-capacity-identity">
                            <strong>{asset.code} — {asset.label}</strong>
                            <span>{type?.label || asset.asset_type_id}</span>
                          </div>
                          {days.map((day) => {
                            const capacity = capacityByAssetDay.get(`${asset.id}:${day}`);
                            const occupiedUnits = Number(capacity?.occupied_units ?? 0);
                            const visibleOccupations = capacity?.visible_occupations ?? [];
                            const hasHiddenOccupancy = Boolean(capacity?.has_hidden_occupancy);
                            const state = capacity?.unavailable
                              ? "Indisponible"
                              : occupiedUnits > 0
                                ? (
                                  visibleOccupations.length === 0 && hasHiddenOccupancy
                                    ? "Occupé hors périmètre"
                                    : "Occupé"
                                )
                                : "Libre";
                            const stateClass = capacity?.unavailable
                              ? "unavailable"
                              : occupiedUnits > 0
                                ? "occupied"
                                : "free";
                            return (
                              <div
                                className={`asset-capacity-cell is-${stateClass}`}
                                key={day}
                              >
                                <strong>{state}</strong>
                                {visibleOccupations.length > 0 && (
                                  <div className="asset-capacity-occupations">
                                    {visibleOccupations.map((occupation) => (
                                      <div
                                        className="asset-capacity-occupation"
                                        key={occupation.allocation_id}
                                      >
                                        {occupation.project_number && <span>{occupation.project_number}</span>}
                                        {occupation.operator_resource_name && (
                                          <small>{resourceDisplayName(occupation.operator_resource_name)}</small>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                )}
                                {hasHiddenOccupancy
                                  && (visibleOccupations.length > 0 || capacity?.unavailable) && (
                                  <small className="asset-capacity-hidden">
                                    {visibleOccupations.length > 0
                                      ? "+ occupation hors périmètre"
                                      : "Occupation hors périmètre"}
                                  </small>
                                )}
                                {occupiedUnits > 1 && (
                                  <small className="asset-capacity-conflict">
                                    {occupiedUnits} occupations · conflit
                                  </small>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })}
                  </div>
                </Fragment>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
