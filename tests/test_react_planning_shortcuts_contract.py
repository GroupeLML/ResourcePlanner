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

    def test_shift_asset_shortcut_uses_explicit_requirement_link_and_existing_commands(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("requirement.demand_number === shift.demand_number", planning)
        self.assertIn("requirement.start_date <= shift.work_date", planning)
        self.assertIn("requirement.end_date >= shift.work_date", planning)
        self.assertIn("assetAssignableShiftIds.has(shift.allocation_id)", planning)
        self.assertIn("Assigner un actif", planning)

        self.assertIn("requirement.demand_number === shift.demand_number", dialog)
        self.assertIn("requirement.start_date <= shift.work_date", dialog)
        self.assertIn("requirement.end_date >= shift.work_date", dialog)
        self.assertIn("asset.asset_type_id === requirement.asset_type_id", dialog)
        self.assertIn("reserveAssetRequirement(", dialog)
        self.assertIn("start_date: selectedAssetId ? requirement.start_date : null", dialog)
        self.assertIn("end_date: selectedAssetId ? requirement.end_date : null", dialog)
        self.assertIn("expected_planning_version: snapshot.planning_version", dialog)
        self.assertIn("getAssetOperatorCandidates(requirement.requirement_id)", dialog)
        self.assertIn("candidate.resource_id === shift.resource_id", dialog)
        self.assertIn("setAssetRequirementOperator(", dialog)
        self.assertIn("expected_planning_version: operatorState.planning_version", dialog)
        self.assertIn('"planning_version_conflict"', dialog)
        self.assertIn('"planning_version_stale"', dialog)
        self.assertIn("la même clé d’idempotence sera réutilisée", dialog)

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

    def test_asset_shortcut_never_turns_failed_qualification_into_success(self) -> None:
        dialog = (ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("n’est pas admissible comme opérateur", dialog)
        self.assertIn("Affectation incomplète", dialog)
        self.assertIn("l’opérateur n’a pas pu être associé", dialog)
        self.assertIn("onRefresh();", dialog)
        self.assertNotIn("QuickAssetAllocation", dialog)


if __name__ == "__main__":
    unittest.main()
