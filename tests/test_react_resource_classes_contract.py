from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactResourceClassesContractTests(unittest.TestCase):
    def test_configuration_exposes_resource_class_admin_surface(self) -> None:
        config = (
            ROOT / "frontend" / "src" / "ConfigurationPage.tsx"
        ).read_text(encoding="utf-8")
        panel = (
            ROOT / "frontend" / "src" / "ResourceClassesPanel.tsx"
        ).read_text(encoding="utf-8")
        client = (
            ROOT / "frontend" / "src" / "resourceClassesApi.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("<ResourceClassesPanel />", config)
        self.assertIn("Classes, coûts moyens et standards TaskCD", panel)
        self.assertIn("Coût moyen CAD/h", panel)
        self.assertIn("Classe par défaut", panel)
        self.assertIn("Exception projet", panel)
        self.assertIn("Résolution effective", panel)
        self.assertIn("Revenir au standard", panel)
        self.assertIn("EXCLUDE", panel)

        self.assertIn(
            "/api/v1/admin/resource-classes/task-standards",
            client,
        )
        self.assertIn("/task-overrides/", client)
        self.assertIn("expected_version", client)

    def test_react_displays_backend_resolution_without_budget_formula(self) -> None:
        panel = (
            ROOT / "frontend" / "src" / "ResourceClassesPanel.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("rule.resolution.status", panel)
        self.assertIn("rule.resolution.resource_class_code", panel)
        self.assertNotIn("BudgetAmount", panel)
        self.assertNotIn("average_hourly_cost_cad /", panel)
        self.assertNotIn("/ row.average_hourly_cost_cad", panel)

    def test_resource_editors_use_canonical_resource_class_catalog(self) -> None:
        resources = (
            ROOT / "frontend" / "src" / "ResourcesPage.tsx"
        ).read_text(encoding="utf-8")
        demands = (
            ROOT / "frontend" / "src" / "DemandsPage.tsx"
        ).read_text(encoding="utf-8")
        editor = (
            ROOT / "frontend" / "src" / "DemandLinesEditor.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("getResourceClassOptions(controller.signal)", resources)
        self.assertIn("setResourceClasses(classRows)", resources)
        self.assertIn("activeResourceClasses", resources)
        self.assertIn("{row.code} · {row.label}", resources)
        self.assertIn("historique/inactive", resources)
        self.assertNotIn('placeholder="Programmation, Installation…"', resources)

        self.assertIn("getResourceClassOptions(controller.signal)", demands)
        self.assertIn("resourceClasses={resourceClasses}", demands)
        client = (
            ROOT / "frontend" / "src" / "resourceClassesApi.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('"/api/v1/resource-classes"', client)
        self.assertIn('"/api/v1/admin/resource-classes"', client)
        operational = client.split(
            "export function getResourceClassOptions", 1
        )[1].split("export function getResourceClasses", 1)[0]
        self.assertNotIn("average_hourly_cost_cad", operational)
        self.assertIn("resourceClasses: ResourceClassOptionReadModel[]", editor)
        self.assertIn("activeResourceClasses", editor)
        self.assertIn("Aucune classe imposée", editor)
        self.assertIn('import SearchableCombobox from "./SearchableCombobox"', editor)
        self.assertIn("value={line.required_resource_class || null}", editor)
        self.assertIn("options={activeResourceClasses.map((row) => ({", editor)
        self.assertIn("value: row.code", editor)
        self.assertIn("label: `${row.code} · ${row.label}`", editor)
        self.assertIn(
            'onChange={(value) => updateLine(index, { required_resource_class: value ?? "" })}',
            editor,
        )
        self.assertIn("historique/inactive", editor)
        self.assertNotIn(
            "resources.map((row) => row.resource_class?.trim())",
            editor,
        )


if __name__ == "__main__":
    unittest.main()
