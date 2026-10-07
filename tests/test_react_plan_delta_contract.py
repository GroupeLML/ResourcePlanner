from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactPlanDeltaContractTests(unittest.TestCase):
    def test_client_uses_read_only_plan_delta_endpoint(self) -> None:
        source = (ROOT / "frontend" / "src" / "planDeltaApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("/api/v1/demands/${encodeURIComponent(number)}/plan-delta", source)
        self.assertIn("/api/v1/demands/${encodeURIComponent(number)}/approval-state", source)
        self.assertIn("getDemandApprovalState", source)
        self.assertIn("envelope_decision", source)
        self.assertIn("diagnostics", source)
        self.assertIn("fetch(", source)
        self.assertNotIn('method: "POST"', source)
        self.assertNotIn('method: "PUT"', source)
        self.assertNotIn('method: "DELETE"', source)

    def test_workflow_renders_delta_only_for_submitted_demands(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('normalStatus(currentDemand.status) !== "soumise"', source)
        self.assertIn('normalStatus(currentDemand.status) === "soumise"', source)
        self.assertIn("getDemandPlanDelta(currentDemand.number, viewScope)", source)
        self.assertIn('data-testid="plan-delta-preview"', source)
        self.assertIn('data-testid="approval-state"', source)
        self.assertIn('data-testid="envelope-decision"', source)
        self.assertIn("getDemandApprovalState(currentDemand.number, viewScope)", source)

    def test_workflow_explains_current_to_proposed_changes_before_approval(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Plan actuel → plan proposé", source)
        self.assertIn("Aucun quart n’est modifié avant l’approbation", source)
        self.assertIn("À ajouter", source)
        self.assertIn("À déplacer", source)
        self.assertIn("À modifier", source)
        self.assertIn("À annuler", source)
        self.assertIn("planDelta.add_count", source)
        self.assertIn("planDelta.move_count", source)
        self.assertIn("planDelta.modify_count", source)
        self.assertIn("planDelta.cancel_count", source)
        self.assertIn("Réapprobation requise", source)
        self.assertIn("planDelta.diagnostics", source)
        self.assertIn("Blocages du plan proposé", source)


if __name__ == "__main__":
    unittest.main()
