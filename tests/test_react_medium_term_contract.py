from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactMediumTermContractTests(unittest.TestCase):
    def test_shell_routes_medium_term_to_real_page(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn('import MediumTermPage from "./MediumTermPage"', app)
        self.assertIn('view === "medium-term"', app)
        self.assertIn('<MediumTermPage onOpenDemands={() => setView("demands")} />', app)
        self.assertIn('import "./medium-term.css"', main)

    def test_page_consumes_dedicated_medium_term_projection_for_selected_window(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getMediumTermBudget(", page)
        self.assertIn("taskCode: taskFilter || undefined", page)
        self.assertIn("resourceClassCode: resourceClassFilter || undefined", page)
        self.assertIn("includeInactiveProjects,", page)
        self.assertIn("/api/v1/medium-term/budget?", api)
        self.assertIn('params.set("project_number", project)', api)
        self.assertIn('params.set("task_catalog_item_id", filters.taskCatalogItemId)', api)
        self.assertIn('params.set("task_code", filters.taskCode)', api)
        self.assertIn('params.set("resource_class_code", filters.resourceClassCode)', api)
        self.assertIn("include_inactive_projects", api)
        self.assertIn("start,", api)
        self.assertIn("end,", api)
        self.assertIn("scope,", api)
        self.assertIn('getWorkPackages("", false, controller.signal, scope)', page)
        self.assertIn("medium_term_window_pair_required", (ROOT / "app" / "server" / "routes_reads.py").read_text(encoding="utf-8"))

    def test_project_selector_defaults_to_backend_active_projects_and_can_include_history(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('const [includeInactiveProjects, setIncludeInactiveProjects] = useState(false)', page)
        self.assertIn("getProjects(!includeInactiveProjects, controller.signal, scope)", page)
        self.assertIn('value={includeInactiveProjects ? "all" : "active"}', page)
        self.assertIn('setIncludeInactiveProjects(event.target.value === "all")', page)
        self.assertIn("Actifs seulement", page)
        self.assertIn("Actifs + historique", page)
        self.assertIn("current && projectRows.some((project) => project.number === current)", page)
        self.assertIn('<option value="">Tous les projets</option>', page)
        self.assertIn('<option value="">Toutes les tâches</option>', page)
        self.assertIn('<option value="">Toutes les classes</option>', page)
        self.assertIn("task.task_catalog_item_id", page)
        self.assertIn("resourceClass.code", page)
        self.assertNotIn('project.status === "Terminé"', page)
        self.assertNotIn('project.status === "Annulé"', page)
        self.assertNotIn('normalize(project.status)', page)

    def test_issue_614a_task_filter_groups_by_code_without_replacing_task_identity(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        routes = (ROOT / "app" / "server" / "routes_reads.py").read_text(
            encoding="utf-8"
        )
        query_port = (ROOT / "app" / "application" / "query_ports.py").read_text(
            encoding="utf-8"
        )
        repository = (
            ROOT / "app" / "infrastructure" / "sql" / "web_query_repository.py"
        ).read_text(encoding="utf-8")

        self.assertIn("taskCode: taskFilter || undefined", page)
        self.assertIn("task.task_code === taskFilter", page)
        self.assertIn(
            'new Map<string, { task_code: string; task_label: string }>()',
            page,
        )
        self.assertIn(
            '<option value={task.task_code} key={task.task_code}>',
            page,
        )
        self.assertIn("{task.task_code} — {task.task_label}", page)
        self.assertNotIn(
            "{task.project_number} · {task.task_code} — {task.task_label}",
            page,
        )
        self.assertIn("taskCatalogItemId?: string;", api)
        self.assertIn("taskCode?: string;", api)
        self.assertIn('params.set("task_code", filters.taskCode)', api)
        self.assertIn("task_code: str | None = Query", routes)
        self.assertIn("task_code: str | None = None", query_port)
        self.assertIn("wanted_task_code = _optional_text(task_code)", repository)
        self.assertIn(
            "and (wanted_task_code is None or task.task_code == wanted_task_code)",
            repository,
        )

    def test_gantt_renders_pm_project_task_work_package_hierarchy_from_backend(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Chargé de projet → Projet ERP → Tâche ERP → WorkPackage", page)
        self.assertIn("task.task_code", page)
        self.assertIn("task.task_label", page)
        self.assertIn("task.budget_amount_cad", page)
        self.assertIn("task.remaining_budget_cad", page)
        self.assertIn("task.planned_wp_hours", page)
        self.assertIn("task.remaining_budget_hours", page)
        self.assertIn("task.diagnostic_state", page)
        self.assertIn("WorkPackages sans tâche ERP", page)
        self.assertIn("Aucun rattachement n’est déduit du nom ou du code.", page)
        self.assertIn("BUDGET_ATTENTION.has(task.diagnostic_state)", page)
        self.assertIn("Budget partiellement structuré", page)
        self.assertNotIn("task.budget_hours - task.planned_wp_hours", page)
        self.assertNotIn("reduce((sum, workPackage)", page)

    def test_budget_attention_is_driven_by_stable_backend_diagnostics(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        for code in (
            "BUDGET_UNAVAILABLE",
            "WORK_PACKAGE_LOAD_UNAVAILABLE",
            "NO_WORK_PACKAGES",
            "OVERALLOCATED",
        ):
            self.assertIn(f'"{code}"', page)
        self.assertIn('PARTIALLY_COVERED: "Budget partiellement structuré"', page)
        self.assertIn('className="mt-yellow-flag"', page)
        self.assertNotIn('"PARTIALLY_COVERED",\n]);', page)

    def test_capacity_band_is_one_compact_backend_driven_row_per_class(self) -> None:
        panel = (ROOT / "frontend" / "src" / "MediumTermCapacityPanel.tsx").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("classRows.map", panel)
        self.assertIn("bucket.capacity_hours", panel)
        self.assertIn("bucket.work_package_hours", panel)
        self.assertIn("bucket.utilization", panel)
        self.assertIn("STATE_LABELS[bucket.state]", panel)
        self.assertIn("mt-capacity-grid is-compact", panel)
        self.assertIn("Charge inconnue — pas 0 h", panel)
        self.assertIn("Non calculable", panel)
        self.assertIn("WORKFORCE_CAPACITY_ZERO", panel)
        self.assertIn("WORK_PACKAGE_LOAD_INCOMPLETE", panel)
        self.assertIn("weeks={projection?.weeks ?? []}", page)
        self.assertIn("diagnostics={projection?.weekly_diagnostics ?? []}", page)
        self.assertNotIn("work_package_hours /", panel)
        self.assertNotIn("capacity_hours -", panel)
        self.assertNotIn("* 100", panel)
        self.assertNotIn(">= 0.85", panel)
        self.assertNotIn("> 1", panel)

    def test_issue_556_groups_by_canonical_project_manager_and_project(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("project_manager_contact_id", api)
        self.assertIn("project_manager_display_name", api)
        self.assertIn("manager_group_key", api)
        self.assertIn("manager_display_name", api)
        self.assertIn("manager_resolution_status", api)
        self.assertIn("manager_diagnostics", api)
        self.assertIn("erp_budget_last_success_at", api)
        self.assertIn("task.manager_group_key", page)
        self.assertIn("task.manager_display_name", page)
        self.assertIn("task.manager_resolution_status", page)
        self.assertNotIn("UNRESOLVED_MANAGER_KEY", page)
        self.assertIn("Sans chargé de projet ERP", page)
        self.assertIn("candidate.project_id === task.project_id", page)
        self.assertIn("collapsedManagers", page)
        self.assertIn("collapsedProjects", page)
        self.assertIn("toggleManager", page)
        self.assertIn("toggleProject", page)
        self.assertNotIn("project.project_manager", page)
        self.assertNotIn("task.project_manager_display_name || task.project_manager_contact_id", page)

    def test_issue_556_budget_toggle_keeps_cad_and_hours_separate(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('type BudgetMode = "initial" | "remaining"', page)
        self.assertIn('option value="initial">Budget initial', page)
        self.assertIn('option value="remaining">Budget restant', page)
        self.assertIn("task.budget_amount_cad", page)
        self.assertIn("task.remaining_budget_cad", page)
        self.assertIn("cad(financialValue)", page)
        self.assertIn("Charge WP", page)
        self.assertIn("hours(task.planned_wp_hours)", page)
        self.assertIn("hours(task.remaining_budget_hours)", page)
        self.assertIn("ERP synchronisé", page)
        self.assertNotIn("remaining_budget_cad /", page)
        self.assertNotIn("budget_amount_cad /", page)

    def test_issue_556_moves_unlinked_classification_after_gantt(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertGreater(
            page.index("<MediumTermUnlinkedSegmentsPanel"),
            page.index('className={`mt-board'),
        )
        self.assertIn("Aucun WorkPackage", page)
        self.assertIn("is-compact", page)

    def test_closed_and_cancelled_load_state_comes_from_backend_projection(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("workPackage.current_load_included", page)
        self.assertIn("workPackage.budget_included", page)
        self.assertIn("Hors charge courante", page)
        self.assertNotIn('status === "closed"', page)
        self.assertNotIn('status === "cancelled"', page)

    def test_editor_reuses_work_package_form_and_exposes_dated_interval_workflow(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("await createWorkPackage(payload, key)", editor)
        self.assertIn("await updateWorkPackage(workPackage.reference, payload, expectedVersion)", editor)
        self.assertIn("mediumTermWorkPackage", editor)
        self.assertIn("load_intervals: intervalPayload(intervalDraft)", editor)
        self.assertIn("Répartition facultative", editor)
        self.assertIn("Intervalles explicites", editor)
        self.assertIn("+ Ajouter un intervalle", editor)
        self.assertIn("Tout remettre en automatique", editor)
        self.assertIn("Charge explicite", editor)
        self.assertIn("Solde automatique", editor)
        self.assertIn("jours calendaires", editor)
        self.assertIn("FastAPI reste autoritaire", editor)
        self.assertIn("closeWorkPackage(", editor)
        self.assertIn("cancelWorkPackage(", editor)
        self.assertIn("reopenWorkPackage(", editor)
        self.assertIn('applyLifecycle("reopen")', editor)
        self.assertIn("Les demandes, besoins, quarts, Delivery et Verification liés ne seront pas modifiés.", editor)
        self.assertIn("retiré de la structuration budgétaire", editor)
        self.assertIn("/close", api)
        self.assertIn("/cancel", api)
        self.assertIn("/reopen", api)
        self.assertNotIn("proposeWorkPackageWeeklyLoads", editor)
        self.assertNotIn("replaceWorkPackageWeeklyLoads", editor)

    def test_editor_handles_cas_atomic_replan_and_read_only_status(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn('"work_package_version_conflict"', editor)
        self.assertIn('"work_package_load_intervals_replan_required"', editor)
        self.assertIn("Corrige les intervalles dans la même sauvegarde.", editor)
        self.assertIn("Recharger le WorkPackage", editor)
        self.assertIn("Statut : {statusLabel(workPackage.status)}", editor)
        self.assertIn("calculé par le backend", editor)
        self.assertNotIn("STATUS_OPTIONS", editor)
        self.assertNotIn('status: form.status', editor)
        self.assertNotIn("status: string;", api.split("export type WorkPackageWrite = {", 1)[1].split("};", 1)[0])

    def test_work_package_resource_class_editor_contract_is_explicit_and_backend_driven(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        resource_api = (ROOT / "frontend" / "src" / "resourceClassesApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("getResourceClassOptions", editor)
        self.assertIn("Classe de ressource", editor)
        self.assertIn("resourceClassTouched", editor)
        self.assertIn("? { resource_class_code: selectedResourceClassCode }", editor)
        self.assertIn("resource_class_code: selectedResourceClassCode", editor)
        self.assertIn("WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE", editor)
        self.assertIn("work_package_resource_class_not_found", editor)
        self.assertIn("work_package_resource_class_inactive", editor)
        self.assertIn("Classe historique inactive", editor)
        self.assertIn("Code WorkPackage historique", editor)
        self.assertNotIn("<span>Code</span>", editor)
        self.assertIn("/api/v1/resource-classes", resource_api)
        for field in (
            "resource_class_code",
            "resource_class_label",
            "resource_class_active",
            "task_resource_class_code",
            "resource_class_diagnostic",
        ):
            self.assertIn(field, api)

    def test_medium_term_presents_work_package_class_without_recomputing_rules(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Classe de ressource :", page)
        self.assertIn("workPackage.resource_class_label", page)
        self.assertIn("workPackage.resource_class_active", page)
        self.assertIn("workPackage.resource_class_diagnostic", page)
        self.assertIn("WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE", page)
        self.assertIn("Code WorkPackage :", page)
        self.assertNotIn("required_resource_class", page)
        self.assertNotIn("task_resource_class_code ??", page)
        self.assertNotIn("resource_class_code ??", page)

    def test_medium_term_does_not_mutate_planning_or_delivery(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "createQuickShift",
            "moveAllocation",
            "planning_version",
            "delivery_version",
            "AssetAllocation",
            "DeliveryPlan",
        ):
            self.assertNotIn(forbidden, page)
            self.assertNotIn(forbidden, editor)


if __name__ == "__main__":
    unittest.main()
