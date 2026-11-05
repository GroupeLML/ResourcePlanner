from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactErpSyncOperationsContractTests(unittest.TestCase):
    def test_shell_exposes_operations_only_through_backend_sync_permission(self) -> None:
        app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")

        self.assertIn('permission: "sync_projects"', app)
        self.assertIn('<ErpSyncOperationsPage />', app)
        self.assertIn('can("sync_projects")', app)
        self.assertIn('can("admin_settings")', page)
        self.assertNotIn('roles.includes("ADMIN")', page)
        self.assertNotIn('roles.includes("COORDINATOR")', page)

    def test_operations_page_consumes_backend_projection_without_recomputing_authority(self) -> None:
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "erpSyncOperationsApi.ts").read_text(encoding="utf-8")

        self.assertIn("getErpSyncOperations", page)
        self.assertIn("operations.scheduler_status", page)
        self.assertIn("operations.freshness_status", page)
        self.assertIn("operations.readiness_blockers", page)
        self.assertIn("operations.recovery_state.state", page)
        self.assertIn('"/api/v1/integrations/acumatica/erp-sync"', api)
        self.assertNotIn("scheduler_decision", page)
        self.assertNotIn("freshness_sla", page)

    def test_commands_use_server_actor_and_client_idempotency_only(self) -> None:
        api = (FRONTEND / "erpSyncOperationsApi.ts").read_text(encoding="utf-8")

        self.assertIn('"Idempotency-Key": idempotencyKey', api)
        self.assertIn("createClientId()", api)
        self.assertNotIn("actor_name", api)
        self.assertNotIn('"actor"', api)
        self.assertIn("csrfHeaders()", api)

    def test_review_scope_and_compensation_confirmation_are_backend_driven(self) -> None:
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "erpSyncOperationsApi.ts").read_text(encoding="utf-8")

        self.assertIn("review.allowed_exception_scopes", page)
        self.assertIn("preview.preview_hash", page)
        self.assertIn("preview?.can_confirm", page)
        self.assertIn("window.confirm", page)
        self.assertIn("/plausibility-review", api)
        self.assertIn("/compensation/preview", api)
        self.assertIn("/compensation/confirm", api)
        self.assertNotIn('threshold_scope: "GLOBAL"', page)


if __name__ == "__main__":
    unittest.main()
