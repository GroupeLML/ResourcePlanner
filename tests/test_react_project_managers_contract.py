from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactProjectManagerAdminContractTests(unittest.TestCase):
    def test_api_exposes_canonical_manager_read_and_idempotent_mutations(self) -> None:
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("ProjectManagersReadModel", api)
        self.assertIn("EffectiveProjectManagerReadModel", api)
        self.assertIn("ProjectCoManagerMutationResult", api)
        self.assertIn("/managers?", api)
        self.assertIn("/co-managers/", api)
        self.assertIn('"PUT"', api)
        self.assertIn('"DELETE"', api)
        self.assertIn('"Idempotency-Key": idempotencyKey', api)
        self.assertIn("expected_version: expectedVersion", api)
        self.assertIn("getProjectManagerCandidateContacts", api)
        self.assertIn("getBusinessContacts(true, signal, false)", api)

    def test_projects_page_uses_backend_diagnostics_and_never_legacy_manager_fk(self) -> None:
        page = (FRONTEND / "ProjectsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("getProjectManagers", page)
        self.assertIn("Chargé principal ERP", page)
        self.assertIn("Co-chargés RessourcePlanner", page)
        self.assertIn("PROJECT_CO_MANAGER_APP_USER_NOT_LINKED", page)
        self.assertIn("PROJECT_CO_MANAGER_CONTACT_INACTIVE", page)
        self.assertIn("PROJECT_MANAGER_IDENTITY_CONFLICT", page)
        self.assertNotIn("project_manager_contact_id", page)
        self.assertNotIn("getProjectBusinessContacts", page)
        self.assertNotIn("projectContactLink", page)

    def test_projects_page_supports_permission_candidates_and_concurrency(self) -> None:
        page = (FRONTEND / "ProjectsPage.tsx").read_text(encoding="utf-8")

        self.assertIn('can("manage_resources")', page)
        self.assertIn('type="search"', page)
        self.assertIn("coManagerCandidates", page)
        self.assertIn("contact.active", page)
        self.assertIn("primaryContactId", page)
        self.assertIn("nominated.has(contact.id)", page)
        self.assertIn("project_co_managers_version_conflict", page)
        self.assertIn("La liste des co-chargés a changé", page)
        self.assertIn("await reloadProjectManagers()", page)

    def test_network_uncertainty_reuses_same_idempotency_intention(self) -> None:
        page = (FRONTEND / "ProjectsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("coManagerIntentKeys = useRef(new Map<string, string>())", page)
        self.assertIn("const existing = coManagerIntentKeys.current.get(intent)", page)
        self.assertIn("if (existing) return existing", page)
        self.assertIn("crypto.randomUUID()", page)
        self.assertIn("Réponse réseau incertaine", page)


if __name__ == "__main__":
    unittest.main()
