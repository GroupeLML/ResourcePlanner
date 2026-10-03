from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactAssetQualificationContractTests(unittest.TestCase):
    def test_planning_contract_exposes_qualification_and_operator(self) -> None:
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        asset_api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("qualification_state:", api)
        self.assertIn("required_competency_ids: string[]", api)
        self.assertIn("operator_resource_id: string | null", api)
        self.assertIn("getAssetOperatorCandidates", asset_api)
        self.assertIn("setAssetRequirementOperator", asset_api)
        self.assertIn("/operator-candidates", asset_api)
        self.assertIn("/operator", asset_api)

    def test_planning_panel_surfaces_missing_qualification_and_candidates(self) -> None:
        source = (ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Qualification manquante", source)
        self.assertIn("Opérateur", source)
        self.assertIn("Prérequis :", source)
        self.assertIn("Aucune compétence obligatoire", source)
        self.assertIn("getAssetOperatorCandidates", source)
        self.assertIn("setAssetRequirementOperator", source)


if __name__ == "__main__":
    unittest.main()
