from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactPlanningWindowOverrideContractTests(unittest.TestCase):
    def test_api_and_drag_drop_keep_override_server_authoritative(self) -> None:
        api = (ROOT / "frontend" / "src" / "manualOverallocationApi.ts").read_text(encoding="utf-8")
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        dialog = (ROOT / "frontend" / "src" / "PlanningDropDialog.tsx").read_text(encoding="utf-8")

        self.assertIn("/planning-window-override-move", api)
        self.assertIn("/planning-window-override", api)
        self.assertIn("include_planning_window_override_options: true", page)
        self.assertIn('actionCode === "OVERRIDE_WINDOW_AND_MOVE"', page)
        self.assertIn("overrideAndMovePlanningWindow(", page)
        self.assertIn("requested_window", dialog)
        self.assertIn("Approuvé (preuve immuable)", dialog)
        self.assertIn("Opérationnel actuel", dialog)
        self.assertIn("Opérationnel projeté", dialog)
        self.assertIn("Motif obligatoire", dialog)
        self.assertIn("Je confirme explicitement la dérogation", dialog)
        self.assertIn("Consentement hors horaire — décision distincte", dialog)
        self.assertNotIn("permissions_for_roles", page)
        self.assertNotIn("ROLE_COORDINATOR", page)

    def test_shift_and_segment_editors_expose_explicit_cancelable_override_flows(self) -> None:
        shift = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")
        segment = (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(encoding="utf-8")

        self.assertIn("shift-window-override", shift)
        self.assertIn("evaluateAllocationDrop(", shift)
        self.assertIn("overrideAndMovePlanningWindow(", shift)
        self.assertIn("windowOverrideEvaluation", shift)
        self.assertIn("planning_version_conflict", shift)
        self.assertIn("planning_authorization_revision_conflict", shift)
        self.assertIn("setWindowOverrideEvaluation(null)", shift)

        self.assertIn("segment-window-override", segment)
        self.assertIn('can("override_planning_window")', segment)
        self.assertIn("extendPlanningWindowOverride(", segment)
        self.assertIn("windowOverrideReason", segment)
        self.assertIn("windowOverrideConfirmed", segment)
        self.assertIn("planning_version_conflict", segment)
        self.assertIn("windowOverrideKey.current = null", segment)


if __name__ == "__main__":
    unittest.main()
