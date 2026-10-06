from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactOperationalPlanningQueueContractTests(unittest.TestCase):
    def test_planning_page_loads_backend_action_queue(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getPlanningActions", page)
        self.assertIn("<PlanningActionPanel", page)
        self.assertIn("PlanningActionReadModel", api)
        self.assertIn("ResourceRecommendationReadModel", api)
        self.assertIn("/api/v1/planning/actions", api)
        self.assertIn("/resource-recommendations", api)

    def test_panel_restores_nicegui_workflow_without_frontend_eligibility_rules(self) -> None:
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("En attente d’approbation", panel)
        self.assertIn("Travaux à planifier", panel)
        self.assertIn("Trouver une ressource", panel)
        self.assertIn("Compétence correspondante", panel)
        self.assertIn("libres prudentes", panel)
        self.assertIn("await assignSegment", panel)
        self.assertIn('can("manage_planning")', panel)

        self.assertNotIn("availability_hours_for_day", panel)
        self.assertNotIn("score +=", panel)
        self.assertNotIn("prudent_free =", panel)

    def test_planning_can_navigate_to_demands_workspace(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("function openDemands()", app)
        self.assertIn("<PlanningPage onOpenDemands={openDemands} />", app)
        self.assertIn("Voir la demande", panel)


if __name__ == "__main__":
    unittest.main()
