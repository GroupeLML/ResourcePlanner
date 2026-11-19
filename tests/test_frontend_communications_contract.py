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
        self.assertIn("Télécharger brouillons", page)
        self.assertIn("downloadProjectCommunicationDrafts", page)
        self.assertIn("URL.createObjectURL", page)
        self.assertIn("sans recréer de brouillon M365", page)
        self.assertIn("Envoyer par SMTP", page)
        self.assertIn("Cette action est irréversible", page)
        self.assertIn("delivery.status", page)
        self.assertIn("window.confirm", page)
        self.assertIn("Confirmer communiqué", page)
        self.assertIn("Aucun message n’a été envoyé", page)
        self.assertIn("source.approvable", page)

    def test_727_generator_cc_is_optional_and_bound_to_preview_identity(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        api = API.read_text(encoding="utf-8")
        self.assertIn("M'ajouter en CC dans les communications", page)
        self.assertIn("useState(false)", page)
        self.assertIn("setAddGeneratorCc(false)", page)
        self.assertIn("preview.generator_identity_fingerprint", page)
        self.assertIn("getProjectCommunicationPreview(weekStart, addGeneratorCc)", page)
        self.assertIn("add_generator_cc", api)
        self.assertIn("expected_generator_identity_fingerprint", api)
        self.assertNotIn("generator_email:", api)
        self.assertNotIn("generator_email:", page)

    def test_page_no_longer_edits_legacy_communication_contacts(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("Destinataires gérés dans Utilisateurs", page)
        self.assertNotIn("saveCommunicationContact", page)
        self.assertNotIn("listCommunicationContacts", page)
        self.assertNotIn("Répertoire explicite", page)
        self.assertNotIn("contact-row", page)

    def test_871_html_preview_is_server_generated_and_text_review_remains_authority(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        api = API.read_text(encoding="utf-8")

        self.assertIn("renderProjectCommunicationReviewHtml", api)
        self.assertIn("/api/v1/communications/project-preview-html", api)
        self.assertIn('JSON.stringify({ body })', api)
        self.assertIn("dangerouslySetInnerHTML", page)
        self.assertIn('data-testid="project-html-preview"', page)
        self.assertIn("Actualiser l’aperçu HTML", page)
        self.assertIn("htmlPreviewStale", page)
        self.assertIn("hasStaleHtmlPreview", page)
        self.assertIn("renderProjectCommunicationReviewHtml(current.body)", page)
        self.assertIn("value={draft.body}", page)
        self.assertNotIn("body_html:", page)

    def test_871_preparation_serializes_only_canonical_review_fields(self) -> None:
        page = PAGE.read_text(encoding="utf-8")

        self.assertIn(
            "function projectCommunicationReview(draft: DraftState): ProjectCommunicationReview",
            page,
        )
        self.assertIn("message_key: draft.message_key", page)
        self.assertIn("include: draft.include", page)
        self.assertIn("subject: draft.subject", page)
        self.assertIn("body: draft.body", page)
        self.assertIn("drafts.map(projectCommunicationReview)", page)
        self.assertNotIn(
            "drafts.map(({ source: _source, ...review }) => review)",
            page,
        )

    def test_api_targets_only_project_v2_controlled_routes(self) -> None:
        api = API.read_text(encoding="utf-8")
        self.assertIn("/api/v1/communications/project-preview", api)
        self.assertIn("/api/v1/communications/project-batches", api)
        self.assertIn('"create-drafts"', api)
        self.assertIn("/draft-download", api)
        self.assertIn("response.blob()", api)
        self.assertIn('"send-smtp"', api)
        self.assertIn("CommunicationDelivery", api)
        self.assertIn("body_html", api)
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

    def test_854_sql_audit_uses_business_project_number_with_nontechnical_fallback(self) -> None:
        api = API.read_text(encoding="utf-8")
        page = PAGE.read_text(encoding="utf-8")

        self.assertIn("project_number: string | null", api)
        self.assertIn("message.project_number?.trim()", page)
        self.assertIn("`Projet ${projectNumber}`", page)
        self.assertIn('"Projet non identifié"', page)
        self.assertNotIn(
            "message.message_key ?? message.project_id ?? message.id",
            page,
        )

    def test_stylesheet_is_loaded(self) -> None:
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn('import "./communications.css"', main)


if __name__ == "__main__":
    unittest.main()
