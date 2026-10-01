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

    def test_selected_project_reuses_medium_term_budget_projection_without_recalculation(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getMediumTermBudgetSummary(selectedProject.number, controller.signal, scope)", page)
        self.assertIn("getMediumTermBudgetSummary(selectedProject.number, undefined, scope)", page)
        self.assertIn("task.task_code", page)
        self.assertIn("task.task_label", page)
        self.assertIn("task.budget_hours", page)
        self.assertIn("task.planned_wp_hours", page)
        self.assertIn("task.remaining_budget_hours", page)
        self.assertIn("task.diagnostic_state", page)
        self.assertIn("projectBudget.unclassified_work_packages", page)
        self.assertIn("Aucun rattachement à une tâche ERP n’est déduit du nom ou du code.", page)
        self.assertIn("Budget partiellement structuré", page)
        self.assertIn("Dépassement du budget", page)
        self.assertIn("Budget ERP non disponible", page)
        self.assertNotIn("task.budget_hours - task.planned_wp_hours", page)
        self.assertNotIn("reduce((sum, workPackage)", page)
        self.assertNotIn("getWorkPackages(", page)
        self.assertIn("export function getMediumTermBudgetSummary(", api)
        self.assertIn("project_number: projectNumber", api)
        self.assertIn("scope,", api)
        self.assertIn("/api/v1/medium-term/budget?", api)

    def test_project_budget_detail_is_readable_without_contact_management_permission(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("<th>Détail</th>", page)
        self.assertIn(">\n                        Ouvrir\n", page)
        self.assertIn("{selectedProjectNumber && (", page)
        self.assertIn("{canManageContacts && (", page)
        self.assertIn("Budgets tâches ERP", page)

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
        self.assertIn("syncAcumaticaActiveProjectTasks()", page)
        self.assertIn("getCurrentAcumaticaProjectTaskSyncRun", page)
        self.assertIn("getAcumaticaProjectTaskSyncRun", page)
        self.assertIn("Synchroniser les tâches des projets actifs", page)
        self.assertIn("Synchronisation en cours…", page)
        self.assertIn("projets traités", page)
        self.assertIn(
            '"/api/v1/integrations/acumatica/projects/tasks/sync"',
            api,
        )
        self.assertIn("projects_synchronized", page)
        self.assertIn("tasks_rejected", page)
        self.assertIn("source_requests", page)
        self.assertIn("requête(s) ERP", page)
        self.assertIn("duration_ms", page)
        self.assertIn("source_requests: number | null", api)
        self.assertIn("projects_processed: number", api)
        self.assertIn("COMPLETED_WITH_ERRORS", api)
        self.assertIn("/api/v1/integrations/acumatica/projects/tasks/sync/current", api)
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
