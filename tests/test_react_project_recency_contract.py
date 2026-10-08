from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "frontend" / "src"


class ProjectRecencySelectorContractTests(unittest.TestCase):
    def test_active_project_selectors_share_the_recency_order(self) -> None:
        files = (
            "QuickShiftEditor.tsx",
            "DemandsPage.tsx",
            "WorkPackageEditor.tsx",
            "MediumTermPage.tsx",
            "DirectAssetReservationForm.tsx",
            "DeliveryPage.tsx",
            "ResourceClassesPanel.tsx",
        )
        for filename in files:
            with self.subTest(filename=filename):
                source = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn("sortProjectsRecentFirst(", source)

    def test_planning_filter_sorts_only_existing_snapshot_options(self) -> None:
        source = (ROOT / "PlanningPage.tsx").read_text(encoding="utf-8")
        self.assertIn("compareProjectNumbersRecentFirst(left[0], right[0])", source)
        self.assertIn("[...snapshot.shifts, ...snapshot.pending_loads, ...actions]", source)

    def test_demand_historical_selected_option_is_preserved(self) -> None:
        source = (ROOT / "DemandsPage.tsx").read_text(encoding="utf-8")
        self.assertIn('historicalIdentity("project", form.project_number)', source)
        self.assertIn("selectedOption={", source)


if __name__ == "__main__":
    unittest.main()
