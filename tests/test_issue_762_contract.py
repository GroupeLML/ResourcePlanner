from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue762FrontendContractTests(unittest.TestCase):
    def test_vehicle_unit_editor_uses_744_commands_without_asset_assignment_fields(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")
        asset_api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(
            encoding="utf-8"
        )
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('assetType.category !== "VEHICLE"', editor)
        self.assertIn("getContinuousVehicleUnitState", editor)
        self.assertIn("createContinuousVehicleAssignment", editor)
        self.assertIn("revokeContinuousVehicleAssignment", editor)
        self.assertIn("transferContinuousVehicleAssignment", editor)
        self.assertIn("cancelContinuousVehicleAssignment", editor)
        self.assertIn("expected_planning_version", asset_api)
        self.assertIn("planning_version", editor)
        self.assertIn("new Map<string, string>()", editor)
        self.assertIn("planning_version_conflict", editor)
        self.assertIn("AssetContinuousAssignmentEditor", panel)

        self.assertNotIn("assigned_resource_id", editor)
        self.assertNotIn("current_resource_id", editor)
        self.assertNotIn("is_assigned", editor)
        self.assertNotIn("updateAsset(", editor)

    def test_unit_profile_distinguishes_active_scheduled_hidden_and_ended_state(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")
        routes = (ROOT / "app" / "server" / "routes_assets.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('État : Actif', editor)
        self.assertIn('État : Planifié pour', editor)
        self.assertIn("Transition planifiée pour", editor)
        self.assertIn("has_ended_history", editor)
        self.assertIn("ressource bénéficiaire n’est pas visible", editor)
        self.assertIn("assignment_details_visible", routes)
        self.assertIn("scheduled_assignment_details_visible", routes)
        self.assertIn("has_ended_history", routes)
        self.assertIn("assignment_authority_capability", routes)


if __name__ == "__main__":
    unittest.main()
