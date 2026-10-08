from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY_REPOSITORY = ROOT / "app" / "infrastructure" / "sql" / "query_repository.py"
QUERY_MODELS = ROOT / "app" / "application" / "query_models.py"
PAGE = ROOT / "frontend" / "src" / "PlanningPage.tsx"
PANEL = ROOT / "frontend" / "src" / "PlanningActionPanel.tsx"
API = ROOT / "frontend" / "src" / "api.ts"


def test_693_backend_queue_projects_actual_uncovered_residual_and_class() -> None:
    repository = QUERY_REPOSITORY.read_text(encoding="utf-8")
    models = QUERY_MODELS.read_text(encoding="utf-8")

    start = repository.index("    def list_planning_actions(")
    end = repository.index("    def recommend_resources(", start)
    queue = repository[start:end]

    assert "segment.remaining_hours <= 0.01" in queue
    assert "planned_hours=float(segment.remaining_hours)" in queue
    assert "required_resource_class=segment.required_resource_class" in queue
    assert "if segment.resource_name:" not in queue
    assert "required_resource_class: str | None = None" in models


def test_693_react_has_one_side_queue_and_one_class_work_projection() -> None:
    page = PAGE.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")

    assert page.count("<PlanningActionPanel") == 1
    assert page.index('<aside className="pending-panel planning-work-queue">') < page.index("<PlanningActionPanel")
    assert "visibleClassWork" in page
    assert "UnplannedWorkRow" in page
    assert "approved-unplanned-card" in page
    assert "Approuvé · prêt à planifier" in page
    assert "Reliquat {hours(action.planned_hours)} h" in page
    assert "Besoin candidat · en attente d’approbation · non matérialisé" in page
    assert "visibleCandidateRowsByResource" in page
    assert "entry.candidate.proposed_resource_id && visibleCandidateRowsByResource.has(entry.candidate.proposed_resource_id)" in page
    assert "draggable={false}" in page
    assert 'writeSegmentDrag(event.dataTransfer, { kind: "SEGMENT", segment_id: action.segment_id })' in page
    assert "<strong>À approuver</strong>" in panel
    assert "<strong>À planifier</strong>" in panel
    assert "required_resource_class: string | null;" in api
