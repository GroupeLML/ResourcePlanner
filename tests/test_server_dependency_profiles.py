from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SERVER_PACKAGES = {"nicegui", "xlwings", "openpyxl"}


def _direct_packages(path: Path) -> set[str]:
    packages: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if separator in line:
                line = line.split(separator, 1)[0]
                break
        packages.add(line.strip().casefold())
    return packages


class ServerDependencyProfileTests(unittest.TestCase):
    def test_server_profile_excludes_legacy_ui_and_excel_packages(self) -> None:
        packages = _direct_packages(ROOT / "requirements-server.txt")

        self.assertTrue({"fastapi", "uvicorn", "sqlalchemy", "alembic", "httpx"} <= packages)
        self.assertEqual(packages & FORBIDDEN_SERVER_PACKAGES, set())


    def test_v1_dependency_profile_and_installer_are_removed(self) -> None:
        self.assertFalse((ROOT / "requirements-legacy.txt").exists())
        self.assertFalse((ROOT / "Installer.bat").exists())

    def test_aggregate_profile_remains_backward_compatible_but_not_server_target(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        packages = _direct_packages(ROOT / "requirements.txt")

        self.assertIn("-r requirements-server.txt", text)
        self.assertEqual(packages & FORBIDDEN_SERVER_PACKAGES, FORBIDDEN_SERVER_PACKAGES)
        self.assertIn("Le serveur Web/SQL utilise requirements-server.txt", text)


    def test_ci_has_server_only_isolation_job(self) -> None:
        workflow = (ROOT / ".github/workflows/syntax-check.yml").read_text(encoding="utf-8")

        self.assertIn("server-isolation:", workflow)
        self.assertIn("requirements-server.txt", workflow)
        self.assertIn("check_server_dependency_isolation.py", workflow)
        self.assertIn("Guard Web/SQL boundaries from V1 imports", workflow)


if __name__ == "__main__":
    unittest.main()
