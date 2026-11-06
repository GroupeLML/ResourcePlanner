from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ErpActivationDeploymentContractTests(unittest.TestCase):
    def test_compose_wires_full_typed_erp_safety_surface_and_keeps_disabled_default(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        disabled_default = (
            'RESOURCEPLANNER_ERP_SYNC_ENABLED: "'
            + "$"
            + '{RESOURCEPLANNER_ERP_SYNC_ENABLED:-false}"'
        )
        required = (
            disabled_default,
            "RESOURCEPLANNER_ACUMATICA_BASE_URL",
            "RESOURCEPLANNER_ACUMATICA_USERNAME",
            "RESOURCEPLANNER_ACUMATICA_PASSWORD",
            "RESOURCEPLANNER_ACUMATICA_PAGE_SIZE",
            "RESOURCEPLANNER_ACUMATICA_TIMEOUT_SECONDS",
            "RESOURCEPLANNER_ERP_SYNC_TIMEZONE",
            "RESOURCEPLANNER_ERP_SYNC_TIME",
            "RESOURCEPLANNER_ERP_SYNC_CATCHUP_HOURS",
            "RESOURCEPLANNER_ERP_SYNC_MIN_SOURCE_RATIO",
            "RESOURCEPLANNER_ERP_SYNC_MIN_PROJECTS",
            "RESOURCEPLANNER_ERP_SYNC_MIN_TASK_ROWS",
            "RESOURCEPLANNER_ERP_SYNC_MAX_EVIDENCE_MIB",
            "RESOURCEPLANNER_ERP_SYNC_EVIDENCE_BUDGET_MIB",
            "RESOURCEPLANNER_ERP_SYNC_FAILURE_RETENTION_DAYS",
            "RESOURCEPLANNER_ERP_SYNC_HISTORY_DAYS",
            "RESOURCEPLANNER_ERP_SYNC_FRESHNESS_SLA_HOURS",
            "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, compose)

    def test_backend_has_explicit_grace_matching_bounded_attempt_and_lease(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("stop_grace_period: 2h5m", compose)

    def test_runbook_drains_current_writers_before_migration_or_generic_up(self) -> None:
        runbook = (
            ROOT / "docs" / "ERP_SYNC_OPERATIONS_RUNBOOK.md"
        ).read_text(encoding="utf-8")
        drain = runbook.index("## 5. Drain avant déploiement")
        migration = runbook.index("appliquer explicitement la migration")
        self.assertLess(drain, migration)
        self.assertIn("Modifier `.env` ou exporter", runbook)
        self.assertIn(
            "ne reconfigure jamais un backend déjà en cours",
            runbook,
        )
        self.assertIn("docker compose stop -t 7500 backend", runbook)
        self.assertIn(
            "docker compose up -d --no-deps --no-build --force-recreate backend",
            runbook,
        )
        self.assertIn("current_owner_attempt_id", runbook)
        self.assertIn("stop_grace_period: 2h5m", runbook)
        self.assertIn("RESOURCEPLANNER_ERP_SYNC_ENABLED=false", runbook)
        self.assertNotIn(
            "export RESOURCEPLANNER_ERP_SYNC_ENABLED=false\ndocker compose up -d\n",
            runbook,
        )

    def test_backend_image_packages_timezone_and_activation_preflight(self) -> None:
        dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
        self.assertIn("tzdata", dockerfile)
        self.assertIn("tools/check_erp_activation_readiness.py", dockerfile)
        self.assertIn("tools/check_server_runtime.py", dockerfile)

    def test_643c_ci_runs_real_sqlserver_and_packaged_timezone_proof(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "syntax-check.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Validate packaged Toronto IANA timezone data", workflow)
        self.assertIn("startsWith(github.event.pull_request.title, '643C')", workflow)
        self.assertIn("tests.test_erp_sync_sqlserver_concurrency", workflow)

    def test_runbook_never_claims_or_performs_implicit_production_activation(self) -> None:
        runbook = (
            ROOT / "docs" / "ERP_SYNC_OPERATIONS_RUNBOOK.md"
        ).read_text(encoding="utf-8")
        self.assertIn("RESOURCEPLANNER_ERP_SYNC_ENABLED=false", runbook)
        self.assertIn("--dedicated-account-confirmed", runbook)
        self.assertIn("--activation", runbook)
        self.assertIn("Rollback code ≠ rollback de données", runbook)
        self.assertIn("cron DSM", runbook)


if __name__ == "__main__":
    unittest.main()
