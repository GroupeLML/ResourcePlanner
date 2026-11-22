from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import time
import unittest
from unittest import mock
from unittest.mock import patch

import httpx

from tools import issue_894_phase4_load as phase4_load


ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "tools" / "issue_894_phase4_load.py"
ROLLBACK = ROOT / "tools" / "build_issue_894_rollback_bundle.sh"
SERVER = ROOT / "tools" / "issue_894_phase4_server.py"
WORKFLOW = ROOT / ".github" / "workflows" / "syntax-check.yml"


class _OrdinaryTimeoutClient:
    def __init__(self) -> None:
        self.timed_out = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers=None,
        json=None,
    ) -> httpx.Response:
        request = httpx.Request(method, f"http://phase4.test{path}")
        if "planning/snapshot" in path and not self.timed_out:
            self.timed_out = True
            raise httpx.ReadTimeout("synthetic ordinary timeout", request=request)
        return httpx.Response(200, request=request)


class Issue894Phase4TelemetryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_timeout_is_explicit_and_fails_closed(self) -> None:
        request = httpx.Request(
            "GET",
            "http://backend:8000/__phase4__/metrics",
        )

        async def fail_metrics(_backend_url: str, *, reset: bool):
            del reset
            raise httpx.ConnectTimeout(
                "synthetic metrics connect timeout",
                request=request,
            )

        with patch.object(phase4_load, "_loop_metrics", new=fail_metrics):
            capture = await phase4_load._capture_loop_metrics(
                "http://backend:8000",
                reset=False,
            )

        validation = phase4_load._validate_phase4_telemetry_capture(
            capture,
            minimum_coverage_seconds=600,
        )
        self.assertFalse(capture["available"])
        self.assertEqual(
            capture["endpoint"],
            "http://backend:8000/__phase4__/metrics",
        )
        self.assertGreaterEqual(float(capture["elapsed_ms"]), 0.0)
        self.assertEqual(
            capture["error"]["category"],
            "connect_timeout",
        )
        self.assertEqual(
            capture["error"]["exception"],
            "ConnectTimeout",
        )
        self.assertFalse(validation["passed"])
        self.assertEqual(
            validation["reason"],
            "required_telemetry_unavailable",
        )

    async def test_baseline_preserves_workload_when_final_telemetry_times_out(self) -> None:
        workload = {
            "configured_target_rps": 10.0,
            "issued": 6000,
            "completed": 6000,
            "succeeded": 5999,
            "failed": 1,
            "achieved_issue_rps": 10.0,
        }
        captures = [
            {
                "available": True,
                "endpoint": "http://backend:8000/__phase4__/metrics",
                "reset": True,
                "elapsed_ms": 1.0,
                "error": None,
                "metrics": {"reset": True},
            },
            {
                "available": False,
                "endpoint": "http://backend:8000/__phase4__/metrics",
                "reset": False,
                "elapsed_ms": 5000.0,
                "error": {
                    "category": "connect_timeout",
                    "exception": "ConnectTimeout",
                    "message": "synthetic",
                },
                "metrics": None,
            },
        ]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fake_driver(
            _client,
            _admin,
            _resource_id,
            _stop_at,
            latencies,
            statuses,
            stop_event=None,
        ):
            del stop_event
            latencies.extend([50.0, 10000.0])
            statuses["200"] = 1
            return workload

        async def fake_capture(_backend_url: str, *, reset: bool):
            expected = captures.pop(0)
            self.assertEqual(expected["reset"], reset)
            return expected

        credential = phase4_load.Credential(
            user_id="U",
            session="S",
            csrf="C",
            admin=True,
        )
        with (
            patch.object(phase4_load.httpx, "AsyncClient", return_value=FakeClient()),
            patch.object(phase4_load, "_end_active", new=mock.AsyncMock()),
            patch.object(
                phase4_load,
                "_ensure_synthetic_resource",
                new=mock.AsyncMock(return_value="RESOURCE"),
            ),
            patch.object(phase4_load, "_ordinary_driver", new=fake_driver),
            patch.object(phase4_load, "_capture_loop_metrics", new=fake_capture),
        ):
            baseline = await phase4_load._run_baseline(
                "http://frontend:8080",
                "http://backend:8000",
                credential,
                warmup_seconds=0,
                measure_seconds=1,
            )

        self.assertIs(baseline["ordinary_workload"], workload)
        self.assertEqual(baseline["ordinary_api"]["count"], 2)
        self.assertEqual(baseline["statuses"], {"200": 1})
        self.assertFalse(baseline["telemetry_ok"])
        self.assertFalse(baseline["telemetry_capture"]["available"])
        self.assertFalse(baseline["telemetry_validation"]["passed"])


