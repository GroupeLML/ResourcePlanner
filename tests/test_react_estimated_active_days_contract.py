from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactEstimatedActiveDaysContractTests(unittest.TestCase):
    def test_demand_editor_explains_hours_days_and_parallel_resources(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Heures estimées totales", source)
        self.assertIn("Jours actifs souhaités", source)
        self.assertIn("Nombre de ressources simultanées", source)
        self.assertIn("ne multiplie jamais les heures estimées", source)
        self.assertIn("Les jours ne créent pas d'heures", source)
        self.assertIn("Number.isInteger(estimatedDays)", source)
        self.assertIn("inclusiveCalendarDays", source)

    def test_period_editor_carries_active_day_target_without_multiplying_hours(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandPeriodsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("desired_active_days", source)
        self.assertIn("Heures totales", source)
        self.assertNotIn("<span>Ressources simultanées</span>", source)
        self.assertIn("Quantité effective", source)
        self.assertIn("Jours actifs souhaités", source)
        self.assertIn("la quantité de personnes est dérivée du besoin maître", source)
        self.assertIn("Cible de répartition", source)
        self.assertIn("await replaceDemandPeriods(selectedDemand.number, payload, selectedDemand.version)", source)

    def test_segment_view_surfaces_target_and_backend_diagnostic(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandSegmentsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("desired_active_days", source)
        self.assertIn("planned_active_days", source)
        self.assertIn("active_day_target_met", source)
        self.assertIn("active_day_diagnostic", source)
        self.assertIn("jour(s) actif(s) · planifié", source)
        self.assertIn("h totales", source)
        self.assertIn("(parallélisme)", source)


if __name__ == "__main__":
    unittest.main()
