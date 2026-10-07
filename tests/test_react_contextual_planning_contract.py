from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactContextualPlanningContractTests(unittest.TestCase):
    def test_planning_consumes_dedicated_backend_policy(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        auth_api = (FRONTEND / "auth-api.ts").read_text(encoding="utf-8")
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getCurrentPlanningViewPolicy", page)
        self.assertIn("PlanningScopeSelector", page)
        self.assertNotIn("useViewScope", page)
        self.assertNotIn('from "./ViewScopeSelector"', page)
        self.assertIn("/api/v1/me/planning-policy", auth_api)
        self.assertIn("getPlanningSnapshot(start, end, controller.signal, scope)", page)
        self.assertIn("getPlanningActions(start, end, controller.signal, scope)", page)
        self.assertIn("getPlanningCapacityGrid(start, end, controller.signal, scope)", page)
        self.assertIn("scope?: ViewScope", api)
        self.assertIn('if (scope) params.set("scope", scope)', api)

    def test_identity_is_part_of_planning_load_and_stale_responses_are_ignored(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("planningIdentityKey", page)
        self.assertIn("principal?.local_user_id", page)
        self.assertIn("principal?.issuer", page)
        self.assertIn("principal?.subject", page)
        self.assertIn("activePlanningRequestKeyRef", page)
        self.assertIn("planningIdentityKeyRef.current !== planningIdentityKey", page)
        self.assertIn("return () => controller.abort()", page)

    def test_identity_change_clears_sensitive_planning_state(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        for statement in (
            "setSnapshot(null)",
            "setActions([])",
            "setCapacityGrid(null)",
            "setCatalogResources([])",
            "setEditingShift(null)",
            "setEditingSegmentId(null)",
            "setQuickShiftOpen(false)",
            "setAssetAssignment(null)",
            "setManualAllocationOpen(false)",
            "setDetailDemandNumber(null)",
            "setDropDialog(null)",
        ):
            self.assertIn(statement, page)

    def test_edit_catalog_and_mutation_affordances_require_manage_planning(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        panel = (FRONTEND / "PlanningActionPanel.tsx").read_text(encoding="utf-8")

        self.assertIn("const catalogRequest = canManagePlanning", page)
        self.assertIn("? getResources(true, controller.signal)", page)
        self.assertIn("setCatalogResources(canManagePlanning ? resourceRows : [])", page)
        self.assertIn("onEditShift={canManagePlanning ? setEditingShift : undefined}", page)
        self.assertIn("onOpenSegment={canManagePlanning ? setEditingSegmentId : undefined}", page)
        self.assertIn("assignment && canDragAssignment", panel)

    def test_contextual_capacity_distinguishes_hidden_commitments_from_availability(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('scope === "mine"', page)
        self.assertIn("hors de votre périmètre", page)
        self.assertIn(
            "neutralisent la disponibilité sans exposer leur détail",
            page,
        )

    def test_technician_neighbor_projection_is_read_only_and_explicit(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('shift.source === "SCOPE_NEIGHBOR"', page)
        self.assertIn("détails hors périmètre", page)
        self.assertIn("disabled={!editable}", page)
        self.assertIn("Détails d’actifs hors périmètre", page)

    def test_planning_demand_modal_keeps_planning_scope(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        detail = (FRONTEND / "DemandDetail.tsx").read_text(encoding="utf-8")
        workflow = (FRONTEND / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")

        self.assertIn("viewScope={scope ?? undefined}", page)
        self.assertIn("getDemandDetail(demandNumber, controller.signal, viewScope)", detail)
        self.assertIn("viewScope={viewScope}", detail)
        self.assertIn("getDemandDetail(demandNumber, undefined, viewScope)", workflow)


if __name__ == "__main__":
    unittest.main()
