from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue775ContractTests(unittest.TestCase):
    def test_unit_form_owns_continuous_assignment_fields_and_single_save(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(
            encoding="utf-8"
        )
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('className="asset-unit-editor"', panel)
        self.assertIn("ContinuousVehicleAssignmentFields", panel)
        self.assertIn("continuousAssignment.commit", panel)
        self.assertIn("Enregistrer l’unité", panel)
        self.assertNotIn("<AssetContinuousAssignmentEditor", panel)
        self.assertNotIn("Enregistrer l’attribution", panel)
        self.assertNotIn("Enregistrer l’attribution", editor)
        self.assertNotIn("Attribution continue", editor)

    def test_attitre_depends_on_continuous_mode_not_temporal_state(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")
        routes = (ROOT / "app" / "server" / "routes_assets.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("state?.has_continuous_assignment", editor)
        self.assertIn('state.assignment?.resource_period_mode === "CONTINUOUS"', editor)
        self.assertIn('"has_continuous_assignment": primary is not None', routes)
        self.assertIn("evaluate_asset_qualifications", routes)
        self.assertIn('AssetRequirement.resource_period_mode', routes)
        self.assertIn('== ResourcePeriodMode.CONTINUOUS.value', routes)
        self.assertNotIn('effective_state === "ACTIVE"\n    && state?.has_continuous_assignment', editor)

    def test_existing_assignment_restores_canonical_start_date(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("next.assignment.start_date", editor)
        self.assertIn("setEffectiveDateState(", editor)
        self.assertIn("Date de début canonique de l’attribution continue.", editor)

    def test_single_save_uses_existing_744_commands_and_fresh_assignment_version(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(
            encoding="utf-8"
        )
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("getContinuousVehicleUnitState(targetAssetId)", editor)
        self.assertIn("createContinuousVehicleAssignment", editor)
        self.assertIn("transferContinuousVehicleAssignment", editor)
        self.assertIn("revokeContinuousVehicleAssignment", editor)
        self.assertIn("cancelContinuousVehicleAssignment", editor)
        self.assertIn("rescheduleContinuousVehicleAssignment", editor)
        self.assertIn("fresh.planning_version", editor)
        self.assertIn("L’unité a été enregistrée, mais l’attribution continue a échoué", panel)
        self.assertIn("continuousAssignment.loading", panel)
        self.assertIn("loadedAssetId !== assetId", editor)
        self.assertIn("effectiveLoading", editor)

        self.assertNotIn("assigned_resource_id", panel)
        self.assertNotIn("assignment_start_date", panel)
        self.assertNotIn("is_assigned", panel)

    def test_hidden_continuous_assignment_remains_checked_without_resource_leak(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("Ce véhicule est attitré à une ressource hors de votre périmètre.", editor)
        self.assertIn("!fresh.assignment_details_visible", editor)
        self.assertIn("authority.message", editor)
        self.assertNotIn("ApprovalScope", editor)


if __name__ == "__main__":
    unittest.main()
