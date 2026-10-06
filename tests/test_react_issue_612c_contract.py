from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactIssue612CContractTests(unittest.TestCase):
    def test_unified_detail_can_expose_backend_workflow_actions(self) -> None:
        detail = (FRONTEND / "DemandDetail.tsx").read_text(encoding="utf-8")
        workflow = (FRONTEND / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")

        self.assertIn("showWorkflowActions = false", detail)
        self.assertIn("showActions={showWorkflowActions}", detail)
        self.assertIn("currentWorkflowState?.available_actions", workflow)

    def test_planning_popup_opens_exact_demand_and_exposes_actions(self) -> None:
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        shift = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")

        self.assertIn("showWorkflowActions", planning)
        self.assertIn("onOpenDemand(shift.demand_number!)", shift)
        self.assertIn("demande {shift.demand_number}", shift)
        self.assertIn("setDetailDemandNumber(demandNumber)", planning)

    def test_medium_term_demand_chip_opens_unified_actionable_detail(self) -> None:
        medium = (FRONTEND / "MediumTermPage.tsx").read_text(encoding="utf-8")

        self.assertIn("onClick={() => onOpenDemand(demand.number)}", medium)
        self.assertIn("onOpenDemand={setDetailDemandNumber}", medium)
        self.assertIn("demandNumber={detailDemandNumber}", medium)
        self.assertIn("showWorkflowActions", medium)


if __name__ == "__main__":
    unittest.main()
