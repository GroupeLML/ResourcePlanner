from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactIssue594DContractTests(unittest.TestCase):
    def test_api_exposes_versioned_idempotent_operational_responsibility_commands(self) -> None:
        api = (FRONTEND / "api.ts").read_text(encoding="utf-8")

        self.assertIn("getProjectOperationalResponsibility", api)
        self.assertIn("setProjectOperationalResponsible", api)
        self.assertIn("setSegmentOperationalResponsible", api)
        self.assertIn("setAllocationOperationalResponsible", api)
        self.assertIn("expected_version: expectedVersion", api)
        self.assertIn("expected_planning_version: expectedPlanningVersion", api)
        self.assertIn('"Idempotency-Key": idempotencyKey', api)

    def test_project_segment_and_shift_editors_use_one_responsibility_control(self) -> None:
        control = (FRONTEND / "OperationalResponsibilityControl.tsx").read_text(
            encoding="utf-8"
        )
        projects = (FRONTEND / "ProjectsPage.tsx").read_text(encoding="utf-8")
        segment = (FRONTEND / "SegmentEditor.tsx").read_text(encoding="utf-8")
        shift = (FRONTEND / "ShiftEditor.tsx").read_text(encoding="utf-8")
        planning = (FRONTEND / "PlanningPage.tsx").read_text(encoding="utf-8")

        self.assertIn('can("manage_resources")', control)
        self.assertIn('can("manage_planning")', control)
        self.assertIn("createClientId()", control)
        self.assertIn("AUTO → MANUAL", control)
        self.assertIn("Aucun override — utiliser l’héritage canonique", control)
        self.assertIn('kind: "project"', projects)
        self.assertIn('kind: "segment"', segment)
        self.assertIn('kind: "allocation"', shift)
        self.assertIn("planningVersion={snapshot.planning_version}", planning)

    def test_dashboard_displays_effective_responsibility_without_redefining_scope(self) -> None:
        dashboard = (FRONTEND / "CoordinatorDashboardPage.tsx").read_text(
            encoding="utf-8"
        )
        backend = (
            ROOT / "app" / "application" / "coordinator_dashboard.py"
        ).read_text(encoding="utf-8")

        self.assertIn("operational_responsible_display_name", dashboard)
        self.assertIn("operational_responsible_source_type", dashboard)
        self.assertIn("resolve_demand_scope(principal, SCOPE_MINE)", backend)
        self.assertIn("resolve_resource_requirements(", backend)
        self.assertLess(
            backend.index("resolve_demand_scope(principal, SCOPE_MINE)"),
            backend.index("resolve_resource_requirements("),
        )


if __name__ == "__main__":
    unittest.main()
