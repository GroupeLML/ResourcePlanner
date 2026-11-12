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

        self.assertIn("<strong>À approuver</strong>", panel)
        self.assertIn("<strong>À planifier</strong>", panel)
        self.assertIn("Approuvé · reliquat à couvrir", panel)
        self.assertIn("Trouver une ressource", panel)
        self.assertIn("Compétences requises satisfaites", panel)
        self.assertIn("libres prudentes", panel)
        self.assertIn("await assignSegment", panel)
        self.assertIn('can("manage_planning")', panel)

        self.assertNotIn("availability_hours_for_day", panel)
        self.assertNotIn("score +=", panel)
        self.assertNotIn("prudent_free =", panel)
        self.assertNotIn("recommendations.sort", panel)

    def test_617c_surfaces_backend_ranking_and_requires_explicit_fallback_confirmation(self) -> None:
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("candidate.recommendation_category", panel)
        self.assertIn("candidate.preferred", panel)
        self.assertIn("Attitré", panel)
        self.assertIn("candidate.competency_state", panel)
        self.assertIn("candidate.missing_competency_ids", panel)
        self.assertIn("candidate.capacity_state", panel)
        self.assertIn("candidate.preferred_resource_status", panel)
        self.assertIn("Recommandé", panel)
        self.assertIn("Repli à confirmer", panel)
        self.assertIn("candidate.fallback_requires_confirmation", panel)
        self.assertIn("window.confirm", panel)
        self.assertIn("confirmation explicite", panel)
        self.assertIn("data-recommendation-rank", panel)

    def test_832_work_queue_card_uses_backend_projection_and_stays_draggable(self) -> None:
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("writeSegmentDrag", panel)
        self.assertIn("draggable={draggable}", panel)
        self.assertIn("data-segment-id={action.segment_id || undefined}", panel)
        self.assertNotIn("Glisser pour définir la cible automatique</span>", panel)

        self.assertIn('<strong>{action.project_number || "Projet"}</strong>', panel)
        self.assertNotIn("Demande {action.demand_number}", panel)
        self.assertNotIn("{action.segment_id && <span>{action.segment_id}</span>}", panel)
        self.assertNotIn("Classe : {action.required_resource_class}", panel)
        self.assertNotIn("action.project_name ? ` — ${action.project_name}`", panel)

        self.assertIn("Ressource proposée :", panel)
        self.assertIn("action.requested_assets", panel)
        self.assertIn("action.priority_competencies", panel)
        self.assertIn("data-competency-priority={competency.priority}", panel)
        self.assertIn('action.confirmation === "Confirmée"', panel)
        self.assertIn('action.confirmation === "Tentative"', panel)

        self.assertIn("PlanningActionAssetRequirementReadModel", api)
        self.assertIn("PlanningActionCompetencyReadModel", api)
        self.assertIn("proposed_resource?: string | null", api)
        self.assertIn("requested_assets?: PlanningActionAssetRequirementReadModel[]", api)
        self.assertIn("priority_competencies?: PlanningActionCompetencyReadModel[]", api)

        self.assertIn(".confirmation-confirmed", styles)
        self.assertIn(".confirmation-tentative", styles)
        self.assertIn(".work-queue-proposed-resource", styles)

    def test_planning_can_navigate_to_demands_workspace(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("function openDemands()", app)
        self.assertIn("<PlanningPage onOpenDemands={openDemands} onOpenDemand={openDemand} />", app)
        self.assertIn("Voir la demande", panel)


if __name__ == "__main__":
    unittest.main()
