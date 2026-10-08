from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
APPLICATION = APP / "application"


class CommandPortArchitectureTests(unittest.TestCase):
    SERVICE_FILES = (
        "allocation_service.py",
        "planning_service.py",
        "quick_shift_service.py",
        "demand_service.py",
        "segment_service.py",
    )

    def test_core_application_services_do_not_import_ui_excel_or_v1_modules(self) -> None:
        forbidden = (
            "nicegui",
            "xlwings",
            "excel_repository",
            "fastapi",
            "pydantic",
            ".v13",
            ".v14",
            ".v15",
            ".v16",
            ".v17",
            ".v18",
            "app.v13",
            "app.v14",
            "app.v15",
            "app.v16",
            "app.v17",
            "app.v18",
        )

        for filename in self.SERVICE_FILES:
            source = (APPLICATION / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            for module in imports:
                self.assertFalse(
                    any(token in module for token in forbidden),
                    f"{filename} leaks transport/storage/V1 dependency: {module}",
                )

    def test_services_expose_typed_command_entrypoints(self) -> None:
        expected = {
            "planning_service.py": ("rebuild_command", "PlanningRebuildCommand"),
            "allocation_service.py": (
                "create_manual_command",
                "update_manual_command",
                "release_manual_command",
                "delete_manual_command",
                "assign_segment_command",
                "ManualAllocationCreateCommand",
            ),
            "quick_shift_service.py": ("create_command", "QuickShiftCreateCommand"),
            "demand_service.py": (
                "create_command",
                "modify_command",
                "submit_command",
                "approve_command",
                "request_correction_command",
                "cancel_command",
                "DemandCreateCommand",
                "DemandUpdateCommand",
            ),
            "segment_service.py": (
                "create_command",
                "update_command",
                "cancel_command",
                "SegmentCreateCommand",
                "SegmentUpdateCommand",
            ),
        }
        for filename, tokens in expected.items():
            source = (APPLICATION / filename).read_text(encoding="utf-8")
            for token in tokens:
                self.assertIn(token, source, f"{filename} missing 6A seam {token}")

    def test_services_share_one_adapter_error_translation_policy(self) -> None:
        for filename in self.SERVICE_FILES:
            source = (APPLICATION / filename).read_text(encoding="utf-8")
            self.assertIn(
                "call_application_port",
                source,
                f"{filename} must use the shared application-port executor",
            )
            self.assertNotIn(
                "application_error_from_exception",
                source,
                f"{filename} must not own adapter-error translation policy",
            )

    def test_demand_and_segment_services_store_ports_not_repository_context(self) -> None:
        demand = (APPLICATION / "demand_service.py").read_text(encoding="utf-8")
        segment = (APPLICATION / "segment_service.py").read_text(encoding="utf-8")

        for source in (demand, segment):
            self.assertNotIn("repository_context", source)
            self.assertNotIn("self._repository", source)
            self.assertNotIn("rebuild_planning", source)
        self.assertIn("ApprovedDemandSyncPort", demand)
        self.assertIn("PlanningCommandPort", demand)
        self.assertIn("SegmentRepositoryPort", segment)
        self.assertIn("PlanningCommandPort", segment)

if __name__ == "__main__":
    unittest.main()
