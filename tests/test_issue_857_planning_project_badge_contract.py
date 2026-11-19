from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "src" / "PlanningPage.tsx"
STYLES = ROOT / "frontend" / "src" / "styles.css"
E2E = ROOT / "frontend" / "e2e" / "issue-709a.spec.ts"


def test_857_project_badge_reuses_existing_manager_marker_authority() -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert "shift.project_number" in page
    assert "shift.project_manager_color_id" in page
    assert "shift.project_manager_color_label" in page
    assert 'backgroundColor: `hsl(${hue} 70% 34%)`' in page
    assert "managerColorLabel ? `Responsable: ${managerColorLabel}` : null" in page
    assert "shift-pm-dot" not in page
    assert "shift.project_manager ?" not in page


def test_857_scope_neighbor_never_receives_manager_badge_details() -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert "const managerColorId = !scopeNeighbor ? shift.project_manager_color_id : null;" in page
    assert "const managerColorLabel = !scopeNeighbor ? shift.project_manager_color_label : null;" in page
    assert "managerColorId={managerColorId}" in page
    assert "managerLabel={managerColorLabel}" in page


def test_857_badge_is_compact_neutral_and_overflow_safe() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert ".shift-project-badge {" in styles
    assert "border-radius: 999px;" in styles
    assert "max-width: 100%;" in styles
    assert "text-overflow: ellipsis;" in styles
    assert "white-space: nowrap;" in styles
    assert "background: #e7edf5;" in styles
    assert ".shift-pm-dot" not in styles


def test_857_browser_acceptance_keeps_dmo_and_existing_shift_semantics() -> None:
    e2e = E2E.read_text(encoding="utf-8")

    assert '"DMO-2026-0108"' in e2e
    assert 'toHaveText("Confirmée")' in e2e
    assert 'toHaveText("1 h")' in e2e
    assert 'Projet 5110 — Responsable : Jean Tremblay' in e2e
    assert 'toHaveCount(0)' in e2e
    assert "Responsable privé" in e2e
