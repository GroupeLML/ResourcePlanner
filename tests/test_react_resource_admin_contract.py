from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactResourceAdminContractTests(unittest.TestCase):
    def test_shell_routes_resources_to_real_page(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn('import ResourcesPage from "./ResourcesPage"', app)
        self.assertIn('"resources"', app)
        self.assertIn('<ResourcesPage />', app)
        self.assertIn('import "./resource-admin.css"', main)

    def test_api_client_exposes_resource_and_availability_admin(self) -> None:
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")

        self.assertIn("email: string | null", api)
        self.assertIn("ResourceAvailabilityRuleReadModel", api)
        self.assertIn("resource_class_codes: string[]", api)
        self.assertIn("resource_class_codes?: string[]", api)
        self.assertIn('"Horaire standard" | "Vacances" | "Jour férié"', api)
        self.assertIn('"/api/v1/resources"', api)
        self.assertIn("/api/v1/resources/${encodeURIComponent(resourceId)}", api)
        self.assertIn('"/api/v1/availability-rules"', api)
        self.assertIn("/api/v1/availability-rules/${encodeURIComponent(ruleId)}", api)
        self.assertIn("createResource", api)
        self.assertIn("updateResource", api)
        self.assertIn("deactivateResource", api)
        self.assertIn("reactivateResource", api)
        self.assertIn("createAvailabilityRule", api)
        self.assertIn("updateAvailabilityRule", api)
        self.assertIn("deactivateAvailabilityRule", api)
        self.assertIn("erp_status: string | null", api)
        self.assertIn("erp_active: boolean", api)
        self.assertIn("erp_department_description: string | null", api)
        self.assertIn("erp_branch_code: string | null", api)

    def test_page_reads_and_refreshes_authoritative_fastapi_state(self) -> None:
        page = (ROOT / "frontend" / "src" / "ResourcesPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("getResources(false, controller.signal)", page)
        self.assertIn("getAvailabilityRules(null, true, false, controller.signal)", page)
        self.assertIn("getAvailabilityRules(selectedId, false, false, controller.signal)", page)
        self.assertIn("await createResource(", page)
        self.assertIn("await updateResource(", page)
        self.assertIn("await deactivateResource(", page)
        self.assertIn("await reactivateResource(", page)
        self.assertIn("await createAvailabilityRule(", page)
        self.assertIn("await updateAvailabilityRule(", page)
        self.assertIn("await deactivateAvailabilityRule(", page)
        self.assertIn("setRefreshKey((value) => value + 1)", page)
        self.assertIn("syncAcumaticaEmployees", page)
        self.assertIn("Synchroniser RP_Employees", page)
        self.assertIn("EmployeID", page)
        self.assertIn("Statut ERP", page)
        self.assertIn("Département", page)
        self.assertIn("Division / succursale", page)

        integration_api = (ROOT / "frontend" / "src" / "acumaticaIntegrationApi.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn('"/api/v1/integrations/acumatica/employees/sync"', integration_api)

    def test_page_covers_cutover_availability_types_without_capacity_math(self) -> None:
        page = (ROOT / "frontend" / "src" / "ResourcesPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Horaire standard", page)
        self.assertIn("Vacances", page)
        self.assertIn("Jour férié", page)
        self.assertIn("Jours fériés", page)
        self.assertIn("Classes visées", page)
        self.assertIn("resource_class_codes: resourceClassCodes", page)
        self.assertIn("Aucune classe sélectionnée = jour férié global", page)
        self.assertIn("Compétences", page)
        self.assertNotIn("capacity_hours", page)
        self.assertNotIn("utilization_pct", page)
        self.assertNotIn("residual_hours", page)


if __name__ == "__main__":
    unittest.main()
