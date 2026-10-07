from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactDemandPeriodsContractTests(unittest.TestCase):
    def test_periods_are_embedded_in_the_unified_demand_detail(self) -> None:
        workspace = (ROOT / "frontend" / "src" / "DemandsWorkspace.tsx").read_text(
            encoding="utf-8"
        )
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn("<DemandsPage />", workspace)
        self.assertNotIn("<DemandPeriodsPage />", workspace)
        self.assertNotIn("Périodes & alternatives", workspace)
        self.assertIn("<DemandPeriodsPage", detail)
        self.assertIn("Options avancées", detail)
        self.assertIn('data-testid="demand-advanced-options"', detail)
        self.assertIn('className="demand-detail-section demand-detail-advanced-options"', detail)
        self.assertNotIn('className="demand-detail-section demand-detail-advanced-options" open', detail)
        self.assertLess(detail.index("Options avancées"), detail.index("<DemandPeriodsPage"))
        self.assertIn("demandNumber={demandNumber}", detail)
        self.assertIn("canonicalDemand={detail.demand}", detail)
        self.assertIn('import "./demand-periods.css"', main)

    def test_api_client_uses_existing_period_and_selection_endpoints(self) -> None:
        source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getDemandPeriods", source)
        self.assertIn("getDemandLinePeriods", source)
        self.assertIn("replaceDemandPeriods", source)
        self.assertIn("replaceDemandLinePeriods", source)
        self.assertIn("selectDemandAlternative", source)
        self.assertIn("selectDemandLineAlternative", source)
        self.assertIn("/periods`,", source)
        self.assertIn("/lines/${encodeURIComponent(lineId)}/periods`,", source)
        self.assertIn("/alternative-groups/${encodeURIComponent(alternativeGroup)}/selection", source)
        self.assertIn("period_id: periodId", source)
        self.assertIn("confirmation_mode:", source)
        self.assertIn("proposed_resource_mode:", source)
        self.assertIn("same_as_period_id:", source)
        self.assertIn("resource_count_provenance:", source)

    def test_editor_models_cumulative_and_exclusive_alternative_periods(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandPeriodsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('kind: "CUMULATIVE"', source)
        self.assertIn('kind: "ALTERNATIVE"', source)
        self.assertIn("nextAlternativeGroup(periods)", source)
        self.assertIn("Le groupe ${group} doit contenir au moins deux options.", source)
        self.assertIn("Travail possible dans l’une de ces fenêtres", source)
        self.assertIn("Les possibilités restent exclusives", source)
        self.assertIn("Retenir cette option", source)
        self.assertIn("Ligne de demande", source)
        self.assertIn("selectedDemand?.line_mode", source)
        self.assertNotIn("<span>Ressources simultanées</span>", source)
        self.assertIn("<span>Quantité effective</span>", source)
        self.assertIn("<span>Mode de confirmation</span>", source)
        self.assertIn('value="INHERIT_MASTER"', source)
        self.assertIn("<span>Mode de ressource</span>", source)
        self.assertIn('value="SAME_AS_PERIOD"', source)
        self.assertIn("<span>Même ressource que</span>", source)
        self.assertIn("candidate.period_id !== period.period_id", source)
        self.assertIn("proposed_resource_id: selected?.id ?? null", source)
        self.assertIn("disabled={!canEdit || saving || dirty || period.selected}", source)

    def test_period_save_preserves_backend_authority_and_reapproval_signal(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandPeriodsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("await replaceDemandPeriods(selectedDemand.number, payload, selectedDemand.version)", source)
        self.assertIn("await replaceDemandLinePeriods(selectedDemand.number, selectedLine.line_id, payload, selectedDemand.version)", source)
        self.assertIn("result.reapproval_required", source)
        self.assertIn("le plan approuvé précédent reste inchangé", source)
        self.assertIn("await selectDemandAlternative(selectedDemand.number, group, periodId)", source)
        self.assertIn("await selectDemandLineAlternative(", source)
        self.assertIn("await onChanged()", source)
        self.assertIn("canonicalDemand", source)
        self.assertIn('row.proposed_resource_mode === "SAME_AS_PERIOD" ? row.same_as_period_id : null', source)
        self.assertIn("resource_count: selectedLine ? 1 : row.resource_count", source)
        self.assertNotIn("projected_hours_without_double_counting", source)


if __name__ == "__main__":
    unittest.main()
