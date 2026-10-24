from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue764ContractTests(unittest.TestCase):
    def test_react_consumes_backend_holiday_metadata_only(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn('reason_code === "HOLIDAY"', planning)
        self.assertIn('holiday_scope === "GLOBAL"', planning)
        self.assertIn("reason_label", planning)
        self.assertIn("holiday-cell-badge", planning)
        self.assertIn("holiday-header-badge", planning)
        self.assertNotIn("availability_state_for_day", planning)
        self.assertNotIn("ClassesRessources", planning)
        self.assertNotIn('"Jour férié" && resource.resource_class', planning)

    def test_shared_planning_surface_covers_detached_workspace(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("<PlanningPage detached />", app)
        self.assertIn("holiday-cell-badge", planning)
        self.assertIn("holiday-header-badge", planning)


if __name__ == "__main__":
    unittest.main()