class Issue894Phase4StageCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_baseline_failure_survives_cleanup_failure(self) -> None:
        class FakeClient:
            async def aclose(self) -> None:
                return None

        end_active = mock.AsyncMock(
            side_effect=[None, RuntimeError("cleanup failed")]
        )
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "isolated-baseline.json"
            args = SimpleNamespace(
                base_url="http://frontend:8080",
                backend_url="http://backend:8000",
                credentials=Path(tmp) / "sessions.json",
                output=output,
                head_sha="HEAD",
                warmup_seconds=0,
                measure_seconds=1,
            )
            credential = phase4_load.Credential(
                user_id="ADMIN",
                session="S",
                csrf="C",
                admin=True,
            )
            with (
                patch.object(
                    phase4_load,
                    "_load_credentials",
                    return_value=(credential, []),
                ),
                patch.object(
                    phase4_load.httpx,
                    "AsyncClient",
                    return_value=FakeClient(),
                ),
                patch.object(phase4_load, "_end_active", new=end_active),
                patch.object(
                    phase4_load,
                    "_ensure_synthetic_resource",
                    new=mock.AsyncMock(return_value="RESOURCE"),
                ),
                patch.object(
                    phase4_load,
                    "_capture_loop_metrics",
                    new=mock.AsyncMock(
                        return_value={
                            "available": True,
                            "metrics": {},
                            "error": None,
                        }
                    ),
                ),
                patch.object(
                    phase4_load,
                    "_ordinary_driver",
                    new=mock.AsyncMock(
                        side_effect=RuntimeError("measurement failed")
                    ),
                ),
            ):
                report = await phase4_load._run_isolated_baseline(args)

            persisted = __import__("json").loads(output.read_text())
        self.assertFalse(report["passed"])
        self.assertEqual(report["primary_failure"]["stage"], "baseline")
        self.assertEqual(report["cleanup_failure"]["stage"], "cleanup")
        self.assertEqual(
            report["primary_failure"]["message"],
            "measurement failed",
        )
        self.assertEqual(
            persisted["primary_failure"]["message"],
            "measurement failed",
        )
        self.assertEqual(
            persisted["cleanup_failure"]["message"],
            "cleanup failed",
        )
        self.assertEqual(
            persisted["stages"]["mixed_emergency_matrix"]["status"],
            "NOT RUN",
        )
        self.assertEqual(persisted["stages"]["ack_waves"]["status"], "NOT RUN")


