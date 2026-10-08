from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class SupportedWebWorkflowsTests(unittest.TestCase):
    def test_obsolete_windows_wrappers_are_removed(self) -> None:
        for name in (
            "Installer_Web.bat",
            "Lancer_Web.bat",
            "Lancer_Serveur.bat",
            "Verifier_Web.bat",
            "Charger_Donnees_Demo.bat",
            "Installer_Importateur_Projets.bat",
            "Importer_Projets_ERP.bat",
            "Importer_Taches_ERP.bat",
        ):
            with self.subTest(path=name):
                self.assertFalse((ROOT / name).exists())

    def test_isolated_backend_and_react_build_survive(self) -> None:
        backend = source("Dockerfile.backend")
        frontend = source("frontend/Dockerfile")
        requirements = source("requirements-server.txt").casefold()
        self.assertIn("requirements-server.txt", backend)
        self.assertIn('CMD ["python", "-m", "app.server"]', backend)
        self.assertIn("npm run build", frontend)
        self.assertIn("/usr/share/nginx/html", frontend)
        for legacy in ("nicegui", "xlwings", "openpyxl"):
            self.assertNotRegex(requirements, rf"(?m)^\\s*{legacy}(?:[=<>~!]|\\s|$)")

    def test_compose_imports_and_demo_remain_explicit(self) -> None:
        compose = source("docker-compose.yml")
        self.assertRegex(compose, r'(?m)^  seed-dev:\\n    profiles: \\["demo"\\]')
        self.assertIn('"--confirm-dev-only"', compose)
        self.assertRegex(compose, r'(?m)^  import-projects:\\n    profiles: \\["tools"\\]')
        self.assertRegex(compose, r'(?m)^  import-tasks:\\n    profiles: \\["tools"\\]')
        self.assertIn("Dockerfile.importer", compose)
        self.assertIn("tools/import_erp_projects.py", source("Dockerfile.importer"))
        self.assertIn("tools/import_erp_tasks.py", source("Dockerfile.importer"))

    def test_documented_cli_hmr_and_smokes_are_preserved(self) -> None:
        doc = source("docs/REACT_V2_DEV.md")
        for token in (
            "python -m alembic upgrade head",
            "python -m app.server",
            "npm run dev",
            "npm run build",
            "RESOURCEPLANNER_DATABASE_URL",
            "RESOURCEPLANNER_FRONTEND_DIST",
            "python tools/check_web_runtime.py",
            "docker compose --profile demo run --rm seed-dev",
        ):
            with self.subTest(token=token):
                self.assertIn(token, doc)
        for tool in (
            "tools/check_installed_web.py",
            "tools/check_server_runtime.py",
            "tools/check_web_runtime.py",
            "tools/smoke_running_web.py",
            "tools/seed_demo_data.py",
        ):
            self.assertTrue((ROOT / tool).is_file())

    def test_import_cli_preserves_preview_and_opt_in_apply(self) -> None:
        for path in ("tools/import_erp_projects.py", "tools/import_erp_tasks.py"):
            with self.subTest(path=path):
                code = source(path)
                self.assertIn('action="store_true"', code)
                self.assertIn('"--apply"', code)
                self.assertIn("transaction.rollback()", code)
                self.assertIn("transaction.commit()", code)


if __name__ == "__main__":
    unittest.main()
