from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.cutover_inventory import (
    LEGACY_REQUIREMENTS,
    _scan_python_boundary,
    build_inventory,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class CutoverInventoryTests(unittest.TestCase):
    def test_retired_v1_runtime_has_no_canonical_boundary_debt(self) -> None:
        inventory = build_inventory(REPO_ROOT)
        self.assertEqual(inventory.unexpected_boundary_violations, ())
        self.assertEqual(inventory.known_boundary_debt, ())

    def test_removed_runtime_stays_removed_without_erasing_erp_import(self) -> None:
        inventory = build_inventory(REPO_ROOT)
        self.assertEqual(inventory.legacy_entrypoints, ())
        self.assertEqual(inventory.migration_tools, ())
        self.assertEqual(inventory.versioned_modules, ())
        self.assertEqual(inventory.compatibility_modules, ())
        self.assertEqual(inventory.ui_modules, ())
        self.assertEqual(inventory.excel_modules, ())
        # Aggregate CI dependencies remain until the separate 336D cleanup.
        self.assertEqual(set(inventory.legacy_requirements), LEGACY_REQUIREMENTS)
        for obsolete in (
            "main.py",
            "app/application/runtime_services.py",
            "app/runtime_composition.py",
            "app/infrastructure/excel",
            "app/infrastructure/migration",
            "requirements-legacy.txt",
            ".github/workflows/windows-release.yml",
        ):
            with self.subTest(path=obsolete):
                self.assertFalse((REPO_ROOT / obsolete).exists())
        self.assertTrue(
            (REPO_ROOT / "app/infrastructure/erp_export/excel_project_source.py").is_file()
        )

    def test_forbidden_external_dependency_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "app/server/bad.py"
            path.parent.mkdir(parents=True)
            path.write_text("import xlwings\n", encoding="utf-8")

            violations = _scan_python_boundary(
                root,
                path,
                allowed_prefixes=("app.server", "app.application"),
            )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].imported_module, "xlwings")

    def test_legacy_app_import_is_detected_but_canonical_import_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "app/server/bad.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                "from app.application import ApplicationFacade\n"
                "from app.excel_repository import ExcelRepository\n",
                encoding="utf-8",
            )

            violations = _scan_python_boundary(
                root,
                path,
                allowed_prefixes=("app.server", "app.application"),
            )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].imported_module, "app.excel_repository")

    def test_relative_import_from_package_init_is_resolved_inside_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "app/application/__init__.py"
            path.parent.mkdir(parents=True)
            path.write_text("from .commands import DemandCreateCommand\n", encoding="utf-8")

            violations = _scan_python_boundary(
                root,
                path,
                allowed_prefixes=("app.application", "app.domain"),
            )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
