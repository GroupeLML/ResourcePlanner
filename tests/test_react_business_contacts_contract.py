from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactBusinessContactContractTests(unittest.TestCase):
    def test_api_exposes_business_contact_writes_and_backend_resolution(self) -> None:
        source = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getBusinessContacts", source)
        self.assertNotIn("setProjectManagerContact", source)
        self.assertIn("setTaskBusinessContacts", source)
        self.assertIn("setResourceCoordinatorContact", source)
        self.assertIn("setDemandOperationalResponsible", source)
        self.assertIn("getDemandDetail", source)
        self.assertIn("getRequestLineContactResolution", source)
        self.assertIn("/api/v1/request-lines/", source)
        self.assertIn("RequestLineContactResolutionReadModel", source)

    def test_resources_page_selects_user_backed_business_contacts(self) -> None:
        source = (FRONTEND / "ResourcesPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("Coordonnateur de la ressource", source)
        self.assertIn("setResourceCoordinatorContact", source)
        self.assertNotIn("BusinessContactsPanel", source)
        self.assertIn('user_backed_only: "true"', api)

    def test_projects_page_exposes_separate_task_responsible_and_coordinator(self) -> None:
        source = (FRONTEND / "ProjectsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("Chargé de projet", source)
        self.assertIn("Chargé de projet principal (ERP)", source)
        self.assertIn("Utilisateur RessourcePlanner non lié", source)
        self.assertNotIn("changeProjectManager", source)
        self.assertIn("Responsable opérationnel", source)
        self.assertIn("Coordonnateur", source)
        self.assertIn("operational_responsible_contact_id", source)
        self.assertIn("coordinator_contact_id", source)
        self.assertIn("Hériter du chargé de projet", source)
        self.assertIn("Aucun coordonnateur de tâche", source)

    def test_demands_page_displays_backend_resolution_and_reapproval_semantics(self) -> None:
        source = (FRONTEND / "DemandsPage.tsx").read_text(encoding="utf-8")

        self.assertIn("ResolutionSummary", source)
        self.assertIn("getDemandDetail", source)
        self.assertIn("row.contacts", source)
        self.assertNotIn("getRequestLineContactResolution", source)
        self.assertIn("Hériter de la tâche puis du chargé de projet", source)
        self.assertIn("nouvelle approbation", source)
        self.assertIn("planning existant conserve son contexte approuvé précédent", source)
        self.assertNotIn(
            "REQUEST_OVERRIDE > TASK_RESPONSIBLE > PROJECT_MANAGER",
            source,
        )

    def test_resolution_component_surfaces_backend_source(self) -> None:
        source = (FRONTEND / "BusinessContactUi.tsx").read_text(encoding="utf-8")
        self.assertIn("Source :", source)
        self.assertIn("CONTACT_REFERENCE_INVALID", source)
        self.assertIn("PROJECT_MANAGER_CONTACT_UNMIGRATED", source)
        self.assertIn("RESOURCE_COORDINATOR", source)
        self.assertIn("TASK_COORDINATOR", source)

    def test_business_contact_styles_are_loaded(self) -> None:
        source = (FRONTEND / "main.tsx").read_text(encoding="utf-8")
        self.assertIn('import "./business-contacts.css";', source)


if __name__ == "__main__":
    unittest.main()
