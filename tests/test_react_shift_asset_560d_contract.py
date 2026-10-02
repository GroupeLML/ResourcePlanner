from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactShiftAsset560DContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        self.editor = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")
        self.dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")
        self.api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        self.asset_api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")

    def test_shift_projection_is_the_only_card_asset_authority(self) -> None:
        self.assertIn("asset_assignment: ShiftAssetReservationReadModel | null", self.api)
        self.assertIn("related_asset_reservations: ShiftAssetReservationReadModel[]", self.api)
        self.assertIn("asset_actions: ShiftAssetActionsReadModel | null", self.api)
        self.assertIn("asset_diagnostics: string[]", self.api)

        self.assertIn("const assignment = shift.asset_assignment", self.planning)
        self.assertIn("Actif : <strong>{assignment.asset_code}</strong>", self.planning)
        self.assertIn("Aucun actif", self.planning)
        self.assertIn("shift.asset_actions?.assign.allowed", self.planning)
        self.assertNotIn("assetsByShift", self.planning)
        self.assertNotIn("assetAssignableShiftIds", self.planning)
        self.assertNotIn("snapshot.asset_allocations.forEach", self.planning)

    def test_popup_keeps_owned_and_request_assets_separate(self) -> None:
        self.assertIn("const assetAssignment = shift.asset_assignment", self.editor)
        self.assertIn("shift.related_asset_reservations", self.editor)
        self.assertIn("Actif affecté au quart", self.editor)
        self.assertIn("Réservations liées à la demande", self.editor)
        self.assertIn("Ces réservations REQUEST sont distinctes", self.editor)
        self.assertIn('onAssetAction("assign")', self.editor)
        self.assertIn('onAssetAction("change")', self.editor)
        self.assertIn('onAssetAction("release")', self.editor)

    def test_selector_is_searchable_and_backend_admissibility_remains_authoritative(self) -> None:
        self.assertIn("SearchableCombobox", self.dialog)
        self.assertIn("getShiftAssetCandidates(shift.allocation_id", self.dialog)
        self.assertIn('candidate.available ? "Disponible" : "Indisponible"', self.dialog)
        self.assertIn("qualificationLabel(candidate.qualification_state)", self.dialog)
        self.assertIn("disabled: !candidate.allowed", self.dialog)
        self.assertIn("backendReasonLabel(candidate.reason)", self.dialog)
        self.assertIn("asset_inactive", self.dialog)
        self.assertIn("operator_not_qualified", self.dialog)
        self.assertIn("asset_unavailable", self.dialog)
        self.assertNotIn("getShiftAssetCandidates", self.planning)

    def test_assign_change_release_use_one_canonical_mutation_and_refresh(self) -> None:
        self.assertIn("/shifts/${encodeURIComponent(shiftId)}/assignment/candidates", self.asset_api)
        self.assertIn("/shifts/${encodeURIComponent(shiftId)}/assignment", self.asset_api)
        self.assertIn("setShiftAssetAssignment(", self.dialog)
        self.assertIn("asset_requirement_id: requirementId", self.dialog)
        self.assertIn("candidateState?.planning_version", self.dialog)
        self.assertIn('\"planning_version_conflict\"', self.dialog)
        self.assertIn("la même clé d’idempotence sera réutilisée", self.dialog)
        self.assertIn("onRefresh();", self.dialog)
        self.assertNotIn("reserveAssetRequirement(", self.dialog)
        self.assertNotIn("setAssetRequirementOperator(", self.dialog)

    def test_diagnostics_and_split_duplicate_policy_are_visible_without_client_mutation(self) -> None:
        for code in (
            "asset_assignment_incomplete",
            "asset_inactive",
            "operator_not_qualified",
            "related_request_reservation_ambiguous",
        ):
            self.assertIn(code, self.editor)
        self.assertIn("L’actif associé restera sur le quart source.", self.editor)
        self.assertNotIn("asset_diagnostics.map((diagnostic) => onAssetAction", self.editor)


if __name__ == "__main__":
    unittest.main()
