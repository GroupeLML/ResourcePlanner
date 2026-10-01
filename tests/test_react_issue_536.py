from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReactOutsideStandardHoursMoveContractTests(unittest.TestCase):
    def test_checkbox_stays_present_after_successful_reevaluation(self) -> None:
        dialog = (ROOT / "frontend" / "src" / "PlanningDropDialog.tsx").read_text(encoding="utf-8")
        self.assertIn("outsideOptionPresented", dialog)
        self.assertIn("setOutsideOptionPresented(true)", dialog)
        self.assertIn("{outsideOptionPresented && (", dialog)
        self.assertIn("checked={outsideStandardHours}", dialog)
        self.assertIn("await onReevaluate(value)", dialog)

    def test_move_uses_preview_consent_and_version(self) -> None:
        page = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("outside_standard_hours: request.outsideStandardHours", page)
        self.assertIn("expected_planning_version: current.evaluation.planning_version", page)
        self.assertIn("outside_standard_hours: boolean", api)
        self.assertIn("expected_planning_version: number", api)


if __name__ == "__main__":
    unittest.main()
