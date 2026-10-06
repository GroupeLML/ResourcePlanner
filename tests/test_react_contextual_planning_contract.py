from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactContextualPlanningContractTests(unittest.TestCase):
    def test_planning_uses_one_shared_scope_for_all_contextual_reads(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("useViewScope", page)
        self.assertIn("ViewScopeSelector", page)
        self.assertIn("getPlanningSnapshot(start, end, controller.signal, scope)", page)
        self.assertIn("getPlanningActions(start, end, controller.signal, scope)", page)
        self.assertIn("getPlanningCapacityGrid(start, end, controller.signal, scope)", page)
        self.assertIn('scope: ViewScope = "global"', api)
        self.assertIn("new URLSearchParams({ start, end, scope })", api)

    def test_scope_resolution_blocks_global_flash_and_stale_context(self) -> None:
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        medium = (FRONTEND / "MediumTermPage.tsx").read_text(encoding="utf-8")

        for source in (planning, medium):
            self.assertIn("if (scopeLoading) return", source)
            self.assertIn("if (scopeError)", source)
            self.assertIn("new AbortController()", source)
            self.assertIn("return () => controller.abort()", source)

        self.assertIn("setSnapshot(null)", planning)
        self.assertIn("setProjection(null)", medium)

    def test_scope_refresh_does_not_close_mutation_editors(self) -> None:
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        medium = (FRONTEND / "MediumTermPage.tsx").read_text(encoding="utf-8")

        self.assertNotIn("setEditingShift(null);\n    setEditingSegmentId(null);", planning)
        self.assertNotIn("setEditor(undefined);\n    setSegmentEditorId(null);", medium)

    def test_planning_keeps_global_resource_catalog_for_mutation_editors(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("getResources(true, controller.signal)", page)
        self.assertIn("setCatalogResources(resourceRows)", page)
        self.assertIn("resources={catalogResources}", page)
        self.assertIn("segments={snapshot.segments}", page)

    def test_contextual_capacity_is_labelled_as_including_outside_commitments(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('scope === "mine"', page)
        self.assertIn(
            "y compris ceux hors de votre périmètre",
            page,
        )

    def test_planning_can_open_the_same_demand_detail_by_demand_number(self) -> None:
        page = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        detail = (FRONTEND / "DemandDetail.tsx").read_text(encoding="utf-8")

        self.assertIn('import DemandDetail from "./DemandDetail"', page)
        self.assertIn("setDetailDemandNumber", page)
        self.assertIn("demandNumber={detailDemandNumber}", page)
        self.assertIn("Ouvrir le détail de la demande", page)
        self.assertIn("getDemandDetail(demandNumber", detail)


if __name__ == "__main__":
    unittest.main()
