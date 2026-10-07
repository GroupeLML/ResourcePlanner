from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLANNING_PAGE = ROOT / "frontend" / "src" / "PlanningPage.tsx"
STYLES = ROOT / "frontend" / "src" / "styles.css"


def test_654_candidates_use_backend_windows_and_stable_resource_ids() -> None:
    source = PLANNING_PAGE.read_text(encoding="utf-8")

    assert "load.candidate_windows" in source
    assert "candidate.proposed_resource_id === resource.id" in source
    assert 'className="resource-row unplanned-candidate-row"' in source
    assert "<strong>À planifier</strong>" in source
    assert "Besoin candidat · non matérialisé" in source
    assert "draggable={false}" in source
    assert "<span>Demande {load.demand_number}</span>" in source
    assert "load.proposed_resource === resource.name" not in source


def test_654_week_controls_are_in_one_sticky_surface() -> None:
    source = PLANNING_PAGE.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'className="planning-sticky-controls"' in source
    assert 'aria-label="Navigation par semaine"' in source
    assert 'className="planning-resource-sort"' in source
    assert ".planning-sticky-controls {" in styles
    assert "position: sticky;" in styles
    assert "top: 64px;" in styles
