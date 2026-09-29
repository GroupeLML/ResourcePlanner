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

    def test_page_uses_existing_canonical_read_endpoints(self) -> None:
        source = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("getProjects(true, controller.signal, scope)", source)
        self.assertIn('getWorkPackages("", true, controller.signal, scope)', source)
        self.assertIn("getPlanningSnapshot(start, end, controller.signal, scope)", source)
        self.assertIn("getMediumTermUnlinkedSegments(start, end, controller.signal, scope)", source)
        self.assertIn("getResources(true, controller.signal)", source)
        self.assertIn("useViewScope", source)
        self.assertIn("ViewScopeSelector", source)
        self.assertIn("if (scopeLoading) return", source)
        self.assertIn("if (scopeError)", source)
        self.assertIn("snapshot.pending_loads", source)
        self.assertIn("snapshot.segments", source)
        self.assertIn("snapshot?.capacity_buckets ?? []", source)
        self.assertNotIn("projected_hours_without_double_counting", source)
        self.assertNotIn("projected_period_hours", source)

    def test_page_exposes_work_packages_filters_backend_statuses_editor_and_capacity(self) -> None:
        source = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Planification moyen terme", source)
        self.assertIn("Chargé de projet", source)
        self.assertIn("Horizon", source)
        self.assertIn("WorkPackages", source)
        self.assertIn("Soumise · modification en attente", source)
        self.assertIn("Soumise · charge potentielle", source)
        self.assertIn("Plan approuvé · tentative", source)
        self.assertIn("Plan approuvé · confirmée", source)
        self.assertIn("Ouvrir les demandes", source)
        self.assertIn("+ WorkPackage", source)
        self.assertIn("<WorkPackageEditor", source)
        self.assertIn("onEdit={setEditor}", source)
        self.assertIn("setRefreshKey((value) => value + 1)", source)
        self.assertIn("<MediumTermCapacityPanel", source)
        self.assertIn("React ne recalcule ni la projection ni le non-double-comptage", source)

    def test_demand_signals_expose_issue_34_details_without_new_business_rules(self) -> None:
        source = (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "frontend" / "src" / "medium-term.css").read_text(encoding="utf-8")

        self.assertIn("demand.desired_start", source)
        self.assertIn("demand.desired_end", source)
        self.assertIn("demand.estimated_hours", source)
        self.assertIn("demand.resource_count", source)
        self.assertIn("demand.required_competencies", source)
        self.assertIn("aria-label={demandDetails(demand, label)}", source)
        self.assertIn('return normalize(demand.confirmation).includes("tentative")', source)
        self.assertIn('return "tentative"', source)
        self.assertIn(".mt-demand-chip.tentative", css)
        self.assertIn(".mt-legend i.tentative", css)

    def test_capacity_panel_displays_backend_fields_without_recalculating_exposure(self) -> None:
        panel = (ROOT / "frontend" / "src" / "MediumTermCapacityPanel.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("bucket.exposure_hours", panel)
        self.assertIn("bucket.capacity_hours", panel)
        self.assertIn("bucket.residual_hours", panel)
        self.assertIn("bucket.replacement_proposal_hours", panel)
        self.assertIn("bucket.replacement_delta_hours", panel)
        self.assertIn("bucket.state", panel)
        self.assertNotIn("firm_hours +", panel)
        self.assertNotIn("submitted_hours +", panel)
        self.assertIn("capacity_buckets: MediumTermCapacityBucketReadModel[]", api)
        self.assertIn("Référence organisationnelle globale", panel)
        self.assertIn("contextual={scope === \"mine\"}", (ROOT / "frontend" / "src" / "MediumTermPage.tsx").read_text(encoding="utf-8"))

    def test_editor_uses_backend_mutations_and_reuses_creation_idempotency_key(self) -> None:
        editor = (ROOT / "frontend" / "src" / "WorkPackageEditor.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("await createWorkPackage(payload, key)", editor)
        self.assertIn("await updateWorkPackage(workPackage.reference, payload, workPackage.version)", editor)
        self.assertIn("previous?.fingerprint === fingerprint", editor)
        self.assertIn("createRetry.current = { fingerprint, key }", editor)
        self.assertIn("if (saving) return", editor)
        self.assertIn("aucune demande n’est déjà liée", editor)
        self.assertIn('"/api/v1/work-packages"', api)
        self.assertIn('/api/v1/work-packages/${encodeURIComponent(reference)}', api)
        self.assertIn('"Idempotency-Key"', api)
        self.assertIn("getTaskCatalog", editor)
        self.assertIn("task_catalog_item_id", editor)
        self.assertIn("required={!editing}", editor)
        self.assertIn("workPackage.version", editor)
        self.assertIn('"work_package_version_conflict"', editor)
        self.assertIn("WorkPackage historique non classé", editor)
        self.assertIn("expected_version: expectedVersion", api)


if __name__ == "__main__":
    unittest.main()
