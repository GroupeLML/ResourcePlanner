from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue760ContractTests(unittest.TestCase):
    def test_shift_asset_dialog_renders_backend_authority_without_scope_logic(self) -> None:
        dialog = (
            ROOT / "frontend" / "src" / "ShiftAssetAssignmentDialog.tsx"
        ).read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "src" / "assetApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("assignment_authority", dialog)
        self.assertIn("assignment_authority.can_assign", dialog)
        self.assertIn("assignment_authority.message", dialog)
        self.assertIn("Admissibilité opérationnelle", dialog)
        self.assertIn(">Attribution</span>", dialog)
        self.assertIn("AUTHORIZED", api)
        self.assertIn("NOT_AUTHORIZED", api)
        self.assertIn("AMBIGUOUS_SCOPE", api)
        self.assertIn("NO_ACTIVE_SCOPE", api)

        self.assertNotIn("ApprovalScope", dialog)
        self.assertNotIn("ROLE_ADMIN", dialog)
        self.assertNotIn("ROLE_COORDINATOR", dialog)
        self.assertNotIn("manage_planning", dialog)

    def test_continuous_vehicle_surface_reuses_safe_backend_message(self) -> None:
        editor = (
            ROOT / "frontend" / "src" / "AssetContinuousAssignmentEditor.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("state.assignment_authority.message", editor)
        self.assertIn("state?.assignment_authority.allowed", editor)
        self.assertNotIn("ApprovalScope", editor)


if __name__ == "__main__":
    unittest.main()
