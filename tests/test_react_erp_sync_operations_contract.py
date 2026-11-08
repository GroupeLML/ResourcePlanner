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

    def test_bootstrap_candidates_and_command_errors_use_distinct_authorities(self) -> None:
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "erpSyncOperationsApi.ts").read_text(encoding="utf-8")

        self.assertIn("getErpSyncBootstrapBaselineCandidates", page)
        self.assertIn("/bootstrap-baseline-candidates", api)
        self.assertIn("bootstrapCandidates.sources", page)
        self.assertIn("source.manifest_id", page)
        self.assertIn("loadError", page)
        self.assertIn("commandError", page)
        self.assertNotIn("setError(null)", page)

    def test_compensation_target_requires_backend_recovery_authority(self) -> None:
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")

        self.assertIn(
            "const retryAttempt = operations?.last_partial_or_failed_attempt ?? null;",
            page,
        )
        self.assertIn(
            "const compensationAttemptId = operations?.recovery_state.attempt_id ?? null;",
            page,
        )
        self.assertNotIn(
            "operations?.recovery_state.attempt_id\n    ?? operations?.last_partial_or_failed_attempt?.id",
            page,
        )

    def test_stale_compensation_refusal_invalidates_local_preview(self) -> None:
        page = (FRONTEND / "ErpSyncOperationsPage.tsx").read_text(encoding="utf-8")
        self.assertIn("STALE_COMPENSATION_CODES", page)
        self.assertIn("erp_sync_recovery_owner_active", page)
        self.assertIn("erp_sync_evidence_configuration_incompatible", page)
        self.assertIn("erp_sync_evidence_protocol_incompatible", page)
        self.assertIn('label === "compensation-confirm"', page)
        self.assertIn("setPreview(null)", page)
        self.assertIn("setCommandError", page)

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
