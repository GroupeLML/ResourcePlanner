from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue535ContractTests(unittest.TestCase):
    def test_simple_need_uses_canonical_searchable_resource_class(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("required_resource_class: string;", source)
        self.assertIn('label="Classe de ressource"', source)
        self.assertIn("<SearchableCombobox", source)
        self.assertIn("task?.resource_class_code", source)
        self.assertIn("historique/inactive", source)
        self.assertIn(
            'required_resource_class: form.required_resource_class || proposed?.resource_class || ""',
            source,
        )

    def test_cards_consume_backend_quick_action_projection_without_status_matrix(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("available_quick_actions", api)
        self.assertIn("const quickActions = demand.available_quick_actions ?? [];", source)
        self.assertIn("<article className={`demand-card", source)
        self.assertIn('className="demand-card-select"', source)
        self.assertIn('className="demand-card-quick-actions"', source)
        self.assertNotIn('if (demand.status === "Brouillon")', source)

    def test_workflow_surfaces_actor_eligible_approval_and_real_cancellation_at_top(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")
        primary = source.index('data-testid="primary-demand-actions"')
        state_grid = source.index('className="workflow-state-grid"')

        self.assertLess(primary, state_grid)
        self.assertIn('const canQuickApprove = actions.includes("approve")', source)
        self.assertIn("actorApprovalLines.length > 0", source)
        self.assertIn('data-testid="approval-action"', source[primary:state_grid])
        self.assertIn('actions.includes("request-cancellation")', source[primary:state_grid])
        self.assertIn('data-testid="cancellation-request-panel"', source[primary:state_grid])
        self.assertIn("hasUnsavedChanges", source[primary:state_grid])


if __name__ == "__main__":
    unittest.main()
