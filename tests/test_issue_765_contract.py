from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue765ContractTests(unittest.TestCase):
    def test_detached_route_reuses_planning_page_and_keeps_auth_shell_out(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("isDetachedPlanningResourcesRoute", app)
        self.assertIn("<PlanningPage detached />", app)
        self.assertIn('className="detached-planning-root"', app)

        self.assertIn("detached?: boolean", planning)
        self.assertIn("Ressources et quarts — ", planning)
        self.assertIn("!detached && snapshot", planning)
        self.assertIn("AssetPlanningPanel", planning)
        self.assertNotIn("DetachedPlanningPage.tsx", app)

    def test_detach_button_serializes_context_and_handles_popup_block(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )
        detached = (ROOT / "frontend" / "src" / "planningDetached.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("Ouvrir dans une fenêtre séparée", planning)
        self.assertIn('window.open(url, "_blank")', planning)
        self.assertIn("Le navigateur a bloqué l’ouverture de la fenêtre", planning)
        for value in (
            "weekStart",
            "scope",
            "search",
            "project",
            "confirmation",
            "classFilter",
            "resourceFilter",
            "onlyWithCapacity",
            "filtersExpanded",
            "resourceSortMode",
        ):
            self.assertIn(value, detached)
        self.assertIn("window.history.replaceState", planning)
        self.assertIn('"/planning/resources-window"', detached)

    def test_cross_window_signal_is_invalidation_only_and_does_not_replace_cas(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )
        detached = (ROOT / "frontend" / "src" / "planningDetached.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn('new BroadcastChannel(PLANNING_CHANNEL)', detached)
        self.assertIn('"planning-invalidated"', detached)
        self.assertIn("planningVersion", detached)
        self.assertIn("channel.close()", detached)
        self.assertNotIn("snapshot:", detached)
        self.assertNotIn("resources:", detached)
        self.assertNotIn("shifts:", detached)
        self.assertIn("subscribePlanningInvalidations", planning)
        self.assertIn("publishPlanningInvalidation", planning)
        self.assertIn("refreshAfterMutation", planning)

        # Existing stale mutation behavior remains intact in the shared Planning
        # controller; remote invalidation only triggers another canonical load.
        self.assertIn("planning_version_conflict", planning)
        self.assertIn("setRefreshKey((value) => value + 1)", planning)

    def test_production_server_serves_only_the_explicit_detached_spa_entry(self) -> None:
        frontend = (ROOT / "app" / "server" / "frontend.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('@app.get("/planning/resources-window"', frontend)
        self.assertIn("@app.get("/"", frontend)
        self.assertNotIn("@app.get("/{path:path}"", frontend)


if __name__ == "__main__":
    unittest.main()
