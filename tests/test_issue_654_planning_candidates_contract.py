from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLANNING_PAGE = ROOT / "frontend" / "src" / "PlanningPage.tsx"
STYLES = ROOT / "frontend" / "src" / "styles.css"
API = ROOT / "frontend" / "src" / "api.ts"


def test_654_candidates_use_backend_windows_and_stable_resource_ids() -> None:
    source = PLANNING_PAGE.read_text(encoding="utf-8")

    assert "load.candidate_windows" in source
    assert "visibleClassWork" in source
    assert 'className="resource-row unplanned-candidate-row"' in source
    assert "<strong>À planifier</strong>" in source
    assert "Besoin candidat · en attente d’approbation · non matérialisé" in source
    assert "draggable={false}" in source
    assert "<span>Demande {load.demand_number}</span>" in source
    assert "load.proposed_resource === resource.name" not in source
    assert "visibleCandidateRowsByResource" in source
    assert "candidates={visibleCandidateRowsByResource.get(resource.id) ?? []}" in source
    assert "dayCandidates.map((entry)" in source



def test_654_candidate_windows_belong_to_pending_load_contract() -> None:
    source = API.read_text(encoding="utf-8")
    pending_start = source.index("export type PendingDemandLoadReadModel = {")
    pending_end = source.index("\n};", pending_start)
    pending_block = source[pending_start:pending_end]
    detail_start = source.index("export type DemandDetailLineReadModel = {")
    detail_end = source.index("\n};", detail_start)
    detail_block = source[detail_start:detail_end]

    assert "candidate_windows: PendingDemandCandidateWindowReadModel[];" in pending_block
    assert "candidate_windows:" not in detail_block


def test_654_week_controls_are_in_one_sticky_surface() -> None:
    source = PLANNING_PAGE.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'className="planning-sticky-controls"' in source
    assert 'aria-label="Navigation par semaine"' in source
    assert 'className="planning-resource-sort"' in source
    assert ".planning-sticky-controls {" in styles
    assert "position: sticky;" in styles
    assert "top: 64px;" in styles
