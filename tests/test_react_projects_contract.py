from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactProjectsContractTests(unittest.TestCase):
    def test_shell_routes_projects_to_real_page(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn('import ProjectsPage from "./ProjectsPage"', app)
        self.assertIn('view === "projects"', app)
        self.assertIn("<ProjectsPage />", app)
        self.assertIn('import "./projects.css"', main)

    def test_projects_page_reads_sql_and_integration_status_through_fastapi(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getProjects(false, controller.signal, scope)", page)
        self.assertIn("ViewScopeSelector", page)
        self.assertIn("useViewScope", page)
        self.assertIn("getAcumaticaIntegrationStatus(controller.signal)", page)
        self.assertIn("await syncAcumaticaProjects()", page)
        self.assertIn("const projectRows = await getProjects(false, undefined, scope)", page)
        self.assertIn('scope: ViewScope = "global"', api)
        self.assertIn('"/api/v1/integrations/acumatica"', api)
        self.assertIn('"/api/v1/integrations/acumatica/projects/sync"', api)
        self.assertNotIn("/entity/", page)
        self.assertNotIn("ACUMATICA_ACCESS_TOKEN", page)

    def test_projects_page_uses_real_erp_status_filter_and_keeps_backend_metrics(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("active: boolean", api)
        self.assertIn("project.active", page)
        self.assertIn("État calculé par le backend", page)
        self.assertIn('const [statusFilter, setStatusFilter] = useState("actif")', page)
        self.assertIn("normalize(project.status) !== statusFilter", page)
        self.assertIn("projectStatuses.map", page)
        self.assertIn('<option value="all">Tous</option>', page)
        self.assertIn('<option value="actif">Actif</option>', page)
        self.assertNotIn("activityFilter", page)
        self.assertIn("Chargé de projet", page)
        self.assertIn("Source", page)
        self.assertIn("Acumatica", page)
        self.assertIn("Local", page)
        self.assertIn("Liés ERP", page)
        self.assertNotIn('includes("terminé")', page)
        self.assertNotIn('includes("fermé")', page)
        self.assertNotIn('includes("annulé")', page)

    def test_projects_table_scrolls_vertically_with_sticky_header(self) -> None:
        css = (ROOT / "frontend" / "src" / "projects.css").read_text(
            encoding="utf-8"
        )

        self.assertIn(".projects-table-scroll", css)
        self.assertIn("max-height: min(62vh, 720px)", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn("position: sticky", css)

    def test_selected_project_can_refresh_erp_tasks_through_fastapi(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("syncAcumaticaProjectTasks(selectedProject.id)", page)
        self.assertIn('getTaskCatalog(selectedProject.number, "", false)', page)
        self.assertIn("getAcumaticaProjectTaskSyncMetadata(selectedProject.id)", page)
        self.assertIn("integration?.project_tasks_configured", page)
        self.assertIn("selectedProject.erp_external_id", page)
        self.assertIn("Synchroniser les tâches ERP", page)
        self.assertIn("lignes reçues", page)
        self.assertIn("tâches", page)
        self.assertIn("rejet", page)
        self.assertIn(
            "/api/v1/integrations/acumatica/projects/",
            api,
        )
        self.assertIn("/tasks/sync", api)
        self.assertIn("/tasks/sync-metadata", api)
        self.assertNotIn("/oDATA/RP_ProjectTasks", page)
        self.assertNotIn("RESOURCEPLANNER_ACUMATICA_PASSWORD", page)

    def test_manual_sync_is_only_rendered_when_backend_reports_configured(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("integration?.configured && (", page)
        self.assertIn("Synchroniser les projets", page)
        self.assertIn("Non configurée sur ce serveur", page)
        self.assertIn("Le portefeuille local SQL demeure entièrement utilisable", page)


if __name__ == "__main__":
    unittest.main()
