from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue570ContractTests(unittest.TestCase):
    def test_request_header_reuses_canonical_workflow_controller(self) -> None:
        demands = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(encoding="utf-8")
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(encoding="utf-8")
        workflow = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")

        self.assertIn('import DemandWorkflowPage from "./DemandWorkflowPage"', demands)
        self.assertIn('data-testid="demand-header-actions"', demands)
        header = demands[demands.index('data-testid="demand-header-actions"'):]
        self.assertIn("<DemandWorkflowPage", header)
        self.assertIn("canonicalDetail={selectedDetail}", header)
        self.assertIn("actionsOnly", header)
        self.assertIn("hasUnsavedChanges={editorDirty || contextDirty}", header)
        self.assertIn("onChanged={() => reloadDemand(selectedDemand.number)}", header)

        self.assertIn("showActions={false}", detail)
        self.assertIn("(currentWorkflowState?.available_actions ?? [])", workflow)
        self.assertIn("showActions = true", workflow)
        self.assertNotIn('if (currentDemand.status === "Brouillon")', workflow)
        self.assertNotIn('if (currentDemand.status === "Soumise")', workflow)

    def test_header_preserves_existing_presentation_hierarchy_only(self) -> None:
        workflow = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")

        self.assertIn('actions.includes("submit")', workflow)
        self.assertIn('className="primary-button"', workflow)
        self.assertIn('canQuickApprove', workflow)
        self.assertIn('className="secondary-button workflow-cancel"', workflow)
        self.assertIn('actions.includes("correction")', workflow)
        self.assertIn('actions.includes("request-cancellation")', workflow)
        self.assertIn('actions.includes("accept-cancellation")', workflow)
        self.assertIn('actions.includes("reject-cancellation")', workflow)
        self.assertIn("showActions &&", workflow)

    def test_actions_only_surface_is_responsive_and_keeps_detail_informational(self) -> None:
        demand_css = (ROOT / "frontend" / "src" / "demands.css").read_text(encoding="utf-8")
        workflow_css = (ROOT / "frontend" / "src" / "demand-workflow.css").read_text(encoding="utf-8")
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(encoding="utf-8")

        self.assertIn(".demand-editor-heading-side", demand_css)
        self.assertIn(".demand-editor-statuses", demand_css)
        self.assertIn(".demand-header-actions .workflow-primary-actions", demand_css)
        self.assertIn("justify-content: flex-start;", demand_css)
        self.assertIn(".demand-workflow-page.actions-only .workflow-state-grid", workflow_css)
        self.assertIn(".demand-workflow-page.actions-only .plan-delta-panel", workflow_css)
        self.assertIn("showActions={false}", detail)
        self.assertIn("État, autorisation active et aperçu plan actuel", detail)

    def test_mutations_keep_shared_refresh_and_csrf_contract(self) -> None:
        workflow = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "demandWorkflowApi.ts").read_text(encoding="utf-8")

        self.assertIn("await refreshAfterMutation(result.demand_number)", workflow)
        self.assertIn("await onChanged?.()", workflow)
        self.assertIn("csrfHeaders()", api)
        self.assertIn('credentials: "include"', api)


if __name__ == "__main__":
    unittest.main()
