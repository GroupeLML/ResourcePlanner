from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentContractTests(unittest.TestCase):
    def test_backend_image_uses_only_web_server_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("requirements-server.txt", dockerfile)
        self.assertIn('CMD ["python", "-m", "app.server"]', dockerfile)
        self.assertNotIn("requirements.txt", dockerfile)
        self.assertNotIn("nicegui", dockerfile.casefold())
        self.assertNotIn("xlwings", dockerfile.casefold())

    def test_importer_image_packages_project_and_task_importers(self) -> None:
        dockerfile = (ROOT / "Dockerfile.importer").read_text(encoding="utf-8")
        self.assertIn("tools/import_erp_projects.py", dockerfile)
        self.assertIn("tools/import_erp_tasks.py", dockerfile)
        self.assertIn("requirements-importer.txt", dockerfile)

    def test_frontend_image_builds_react_then_serves_with_nginx(self) -> None:
        dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM node:22-alpine AS build", dockerfile)
        self.assertIn("npm run build", dockerfile)
        self.assertIn("FROM nginx:", dockerfile)
        self.assertIn("/usr/share/nginx/html", dockerfile)

        nginx = (ROOT / "frontend" / "default.conf.template").read_text(encoding="utf-8")
        self.assertIn("proxy_pass http://backend:8000", nginx)
        self.assertIn("location /api/", nginx)
        self.assertIn("location = /health", nginx)
        self.assertIn("location = /ready", nginx)
        self.assertIn("try_files $uri $uri/ /index.html", nginx)

    def test_local_compose_has_explicit_migration_gate_and_no_database_container(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("migrate:", compose)
        self.assertIn('command: ["python", "-m", "alembic", "upgrade", "head"]', compose)
        self.assertIn("condition: service_completed_successfully", compose)
        self.assertIn("seed-dev:", compose)
        self.assertIn('command: ["python", "tools/seed_demo_data.py"]', compose)
        self.assertIn("RESOURCEPLANNER_DEV_USER_SWITCHER", compose)
        self.assertIn("RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY", compose)
        self.assertIn(
            'RESOURCEPLANNER_OIDC_CLAIM_DIAGNOSTICS: "${RESOURCEPLANNER_OIDC_CLAIM_DIAGNOSTICS:-false}"',
            compose,
        )
        self.assertIn("import-projects:", compose)
        self.assertIn("import-tasks:", compose)
        self.assertIn('entrypoint: ["python", "tools/import_erp_tasks.py"]', compose)
        self.assertIn("backend:", compose)
        self.assertIn("frontend:", compose)
        self.assertNotIn("mssql:", compose.casefold())
        self.assertNotIn("sqlserver:", compose.casefold())

    def test_synology_compose_requires_version_tag_and_keeps_migration_manual(self) -> None:
        compose = (ROOT / "deploy" / "synology" / "compose.yml").read_text(encoding="utf-8")
        self.assertIn("RESOURCEPLANNER_IMAGE_TAG:?", compose)
        self.assertIn("RESOURCEPLANNER_DATABASE_URL:?", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn('RESOURCEPLANNER_DEV_USER_SWITCHER: "false"', compose)
        self.assertIn("RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY", compose)
        env_example = (ROOT / "deploy" / "synology" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY=", env_example)
        self.assertNotIn("alembic upgrade head", compose)
        self.assertNotIn("migrate:", compose)


if __name__ == "__main__":
    unittest.main()
