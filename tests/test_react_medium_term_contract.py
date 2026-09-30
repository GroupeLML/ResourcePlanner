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

        self.assertIn("getMediumTermBudget(projectFilter, start, end, controller.signal, scope)", page)
        self.assertIn("/api/v1/medium-term/budget?", api)
        self.assertIn("project_number: projectNumber", api)
        self.assertIn("start,", api)
        self.assertIn("end,", api)
        self.assertIn("scope,", api)
        self.assertIn("getWorkPackages("", false, controller.signal, scope)", page)
        self.assertIn("medium_term_window_pair_required", (ROOT / "app" / "server" / "routes_reads.py").read_text(encoding="utf-8"))

    def test_gantt_renders_project_task_work_package_budget_hierarchy_from_backend(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Projet → tâche ERP → WorkPackage", page)
        self.assertIn("task.task_code", page)
        self.assertIn("task.task_label", page)
        self.assertIn("task.budget_hours", page)
        self.assertIn("task.planned_wp_hours", page)
        self.assertIn("task.remaining_budget_hours", page)
        self.assertIn("task.diagnostic_state", page)
        self.assertIn("WorkPackages non classés", page)
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
        self.assertIn('"PARTIALLY_COVERED": "Budget partiellement structuré"', page)
        self.assertIn('className="mt-yellow-flag"', page)
        self.assertNotIn('"PARTIALLY_COVERED",\n]);', page)

    def test_capacity_band_displays_backend_fields_and_preserves_unknown_load(self) -> None:
        panel = (ROOT / "frontend" / "src" / "MediumTermCapacityPanel.tsx").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("week.capacity_hours", panel)
        self.assertIn("week.work_package_hours", panel)
        self.assertIn("week.utilization", panel)
        self.assertIn("Charge non disponible", panel)
        self.assertIn("Charge inconnue — pas 0 h", panel)
        self.assertIn("Non calculable", panel)
        self.assertIn("WORKFORCE_CAPACITY_ZERO", panel)
        self.assertIn("WORK_PACKAGE_LOAD_INCOMPLETE", panel)
        self.assertIn("weeks={projection?.weeks ?? []}", page)
        self.assertIn("diagnostics={projection?.weekly_diagnostics ?? []}", page)
        self.assertNotIn("work_package_hours /", panel)
        self.assertNotIn("capacity_hours -", panel)
        self.assertNotIn("* 100", panel)

    def test_closed_and_cancelled_load_state_comes_from_backend_projection(self) -> None:
        page = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("workPackage.current_load_included", page)
        self.assertIn("workPackage.budget_included", page)
        self.assertIn("Hors charge courante", page)
        self.assertNotIn('status === "closed"', page)
        self.assertNotIn('status === "cancelled"', page)

    def test_editor_reuses_work_package_form_and_exposes_explicit_weekly_workflow(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("await createWorkPackage(payload, key)", editor)
        self.assertIn("await updateWorkPackage(workPackage.reference, payload, workPackage.version)", editor)
        self.assertIn("mediumTermWorkPackage", editor)
        self.assertIn("proposeWorkPackageWeeklyLoads(workPackage.reference)", editor)
        self.assertIn("replaceWorkPackageWeeklyLoads(", editor)
        self.assertIn("Proposition AUTO prévisualisée — elle n’est pas encore enregistrée.", editor)
        self.assertIn("Accepter la proposition AUTO", editor)
        self.assertIn("Enregistrer la répartition manuelle", editor)
        self.assertIn('setWeeklyOrigin("MANUAL")', editor)
        self.assertIn("Somme affichée", editor)
        self.assertIn("FastAPI reste autoritaire pour la validation exacte.", editor)
        self.assertIn("/weekly-loads/proposal", api)
        self.assertIn("/weekly-loads", api)
        self.assertIn('"Idempotency-Key"', api)

    def test_editor_handles_cas_and_replan_without_silent_redistribution(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('"work_package_version_conflict"', editor)
        self.assertIn('"work_package_weekly_load_replan_required"', editor)
        self.assertIn("aucune redistribution automatique n’a été faite", editor)
        self.assertIn("Recharger le WorkPackage", editor)
        self.assertIn("proposalFingerprint === currentFingerprint", editor)
        self.assertNotIn("proposeWorkPackageWeeklyLoads(workPackage.reference);\n      await replace", editor)

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
