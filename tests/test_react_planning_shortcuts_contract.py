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
        self.assertIn("Créer un Quick Shift pour ${resourceDisplayName(resource.name)} le ${iso}", planning)
        self.assertIn("setQuickShiftSeed({ resourceId: targetResource.id, day })", planning)
        self.assertIn("initialResourceId={quickShiftSeed?.resourceId ?? null}", planning)
        self.assertIn("defaultDay={quickShiftSeed?.day ?? quickShiftDefaultDay}", planning)
        self.assertIn("Quick Shift créé pour ${resourceDisplayName(resourceName)} le ${day}.", planning)

    def test_shift_asset_shortcut_consumes_canonical_560c_contracts(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(encoding="utf-8")

        self.assertIn("shift.asset_assignment", planning)
        self.assertIn("shift.asset_actions?.assign.allowed", planning)
        self.assertIn("shift.asset_diagnostics", planning)
        self.assertIn("Aucun actif", planning)
        self.assertIn("Assigner un actif", planning)
        self.assertNotIn("assetsByShift", planning)
        self.assertNotIn("assetAssignableShiftIds", planning)
        self.assertNotIn("requirement.demand_number === shift.demand_number", planning)

        self.assertIn("getShiftAssetCandidates(", dialog)
        self.assertIn("setShiftAssetAssignment(", dialog)
        self.assertIn("selectedCandidate?.allowed", dialog)
        self.assertIn("candidate.reason", dialog)
        self.assertIn("planningVersion", dialog)
        self.assertIn("candidateState?.planning_version", dialog)
        self.assertIn("la même clé d’idempotence sera réutilisée", dialog)
        self.assertNotIn("reserveAssetRequirement(", dialog)
        self.assertNotIn("setAssetRequirementOperator(", dialog)
        self.assertNotIn("snapshot.asset_requirements", dialog)

        self.assertIn("/api/v1/assets/shifts/", api)
        self.assertIn("/assignment/candidates", api)
        self.assertIn("/assignment", api)

    def test_shift_actions_are_siblings_and_cell_shortcut_keeps_drop_target(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

        shift_card = planning[planning.index("function ShiftCard({"):planning.index("function PendingGhostCard(")]
        self.assertIn("<article", shift_card)
        self.assertIn('className="shift-card-main"', shift_card)
        self.assertIn('className="shift-asset-action"', shift_card)
        self.assertLess(shift_card.index("</button>"), shift_card.index('className="shift-asset-action"'))

        self.assertIn("planning-drop-day", planning)
        self.assertIn("onDragOver={(event) =>", planning)
        self.assertIn("onDrop={(event) =>", planning)
        self.assertIn(".planning-day-cell:hover .cell-quick-shift-button", styles)
        self.assertIn(".planning-day-cell:focus-within .cell-quick-shift-button", styles)

    def test_shift_asset_candidate_authority_and_stale_state_fail_closed(self) -> None:
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("disabled: !candidate.allowed", dialog)
        self.assertIn("!selectedCandidate?.allowed", dialog)
        self.assertIn('"planning_version_conflict"', dialog)
        self.assertIn("L’état canonique est resynchronisé", dialog)
        self.assertIn("onRefresh();", dialog)
        self.assertIn("setCandidateReloadKey", dialog)
        self.assertNotIn("candidate.available &&", dialog)
        self.assertNotIn("candidate.qualification_state ===", dialog)
        self.assertNotIn("QuickAssetAllocation", dialog)


if __name__ == "__main__":
    unittest.main()
