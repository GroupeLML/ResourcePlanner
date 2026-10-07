from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DemandHistoryFrontendContractTests(unittest.TestCase):
    def test_api_reads_canonical_history_endpoint(self) -> None:
        source = (ROOT / "frontend/src/demandHistoryApi.ts").read_text(encoding="utf-8")
        self.assertIn("/api/v1/demands/${encodeURIComponent(number)}/history", source)
        self.assertIn("DemandHistoryReadModel", source)
        self.assertNotIn("resource_id", source)

    def test_page_uses_backend_events_and_exposes_timeline_fields(self) -> None:
        source = (ROOT / "frontend/src/DemandHistoryPage.tsx").read_text(encoding="utf-8")
        self.assertIn("getDemandHistory", source)
        self.assertIn("previous_status", source)
        self.assertIn("actor_name", source)
        self.assertIn("occurred_at", source)
        self.assertIn("Acteur non enregistré", source)
        self.assertNotIn("updated_at", source)

    def test_unified_detail_exposes_history_without_an_independent_selector(self) -> None:
        workspace = (ROOT / "frontend/src/DemandsWorkspace.tsx").read_text(encoding="utf-8")
        detail = (ROOT / "frontend/src/DemandDetail.tsx").read_text(encoding="utf-8")
        history = (ROOT / "frontend/src/DemandHistoryPage.tsx").read_text(encoding="utf-8")

        self.assertNotIn('"history"', workspace)
        self.assertNotIn("<DemandHistoryPage />", workspace)
        self.assertIn("<DemandHistoryPage demandNumber={demandNumber} viewScope={viewScope} embedded refreshToken={refreshKey} />", detail)
        self.assertIn("demandNumber?: string", history)
        self.assertIn("refreshToken?: number", history)


if __name__ == "__main__":
    unittest.main()
