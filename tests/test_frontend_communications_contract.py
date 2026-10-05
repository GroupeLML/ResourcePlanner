from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "App.tsx"
PAGE = ROOT / "frontend" / "src" / "CommunicationsPage.tsx"
API = ROOT / "frontend" / "src" / "communicationsApi.ts"
MAIN = ROOT / "frontend" / "src" / "main.tsx"


class FrontendCommunicationsContractTests(unittest.TestCase):
    def test_navigation_is_permission_gated_and_page_is_wired(self) -> None:
        app = APP.read_text(encoding="utf-8")
        self.assertIn('permission: "manage_communications"', app)
        self.assertIn('import CommunicationsPage from "./CommunicationsPage"', app)
        self.assertIn("<CommunicationsPage />", app)

    def test_page_uses_project_to_cc_review_and_explicit_draft_creation(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("Communication par projet", page)
        self.assertIn("To", page)
        self.assertIn("CC", page)
        self.assertIn("Courriel manquant", page)
        self.assertIn("Blocage", page)
        self.assertIn("Anomalie", page)
        self.assertIn("Préparer le lot", page)
        self.assertIn("Approuver", page)
        self.assertIn("Créer brouillons M365", page)
        self.assertIn("Envoyer par SMTP", page)
        self.assertIn("Cette action est irréversible", page)
        self.assertIn("delivery.status", page)
        self.assertIn("window.confirm", page)
        self.assertIn("Confirmer communiqué", page)
        self.assertIn("Aucun message n’a été envoyé", page)
        self.assertIn("source.approvable", page)

    def test_page_no_longer_edits_legacy_communication_contacts(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("Destinataires gérés dans Utilisateurs", page)
        self.assertNotIn("saveCommunicationContact", page)
        self.assertNotIn("listCommunicationContacts", page)
        self.assertNotIn("Répertoire explicite", page)
        self.assertNotIn("contact-row", page)

    def test_api_targets_only_project_v2_controlled_routes(self) -> None:
        api = API.read_text(encoding="utf-8")
        self.assertIn("/api/v1/communications/project-preview", api)
        self.assertIn("/api/v1/communications/project-batches", api)
        self.assertIn('"create-drafts"', api)
        self.assertIn('"send-smtp"', api)
        self.assertIn("CommunicationDelivery", api)
        self.assertNotIn("/api/v1/communications/contacts", api)
        self.assertNotIn("/api/v1/communications/preview?", api)
        self.assertNotIn("graph.microsoft", api.lower())
        self.assertNotIn("outlook", api.lower())
        self.assertNotIn("/send", api.lower())

    def test_project_contract_exposes_recipients_diagnostics_and_model_version(self) -> None:
        api = API.read_text(encoding="utf-8")
        self.assertIn("to_recipient", api)
        self.assertIn("to_recipients", api)
        self.assertIn("cc_recipients", api)
        self.assertIn("ProjectMessageDiagnostic", api)
        self.assertIn("approvable", api)
        self.assertIn("message_key", api)
        self.assertIn("cc_emails", api)
        self.assertIn("to_emails", api)
        self.assertIn("model_version", api)
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("source.to_recipients", page)
        self.assertIn("message.to_emails", page)
        self.assertIn("co-chargés RP", page)

    def test_stylesheet_is_loaded(self) -> None:
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn('import "./communications.css"', main)


if __name__ == "__main__":
    unittest.main()
