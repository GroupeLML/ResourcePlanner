from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class FrontendManualOverallocationContractTests(unittest.TestCase):
    def test_api_exposes_both_explicit_decisions_and_structured_context(self) -> None:
        source = (FRONTEND / "manualOverallocationApi.ts").read_text(encoding="utf-8")
        self.assertIn('"KEEP_EXCEPTION" | "INCREASE_PLANNED"', source)
        self.assertIn("OverallocationApiError", source)
        self.assertIn("context", source)
        self.assertIn("overallocation_policy", source)
        self.assertIn("allow_locked_overallocation", source)

    def test_shift_editor_requires_explicit_choice_before_increasing_exception(self) -> None:
        source = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")
        self.assertIn("allocation_overallocation_choice_required", source)
        self.assertIn('save("INCREASE_PLANNED")', source)
        self.assertIn('save("KEEP_EXCEPTION")', source)
        self.assertIn("Augmenter les heures prévues", source)
        self.assertIn("Conserver la dérogation", source)
        self.assertIn("segment_overallocated_hours", source)

    def test_segment_editor_requires_explicit_override_when_budget_is_reduced(self) -> None:
        source = (FRONTEND / "SegmentEditor.tsx").read_text(encoding="utf-8")
        self.assertIn("segment_overallocation_choice_required", source)
        self.assertIn("allowLockedOverallocation", source)
        self.assertIn("Conserver la dérogation", source)
        self.assertIn("Ajuster les heures de base opérationnelles", source)
        self.assertIn("overallocated_hours", source)

    def test_operational_views_surface_requirement_overallocation(self) -> None:
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")
        segments = (FRONTEND / "DemandSegmentsPage.tsx").read_text(encoding="utf-8")
        main = (FRONTEND / "main.tsx").read_text(encoding="utf-8")

        self.assertIn("Surallocation manuelle", planning)
        self.assertIn("segment_overallocated_hours", planning)
        self.assertIn("shift-overallocated", planning)
        self.assertIn("overallocated_hours", segments)
        self.assertIn("segment-card-overallocated", segments)
        self.assertIn('import "./overallocation.css"', main)


if __name__ == "__main__":
    unittest.main()
