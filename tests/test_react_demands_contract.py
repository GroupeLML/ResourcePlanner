from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactDemandsContractTests(unittest.TestCase):
    def test_shell_routes_demands_view_to_real_workspace(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        workspace = (ROOT / "frontend" / "src" / "DemandsWorkspace.tsx").read_text(
            encoding="utf-8"
        )
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn('import DemandsWorkspace from "./DemandsWorkspace"', app)
        self.assertIn('view === "demands"', app)
        self.assertIn("<DemandsWorkspace />", app)
        self.assertIn("<DemandsPage />", workspace)
        self.assertIn('import "./demands.css"', main)

    def test_api_client_uses_canonical_demand_and_work_package_endpoints(self) -> None:
        source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn('"/api/v1/demands"', source)
        self.assertIn('/api/v1/demands/${encodeURIComponent(number)}', source)
        self.assertIn("/api/v1/work-packages?", source)
        self.assertIn("/detail", source)
        self.assertIn("DemandDetailReadModel", source)
        self.assertIn('"Idempotency-Key"', source)
        self.assertIn("work_package_ref", source)

    def test_list_composes_terminal_filter_status_project_search_and_creation_sort(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("effective_status?: string | null", api)
        self.assertIn("terminal?: boolean", api)
        self.assertIn("created_at?: string | null", api)
        self.assertIn("Inclure les demandes terminées", source)
        self.assertIn("Plus récentes d’abord", source)
        self.assertIn("Plus anciennes d’abord", source)
        self.assertIn('useState<"newest" | "oldest">("newest")', source)
        self.assertIn(
            'if (!includeTerminated && statusFilter === "all" && demand.terminal)',
            source,
        )
        self.assertIn(
            'if (statusFilter !== "all" && effectiveStatus !== statusFilter)',
            source,
        )
        self.assertIn(
            'if (projectFilter !== "all" && demand.project_number !== projectFilter)',
            source,
        )
        self.assertIn("demandSearchText(demand)", source)
        self.assertIn('sortOrder === "newest" ? -1 : 1', source)
        self.assertIn("demandCreatedAtMs(left) - demandCreatedAtMs(right)", source)
        self.assertIn(
            "[demands, search, statusFilter, projectFilter, includeTerminated, sortOrder]",
            source,
        )
        self.assertNotIn("desired_start) -", source)

    def test_role_simplification_uses_backend_technical_projection(self) -> None:
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        demands = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("DemandDetailTechnicalContextReadModel", api)
        self.assertIn("technical_context: DemandDetailTechnicalContextReadModel | null", api)
        self.assertIn("const technicalContext = detail.technical_context", detail)
        self.assertIn("{technicalContext && (", detail)
        self.assertIn("Diagnostic technique / Contexte backend", detail)
        self.assertIn('data-testid="demand-technical-context"', detail)
        self.assertNotIn("principal?.roles", detail)
        self.assertNotIn("Contexte backend v{selectedDetail.version}", demands)
        self.assertNotIn("Version {detail.version}", detail)

    def test_creation_reuses_idempotency_key_for_identical_retry(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("previous?.fingerprint === fingerprint", source)
        self.assertIn("createRetry.current = { fingerprint, key }", source)
        self.assertIn("await createDemand(payload, key)", source)
        self.assertIn("if (saving) return", source)

    def test_editor_preserves_business_separation_and_authoritative_references(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Responsable projet", source)
        self.assertIn("restent en lecture seule", source)
        self.assertIn("Plage moyen terme / WorkPackage", source)
        self.assertIn('value="Tentative"', source)
        self.assertIn('value="Confirmée"', source)
        self.assertIn("Approbation ≠ confirmation", source)
        self.assertIn("getDemandDetail", source)
        self.assertIn("<DemandDetail", source)
        self.assertIn("canonicalDetail={selectedDetail}", source)
        self.assertIn("hasUnsavedChanges={editorDirty || contextDirty}", source)
        self.assertIn("confirmDiscardChanges", source)
        self.assertNotIn('name="project_manager"', source)

    def test_simple_demand_keeps_task_selector_without_catalog_search_field(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("Recherche catalogue ERP", source)
        self.assertNotIn("taskSearch", source)
        self.assertIn("<span>Tâche ERP</span>", source)
        self.assertIn("getTaskCatalog(projectNumber", source)
        self.assertIn("<SearchableCombobox", source)
        self.assertIn("visibleTasks.filter", source)

    def test_multi_line_editor_groups_dates_and_keeps_compact_business_fields(self) -> None:
        editor = (ROOT / "frontend" / "src" / "DemandLinesEditor.tsx").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "frontend" / "src" / "demands.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('className="request-line-date-group"', editor)
        self.assertIn('data-testid={`request-line-dates-${index}`}', editor)
        self.assertIn("<span>Début</span>", editor)
        self.assertIn("<span>Fin</span>", editor)
        self.assertIn('className="request-line-compact-field"', editor)
        self.assertIn("<span>Confirmation</span>", editor)
        self.assertIn("<span>Classe de ressource</span>", editor)
        self.assertIn(".request-line-date-group {", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn(".request-line-compact-field", css)
        self.assertIn("grid-template-columns: 1fr;", css)


if __name__ == "__main__":
    unittest.main()
