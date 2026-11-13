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
        self.assertIn("<AssetCatalogPanel competencies={competencies} resources={resources} />", page)
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
        self.assertIn('className="pending-panel planning-work-queue"', source[human:asset_panel])

        self.assertIn("asset_assignment: ShiftAssetReservationReadModel | null", api)
        self.assertIn("related_asset_reservations: ShiftAssetReservationReadModel[]", api)
        self.assertIn("asset_actions: ShiftAssetActionsReadModel | null", api)
        self.assertIn("asset_diagnostics: string[]", api)

        self.assertIn("shift.asset_assignment", source)
        self.assertIn('"Actif affecté au quart"', source)
        self.assertNotIn("snapshot.asset_allocations.forEach", source)
        self.assertNotIn("allocation.operator_resource_id !== shift.resource_id", source)
        self.assertNotIn("requirement.demand_number !== shift.demand_number", source)

        self.assertIn("Réservations liées ou héritées", editor)
        self.assertIn("Réservations liées à la demande", editor)
        self.assertIn("shift.related_asset_reservations", editor)
        self.assertIn("reservation.association_kind", editor)
        self.assertIn("Actif lié", source)
        self.assertIn("visibleAssets", source)
        self.assertIn("reservation.allocation_id", source)
        self.assertIn("designate_project", source)
        self.assertIn("Associer une réservation projet", source)
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")
        asset_api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")
        self.assertIn("DESIGNATE_PROJECT_OPERATOR", dialog)
        self.assertIn('"designate_project"', dialog)
        self.assertIn("candidate.selection_mode === \"DESIGNATE_PROJECT_OPERATOR\"", dialog)
        self.assertIn("Aucune nouvelle réservation physique n’est créée.", dialog)
        self.assertIn("asset_allocation_id", asset_api)
        self.assertIn("shift.asset_actions?.change.allowed", editor)
        self.assertIn("shift.asset_actions?.release.allowed", editor)
        self.assertIn("L’actif associé restera sur le quart source.", editor)
        self.assertNotIn("proposed_asset_id", source)

    def test_asset_capacity_cells_use_backend_scoped_occupation_projection(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx").read_text(encoding="utf-8")
        occupancy = (ROOT / "frontend" / "src" / "AssetUnitOccupancyPanel.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("visible_occupations: AssetAllocationPlanningReadModel[]", api)
        self.assertIn("has_hidden_occupancy: boolean", api)
        self.assertIn("visible_in_unit_occupancy: boolean", api)
        self.assertIn("<AssetUnitOccupancyPanel snapshot={snapshot} />", panel)
        self.assertIn("capacity?.visible_occupations", occupancy)
        self.assertIn("capacity?.has_hidden_occupancy", occupancy)
        self.assertIn("asset.active && asset.visible_in_unit_occupancy", occupancy)
        self.assertIn('data-testid="asset-unit-occupancy-toggle"', occupancy)
        self.assertIn("aria-expanded={expanded}", occupancy)
        self.assertIn("setExpanded((value) => !value)", occupancy)
        self.assertIn("occupation.project_number", occupancy)
        self.assertIn("occupation.operator_resource_name", occupancy)
        self.assertIn("Occupé hors périmètre", occupancy)
        self.assertNotIn("snapshot.asset_allocations.filter", occupancy)

    def test_approved_asset_requirements_group_from_snapshot_and_collapse_locally(self) -> None:
        panel = (ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "snapshot.asset_requirements.filter((row) => !row.asset_id)",
            panel,
        )
        self.assertIn(
            "snapshot.asset_requirements.filter((row) => Boolean(row.asset_id))",
            panel,
        )
        self.assertIn("unreservedRequirements.length", panel)
        self.assertIn("reservedRequirements.length", panel)
        self.assertIn("À réserver ({unreservedRequirements.length})", panel)
        self.assertIn("Réservé ({reservedRequirements.length})", panel)
        self.assertIn('data-testid="approved-asset-requirements-toggle"', panel)
        self.assertIn('data-testid="unreserved-asset-requirements-toggle"', panel)
        self.assertIn('data-testid="reserved-asset-requirements-toggle"', panel)
        self.assertIn("aria-expanded={approvedRequirementsExpanded}", panel)
        self.assertIn("aria-expanded={unreservedExpanded}", panel)
        self.assertIn("aria-expanded={reservedExpanded}", panel)
        self.assertIn(
            "setApprovedRequirementsExpanded((value) => !value)",
            panel,
        )
        self.assertIn("setUnreservedExpanded((value) => !value)", panel)
        self.assertIn("setReservedExpanded((value) => !value)", panel)
        self.assertIn("unreservedRequirements.map((requirement) => (", panel)
        self.assertIn("reservedRequirements.map((requirement) => (", panel)
        self.assertEqual(panel.count("<AssetRequirementCard"), 2)
        self.assertEqual(panel.count("key={requirement.requirement_id}"), 2)
        self.assertIn("Aucun besoin à réserver dans cette fenêtre.", panel)
        self.assertIn("Aucun besoin réservé dans cette fenêtre.", panel)
        self.assertIn("<AssetUnitOccupancyPanel snapshot={snapshot} />", panel)

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
        self.assertIn("start_date: mode === \"release\" || projectReuse ? null : reservationStart", dialog)
        self.assertIn("end_date: mode === \"release\" || projectReuse ? null : reservationEnd", dialog)

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
