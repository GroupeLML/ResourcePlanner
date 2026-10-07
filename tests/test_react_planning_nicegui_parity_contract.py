from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactPlanningNiceGuiParityContractTests(unittest.TestCase):
    def test_planning_restores_capacity_and_filter_surfaces(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getPlanningCapacityGrid", page)
        self.assertIn("Seulement avec capacité", page)
        self.assertIn("Toutes les classes", page)
        self.assertIn("Toutes les ressources", page)
        self.assertIn("Heures non placées", page)
        self.assertIn("pending-ghost-card", page)
        self.assertIn("PlanningCapacityGridReadModel", api)
        self.assertIn("/api/v1/planning/capacity-grid", api)

    def test_manual_allocation_is_distinct_from_quick_shift(self) -> None:
        editor = (ROOT / "frontend" / "src" / "ManualAllocationEditor.tsx").read_text(
            encoding="utf-8"
        )
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Créer un quart manuel", editor)
        self.assertIn("Ce n’est pas un Quick Shift", editor)
        self.assertIn("createManualAllocationWithOverallocation", editor)
        self.assertIn("+ Quart manuel", planning)
        self.assertIn("+ Quick Shift", planning)

    def test_locked_shift_lifecycle_is_exposed(self) -> None:
        shift = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "manualOverallocationApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("Revenir à l’automatique", shift)
        self.assertIn("Supprimer le quart manuel", shift)
        self.assertIn("releaseManualAllocation", shift)
        self.assertIn("deleteManualAllocation", shift)
        self.assertIn("/release", api)
        self.assertIn('method: "DELETE"', api)

    def test_action_queue_can_open_segment_directly(self) -> None:
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Modifier le segment", panel)
        self.assertIn("onOpenSegment", panel)
        self.assertIn("onOpenSegment={canManagePlanning ? setEditingSegmentId : undefined}", planning)

    def test_react_does_not_reimplement_availability_engine(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        editor = (ROOT / "frontend" / "src" / "ManualAllocationEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("availability_hours_for_day", page)
        self.assertNotIn("availability_state_for_day", page)
        self.assertNotIn("availability_hours_for_day", editor)

    def test_rebuild_stays_support_only_not_a_planning_button(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        commands = (ROOT / "app" / "server" / "routes_commands.py").read_text(encoding="utf-8")

        self.assertNotIn(">Recalculer<", page)
        self.assertIn('/planning/rebuild', commands)


if __name__ == "__main__":
    unittest.main()
