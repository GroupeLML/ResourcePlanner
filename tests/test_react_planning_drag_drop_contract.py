from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactPlanningDragDropContractTests(unittest.TestCase):
    def test_drag_payloads_keep_shift_and_segment_intents_distinct(self) -> None:
        drag = (ROOT / "frontend" / "src" / "planningDragDrop.ts").read_text(encoding="utf-8")

        self.assertIn("application/x-resourceplanner-shift", drag)
        self.assertIn("application/x-resourceplanner-segment", drag)
        self.assertIn('kind: "SHIFT"', drag)
        self.assertIn('kind: "SEGMENT"', drag)
        self.assertIn("writeShiftDrag", drag)
        self.assertIn("writeSegmentDrag", drag)

    def test_shift_drop_is_evaluated_before_any_write_and_keeps_backend_authority(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "PlanningDropDialog.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "manualOverallocationApi.ts").read_text(encoding="utf-8")

        self.assertIn("evaluateAllocationDrop(payload.allocation_id", page)
        self.assertIn("<PlanningDropDialog", page)
        self.assertIn("evaluation.actions.filter", dialog)
        self.assertIn("Action refusée par le backend", dialog)
        self.assertIn("/evaluate-drop", api)
        self.assertIn("/extend-and-move", api)
        self.assertIn("/propose-window-extension", api)
        self.assertIn("planning-drop-day", page)
        self.assertIn("writeShiftDrag", page)
        self.assertIn("setRefreshKey", page)
        self.assertIn("action.auto_execute", page)
        self.assertIn("confirm_window_extension: false", page)
        self.assertNotIn("setSnapshot((current)", page)

    def test_context_dialog_reuses_existing_commands_and_preserves_idempotent_retries(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "PlanningDropDialog.tsx").read_text(encoding="utf-8")

        self.assertIn('actionCode === "MOVE"', page)
        self.assertIn('actionCode === "SPLIT"', page)
        self.assertIn('actionCode === "DUPLICATE"', page)
        self.assertIn('actionCode === "EXTEND_AND_MOVE"', page)
        self.assertIn('actionCode === "PROPOSE_WINDOW_EXTENSION"', page)
        self.assertIn("moveAllocation(current.payload.allocation_id", page)
        self.assertIn("splitAllocationAtomic(", page)
        self.assertIn("duplicateAllocationAtomic(", page)
        self.assertIn("extendAndMoveAllocationAtomic(", page)
        self.assertIn("proposeAllocationWindowExtension(", page)
        self.assertIn("actionKeys: dropActionKeys(evaluation)", page)
        self.assertIn("la même clé d’idempotence sera réutilisée", page)
        self.assertIn("Aucun quart n’a été déplacé", page)
        self.assertIn('data-drop-action={action.code}', dialog)

    def test_context_dialog_surfaces_backend_impacts_warnings_and_explicit_cancel(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "PlanningDropDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("evaluation.current_window", dialog)
        self.assertIn("evaluation.proposed_window", dialog)
        self.assertIn("evaluation.authorization_decision", dialog)
        self.assertIn("evaluation.warnings.map", dialog)
        self.assertIn("evaluation.projected_excess_hours", dialog)
        self.assertIn("Autoriser explicitement le quart hors horaire", dialog)
        self.assertIn("Décision de surallocation requise", dialog)
        self.assertIn("Annuler", dialog)
        self.assertIn("+ Quart manuel", page)
        self.assertIn("+ Quick Shift", page)
        self.assertNotIn("compare_approval_envelopes", page)
        self.assertNotIn("approval_revision_id ===", page)

    def test_unassigned_segment_can_be_dragged_to_resource_but_buttons_remain_fallback(self) -> None:
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(encoding="utf-8")
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("writeSegmentDrag", panel)
        self.assertIn("Glisser pour définir la cible automatique", panel)
        self.assertIn("Trouver une ressource", panel)
        self.assertIn("Modifier le segment", panel)
        self.assertIn("assignSegment(payload.segment_id", page)
        self.assertIn("planning-drop-resource", page)
        self.assertIn("alternative clavier", page)

    def test_331e_frontend_keeps_actual_shift_resource_distinct_from_automatic_target(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "PlanningActionPanel.tsx").read_text(encoding="utf-8")
        segments_api = (ROOT / "frontend" / "src" / "segments-api.ts").read_text(encoding="utf-8")
        shift_editor = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")
        manual_editor = (ROOT / "frontend" / "src" / "ManualAllocationEditor.tsx").read_text(encoding="utf-8")
        segment_editor = (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(encoding="utf-8")
        segment_page = (ROOT / "frontend" / "src" / "DemandSegmentsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("resource_id: targetResource.id", page)
        self.assertNotIn("technician: targetResource.name", page)
        self.assertIn("assignSegment(payload.segment_id, targetResource.id)", page)
        self.assertIn("assignSegment(selectedAction.segment_id, candidate.resource_id)", panel)
        self.assertIn("{ resource_id: resourceId }", segments_api)

        self.assertIn("Ressource du quart", shift_editor)
        self.assertIn("resource_id: resourceId", shift_editor)
        self.assertIn("modifie seulement ce quart", shift_editor)

        self.assertIn("automatic_target_resource_id", manual_editor)
        self.assertIn("resource_id: resourceId", manual_editor)
        self.assertIn("s’applique uniquement au quart manuel", manual_editor)

        self.assertIn("Cible automatique du reliquat", segment_editor)
        self.assertIn("automatic_target_resource_id", segment_editor)
        self.assertIn("Cible automatique :", segment_page)
        self.assertIn("Ressources mobilisées :", segment_page)
        self.assertIn("reliquat non couvert", segment_page)

    def test_drag_drop_is_permission_gated_and_backend_rules_are_not_reimplemented(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('can("manage_planning")', page)
        self.assertIn("dragEnabled={canManagePlanning && !dropBusy}", page)
        self.assertIn("action.auto_execute", page)
        self.assertNotIn('evaluation.origin === "QUICK_SHIFT"', page)
        self.assertNotIn("availability_hours_for_day", page)
        self.assertNotIn("manual_overallocation_impact", page)

    def test_backend_has_dedicated_move_route_and_audit_action(self) -> None:
        routes = (ROOT / "app" / "server" / "routes_commands.py").read_text(encoding="utf-8")
        audit = (ROOT / "app" / "infrastructure" / "sql" / "planning_audit.py").read_text(
            encoding="utf-8"
        )
        adapter = (ROOT / "app" / "infrastructure" / "sql" / "command_adapters.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('/allocations/{allocation_id}/move', routes)
        self.assertIn("ManualAllocationMoveCommand", routes)
        self.assertIn('action="Déplacement quart"', audit)
        self.assertIn('shift.source = "MANUAL"', adapter)
        self.assertIn("shift.locked = True", adapter)


if __name__ == "__main__":
    unittest.main()
