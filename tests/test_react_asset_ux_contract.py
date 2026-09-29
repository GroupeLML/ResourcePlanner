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

    def test_planning_keeps_humans_first_and_links_only_actual_allocations(self) -> None:
        source = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        human = source.index('<div className="planning-layout">')
        asset_panel = source.index("<AssetPlanningPanel", human)
        self.assertLess(human, asset_panel)
        self.assertIn("Ressources et quarts", source[human:asset_panel])
        self.assertIn("Demandes en attente", source[human:asset_panel])

        self.assertIn("snapshot.asset_allocations.forEach", source)
        self.assertIn("allocation.operator_resource_id !== shift.resource_id", source)
        self.assertIn("requirement.demand_number !== shift.demand_number", source)
        self.assertIn(
            "allocation.start_date > shift.work_date || allocation.end_date < shift.work_date",
            source,
        )
        self.assertIn("labels.set(allocation.asset_id", source)
        self.assertIn('aria-label="Actifs réservés"', source)
        self.assertNotIn("proposed_asset_id", source)

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
