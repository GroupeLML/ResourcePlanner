from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactQuickShiftContractTests(unittest.TestCase):
    def test_api_client_uses_authoritative_endpoints_and_idempotency_header(self) -> None:
        source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn('"/api/v1/quick-shifts"', source)
        self.assertIn('"Idempotency-Key"', source)
        self.assertIn('/api/v1/projects?', source)
        self.assertIn('/api/v1/resources?', source)

    def test_dialog_reuses_same_key_for_retry_of_same_payload(self) -> None:
        source = (ROOT / "frontend" / "src" / "QuickShiftEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("previous?.fingerprint === fingerprint", source)
        self.assertIn("retryReceipt.current = { fingerprint, key: idempotencyKey }", source)
        self.assertIn("await createQuickShift(payload, idempotencyKey)", source)
        self.assertIn("if (saving) return", source)
        self.assertIn("getProjects(true", source)
        self.assertIn("getResources(true", source)

    def test_shift_delete_sends_planning_cas_and_idempotency_without_frontend_budget_math(self) -> None:
        api = (ROOT / "frontend" / "src" / "manualOverallocationApi.ts").read_text(
            encoding="utf-8"
        )
        editor = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("expected_planning_version=", api)
        self.assertIn('"Idempotency-Key": idempotencyKey', api)
        self.assertIn("planningVersion", editor)
        self.assertIn("deleteIdempotencyKey.current ?? newAtomicIdempotencyKey()", editor)
        self.assertIn("deleteIdempotencyKey.current = idempotencyKey", editor)
        self.assertIn("deleteIdempotencyKey.current = null", editor)
        self.assertNotIn("planned_hours -", editor)
        self.assertNotIn("segment_planned_hours -", editor)

    def test_planning_exposes_quick_shift_without_fake_request_language(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )
        dialog = (ROOT / "frontend" / "src" / "QuickShiftEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("+ Quick Shift", planning)
        self.assertIn("<QuickShiftEditor", planning)
        self.assertIn("sans créer de demande fictive", dialog)
        self.assertIn('value="Tentative"', dialog)
        self.assertIn('value="Confirmée"', dialog)


if __name__ == "__main__":
    unittest.main()
