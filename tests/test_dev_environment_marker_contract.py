from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DevEnvironmentMarkerContractTests(unittest.TestCase):
    def test_dev_environment_flows_from_compose_to_vite(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn(
            'VITE_RESOURCEPLANNER_ENVIRONMENT: "${RESOURCEPLANNER_ENVIRONMENT:-PROD}"',
            compose,
        )
        self.assertIn("ARG VITE_RESOURCEPLANNER_ENVIRONMENT=PROD", dockerfile)
        self.assertIn(
            "ENV VITE_RESOURCEPLANNER_ENVIRONMENT=${VITE_RESOURCEPLANNER_ENVIRONMENT}",
            dockerfile,
        )
        self.assertIn("RESOURCEPLANNER_ENVIRONMENT=PROD", env_example)

    def test_dev_marker_is_opt_in_and_scoped(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("import.meta.env.VITE_RESOURCEPLANNER_ENVIRONMENT", app)
        self.assertIn('RUNTIME_ENVIRONMENT === "DEV"', app)
        self.assertIn("ENVIRONNEMENT DEV", app)
        self.assertIn("is-dev-environment", app)

        self.assertIn(".app-shell.is-dev-environment .app-sidebar", styles)
        self.assertIn(".app-shell.is-dev-environment .topbar", styles)
        self.assertIn(".app-shell.is-dev-environment .environment-banner", styles)


if __name__ == "__main__":
    unittest.main()
