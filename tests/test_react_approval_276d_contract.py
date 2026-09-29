from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactApproval276DContractTests(unittest.TestCase):
    def test_demand_detail_exposes_common_line_approval_projection(self) -> None:
        api = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(encoding="utf-8")
        client = (ROOT / "frontend" / "src" / "demandWorkflowApi.ts").read_text(encoding="utf-8")

        self.assertIn("approval_cycle: ApprovalCycleProgressReadModel | null", api)
        self.assertIn("actor_approvable_request_line_ids", api)
        self.assertIn("/approval-votes", client)
        self.assertIn('"Idempotency-Key": idempotencyKey', client)
        self.assertIn("Approuver mes lignes", page)
        self.assertIn("Approbation enregistrée —", page)
        self.assertIn("lignes sur", page)
        self.assertIn("Progression par ligne", page)
        self.assertIn("Approbateur(s) admissible(s)", page)
        self.assertIn("Les champs globaux d’approbation restent vides", page)

    def test_configuration_administers_276e_resource_class_routing(self) -> None:
        panel = (ROOT / "frontend" / "src" / "ApprovalScopesPanel.tsx").read_text(encoding="utf-8")
        client = (ROOT / "frontend" / "src" / "approvalScopesApi.ts").read_text(encoding="utf-8")

        self.assertIn("getResourceClasses", panel)
        self.assertIn("Classes couvertes", panel)
        self.assertIn("scope.resource_class_codes.includes", panel)
        self.assertIn("setApprovalScopeResourceClass", panel)
        self.assertIn("Overrides de tâches", panel)
        self.assertIn("Associations exceptionnelles", panel)
        self.assertIn("resource_class_codes: string[]", client)
        self.assertIn("/resource-classes/", client)
        self.assertIn("expected_version", client)

    def test_configuration_administers_scopes_and_explicit_approvers(self) -> None:
        config = (ROOT / "frontend" / "src" / "ConfigurationPage.tsx").read_text(encoding="utf-8")
        panel = (ROOT / "frontend" / "src" / "ApprovalScopesPanel.tsx").read_text(encoding="utf-8")
        client = (ROOT / "frontend" / "src" / "approvalScopesApi.ts").read_text(encoding="utf-8")

        self.assertIn("<ApprovalScopesPanel />", config)
        self.assertIn("Périmètres et approbateurs", panel)
        self.assertIn('role.permissions.includes("approve_demands")', panel)
        self.assertIn("/api/v1/admin/approval-scopes", client)
        self.assertIn("/approvers/", client)
        self.assertIn("expected_version", client)


if __name__ == "__main__":
    unittest.main()
