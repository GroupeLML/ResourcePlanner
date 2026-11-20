from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class Issue894Phase3ContractTests(unittest.TestCase):
    def test_global_provider_is_inside_identity_lifetime_above_view_scope(self) -> None:
        main = (FRONTEND / "main.tsx").read_text(encoding="utf-8")
        self.assertIn('import EmergencyBroadcastProvider from "./EmergencyBroadcastProvider"', main)
        self.assertIn("<EmergencyBroadcastProvider key={identityKey}>", main)
        self.assertIn("<ViewScopeProvider>", main)
        self.assertLess(
            main.index("<EmergencyBroadcastProvider key={identityKey}>"),
            main.index("<ViewScopeProvider>"),
        )

    def test_runtime_client_uses_no_store_credentials_and_csrf_for_post(self) -> None:
        api = (FRONTEND / "emergencyBroadcastApi.ts").read_text(encoding="utf-8")
        self.assertIn('credentials: "include"', api)
        self.assertIn('cache: "no-store"', api)
        self.assertIn("csrfHeaders()", api)
        self.assertIn("/api/v1/runtime/status", api)
        self.assertIn("/acknowledgment", api)
        self.assertIn("body: JSON.stringify({})", api)
        self.assertNotIn("JSON.stringify({ user_id", api)

    def test_provider_has_completion_polling_backoff_and_identity_guards(self) -> None:
        provider = (FRONTEND / "EmergencyBroadcastProvider.tsx").read_text(encoding="utf-8")
        self.assertIn("POLL_MIN_MS = 4_000", provider)
        self.assertIn("POLL_MAX_MS = 6_000", provider)
        self.assertIn("STATUS_TIMEOUT_MS = 3_000", provider)
        self.assertIn("ACK_TIMEOUT_MS = 10_000", provider)
        self.assertIn("BACKOFF_MS = [10_000, 20_000, 40_000, 60_000]", provider)
        self.assertIn("lifecycleGenerationRef", provider)
        self.assertIn("requestGenerationRef", provider)
        self.assertIn("known_broadcast_id", (FRONTEND / "emergencyBroadcastApi.ts").read_text(encoding="utf-8"))
        self.assertNotIn("setInterval", provider)
        self.assertNotIn("WebSocket", provider)
        self.assertNotIn("EventSource", provider)
        self.assertNotIn("localStorage", provider)

    def test_modal_is_plain_text_blocking_and_accessible(self) -> None:
        provider = (FRONTEND / "EmergencyBroadcastProvider.tsx").read_text(encoding="utf-8")
        self.assertIn('role="alertdialog"', provider)
        self.assertIn('aria-modal="true"', provider)
        self.assertIn("J’ai compris", provider)
        self.assertIn("inertRoot.inert = true", provider)
        self.assertIn('event.key === "Escape"', provider)
        self.assertIn("white-space: pre-wrap", (FRONTEND / "emergency-broadcast.css").read_text(encoding="utf-8"))
        self.assertNotIn("dangerouslySetInnerHTML", provider)

    def test_admin_surface_is_under_existing_admin_settings_configuration(self) -> None:
        app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        configuration = (FRONTEND / "ConfigurationPage.tsx").read_text(encoding="utf-8")
        admin = (FRONTEND / "EmergencyBroadcastAdminPanel.tsx").read_text(encoding="utf-8")
        self.assertIn('permission: "admin_settings"', app)
        self.assertIn('view === "configuration" && can("admin_settings")', app)
        self.assertIn("<EmergencyBroadcastAdminPanel />", configuration)
        self.assertIn("Diffuser maintenant", admin)
        self.assertIn("Terminer la diffusion", admin)
        self.assertIn("presence_complete", admin)
        self.assertIn("getEmergencyBroadcastHistory", admin)
        self.assertIn("getEmergencyAcknowledgmentHistory", admin)
        self.assertIn("crypto.randomUUID()", admin)
        self.assertIn("160", admin)
        self.assertIn("4000", admin)

    def test_e2e_fixture_exposes_only_test_scoped_failure_controls(self) -> None:
        fixture = (ROOT / "tools" / "run_frontend_e2e.py").read_text(encoding="utf-8")
        self.assertIn("/__e2e__/emergency/failure-mode", fixture)
        self.assertIn("/__e2e__/emergency/restart-ephemeral", fixture)
        self.assertIn("controlled E2E emergency status failure", fixture)
        self.assertIn("controlled E2E emergency acknowledgment failure", fixture)


if __name__ == "__main__":
    unittest.main()
