from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapProjectTaskContractTests(unittest.TestCase):
    def test_projects_page_exposes_canonical_acumatica_project_sync(self) -> None:
        configuration = (
            ROOT / "frontend" / "src" / "ConfigurationPage.tsx"
        ).read_text(encoding="utf-8")
        projects = (
            ROOT / "frontend" / "src" / "ProjectsPage.tsx"
        ).read_text(encoding="utf-8")
        api = (
            ROOT / "frontend" / "src" / "api.ts"
        ).read_text(encoding="utf-8")

        self.assertNotIn("AcumaticaProjectSyncPanel", configuration)
        self.assertNotIn("Synchroniser les projets", configuration)
        self.assertIn("Synchroniser les projets", projects)
        self.assertIn("await syncAcumaticaProjects()", projects)
        self.assertIn(
            "/api/v1/integrations/acumatica/projects/sync",
            api,
        )
        self.assertNotIn("import-projects", projects)

    def test_task_bootstrap_documentation_states_minimal_contract_and_replay(self) -> None:
        doc = (ROOT / "docs" / "ERP_TASK_CATALOG.md").read_text(encoding="utf-8")

        for required in ("ID projet", "ID tâche", "Description", "Statut"):
            self.assertIn(required, doc)
        self.assertIn("Sans changement", doc)
        self.assertIn("erp_task_catalog_duplicate_key", doc)
        self.assertIn("erp_task_catalog_row_invalid", doc)
        self.assertIn("--apply", doc)


if __name__ == "__main__":
    unittest.main()
