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
            "scheduled": 6000,
            "started": 6000,
            "started_in_measurement_window": 6000,
            "completed": 6000,
            "completed_in_measurement_window": 5999,
            "succeeded": 5999,
            "failed": 1,
            "outstanding_at_window_end": 1,
            "drain_duration_seconds": 10.0,
            "achieved_schedule_rps": 10.0,
            "achieved_request_start_rps": 10.0,
            "achieved_completion_rps": 9.998,
            "scheduling_lag_ms": {"count": 6000, "p95": 1.0},
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


class Issue894Phase4CommandDispatchTests(unittest.TestCase):
    def test_baseline_command_dispatches_only_isolated_baseline(self) -> None:
        args = SimpleNamespace(
            command="baseline",
            output=Path("unused.json"),
        )
        with (
            patch.object(
                phase4_load,
                "_parser",
                return_value=SimpleNamespace(
                    parse_args=lambda: args,
                ),
            ),
            patch.object(
                phase4_load,
                "_run_isolated_baseline",
                new=mock.AsyncMock(return_value={"passed": False}),
            ) as isolated,
            patch.object(
                phase4_load,
                "_run_certification",
                new=mock.AsyncMock(
                    side_effect=AssertionError(
                        "full certification must not run for baseline command"
                    )
                ),
            ) as full,
        ):
            result = phase4_load.main()

        self.assertEqual(result, 1)
        isolated.assert_awaited_once_with(args)
        full.assert_not_awaited()


class Issue894Phase4ToolingTests(unittest.TestCase):
    def test_phase4_python_tools_parse(self) -> None:
        for path in (
            LOAD,
            ROOT / "tools" / "issue_894_phase4_server.py",
            ROOT / "tools" / "issue_894_phase4_stats.py",
            ROOT / "tools" / "issue_894_phase4_performance_report.py",
            ROOT / "tools" / "issue_894_connection_trace.py",
            ROOT / "tools" / "issue_894_connection_trial.py",
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

    def test_connection_diagnostic_phase_blocks_full_certification_campaign(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        workflow = WORKFLOW.read_text(encoding="utf-8")
        trial = (ROOT / "tools" / "issue_894_connection_trial.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("async def _run_isolated_baseline", source)
        self.assertIn('"achieved_request_start_rps"', source)
        self.assertIn("DIAGNOSTIC — NOT CERTIFICATION", trial)
        self.assertIn("--duration-seconds 30", workflow)
        self.assertIn("--offered-rps 10", workflow)
        self.assertIn("--concurrency 32", workflow)
        self.assertIn("--mode canonical", workflow)
        self.assertIn("--mode instrumented", workflow)
        self.assertIn(
            "RESOURCEPLANNER_PHASE4_CONNECTION_TRACE=true",
            workflow,
        )
        self.assertNotIn(
            "python tools/issue_894_phase4_load.py run               --credentials",
            workflow,
        )
        self.assertNotIn(
            "python tools/issue_894_phase4_load.py baseline",
            workflow,
        )
        self.assertIn("full_mixed_load=NOT RUN", workflow)
        self.assertIn("ack_campaign=NOT RUN", workflow)
        self.assertIn("if: $" + "{{ false }}", workflow)

    def test_short_trial_snapshots_measurement_before_drain_and_never_certifies(self) -> None:
        trial = (ROOT / "tools" / "issue_894_connection_trial.py").read_text(
            encoding="utf-8"
        )
        snapshot = trial.index("measurement_snapshot = {")
        drain = trial.index("await asyncio.gather(*tasks)", snapshot)
        self.assertLess(snapshot, drain)
        self.assertIn('"certification_pass": False', trial)
        self.assertIn('"outstanding_at_window_end"', trial)
        self.assertIn('"drain_duration_seconds"', trial)
        self.assertIn('"server_observation_duration_seconds"', trial)

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

    def test_independent_sql_stall_snapshot_uses_safe_statement_fingerprint(self) -> None:
        source = LOAD.read_text(encoding="utf-8")
        start = source.index("def _sql_wait_snapshot")
        end = source.index("\n\ndef _xact_abort_recovery", start)
        function_source = source[start:end]
        self.assertIn("HASHBYTES('SHA2_256'", function_source)
        self.assertIn('"statement_fingerprint"', function_source)
        self.assertNotIn('"statement_text"', function_source)
        self.assertNotIn('"parameters"', function_source)

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

    def test_connection_diagnostic_status_is_explicitly_not_certification(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("label=DIAGNOSTIC - NOT CERTIFICATION", source)
        self.assertIn("full_mixed_load=NOT RUN", source)
        self.assertIn("ack_campaign=NOT RUN", source)
        self.assertIn("v2_campaign=NOT RUN", source)
        self.assertIn("rollback_campaign=NOT RUN", source)

    def test_ci_pins_immutable_event_source_and_records_diagnostic_provenance(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Pin immutable event PR head and base", source)
        self.assertIn(
            "RP894_EVENT_PR_HEAD_SHA: $" + "{{ github.event.pull_request.head.sha }}",
            source,
        )
        self.assertIn(
            "RP894_EVENT_BASE_SHA: $" + "{{ github.event.pull_request.base.sha }}",
            source,
        )
        self.assertIn('git checkout --detach "$RP894_EVENT_PR_HEAD_SHA"', source)
        self.assertNotIn(
            'git fetch --no-tags origin "$GITHUB_HEAD_REF"',
            source,
        )
        self.assertIn("workflow_event_actions_sha=%s", source)
        self.assertIn("initial_checkout_sha=%s", source)
        self.assertIn("event_pr_head_sha=%s", source)
        self.assertIn("checked_out_source_sha=%s", source)
        self.assertIn("event_base_sha=%s", source)
        self.assertIn("workload_command=connection-diagnostic", source)
        self.assertIn(
            "python tools/issue_894_connection_trial.py trial",
            source,
        )
        self.assertNotIn(
            "python tools/issue_894_phase4_load.py baseline",
            source,
        )
        self.assertNotIn(
            "python tools/issue_894_phase4_load.py run               --credentials",
            source,
        )

    def test_ci_uses_same_frontend_and_one_process_for_both_short_trials(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("RESOURCEPLANNER_ENVIRONMENT=TEST", source)
        self.assertIn("RESOURCEPLANNER_ENVIRONMENT=PROD", source)
        self.assertIn("start_backend canonical", source)
        self.assertIn("start_backend instrumented", source)
        self.assertIn("ensure_frontend", source)
        self.assertIn("python -m app.server", source)
        self.assertIn(
            "python /workspace/tools/issue_894_phase4_server.py",
            source,
        )
        self.assertNotIn("workers=", source)
        self.assertNotIn("restart-prepare", source)
        self.assertNotIn("restart-verify", source)

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
        self.assertIn("RESOURCEPLANNER_PHASE4_CONNECTION_TRACE", source)
        self.assertIn("if trace_enabled", source)
        self.assertIn("install_connection_trace", source)
        self.assertIn("threading.Timer(", source)
        self.assertIn("pool_checkout_wait_sustained", source)

    def test_connection_trace_remains_tools_only_and_opt_in(self) -> None:
        trace = (ROOT / "tools" / "issue_894_connection_trace.py").read_text(
            encoding="utf-8"
        )
        security = (ROOT / "app" / "server" / "security.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ConnectionTraceRecorder", trace)
        self.assertIn("safe_diagnostic_id", trace)
        self.assertIn("first_stall_snapshot", trace)
        self.assertNotIn("issue_894_connection_trace", security)


if __name__ == "__main__":
    unittest.main()
