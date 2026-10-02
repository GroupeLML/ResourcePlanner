from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactPlanningShortcutContractTests(unittest.TestCase):
    def test_quick_shift_cell_uses_stable_resource_identity_and_date_without_week_clamp(self) -> None:
        editor = (ROOT / "frontend" / "src" / "QuickShiftEditor.tsx").read_text(encoding="utf-8")
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("initialResourceId?: string | null", editor)
        self.assertIn("const [projectId, setProjectId] = useState(\"\")", editor)
        self.assertIn("const [resourceId, setResourceId] = useState(\"\")", editor)
        self.assertIn("sortedResources.find((row) => row.id === initialResourceId)?.id", editor)
        self.assertIn("value={projectId || null}", editor)
        self.assertIn("value={resourceId || null}", editor)
        self.assertIn("value: row.id", editor)
        self.assertIn("project_number: selectedProject.number", editor)
        self.assertIn("technician: selectedResource.name", editor)
        self.assertIn("Sélectionne une date valide.", editor)
        self.assertNotIn('min={weekStart}', editor)
        self.assertNotIn('max={weekEnd}', editor)
        self.assertNotIn("La date doit être comprise dans la semaine affichée.", editor)

        self.assertIn('className="cell-quick-shift-button"', planning)
        self.assertIn("Créer un Quick Shift pour ${resource.name} le ${iso}", planning)
        self.assertIn("setQuickShiftSeed({ resourceId: targetResource.id, day })", planning)
        self.assertIn("initialResourceId={quickShiftSeed?.resourceId ?? null}", planning)
        self.assertIn("defaultDay={quickShiftSeed?.day ?? quickShiftDefaultDay}", planning)
        self.assertIn("Quick Shift créé pour ${resourceName} le ${day}.", planning)

    def test_shift_asset_shortcut_consumes_canonical_shift_projection_and_command(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")
        asset_api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")

        self.assertIn("shift.asset_assignment", planning)
        self.assertIn("shift.asset_actions?.assign.allowed", planning)
        self.assertIn("Aucun actif", planning)
        self.assertIn("Actif : <strong>{assignment.asset_code}</strong>", planning)
        self.assertNotIn("requirement.demand_number === shift.demand_number", planning)
        self.assertNotIn("snapshot.asset_allocations.forEach", planning)
        self.assertNotIn("assetAssignableShiftIds", planning)
        self.assertNotIn("assetsByShift", planning)

        self.assertIn("getShiftAssetCandidates(shift.allocation_id", dialog)
        self.assertIn("setShiftAssetAssignment(", dialog)
        self.assertIn("selectedCandidate?.allowed", dialog)
        self.assertIn("candidateState?.planning_version", dialog)
        self.assertIn("asset_requirement_id: requirementId", dialog)
        self.assertIn('"planning_version_conflict"', dialog)
        self.assertIn('"planning_version_stale"', dialog)
        self.assertIn("la même clé d’idempotence sera réutilisée", dialog)
        self.assertNotIn("reserveAssetRequirement(", dialog)
        self.assertNotIn("setAssetRequirementOperator(", dialog)

        self.assertIn("/assignment/candidates", asset_api)
        self.assertIn("/assignment", asset_api)

    def test_shift_actions_are_siblings_and_cell_shortcut_keeps_drop_target(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

        shift_card = planning[planning.index("function ShiftCard({"):planning.index("function PendingLoadCard(")]
        self.assertIn("<article", shift_card)
        self.assertIn('className="shift-card-main"', shift_card)
        self.assertIn('className="shift-asset-action"', shift_card)
        self.assertLess(shift_card.index("</button>"), shift_card.index('className="shift-asset-action"'))

        self.assertIn("planning-drop-day", planning)
        self.assertIn("onDragOver={(event) =>", planning)
        self.assertIn("onDrop={(event) =>", planning)
        self.assertIn(".planning-day-cell:hover .cell-quick-shift-button", styles)
        self.assertIn(".planning-day-cell:focus-within .cell-quick-shift-button", styles)

    def test_asset_shortcut_never_turns_backend_rejection_into_local_success(self) -> None:
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("disabled: !candidate.allowed", dialog)
        self.assertIn("!selectedCandidate?.allowed", dialog)
        self.assertIn("backendReasonLabel(selectedCandidate.reason)", dialog)
        self.assertIn("setFeedback({ tone: \"error\", message: messageFromError(reason) })", dialog)
        self.assertIn("onRefresh();", dialog)
        self.assertNotIn("QuickAssetAllocation", dialog)

    def test_shift_popup_keeps_owned_asset_and_request_reservations_distinct(self) -> None:
        editor = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")

        self.assertIn("const assetAssignment = shift.asset_assignment", editor)
        self.assertIn("shift.related_asset_reservations", editor)
        self.assertIn("Réservations liées à la demande", editor)
        self.assertIn("Ces réservations REQUEST sont distinctes", editor)
        self.assertIn('onAssetAction("assign")', editor)
        self.assertIn('onAssetAction("change")', editor)
        self.assertIn('onAssetAction("release")', editor)
        self.assertIn("L’actif associé restera sur le quart source.", editor)


if __name__ == "__main__":
    unittest.main()
