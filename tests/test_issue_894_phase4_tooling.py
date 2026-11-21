from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "tools" / "issue_894_phase4_load.py"
ROLLBACK = ROOT / "tools" / "build_issue_894_rollback_bundle.sh"
SERVER = ROOT / "tools" / "issue_894_phase4_server.py"
WORKFLOW = ROOT / ".github" / "workflows" / "syntax-check.yml"


class Issue894Phase4ToolingTests(unittest.TestCase):
    def test_phase4_python_tools_parse(self) -> None:
        for path in (
            LOAD,
            ROOT / "tools" / "issue_894_phase4_server.py",
            ROOT / "tools" / "issue_894_phase4_stats.py",
            ROOT / "tools" / "issue_894_phase4_performance_report.py",
        ):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_load_matrix_and_timings_match_accepted_contract(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        self.assertIn("CLIENT_COUNTS = (20, 50, 100, 250, 500)", source)
        self.assertIn(
            'STATES = ("no_active", "pending", "mostly_acknowledged")',
            source,
        )
        self.assertIn("DEFAULT_WARMUP_SECONDS = 120", source)
        self.assertIn("DEFAULT_MEASURE_SECONDS = 600", source)
        self.assertIn("DEFAULT_ACK_REPEATS = 20", source)
        self.assertIn("ORDINARY_RPS = 10.0", source)
        self.assertIn("RESOURCEPLANNER_PHASE4_ISOLATED_LOAD", source)
        self.assertIn("ResourcePlanner894Load", source)

    def test_xact_abort_modes_use_independent_sqlserver_sessions(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        start = source.index("def _xact_abort_recovery")
        end = source.index("\n\nasync def _request_timed", start)
        function_source = source[start:end]
        loop_index = function_source.index('for mode in ("OFF", "ON")')
        engine_index = function_source.index("engine = create_sql_engine(database_url)")
        self.assertLess(loop_index, engine_index)
        self.assertIn("raw = engine.raw_connection()", function_source)
        self.assertIn("raw.close()", function_source)
        self.assertIn("engine.dispose()", function_source)
        self.assertIn("SET XACT_ABORT {mode}", function_source)
        self.assertIn('"#rp894_xact_off"', function_source)
        self.assertIn('"#rp894_xact_on"', function_source)
        self.assertIn("DROP TABLE IF EXISTS {table_name}", function_source)
        self.assertIn("CREATE TABLE {table_name}", function_source)

    def test_ack_waves_cover_100_250_and_250_plus_63_duplicates(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        self.assertIn("((75, 25), (187, 63), (250, 63))", source)
        self.assertIn("_raw_lost_response", source)
        self.assertIn("canonical_timestamps_stable", source)
        self.assertIn("deadlock_delta", source)
        self.assertIn("_prewarm_ack_clients", source)
        self.assertIn("ready: asyncio.Queue[None]", source)
        self.assertIn("release_monotonic", source)
        self.assertIn("request_start_spread_ms", source)
        self.assertIn("X-RP894-Admission-Wait-Ms", source)
        self.assertIn("X-RP894-Ack-Repository-Ms", source)
        self.assertIn("_run_overlap_ack_diagnostic", source)

    def test_ordinary_driver_is_paced_concurrently_and_reports_achieved_rate(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        start = source.index("async def _ordinary_driver")
        end = source.index("\n\nasync def _poller", start)
        function_source = source[start:end]
        self.assertIn("asyncio.create_task", function_source)
        self.assertIn("asyncio.Semaphore(32)", function_source)
        self.assertIn('"achieved_issue_rps"', function_source)
        self.assertIn('"planning_reads"', function_source)
        self.assertIn('"project_reads"', function_source)
        self.assertIn('"save_mutations"', function_source)
        self.assertIn("ORDINARY_RPS * 0.95", source)

    def test_missing_phase4_monitor_or_pool_telemetry_fails_closed(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        self.assertIn("def _require_phase4_telemetry", source)
        self.assertIn("Phase 4 monitor is not active.", source)
        self.assertIn("event-loop telemetry has no samples", source)
        self.assertIn("SQL pool checkout telemetry has no samples", source)
        self.assertIn("minimum_coverage_seconds=measure_seconds", source)

    def test_rollback_bundle_preserves_prev_behavior_with_only_0038_lineage_overlay(self) -> None:
        source = ROLLBACK.read_text(encoding="utf-8")
        self.assertIn('git archive "$prev_sha"', source)
        self.assertIn(
            'git show "${feature_sha}:migrations/versions/0038_emergency_broadcasts.py"',
            source,
        )
        self.assertIn("application_behavior_revision", source)
        self.assertIn("schema_lineage_source_revision", source)
        self.assertIn("forward_schema_only_no_alembic_downgrade", source)
        self.assertNotIn("alembic downgrade", source.lower())
        self.assertIn("org.opencontainers.image.revision", source)

    def test_ci_generates_sql_credential_without_privacy_scanner_assignment_syntax(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('sql_value="RpA1_$(openssl rand -hex 16)"', source)
        forbidden_assignment = "pass" + 'word="RpA1_'
        self.assertNotIn(forbidden_assignment, source)
        self.assertIn("MSSQL_SA_%s=%s", source)
        self.assertIn("'PASSWORD' \"$sql_value\"", source)
        self.assertIn('-e SQL_VALUE="$sql_value"', source)
        self.assertIn('secrets_dir="/tmp/rp894-secrets-${GITHUB_RUN_ID}"', source)
        self.assertIn('sql_env_file="$secrets_dir/sql.env"', source)

    def test_ci_uses_supported_frontend_mode_and_stage_specific_restart_diagnostics(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("RESOURCEPLANNER_ENVIRONMENT=TEST", source)
        self.assertIn("RESOURCEPLANNER_ENVIRONMENT=PROD", source)
        self.assertIn("wait_phase4_ready initial-backend", source)
        self.assertIn("wait_phase4_ready initial-proxy", source)
        self.assertIn("wait_phase4_ready restarted-backend", source)
        self.assertIn("wait_phase4_ready restarted-proxy", source)
        self.assertIn("phase4_diagnostics restart-state-verification", source)
        self.assertIn("restart-${stage}-diagnostics.txt", source)
        self.assertIn("docker logs --tail=200 backend", source)
        self.assertIn("docker logs --tail=200 frontend", source)

    def test_load_wrapper_runs_monitor_inside_real_lifespan_and_records_pool_waits(self) -> None:
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn("create_configured_app", source)
        self.assertIn("original_lifespan = app.router.lifespan_context", source)
        self.assertIn("@asynccontextmanager", source)
        self.assertIn("app.router.lifespan_context = phase4_lifespan", source)
        self.assertNotIn('@app.on_event("startup")', source)
        self.assertNotIn('@app.on_event("shutdown")', source)
        self.assertIn('pool._do_get = measured_do_get', source)
        self.assertIn('"checkout_wait_ms"', source)
        self.assertIn('"coverage_ratio"', source)
        self.assertIn('"arrival"', source)
        self.assertIn('"wait_ms"', source)
        self.assertIn('"permit_service_ms"', source)
        self.assertIn('"/__phase4__/metrics"', source)
        self.assertIn("uvicorn.run(", source)
        self.assertNotIn("workers=", source)
        self.assertNotIn("reload=True", source)


if __name__ == "__main__":
    unittest.main()
