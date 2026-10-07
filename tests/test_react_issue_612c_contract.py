from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactIssue612CContractTests(unittest.TestCase):
    def test_unified_detail_stays_informational_and_actions_use_backend_projection(self) -> None:
        detail = (FRONTEND / "DemandDetail.tsx").read_text(encoding="utf-8")
        workflow = (FRONTEND / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")

        self.assertIn("showActions={false}", detail)
        self.assertIn("currentWorkflowState?.available_actions", workflow)
        self.assertIn("actionsOnly", workflow)

    def test_planning_popup_opens_exact_demand_and_exposes_canonical_actions(self) -> None:
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        shift = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")

        self.assertIn('import DemandWorkflowPage from "./DemandWorkflowPage"', planning)
        self.assertIn("demandNumber={detailDemandNumber}", planning)
        self.assertIn("actionsOnly", planning)
        self.assertIn("hasUnsavedChanges={detailContextDirty}", planning)
        self.assertIn("onOpenDemand(shift.demand_number!)", shift)
        self.assertIn("demande {shift.demand_number}", shift)
        self.assertIn("setDetailDemandNumber(demandNumber)", planning)

    def test_659c_planning_popup_opens_current_demand_in_demands_workspace(self) -> None:
        app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        shift = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")

        self.assertIn("onOpenDemand={openDemand}", app)
        self.assertIn("setDemandToOpen(number)", app)
        self.assertIn("initialDemandNumber={demandToOpen}", app)
        self.assertIn("onOpenDemand?: (demandNumber: string) => void;", planning)
        self.assertIn("Ouvrir dans Demandes", planning)
        self.assertIn("const demandNumber = detailDemandNumber;", planning)
        self.assertIn("onOpenDemand(demandNumber);", planning)
        self.assertIn("shift.demand_number ? (", shift)

    def test_medium_term_demand_gantt_opens_unified_detail_with_canonical_actions(self) -> None:
        medium = (FRONTEND / "MediumTermPage.tsx").read_text(encoding="utf-8")

        self.assertIn("mt-demand-gantt-bar", medium)
        self.assertIn("onClick={() => onOpenDemand(demandPeriod.demand_number)}", medium)
        self.assertIn("onOpenDemand={setDetailDemandNumber}", medium)
        self.assertIn('import DemandWorkflowPage from "./DemandWorkflowPage"', medium)
        self.assertIn("demandNumber={detailDemandNumber}", medium)
        self.assertIn("actionsOnly", medium)
        self.assertIn("hasUnsavedChanges={detailContextDirty}", medium)


if __name__ == "__main__":
    unittest.main()
