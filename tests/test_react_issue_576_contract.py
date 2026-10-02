from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue576ContractTests(unittest.TestCase):
    def test_segment_editor_uses_canonical_origin_for_autonomous_edits(self) -> None:
        editor = (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('new Set(["QUICK_SHIFT", "AD_HOC"])', editor)
        self.assertIn("isCanonicalAdHocSegment(segment)", editor)
        self.assertIn("const editingCanonicalAdHoc = Boolean(", editor)
        self.assertIn("!effectiveDemandNumber && !editingCanonicalAdHoc", editor)
        self.assertIn("Une demande est requise pour créer un segment.", editor)
        self.assertNotIn(
            'if (!effectiveDemandNumber) {\n      setError("Une demande est requise pour créer ou modifier un segment.")',
            editor,
        )

    def test_ad_hoc_confirmation_is_explicit_and_residual_math_stays_out_of_react(self) -> None:
        editor = (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("!segment.demand_number && isCanonicalAdHocSegment(segment)", editor)
        self.assertIn(
            '!editingCanonicalAdHoc && <option value="inherit">Héritée de la demande</option>',
            editor,
        )
        self.assertIn(
            'confirmation: form.confirmation === "inherit" ? null : form.confirmation',
            editor,
        )
        self.assertNotIn("remaining_hours =", editor)
        self.assertNotIn("planned_hours *", editor)

    def test_quick_shift_creation_marks_confirmation_as_explicit(self) -> None:
        service = (ROOT / "app" / "application" / "quick_shift_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"ConfirmationOverride": True', service)


if __name__ == "__main__":
    unittest.main()
