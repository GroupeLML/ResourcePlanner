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
        self.assertIn('can("admin_settings")', panel)
        self.assertIn("setAssetApprover", panel)
        self.assertIn("approver_user_ids", api)
        self.assertIn("approver_candidates", api)
        self.assertIn("/approvers/", api)

    def test_planning_keeps_humans_first_and_uses_shift_owned_asset_projection(self) -> None:
        source = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        editor = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")

        human = source.index('<div className="planning-layout">')
        asset_panel = source.index("<AssetPlanningPanel", human)
        self.assertLess(human, asset_panel)
        self.assertIn("Ressources et quarts", source[human:asset_panel])
        self.assertIn("Demandes en attente", source[human:asset_panel])

        self.assertIn("asset_assignment: ShiftAssetReservationReadModel | null", api)
        self.assertIn("related_asset_reservations: ShiftAssetReservationReadModel[]", api)
        self.assertIn("asset_actions: ShiftAssetActionsReadModel | null", api)
        self.assertIn("asset_diagnostics: string[]", api)

        self.assertIn("shift.asset_assignment", source)
        self.assertIn('aria-label={asset ? "Actif affecté au quart"', source)
        self.assertNotIn("snapshot.asset_allocations.forEach", source)
        self.assertNotIn("allocation.operator_resource_id !== shift.resource_id", source)
        self.assertNotIn("requirement.demand_number !== shift.demand_number", source)

        self.assertIn("Réservations liées à la demande", editor)
        self.assertIn("shift.related_asset_reservations", editor)
        self.assertIn("shift.asset_actions?.change.allowed", editor)
        self.assertIn("shift.asset_actions?.release.allowed", editor)
        self.assertIn("L’actif associé restera sur le quart source.", editor)
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

    def test_request_asset_dates_are_explicit_and_distinct_from_window(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("Fenêtre autorisée", panel)
        self.assertIn("Budget d’usage", panel)
        self.assertIn("Dates réservées", panel)
        self.assertIn("Date début réelle", panel)
        self.assertIn("Date fin réelle", panel)
        self.assertIn("allocation_start_date ?? requirement.start_date", panel)
        self.assertIn("allocation_end_date ?? requirement.start_date", panel)
        self.assertIn('startDate ?? "none"', panel)
        self.assertIn('endDate ?? "none"', panel)
        self.assertNotIn("start_date: assetId ? requirement.start_date : null", panel)
        self.assertIn("Date début réelle", dialog)
        self.assertIn("Date fin réelle", dialog)
        self.assertIn("start_date: mode === \"release\" ? null : reservationStart", dialog)
        self.assertIn("end_date: mode === \"release\" ? null : reservationEnd", dialog)

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
