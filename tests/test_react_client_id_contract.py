from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"
E2E = ROOT / "frontend" / "e2e" / "v2-local-acceptance.spec.ts"

TRANSVERSE_MUTATION_FILES = (
    "WorkPackageEditor.tsx",
    "ProjectsPage.tsx",
    "QuickShiftEditor.tsx",
    "SegmentEditor.tsx",
    "ShiftEditor.tsx",
    "ManualAllocationEditor.tsx",
    "ResourcesPage.tsx",
    "DemandsPage.tsx",
    "DirectAssetReservationForm.tsx",
    "PlanningPage.tsx",
    "DemandLinesEditor.tsx",
    "DemandPeriodsPage.tsx",
    "AssetPlanningPanel.tsx",
    "ShiftAssetAssignmentDialog.tsx",
    "DemandWorkflowPage.tsx",
)


class ReactClientIdContractTests(unittest.TestCase):
    def test_shared_generator_prefers_native_and_has_compatible_fallbacks(self) -> None:
        helper = (FRONTEND / "clientId.ts").read_text(encoding="utf-8")

        self.assertIn('typeof randomUUID === "function"', helper)
        self.assertIn('typeof getRandomValues === "function"', helper)
        self.assertIn("fallbackSequence", helper)

    def test_user_mutations_reuse_shared_generator_without_direct_random_uuid_calls(self) -> None:
        for filename in TRANSVERSE_MUTATION_FILES:
            with self.subTest(filename=filename):
                source = (FRONTEND / filename).read_text(encoding="utf-8")
                self.assertIn('from "./clientId"', source)
                self.assertIn("createClientId()", source)
                self.assertNotIn("crypto.randomUUID", source)
                self.assertNotIn("globalThis.crypto?.randomUUID", source)

    def test_browser_contract_explicitly_covers_missing_random_uuid(self) -> None:
        acceptance = E2E.read_text(encoding="utf-8")

        self.assertIn("randomUUID: undefined", acceptance)
        self.assertIn("createClientId(cryptoApi)", acceptance)


if __name__ == "__main__":
    unittest.main()
