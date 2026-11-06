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

    def test_runbook_keeps_writers_stopped_through_readonly_sql_and_migration(self) -> None:
        runbook = (
            ROOT / "docs" / "ERP_SYNC_OPERATIONS_RUNBOOK.md"
        ).read_text(encoding="utf-8")

        stop = runbook.index("docker compose stop -t 7500 backend")
        readonly_sql = runbook.index("SELECT\n       c.id,")
        migration = runbook.index("docker compose run --rm --no-deps migrate")
        disabled_start = runbook.index(
            "docker compose up -d --no-deps --no-build backend"
        )

        self.assertLess(stop, readonly_sql)
        self.assertLess(readonly_sql, migration)
        self.assertLess(migration, disabled_start)
        self.assertNotIn(
            "--force-recreate backend",
            runbook[stop:migration],
        )
        self.assertNotIn(
            "docker compose up",
            runbook[stop:migration],
        )
        self.assertIn("erp_sync_control", runbook[readonly_sql:migration])
        self.assertIn("erp_sync_attempts", runbook[readonly_sql:migration])
        self.assertIn("erp_sync_recovery_state", runbook[readonly_sql:migration])
        self.assertIn("protocol_version", runbook[readonly_sql:migration])
        self.assertIn("config_fingerprint", runbook[readonly_sql:migration])
        self.assertIn("generation", runbook[readonly_sql:migration])
        self.assertIn("owner_attempt_id", runbook[readonly_sql:migration])
        self.assertIn("premier déploiement 643c", runbook.lower())
        self.assertIn("RESOURCEPLANNER_ERP_SYNC_ENABLED=false", runbook)
        self.assertIn("stop_grace_period: 2h5m", runbook)

    def test_migrate_service_is_one_shot_and_does_not_launch_application_runtime(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        migrate_start = compose.index("  migrate:")
        import_projects = compose.index("  import-projects:")
        migrate = compose[migrate_start:import_projects]

        self.assertIn(
            'command: ["python", "-m", "alembic", "upgrade", "head"]',
            migrate,
        )
        self.assertIn('restart: "no"', migrate)
        self.assertNotIn("app.server", migrate)
        self.assertNotIn("erp-sync-scheduler", migrate)

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
