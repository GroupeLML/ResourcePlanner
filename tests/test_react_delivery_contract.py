from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactDeliveryContractTests(unittest.TestCase):
    def test_shell_routes_delivery_to_real_page_and_loads_styles(self) -> None:
        app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        main = (FRONTEND / "main.tsx").read_text(encoding="utf-8")

        self.assertIn('import DeliveryPage from "./DeliveryPage"', app)
        self.assertIn('{ key: "delivery", label: "Delivery"', app)
        self.assertIn('view === "delivery"', app)
        self.assertIn("<DeliveryPage />", app)
        self.assertIn('import "./delivery.css"', main)

    def test_delivery_page_reads_workpackage_board_and_backend_rollup(self) -> None:
        page = (FRONTEND / "DeliveryPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "deliveryApi.ts").read_text(encoding="utf-8")

        self.assertIn("getProjects(false, controller.signal, scope)", page)
        self.assertIn("getWorkPackages(projectNumber, false, controller.signal, scope)", page)
        self.assertIn("getDeliveryBoardForWorkPackage(workPackageId", page)
        self.assertIn("getDeliverySummary(workPackageId", page)
        self.assertIn("/api/v1/delivery/work-packages/", api)
        self.assertIn("/summary", api)
        self.assertIn("/plan", api)
        self.assertIn("human_reserved_hours", page)
        self.assertIn("forecast_capacity_balance_hours", page)
        self.assertIn("observed_planning_version", page)
        self.assertIn("plan actif/approuvé", page)
        self.assertNotIn("actual_hours", page)
        self.assertNotIn("planned_hours -", page)

    def test_kanban_uses_backend_actions_instead_of_role_checks(self) -> None:
        page = (FRONTEND / "DeliveryPage.tsx").read_text(encoding="utf-8")

        self.assertIn("item.actions", page)
        self.assertIn('planActions.includes("MANAGE_STRUCTURE")', page)
        self.assertIn('"UPDATE_OWN_STORY_STATUS"', page)
        self.assertIn('"UPDATE_OWN_REMAINING_HOURS"', page)
        self.assertIn('"ASSIGN_STORIES"', page)
        self.assertIn('"ESTIMATE_STORIES"', page)
        self.assertIn('"DOCUMENT_OWN_BLOCKAGE"', page)
        self.assertNotIn('roles.includes("PROJECT_MANAGER")', page)
        self.assertNotIn('roles.includes("TECHNICIAN")', page)
        self.assertNotIn('can("manage_planning")', page)

    def test_delivery_mutations_use_delivery_version_and_existing_board_api(self) -> None:
        page = (FRONTEND / "DeliveryPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "deliveryApi.ts").read_text(encoding="utf-8")

        self.assertIn("board.plan.delivery_version", page)
        self.assertIn("expected_delivery_version", api)
        self.assertIn("createDeliveryPlan", page)
        self.assertIn("activateDeliveryPlan", page)
        self.assertIn("archiveDeliveryPlan", page)
        self.assertIn("setDeliveryLead", page)
        self.assertIn("createDeliveryItem", page)
        self.assertIn("updateDeliveryItem", page)
        self.assertIn("documentDeliveryBlockage", page)
        self.assertNotIn("expected_planning_version", api)
        self.assertNotIn("/api/v1/planning", api)

    def test_delivery_version_conflict_reloads_authoritative_board_for_retry(self) -> None:
        page = (FRONTEND / "DeliveryPage.tsx").read_text(encoding="utf-8")

        self.assertIn('reason.code !== "delivery_version_conflict"', page)
        self.assertIn("await reloadDelivery()", page)
        self.assertIn("version courante", page)
        self.assertIn("Réessayez l'action", page)
        self.assertIn("onMutationFailure={handleMutationFailure}", page)
        self.assertIn("const created = await runMutation(", page)
        self.assertIn("if (created) {", page)

    def test_delivery_ui_covers_epics_stories_assignment_estimates_and_remaining(self) -> None:
        page = (FRONTEND / "DeliveryPage.tsx").read_text(encoding="utf-8")

        self.assertIn("Epics", page)
        self.assertIn("Kanban", page)
        self.assertIn("AppUser assigné", page)
        self.assertIn("Estimation (h)", page)
        self.assertIn("Restant (h)", page)
        self.assertIn("Référence", page)
        self.assertIn("Échéance", page)
        self.assertIn("Story sans Epic", page)
        self.assertIn("Story(s) annulée(s)", page)
        self.assertIn("Note de blocage", page)

    def test_delivery_styles_keep_kanban_scrollable_and_responsive(self) -> None:
        css = (FRONTEND / "delivery.css").read_text(encoding="utf-8")

        self.assertIn(".delivery-kanban-scroll", css)
        self.assertIn("overflow-x: auto", css)
        self.assertIn("grid-template-columns: repeat(5", css)
        self.assertIn("@media (max-width: 860px)", css)
        self.assertIn("@media (max-width: 600px)", css)


if __name__ == "__main__":
    unittest.main()