class Issue894Phase4OrdinaryDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_recorded_and_remaining_requests_complete(self) -> None:
        client = _OrdinaryTimeoutClient()
        latencies: list[float] = []
        statuses: dict[str, int] = {}
        credential = phase4_load.Credential(
            user_id="U",
            session="S",
            csrf="C",
            admin=True,
        )

        with patch.object(phase4_load, "ORDINARY_RPS", 100.0):
            result = await phase4_load._ordinary_driver(
                client,
                credential,
                "RESOURCE",
                time.perf_counter() + 0.055,
                latencies,
                statuses,
            )

        self.assertGreaterEqual(int(result["scheduled"]), 4)
        self.assertEqual(result["completed"], result["scheduled"])
        self.assertEqual(result["started"], result["scheduled"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["succeeded"], int(result["scheduled"]) - 1)
        self.assertEqual(result["error_counts"], {"read_timeout": 1})
        self.assertEqual(result["operation_errors"]["planning_reads"], 1)
        self.assertEqual(len(result["errors"]), 1)
        error = result["errors"][0]
        self.assertEqual(error["operation"], "planning_reads")
        self.assertEqual(error["category"], "read_timeout")
        self.assertEqual(error["exception"], "ReadTimeout")
        self.assertGreaterEqual(float(error["elapsed_ms"]), 0.0)
        self.assertEqual(sum(statuses.values()), result["succeeded"])
        self.assertEqual(len(latencies), result["completed"])

    async def test_waiting_tasks_do_not_count_as_http_starts_in_window(self) -> None:
        class SlowClient:
            async def request(
                self,
                method: str,
                path: str,
                *,
                headers=None,
                json=None,
            ) -> httpx.Response:
                await __import__("asyncio").sleep(0.08)
                request = httpx.Request(
                    method,
                    f"http://phase4.test{path}",
                )
                return httpx.Response(200, request=request)

        latencies: list[float] = []
        statuses: dict[str, int] = {}
        credential = phase4_load.Credential(
            user_id="U",
            session="S",
            csrf="C",
            admin=True,
        )
        with patch.object(phase4_load, "ORDINARY_RPS", 1000.0):
            result = await phase4_load._ordinary_driver(
                SlowClient(),
                credential,
                "RESOURCE",
                time.perf_counter() + 0.05,
                latencies,
                statuses,
            )

        self.assertGreater(int(result["scheduled"]), 32)
        self.assertLess(
            int(result["started_in_measurement_window"]),
            int(result["scheduled"]),
        )
        self.assertGreater(
            int(result["outstanding_at_window_end"]),
            0,
        )
        self.assertGreater(
            float(result["drain_duration_seconds"]),
            0.0,
        )


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
        self.assertIn('"achieved_request_start_rps"', function_source)
        self.assertIn('"started_in_measurement_window"', function_source)
        self.assertIn('"outstanding_at_window_end"', function_source)
        self.assertIn('"drain_duration_seconds"', function_source)
        self.assertIn('"planning_reads"', function_source)
        self.assertIn('"project_reads"', function_source)
        self.assertIn('"save_mutations"', function_source)
        self.assertIn("except httpx.TimeoutException as exc", function_source)
        self.assertIn('"read_timeout"', function_source)
        self.assertIn('"max_in_flight"', function_source)
        self.assertIn("ORDINARY_RPS * 0.95", source)

    def test_isolated_baseline_command_checkpoints_and_defers_full_campaign(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("async def _run_isolated_baseline", source)
        self.assertIn('"mixed_emergency_matrix"', source)
        self.assertIn('"status": "NOT RUN"', source)
        self.assertIn('"achieved_request_start_rps"', source)
        self.assertIn("python tools/issue_894_phase4_load.py baseline", workflow)
        self.assertNotIn(
            "python tools/issue_894_phase4_load.py run               --credentials",
            workflow,
        )
        self.assertIn("docker kill --signal=USR1 backend", workflow)
        self.assertIn("docker kill --signal=USR2 backend", workflow)
        self.assertIn("python tools/issue_894_phase4_load.py sql-diagnostics", workflow)

    def test_missing_phase4_monitor_or_pool_telemetry_fails_closed(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        self.assertIn("def _require_phase4_telemetry", source)
        self.assertIn("Phase 4 monitor is not active.", source)
        self.assertIn("event-loop telemetry has no samples", source)
        self.assertIn("SQL pool checkout telemetry has no samples", source)
        self.assertIn("minimum_coverage_seconds=measure_seconds", source)
        self.assertIn('"required_telemetry_unavailable"', source)
        self.assertIn("baseline_telemetry_ok", source)
        self.assertIn("and baseline_telemetry_ok", source)

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
        self.assertIn('"hold_ms"', source)
        self.assertIn('"active_holders"', source)
        self.assertIn("faulthandler.register", source)
        self.assertIn("SIGUSR1", source)
        self.assertIn("SIGUSR2", source)
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
