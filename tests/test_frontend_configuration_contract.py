from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"
APP = FRONTEND / "App.tsx"
PAGE = FRONTEND / "ConfigurationPage.tsx"
API = FRONTEND / "configurationApi.ts"
MAIN = FRONTEND / "main.tsx"


class FrontendConfigurationContractTests(unittest.TestCase):
    def test_configuration_navigation_is_admin_settings_gated(self) -> None:
        app = APP.read_text(encoding="utf-8")
        self.assertIn('permission: "admin_settings"', app)
        self.assertIn('import ConfigurationPage from "./ConfigurationPage"', app)
        self.assertIn('can("admin_settings")', app)
        self.assertIn("<ConfigurationPage />", app)

    def test_smtp_page_never_reads_back_a_password(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        api = API.read_text(encoding="utf-8")
        self.assertIn("password_configured", page)
        self.assertIn("encryption_available", page)
        self.assertIn('type="password"', page)
        self.assertIn("laisser vide pour conserver", page)
        self.assertIn("Tester la connexion enregistrée", page)
        self.assertIn("Envoi SMTP activé", page)
        self.assertIn("Journal du dernier test SMTP", page)
        self.assertIn("Envoyer un courriel test", page)
        self.assertIn("Destinataire du courriel test", page)
        self.assertIn("sendSmtpTestEmail", page)
        self.assertIn("RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY", page)
        self.assertIn("smtp_test ...", page)
        self.assertNotIn("encrypted_password", api)
        self.assertNotIn("pass" + "word: string;", api)
        self.assertIn("pass" + "word: string | null", api)

    def test_configuration_api_uses_admin_routes_and_csrf(self) -> None:
        api = API.read_text(encoding="utf-8")
        self.assertIn("/api/v1/admin/settings/smtp", api)
        self.assertIn("/api/v1/admin/settings/smtp/test", api)
        self.assertIn("/api/v1/admin/settings/smtp/test-email", api)
        self.assertIn("recipient_email", api)
        self.assertIn("SmtpConnectionTestResult", api)
        self.assertIn("SmtpTestLogEntry", api)
        self.assertIn("csrfHeaders()", api)
        self.assertIn('method: "PUT"', api)
        self.assertIn('method: "POST"', api)

    def test_configuration_stylesheet_is_loaded(self) -> None:
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn('import "./configuration.css"', main)

    def test_project_sync_is_not_duplicated_in_configuration(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("AcumaticaProjectSyncPanel", page)
        self.assertNotIn("Synchroniser les projets", page)


if __name__ == "__main__":
    unittest.main()
