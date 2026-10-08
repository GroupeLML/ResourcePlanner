from __future__ import annotations

import ast
from pathlib import Path
import unittest

from app.application.commands import PlanningRebuildCommand
from app.application.errors import ApplicationOperationError
from app.application.planning_service import PlanningService


class _PlanningCommands:
    def __init__(self, result=None, failure: Exception | None = None) -> None:
        self.calls = 0
        self.result = result or {"allocated_hours": 24.0, "engine": "pure"}
        self.failure = failure

    def rebuild(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class PlanningServiceTests(unittest.TestCase):
    def test_typed_rebuild_command_delegates_once_and_returns_plain_dict(self) -> None:
        commands = _PlanningCommands()

        result = PlanningService(commands).rebuild_command(PlanningRebuildCommand())

        self.assertEqual(commands.calls, 1)
        self.assertEqual(result, {"allocated_hours": 24.0, "engine": "pure"})
        self.assertIsInstance(result, dict)

    def test_legacy_rebuild_routes_through_typed_command(self) -> None:
        commands = _PlanningCommands()
        result = PlanningService(commands).rebuild()

        self.assertEqual(commands.calls, 1)
        self.assertEqual(result["engine"], "pure")

    def test_rebuild_translates_adapter_failure_to_application_error(self) -> None:
        commands = _PlanningCommands(failure=RuntimeError("planning failed"))

        with self.assertRaises(ApplicationOperationError) as raised:
            PlanningService(commands).rebuild_command(PlanningRebuildCommand())

        self.assertEqual(str(raised.exception), "planning failed")
        self.assertEqual(raised.exception.code, "planning_rebuild_failed")


    def test_application_service_has_no_ui_storage_or_v1_import(self) -> None:
        path = Path(__file__).resolve().parents[1] / "app" / "application" / "planning_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")

        forbidden_prefixes = (
            "nicegui",
            "xlwings",
            "app.excel_repository",
            "app.v13",
            "app.v14",
            "app.v15",
            "app.v16",
            "app.v17",
            "app.v18",
        )
        for module in imported_modules:
            self.assertFalse(
                module.startswith(forbidden_prefixes),
                f"planning_service.py must stay transport/storage agnostic; found import {module}",
            )


    def test_v1_runtime_bridge_is_absent(self) -> None:
        path = Path(__file__).resolve().parents[1] / "app/application/runtime_services.py"
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
