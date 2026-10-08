from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WebLauncherContractTests(unittest.TestCase):
    def test_web_installer_uses_isolated_server_dependencies_and_builds_react(self) -> None:
        installer = (ROOT / "Installer_Web.bat").read_text(encoding="utf-8")

        self.assertIn(".venv-web", installer)
        self.assertIn("requirements-server.txt", installer)
        self.assertIn("constraints-release.txt", installer)
        self.assertNotIn("requirements-legacy.txt", installer)
        self.assertIn("where node", installer)
        self.assertIn("npm install --no-audit --no-fund", installer)
        self.assertIn("npm run build", installer)
        self.assertIn("frontend\\dist\\index.html", installer)

    def test_web_launcher_requires_build_and_enables_same_origin_frontend(self) -> None:
        launcher = (ROOT / "Lancer_Web.bat").read_text(encoding="utf-8")

        self.assertIn(".venv-web\\Scripts\\python.exe", launcher)
        self.assertIn("frontend\\dist\\index.html", launcher)
        self.assertIn("frontend\\dist\\assets", launcher)
        self.assertIn("RESOURCEPLANNER_FRONTEND_DIST", launcher)
        self.assertIn('".venv-web\\Scripts\\python.exe" -m app.server', launcher)

    def test_local_sqlite_migrations_remain_guarded_in_web_launcher(self) -> None:
        launcher = (ROOT / "Lancer_Web.bat").read_text(encoding="utf-8")

        self.assertIn("if not defined RESOURCEPLANNER_DATABASE_URL", launcher)
        self.assertIn(
            'set "RESOURCEPLANNER_DATABASE_URL=sqlite:///./resourceplanner_server.db"',
            launcher,
        )
        self.assertIn('set "LOCAL_SQLITE_MODE=1"', launcher)
        self.assertIn('if "%LOCAL_SQLITE_MODE%"=="1"', launcher)
        self.assertIn('".venv-web\\Scripts\\python.exe" -m alembic upgrade head', launcher)


    def test_v1_desktop_launchers_are_absent(self) -> None:
        self.assertFalse((ROOT / "Lancer_Application.bat").exists())
        self.assertFalse((ROOT / "Lancer_Application_Legacy.bat").exists())
        self.assertFalse((ROOT / "main.py").exists())


if __name__ == "__main__":
    unittest.main()
