from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactIssue593ContractTests(unittest.TestCase):
    def test_shift_navigation_uses_canonical_requirement_identity(self) -> None:
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")
        shift = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn("requirement_id: string | null;", api)
        self.assertIn("Modifier le segment parent", shift)
        self.assertIn(
            "segmentId={shift.requirement_id || shift.segment_id}",
            shift,
        )
        self.assertIn("onOpenDemand={onOpenDemand}", shift)
        self.assertIn("setDetailDemandNumber(demandNumber)", planning)

    def test_segment_parent_demand_action_uses_backend_projection(self) -> None:
        segment = (FRONTEND / "SegmentEditor.tsx").read_text(encoding="utf-8")

        self.assertIn(
            "const effectiveDemandNumber = segment?.demand_number ?? demand?.number ?? null;",
            segment,
        )
        self.assertIn("Ouvrir la demande parente", segment)
        self.assertIn("onOpenDemand(effectiveDemandNumber)", segment)
        self.assertIn("Aucune demande parente — besoin ad hoc.", segment)
        self.assertIn(
            "Aucune demande parente disponible pour ce segment.",
            segment,
        )


if __name__ == "__main__":
    unittest.main()
