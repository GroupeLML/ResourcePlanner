from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue656ContractTests(unittest.TestCase):
    def test_planning_loads_server_personal_order_but_gates_mutation_controls_on_manage_planning(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getPlanningResourceOrder(controller.signal)", planning)
        self.assertIn("setManualResourceOrder(new Map(Object.entries(personalOrder.positions)))", planning)
        self.assertIn("compareManualResources(left, right, manualResourceOrder)", planning)
        self.assertIn('manualOrder={canManagePlanning && resourceSortMode === "manual" ? {', planning)
        self.assertIn('"/api/v1/planning/resource-order"', api)

    def test_other_sort_modes_remain_independent_from_personal_manual_positions(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('if (mode === "manual")', planning)
        self.assertIn('if (mode === "availability")', planning)
        self.assertIn('resourceDisplayName(left.resource.name).localeCompare', planning)
        self.assertIn("manualResourceOrder", planning)


if __name__ == "__main__":
    unittest.main()
