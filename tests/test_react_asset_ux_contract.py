from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactAssetUxContractTests(unittest.TestCase):
    def test_resources_page_exposes_separate_asset_catalog_crud(self) -> None:
        page = (ROOT / "frontend" / "src" / "ResourcesPage.tsx").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")

        self.assertIn('import AssetCatalogPanel from "./AssetCatalogPanel"', page)
        self.assertIn("<AssetCatalogPanel competencies={competencies} />", page)
        self.assertIn("Catalogue des actifs", panel)
        self.assertIn("Nouvelle unité physique", panel)
        self.assertIn("Compétences / permis requis", panel)
        self.assertIn("Ajouter l’indisponibilité", panel)
        self.assertIn('can("manage_planning")', panel)
        self.assertIn("createAssetType", panel)
        self.assertIn("updateAssetType", panel)
        self.assertIn("setAssetTypeActive", panel)
        self.assertIn("setAssetTypeQualification", panel)
        self.assertIn("createAsset", panel)
        self.assertIn("updateAsset", panel)
        self.assertIn("setAssetActive", panel)
        self.assertIn("addAssetUnavailability", panel)
        self.assertIn("removeAssetUnavailability", panel)

        self.assertIn('"/api/v1/assets/types"', api)
        self.assertIn('"/qualification"', api)
        self.assertIn('"/api/v1/assets"', api)
        self.assertIn('"/api/v1/assets/requirements"', api)

    def test_asset_catalog_administers_specific_approval_authority(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")

        self.assertIn("Approbateurs spécifiques", panel)
        self.assertIn("additifs au périmètre", panel)
        self.assertIn('can("manage_resources")', panel)
        self.assertIn("setAssetApprover", panel)
        self.assertIn("approver_user_ids", api)
        self.assertIn("approver_candidates", api)
        self.assertIn("/approvers/", api)

    def test_planning_keeps_humans_first_and_consumes_canonical_shift_asset_projection(self) -> None:
        source = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        human = source.index('<div className="planning-layout">')
        asset_panel = source.index("<AssetPlanningPanel", human)
        self.assertLess(human, asset_panel)
        self.assertIn("Ressources et quarts", source[human:asset_panel])
        self.assertIn("Demandes en attente", source[human:asset_panel])

        self.assertIn("shift.asset_assignment", source)
        self.assertIn("shift.asset_actions?.assign.allowed", source)
        self.assertIn("assignment.asset_code", source)
        self.assertIn("asset_assignment: ShiftAssetReservationReadModel | null", api)
        self.assertIn("related_asset_reservations: ShiftAssetReservationReadModel[]", api)
        self.assertNotIn("snapshot.asset_allocations.forEach", source)
        self.assertNotIn("allocation.operator_resource_id !== shift.resource_id", source)
        self.assertNotIn("requirement.demand_number !== shift.demand_number", source)
        self.assertNotIn("labels.set(allocation.asset_id", source)
        self.assertNotIn("proposed_asset_id", source)

    def test_asset_capacity_cells_use_backend_scoped_occupation_projection(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("visible_occupations: AssetAllocationPlanningReadModel[]", api)
        self.assertIn("has_hidden_occupancy: boolean", api)
        self.assertIn("capacity?.visible_occupations", panel)
        self.assertIn("capacity?.has_hidden_occupancy", panel)
        self.assertIn("occupation.project_number", panel)
        self.assertIn("occupation.operator_resource_name", panel)
        self.assertIn("Occupé hors périmètre", panel)
        self.assertNotIn("snapshot.asset_allocations.filter", panel)

    def test_asset_api_preserves_backend_authority_and_planning_cas(self) -> None:
        api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "AssetCatalogPanel.tsx").read_text(encoding="utf-8")

        self.assertIn("expected_planning_version", api)
        self.assertIn("planning_version_conflict", panel)
        self.assertIn("getAssetCatalog", panel)
        self.assertIn("getAssetPlanningState", panel)
        self.assertNotIn("capacity_units", panel)
        self.assertNotIn("occupied_units", panel)
        self.assertNotIn("remaining_units", panel)


if __name__ == "__main__":
    unittest.main()
