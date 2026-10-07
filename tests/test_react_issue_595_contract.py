from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue595ContractTests(unittest.TestCase):
    def test_planning_manual_order_uses_persisted_backend_command(self) -> None:
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("reorderPlanningResource", api)
        self.assertIn("/api/v1/planning/resources/", api)
        self.assertIn('"Idempotency-Key": idempotencyKey', api)
        self.assertIn("await reorderPlanningResource(resource.id, direction, createClientId())", page)
        self.assertIn('resourceSortMode === "manual"', page)
        self.assertIn("const orderResources = canManagePlanning ? catalogResources : (snapshot?.resources ?? [])", page)
        self.assertIn("orderResources.forEach", page)
        self.assertIn("manualOrderAvailability.get(resource.id)", page)

    def test_manual_controls_are_keyboard_native_and_other_modes_do_not_mutate_order(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('aria-label={`Monter ${resourceDisplayName(resource.name)}`}', page)
        self.assertIn('aria-label={`Descendre ${resourceDisplayName(resource.name)}`}', page)
        self.assertIn('type="button"', page)
        self.assertIn('<option value="availability">Disponibilité</option>', page)
        self.assertIn('<option value="alphabetical">Alphabétique</option>', page)
        self.assertEqual(page.count("reorderPlanningResource("), 1)


if __name__ == "__main__":
    unittest.main()
