from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import time
import unittest
from unittest import mock

import tools.issue_894_connection_trial as connection_trial

from tools.issue_894_connection_trace import (
    ConnectionTraceRecorder,
    _statement_target_role,
    safe_diagnostic_id,
)
from tools.issue_894_connection_trial import (
    DIAGNOSTIC_LABEL,
    FAILURE_RATE_MATERIAL_DELTA,
    TARGET_RESULT_FINGERPRINTS,
    _compare,
    _correlate_timeouts,
    _fetch_time_consistent,
    _ordered_xe_file_rows,
    _parse_xe_event_xml,
    _resolve_probe_server_anchor,
    _server_client_correlation,
    _server_execution_events,
    _summarize_trace,
    _validated_trial_report,
    _xe_cleanup,
    _xe_finalize_capture_cleanup,
    _xe_start,
    _xe_stop_collect,
    _xe_verify_files_removed,
)


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _trial_report(
    mode: str,
    **overrides: object,
) -> dict[str, object]:
    traced_mode = mode in {"instrumented", "profiled"}
    targeted = {
        fingerprint: {
            "evidence_complete": traced_mode,
            "fetch_profile": {
                "evidence_complete": mode == "profiled",
            },
        }
        for fingerprint in TARGET_RESULT_FINGERPRINTS
    }
    report: dict[str, object] = {
        "schema": "issue-894-connection-trial-v1",
        "label": DIAGNOSTIC_LABEL,
        "mode": mode,
        "certification_pass": False,
        "diagnostic_acceptance_behavior_failed": True,
        "achieved_request_start_rps": 3.5,
        "completed": 100,
        "failed": 50,
        "drain_duration_seconds": 20.0,
        "latency": {"p95_ms": 10000.0},
        "trace_summary": {
            "event_counts": (
                {
                    "dbapi_fetch": 3,
                    "result_consumer": 3,
                    **({"fetch_profile": 3} if mode == "profiled" else {}),
                }
                if traced_mode
                else {}
            ),
            "list_shifts_fingerprint": TARGET_RESULT_FINGERPRINTS[0],
            "target_fingerprints": TARGET_RESULT_FINGERPRINTS,
            "targeted_result_timing": targeted,
            "targeted_timing_complete": traced_mode,
            "fetch_profile_complete": mode == "profiled",
        },
        "server_client_correlation": (
            {"complete": True}
            if traced_mode
            else None
        ),
    }
    report.update(overrides)
    return report


def _comparison(
    canonical: dict[str, object],
    instrumented: dict[str, object],
) -> dict[str, object]:
    canonical_report = _trial_report("canonical", **canonical)
    instrumented_report = _trial_report("instrumented", **instrumented)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical_path = root / "canonical.json"
        instrumented_path = root / "instrumented.json"
        output_path = root / "comparison.json"
        canonical_path.write_text(
            json.dumps(canonical_report),
            encoding="utf-8",
        )
        instrumented_path.write_text(
            json.dumps(instrumented_report),
            encoding="utf-8",
        )
        return _compare(
            SimpleNamespace(
                canonical=canonical_path,
                instrumented=instrumented_path,
                output=output_path,
            )
        )


class ConnectionTraceRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_ids_are_opaque_and_do_not_preserve_session_like_input(self) -> None:
        candidate = "session-cookie-value"
        diagnostic_id = safe_diagnostic_id(candidate)
        self.assertNotEqual(diagnostic_id, candidate)
        self.assertRegex(diagnostic_id, r"^[a-f0-9]{16,32}$")

    async def test_connection_checkout_statement_transaction_and_return_share_request_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            request_id = "0123456789abcdef0123"
            request_token, family_token = recorder.begin_request(
                request_id,
                "planning_read",
            )
            spid_cursor = mock.MagicMock()
            spid_cursor.fetchone.return_value = (77,)
            dbapi_connection = mock.MagicMock()
            dbapi_connection.cursor.return_value = spid_cursor
            connection_record = SimpleNamespace(info={})
            connection = SimpleNamespace(
                connection=SimpleNamespace(
                    _connection_record=connection_record,
                )
            )
            checkout_started = recorder.pool_checkout_request()
            recorder.on_checkout(dbapi_connection, connection_record)
            recorder.pool_checkout_complete(checkout_started)
            statement_started = time.perf_counter()
            recorder.on_statement_start(connection, "SELECT synthetic")
            await asyncio.sleep(0)
            recorder.on_statement_end(
                connection,
                "SELECT synthetic",
                statement_started,
            )
            recorder.on_materialized()
            recorder.on_transaction_end(connection, "rollback")
            recorder.on_checkin(connection_record)
            recorder.end_request(request_id, status="200")
            recorder.reset_request_context(request_token, family_token)

            rows = _rows(path)

        kinds = [str(row["kind"]) for row in rows]
        self.assertLess(kinds.index("pool_checkout_request"), kinds.index("connection_acquired"))
        self.assertLess(kinds.index("connection_acquired"), kinds.index("statement_start"))
        self.assertLess(kinds.index("statement_start"), kinds.index("statement_end"))
        self.assertLess(kinds.index("statement_end"), kinds.index("transaction_end"))
        self.assertLess(kinds.index("transaction_end"), kinds.index("connection_return"))
        self.assertLess(kinds.index("connection_return"), kinds.index("request_end"))
        owned = [
            row
            for row in rows
            if row["kind"] in {
                "connection_acquired",
                "statement_start",
                "statement_end",
                "transaction_end",
                "connection_return",
                "request_end",
            }
        ]
        self.assertTrue(owned)
        self.assertTrue(all(row.get("request_id") == request_id for row in owned))
        probe = next(row for row in rows if row["kind"] == "spid_probe")
        started_row = next(
            row for row in rows if row["kind"] == "statement_start"
        )
        self.assertEqual(probe["connection_execution_sequence"], 1)
        self.assertEqual(
            started_row["connection_execution_sequence"],
            2,
        )
        returned = next(row for row in rows if row["kind"] == "connection_return")
        self.assertGreaterEqual(float(returned["holding_ms"]), 0.0)
        self.assertIn("post_statement_connection_residence_ms", returned)
        ended = next(row for row in rows if row["kind"] == "request_end")
        self.assertEqual(ended["orm_materialization_count"], 1)

    async def test_connection_execution_sequence_persists_across_checkouts(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            spid_cursor = mock.MagicMock()
            spid_cursor.fetchone.return_value = (77,)
            dbapi_connection = mock.MagicMock()
            dbapi_connection.cursor.return_value = spid_cursor
            connection_record = SimpleNamespace(info={})
            connection = SimpleNamespace(
                connection=SimpleNamespace(
                    _connection_record=connection_record,
                )
            )

            recorder.on_checkout(dbapi_connection, connection_record)
            first_sequence, first_spid, first_connection_sequence = (
                recorder.on_statement_start(connection, "SELECT first")
            )
            recorder.on_checkin(connection_record)

            recorder.on_checkout(dbapi_connection, connection_record)
            second_sequence, second_spid, second_connection_sequence = (
                recorder.on_statement_start(connection, "SELECT second")
            )
            recorder.on_checkin(connection_record)
            rows = _rows(path)

        self.assertEqual(first_spid, 77)
        self.assertEqual(second_spid, 77)
        self.assertEqual(first_sequence, 1)
        self.assertEqual(second_sequence, 1)
        self.assertEqual(first_connection_sequence, 2)
        self.assertEqual(second_connection_sequence, 3)
        probes = [row for row in rows if row["kind"] == "spid_probe"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["connection_execution_sequence"], 1)

    async def test_driver_execute_lifecycle_records_execution_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            context = SimpleNamespace(
                _rp894_trace_connection_id=101,
                _rp894_trace_spid=77,
                _rp894_trace_statement_sequence=4,
                _rp894_trace_connection_execution_sequence=9,
                _rp894_trace_fingerprint="abcdef0123456789",
            )
            recorder.record_driver_execute(
                context,
                kind="driver_execute_entry",
            )
            recorder.record_driver_execute(
                context,
                kind="driver_execute_return",
            )
            rows = _rows(path)

        self.assertEqual(
            [row["kind"] for row in rows],
            ["driver_execute_entry", "driver_execute_return"],
        )
        for row in rows:
            self.assertEqual(row["connection_id"], 101)
            self.assertEqual(row["spid"], 77)
            self.assertEqual(
                row["connection_execution_sequence"],
                9,
            )
            self.assertEqual(
                row["statement_fingerprint"],
                "abcdef0123456789",
            )
            self.assertNotIn("statement", row)
            self.assertNotIn("parameters", row)

    async def test_fetch_profiler_rejects_non_target_span(self) -> None:
        with TemporaryDirectory() as tmp:
            recorder = ConnectionTraceRecorder(
                Path(tmp) / "trace.jsonl",
                fetch_profile_enabled=True,
            )
            profile_id = recorder.begin_fetch_profile(
                fingerprint="not-target",
                connection_id=1,
                target_role="",
                spid=70,
                statement_sequence=1,
                connection_execution_sequence=2,
            )
        self.assertIsNone(profile_id)

    async def test_executor_submission_worker_start_completion_and_resume_are_ordered(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            request_id = "fedcba9876543210abcd"
            request_token, family_token = recorder.begin_request(
                request_id,
                "project_read",
            )
            phase_token = recorder.set_executor_phase("auth")

            async def original(func):
                return await asyncio.to_thread(func)

            result = await recorder.run_sync(original, lambda: "ok")
            recorder.reset_executor_phase(phase_token)
            recorder.end_request(request_id, status="200")
            recorder.reset_request_context(request_token, family_token)
            rows = _rows(path)

        self.assertEqual(result, "ok")
        events = {
            str(row["kind"]): row
            for row in rows
            if str(row["kind"]).startswith("executor_")
        }
        self.assertLess(
            float(events["executor_submit"]["monotonic"]),
            float(events["executor_worker_start"]["monotonic"]),
        )
        self.assertLessEqual(
            float(events["executor_worker_start"]["monotonic"]),
            float(events["executor_worker_complete"]["monotonic"]),
        )
        self.assertLessEqual(
            float(events["executor_worker_complete"]["monotonic"]),
            float(events["executor_coroutine_resume"]["monotonic"]),
        )
        self.assertTrue(
            all(row.get("phase") == "auth" for row in events.values())
        )
        self.assertGreaterEqual(
            float(events["executor_worker_start"]["queue_wait_ms"]),
            0.0,
        )

    async def test_timeout_can_correlate_with_later_server_end_and_connection_return(self) -> None:
        diagnostic_id = "00112233445566778899"
        timeout_at = 10.0
        errors = [
            {
                "diagnostic_id": diagnostic_id,
                "operation": "planning_reads",
                "category": "read_timeout",
            }
        ]
        request_records = {
            diagnostic_id: {
                "client_timeout_monotonic": timeout_at,
            }
        }
        trace_rows = [
            {
                "kind": "connection_return",
                "request_id": diagnostic_id,
                "monotonic": 12.0,
                "holding_ms": 8000.0,
                "post_statement_connection_residence_ms": 2500.0,
            },
            {
                "kind": "request_end",
                "request_id": diagnostic_id,
                "monotonic": 13.0,
                "server_request_ms": 13000.0,
                "orm_materialization_count": 4,
            },
        ]

        correlated = _correlate_timeouts(
            errors,
            request_records,
            trace_rows,
        )
        self.assertEqual(len(correlated), 1)
        self.assertIsNotNone(correlated[0]["server_request_end"])
        self.assertEqual(
            len(correlated[0]["connection_returns_after_client_timeout"]),
            1,
        )

    async def test_trace_summary_separates_auth_queue_sql_and_post_statement_residence(self) -> None:
        rows = [
            {
                "kind": "executor_worker_start",
                "phase": "auth",
                "queue_wait_ms": 25.0,
            },
            {
                "kind": "executor_worker_complete",
                "phase": "auth",
                "worker_ms": 80.0,
            },
            {
                "kind": "pool_checkout_complete",
                "wait_ms": 40.0,
            },
            {
                "kind": "statement_end",
                "statement_ms": 5.0,
            },
            {
                "kind": "connection_return",
                "holding_ms": 120.0,
                "post_statement_connection_residence_ms": 60.0,
            },
            {
                "kind": "request_end",
                "orm_materialization_count": 7,
            },
        ]

        summary = _summarize_trace(rows)
        self.assertEqual(
            summary["executor_by_phase"]["auth"]["queue_ms"]["p95_ms"],
            25.0,
        )
        self.assertEqual(
            summary["executor_by_phase"]["auth"]["worker_ms"]["p95_ms"],
            80.0,
        )
        self.assertEqual(summary["pool_checkout_wait_ms"]["p95_ms"], 40.0)
        self.assertEqual(summary["statement_execution_ms"]["p95_ms"], 5.0)
        self.assertEqual(
            summary["post_statement_connection_residence_ms"]["p95_ms"],
            60.0,
        )
        self.assertEqual(summary["connection_holding_ms"]["p95_ms"], 120.0)
        self.assertEqual(summary["orm_materialization_count"]["p95_ms"], 7.0)
        self.assertEqual(DIAGNOSTIC_LABEL, "DIAGNOSTIC — NOT CERTIFICATION")

    def test_comparator_tolerates_small_normalized_failure_variation(self) -> None:
        report = _comparison(
            {
                "achieved_request_start_rps": 3.50,
                "latency": {"p95_ms": 10007.7},
                "completed": 105,
                "failed": 76,
            },
            {
                "achieved_request_start_rps": 3.47,
                "latency": {"p95_ms": 10008.8},
                "completed": 104,
                "failed": 83,
            },
        )

        self.assertFalse(report["material_instrumentation_deterioration"])
        self.assertTrue(report["interpretation_allowed"])
        self.assertAlmostEqual(
            report["canonical"]["failure_rate"],
            76 / 105,
        )
        self.assertAlmostEqual(
            report["instrumented"]["failure_rate"],
            83 / 104,
        )
        self.assertLessEqual(
            report["delta"]["failure_rate"],
            FAILURE_RATE_MATERIAL_DELTA,
        )

    def test_comparator_detects_meaningful_failure_rate_degradation(self) -> None:
        report = _comparison(
            {
                "achieved_request_start_rps": 3.50,
                "latency": {"p95_ms": 10000.0},
                "completed": 100,
                "failed": 60,
            },
            {
                "achieved_request_start_rps": 3.50,
                "latency": {"p95_ms": 10000.0},
                "completed": 100,
                "failed": 75,
            },
        )

        self.assertTrue(report["material_instrumentation_deterioration"])
        self.assertFalse(report["interpretation_allowed"])
        self.assertGreater(
            report["delta"]["failure_rate"],
            FAILURE_RATE_MATERIAL_DELTA,
        )

    def test_comparator_still_detects_throughput_degradation(self) -> None:
        report = _comparison(
            {
                "achieved_request_start_rps": 10.0,
                "latency": {"p95_ms": 1000.0},
                "completed": 100,
                "failed": 20,
            },
            {
                "achieved_request_start_rps": 8.9,
                "latency": {"p95_ms": 1000.0},
                "completed": 100,
                "failed": 20,
            },
        )

        self.assertTrue(report["material_instrumentation_deterioration"])

    def test_comparator_still_detects_latency_degradation(self) -> None:
        report = _comparison(
            {
                "achieved_request_start_rps": 10.0,
                "latency": {"p95_ms": 1000.0},
                "completed": 100,
                "failed": 20,
            },
            {
                "achieved_request_start_rps": 10.0,
                "latency": {"p95_ms": 1200.0},
                "completed": 100,
                "failed": 20,
            },
        )

        self.assertTrue(report["material_instrumentation_deterioration"])

    def test_comparator_publishes_overhead_when_correlation_is_incomplete(self) -> None:
        report = _comparison(
            {
                "achieved_request_start_rps": 7.27,
                "latency": {"p95_ms": 1000.0},
                "completed": 100,
                "failed": 20,
            },
            {
                "achieved_request_start_rps": 6.07,
                "latency": {"p95_ms": 1000.0},
                "completed": 100,
                "failed": 20,
                "server_client_correlation": {"complete": False},
            },
        )

        self.assertFalse(report["correlation_complete"])
        self.assertTrue(
            report["material_instrumentation_deterioration"]
        )
        self.assertFalse(report["interpretation_allowed"])
        self.assertAlmostEqual(
            report["materiality_thresholds"][
                "request_start_rate_floor"
            ],
            7.27 * 0.90,
        )

    def test_fetch_profile_summary_is_target_scoped_and_separates_wall_cpu(self) -> None:
        rows = [
            {
                "kind": "dbapi_fetch",
                "statement_fingerprint": TARGET_RESULT_FINGERPRINTS[0],
                "target_role": "list_shifts_initial",
                "connection_id": 11,
                "request_id": "a" * 20,
                "fetched_row_count": 10,
                "fetch_elapsed_ms": 500.0,
                "fetch_cpu_ms": 15.0,
            },
            {
                "kind": "result_consumer",
                "statement_fingerprint": TARGET_RESULT_FINGERPRINTS[0],
                "connection_id": 11,
                "request_id": "a" * 20,
                "consumer_elapsed_ms": 510.0,
                "nested_fetch_elapsed_ms": 500.0,
                "non_nested_consumer_elapsed_ms": 10.0,
                "immediate_projection_elapsed_ms": 5.0,
                "worker_cpu_ms": 18.0,
                "non_nested_worker_cpu_ms": 3.0,
                "call_site": "app/infrastructure/sql/test.py:fn:1",
            },
            {
                "kind": "fetch_profile",
                "statement_fingerprint": TARGET_RESULT_FINGERPRINTS[0],
                "target_role": "list_shifts_initial",
                "connection_id": 11,
                "request_id": "a" * 20,
                "wall_elapsed_ms": 500.0,
                "thread_cpu_ms": 20.0,
                "sample_count": 10,
                "running_samples": 2,
                "waiting_samples": 7,
                "unresolved_samples": 1,
                "wait_channels": [
                    {"category": "wait_woken", "samples": 7},
                ],
                "native_stack_symbols": [],
                "python_stack_categories": [
                    {
                        "category": "python:cursor.py:fetchall",
                        "samples": 10,
                    },
                ],
                "native_stack_available": False,
                "payload_capture": False,
            },
            {
                "kind": "fetch_profile",
                "statement_fingerprint": "non-target",
                "target_role": "",
                "connection_id": 99,
                "wall_elapsed_ms": 9999.0,
                "thread_cpu_ms": 9999.0,
                "sample_count": 99,
                "running_samples": 99,
                "waiting_samples": 0,
                "unresolved_samples": 0,
            },
        ]

        summary = _summarize_trace(rows)
        detail = summary["targeted_result_timing"][
            TARGET_RESULT_FINGERPRINTS[0]
        ]["fetch_profile"]

        self.assertEqual(detail["profiled_fetch_spans"], 1)
        self.assertEqual(detail["wall_elapsed_ms"]["p50_ms"], 500.0)
        self.assertEqual(detail["thread_cpu_ms"]["p50_ms"], 20.0)
        self.assertEqual(detail["sample_count"], 10)
        self.assertAlmostEqual(detail["running_share"], 0.2)
        self.assertAlmostEqual(detail["waiting_share"], 0.7)
        self.assertAlmostEqual(detail["unresolved_share"], 0.1)
        self.assertEqual(
            detail["dominant_wait_channels"][0]["category"],
            "wait_woken",
        )
        self.assertFalse(detail["payload_capture"])

    def test_profiled_validation_requires_complete_target_profiles(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiled.json"
            report = _trial_report("profiled")
            report["trace_summary"]["fetch_profile_complete"] = False
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(ValueError):
                _validated_trial_report(
                    path,
                    expected_mode="profiled",
                )

    def test_profile_materiality_is_independent_of_sql_correlation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_path = root / "instrumented.json"
            profiled_path = root / "profiled.json"
            output = root / "profile-comparison.json"
            baseline = _trial_report(
                "instrumented",
                achieved_request_start_rps=4.0,
                completed=100,
                failed=1,
                latency={"p95_ms": 1000.0},
                server_client_correlation={"complete": False},
            )
            profiled = _trial_report(
                "profiled",
                achieved_request_start_rps=3.95,
                completed=100,
                failed=1,
                latency={"p95_ms": 1010.0},
                server_client_correlation={"complete": False},
            )
            baseline_path.write_text(
                json.dumps(baseline),
                encoding="utf-8",
            )
            profiled_path.write_text(
                json.dumps(profiled),
                encoding="utf-8",
            )

            report = connection_trial._compare_profiled(
                SimpleNamespace(
                    instrumented=baseline_path,
                    profiled=profiled_path,
                    output=output,
                )
            )

        self.assertFalse(report["material_profiling_deterioration"])
        self.assertTrue(report["interpretation_allowed"])

    def test_profile_materiality_fails_on_start_rate_regression(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_path = root / "instrumented.json"
            profiled_path = root / "profiled.json"
            output = root / "profile-comparison.json"
            baseline_path.write_text(
                json.dumps(
                    _trial_report(
                        "instrumented",
                        achieved_request_start_rps=4.0,
                    )
                ),
                encoding="utf-8",
            )
            profiled_path.write_text(
                json.dumps(
                    _trial_report(
                        "profiled",
                        achieved_request_start_rps=3.5,
                    )
                ),
                encoding="utf-8",
            )

            report = connection_trial._compare_profiled(
                SimpleNamespace(
                    instrumented=baseline_path,
                    profiled=profiled_path,
                    output=output,
                )
            )

        self.assertTrue(report["material_profiling_deterioration"])
        self.assertFalse(report["interpretation_allowed"])

    def test_canonical_reproduced_failure_is_valid_diagnostic_execution(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "canonical.json"
            report = _trial_report(
                "canonical",
                failed=59,
                diagnostic_acceptance_behavior_failed=True,
            )
            output.write_text(json.dumps(report), encoding="utf-8")
            validated = _validated_trial_report(
                output,
                expected_mode="canonical",
            )

        self.assertTrue(validated["diagnostic_acceptance_behavior_failed"])
        self.assertFalse(validated["certification_pass"])

    def test_instrumented_reproduced_failure_is_valid_diagnostic_execution(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "instrumented.json"
            report = _trial_report(
                "instrumented",
                failed=64,
                diagnostic_acceptance_behavior_failed=True,
            )
            output.write_text(json.dumps(report), encoding="utf-8")
            validated = _validated_trial_report(
                output,
                expected_mode="instrumented",
            )

        self.assertTrue(validated["diagnostic_acceptance_behavior_failed"])
        self.assertFalse(validated["certification_pass"])

    def test_missing_canonical_evidence_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "canonical.json"
            with self.assertRaises(FileNotFoundError):
                _validated_trial_report(
                    missing,
                    expected_mode="canonical",
                )

    def test_missing_instrumented_evidence_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "instrumented.json"
            with self.assertRaises(FileNotFoundError):
                _validated_trial_report(
                    missing,
                    expected_mode="instrumented",
                )

    def test_malformed_trial_evidence_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "canonical.json"
            output.write_text(
                json.dumps({"schema": "wrong"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _validated_trial_report(
                    output,
                    expected_mode="canonical",
                )

    def test_trial_main_returns_zero_when_valid_report_reproduces_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "canonical.json"
            args = SimpleNamespace(
                command="trial",
                output=output,
                mode="canonical",
            )
            report = _trial_report(
                "canonical",
                diagnostic_acceptance_behavior_failed=True,
            )

            async def fake_trial(_args):
                output.write_text(json.dumps(report), encoding="utf-8")
                return report

            with (
                mock.patch.object(
                    connection_trial,
                    "_parser",
                    return_value=SimpleNamespace(
                        parse_args=lambda: args,
                    ),
                ),
                mock.patch.object(
                    connection_trial,
                    "_trial",
                    new=fake_trial,
                ),
            ):
                result = connection_trial.main()

        self.assertEqual(result, 0)

    def test_compare_main_fails_when_interpretation_is_not_allowed(self) -> None:
        args = SimpleNamespace(command="compare")
        report = {
            "material_instrumentation_deterioration": False,
            "interpretation_allowed": False,
        }
        with (
            mock.patch.object(
                connection_trial,
                "_parser",
                return_value=SimpleNamespace(
                    parse_args=lambda: args,
                ),
            ),
            mock.patch.object(
                connection_trial,
                "_compare",
                return_value=report,
            ),
        ):
            result = connection_trial.main()

        self.assertEqual(result, 1)

    def test_compare_main_fails_on_material_instrumentation_deterioration(self) -> None:
        args = SimpleNamespace(command="compare")
        report = {
            "material_instrumentation_deterioration": True,
            "interpretation_allowed": False,
        }
        with (
            mock.patch.object(
                connection_trial,
                "_parser",
                return_value=SimpleNamespace(
                    parse_args=lambda: args,
                ),
            ),
            mock.patch.object(
                connection_trial,
                "_compare",
                return_value=report,
            ),
        ):
            result = connection_trial.main()

        self.assertEqual(result, 1)

    async def test_targeted_fetch_and_consumer_timing_are_correlated_and_private(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            request_id = "0123456789abcdef0123"
            request_token, family_token = recorder.begin_request(
                request_id,
                "planning_read",
            )
            span = recorder.begin_result_consumer()
            fetch_started = time.perf_counter()
            fetch_cpu_started = time.thread_time()
            await asyncio.sleep(0)
            fetch_ended = time.perf_counter()
            fetch_cpu_ended = time.thread_time()
            recorder.record_dbapi_fetch(
                fingerprint=TARGET_RESULT_FINGERPRINTS[0],
                connection_id=12345,
                started=fetch_started,
                ended=fetch_ended,
                cpu_started=fetch_cpu_started,
                cpu_ended=fetch_cpu_ended,
                row_count=321,
                call_site=(
                    "app/infrastructure/sql/demand_repository.py:"
                    "_aggregate_children:479"
                ),
            )
            nested_started = time.perf_counter()
            nested_cpu_started = time.thread_time()
            await asyncio.sleep(0.002)
            nested_ended = time.perf_counter()
            nested_cpu_ended = time.thread_time()
            recorder.record_dbapi_fetch(
                fingerprint="not-targeted",
                connection_id=12345,
                started=nested_started,
                ended=nested_ended,
                cpu_started=nested_cpu_started,
                cpu_ended=nested_cpu_ended,
                row_count=7,
                call_site="unmapped",
            )
            recorder.end_result_consumer(span)
            recorder.end_request(request_id, status="200")
            recorder.reset_request_context(request_token, family_token)
            rows = _rows(path)

        fetch = next(row for row in rows if row["kind"] == "dbapi_fetch")
        consumer = next(
            row for row in rows if row["kind"] == "result_consumer"
        )
        self.assertEqual(fetch["request_id"], request_id)
        self.assertEqual(fetch["connection_id"], 12345)
        self.assertEqual(
            fetch["statement_fingerprint"],
            TARGET_RESULT_FINGERPRINTS[0],
        )
        self.assertEqual(fetch["fetched_row_count"], 321)
        self.assertGreaterEqual(float(fetch["fetch_elapsed_ms"]), 0.0)
        self.assertGreaterEqual(float(fetch["fetch_cpu_ms"]), 0.0)
        self.assertEqual(consumer["request_id"], request_id)
        self.assertEqual(consumer["connection_id"], 12345)
        self.assertGreaterEqual(float(consumer["consumer_elapsed_ms"]), 0.0)
        self.assertGreaterEqual(
            float(consumer["nested_fetch_elapsed_ms"]),
            0.0,
        )
        self.assertGreaterEqual(
            float(consumer["non_nested_consumer_elapsed_ms"]),
            0.0,
        )
        self.assertGreaterEqual(
            float(consumer["nested_fetch_elapsed_ms"]),
            float(fetch["fetch_elapsed_ms"]),
        )
        self.assertGreaterEqual(
            float(consumer["immediate_projection_elapsed_ms"]),
            0.0,
        )
        self.assertLessEqual(
            float(consumer["immediate_projection_elapsed_ms"]),
            float(consumer["non_nested_consumer_elapsed_ms"]) + 0.5,
        )
        self.assertGreaterEqual(float(consumer["worker_cpu_ms"]), 0.0)
        self.assertGreaterEqual(
            float(consumer["non_nested_worker_cpu_ms"]),
            0.0,
        )
        serialized = json.dumps([fetch, consumer], sort_keys=True)
        self.assertNotIn("parameters", serialized)
        self.assertNotIn("row_contents", serialized)
        self.assertNotIn("model_fields", serialized)

    def test_targeted_summary_separates_nested_fetch_from_non_nested_projection(self) -> None:
        rows: list[dict[str, object]] = []
        holders: list[dict[str, object]] = []
        for index, fingerprint in enumerate(TARGET_RESULT_FINGERPRINTS, start=1):
            request_id = f"request-{index}"
            connection_id = index
            call_site = (
                "app/infrastructure/sql/demand_repository.py:"
                f"consumer_{index}:100"
            )
            rows.extend(
                [
                    {
                        "kind": "dbapi_fetch",
                        "request_id": request_id,
                        "route_family": "planning_read",
                        "connection_id": connection_id,
                        "statement_fingerprint": fingerprint,
                        "call_site": call_site,
                        "fetched_row_count": 100 * index,
                        "fetch_elapsed_ms": 250.0 * index,
                        "fetch_cpu_ms": 10.0 * index,
                    },
                    {
                        "kind": "result_consumer",
                        "request_id": request_id,
                        "route_family": "planning_read",
                        "connection_id": connection_id,
                        "statement_fingerprint": fingerprint,
                        "call_site": call_site,
                        "consumer_elapsed_ms": 300.0 * index,
                        "nested_fetch_elapsed_ms": 250.0 * index,
                        "non_nested_consumer_elapsed_ms": 50.0 * index,
                        "immediate_projection_elapsed_ms": 40.0 * index,
                        "worker_cpu_ms": 20.0 * index,
                        "non_nested_worker_cpu_ms": 10.0 * index,
                    },
                ]
            )
            holders.append(
                {
                    "request_id": request_id,
                    "connection_id": connection_id,
                }
            )
        rows.append(
            {
                "kind": "first_stall_snapshot",
                "holders": holders,
            }
        )

        summary = _summarize_trace(rows)

        self.assertTrue(summary["targeted_timing_complete"])
        for fingerprint in TARGET_RESULT_FINGERPRINTS:
            detail = summary["targeted_result_timing"][fingerprint]
            self.assertTrue(detail["evidence_complete"])
            self.assertEqual(detail["observed_fetches"], 1)
            self.assertEqual(detail["observed_consumers"], 1)
            self.assertEqual(
                detail["first_stall_request_connection_matches"],
                1,
            )
            self.assertGreater(
                detail["consumer_elapsed_ms"]["p50_ms"],
                detail["nested_fetch_elapsed_ms"]["p50_ms"],
            )
            self.assertGreaterEqual(
                detail["non_nested_consumer_elapsed_ms"]["p50_ms"],
                detail["immediate_projection_elapsed_ms"]["p50_ms"],
            )
            self.assertGreater(
                detail["worker_cpu_ms"]["p50_ms"],
                0.0,
            )

    def test_statement_role_matches_initial_list_shifts_projection_only(self) -> None:
        initial_sql = """
            SELECT shifts.id,
                   resource_requirements.id,
                   resources.id,
                   projects.id,
                   workforce_requests.id,
                   workforce_requests.legacy_demand_number,
                   workforce_requests.requester_name,
                   workforce_requests.emergency_override_active
            FROM shifts
            JOIN resource_requirements
              ON shifts.resource_requirement_id = resource_requirements.id
            JOIN resources ON shifts.resource_id = resources.id
            JOIN projects ON resource_requirements.project_id = projects.id
            LEFT OUTER JOIN workforce_requests
              ON resource_requirements.workforce_request_id = workforce_requests.id
        """
        later_asset_sql = """
            SELECT asset_requirements.id, asset_allocations.id, assets.id
            FROM asset_requirements
            LEFT OUTER JOIN asset_allocations
              ON asset_allocations.asset_requirement_id = asset_requirements.id
        """

        self.assertEqual(
            _statement_target_role(initial_sql),
            "list_shifts_initial",
        )
        self.assertEqual(_statement_target_role(later_asset_sql), "")

    def test_real_style_trace_identifies_initial_list_shifts_and_ignores_later_fetch(self) -> None:
        changed = "7490421461222515"
        later_list_shifts_fetch = "9abe91efc0db1ae6"
        rows: list[dict[str, object]] = [
            {
                "kind": "dbapi_fetch",
                "request_id": "req-shifts",
                "connection_id": 101,
                "statement_fingerprint": changed,
                "call_site": (
                    "app/infrastructure/sql/query_repository.py:"
                    "list_shifts:2346"
                ),
                "target_role": "list_shifts_initial",
                "fetched_row_count": 180,
                "fetch_elapsed_ms": 110.0,
                "fetch_cpu_ms": 58.0,
            },
            {
                "kind": "result_consumer",
                "request_id": "req-shifts",
                "connection_id": 101,
                "statement_fingerprint": changed,
                "call_site": (
                    "app/infrastructure/sql/query_repository.py:"
                    "list_shifts:2346"
                ),
                "target_role": "list_shifts_initial",
                "consumer_elapsed_ms": 118.0,
                "nested_fetch_elapsed_ms": 110.0,
                "non_nested_consumer_elapsed_ms": 8.0,
                "immediate_projection_elapsed_ms": 7.5,
                "worker_cpu_ms": 66.0,
                "non_nested_worker_cpu_ms": 8.0,
            },
            {
                "kind": "dbapi_fetch",
                "request_id": "req-shifts",
                "connection_id": 101,
                "statement_fingerprint": later_list_shifts_fetch,
                "call_site": (
                    "app/infrastructure/sql/query_repository.py:"
                    "list_shifts:2548"
                ),
                "fetched_row_count": 8,
                "fetch_elapsed_ms": 15.0,
                "fetch_cpu_ms": 4.0,
            },
        ]
        for index, fingerprint in enumerate(
            TARGET_RESULT_FINGERPRINTS[1:],
            start=1,
        ):
            call_site = (
                "app/infrastructure/sql/demand_repository.py:"
                + (
                    "list_with_cancellation_materialization:791"
                    if index == 1
                    else "_aggregate_children:479"
                )
            )
            rows.extend(
                [
                    {
                        "kind": "dbapi_fetch",
                        "request_id": f"req-{index}",
                        "connection_id": 101 + index,
                        "statement_fingerprint": fingerprint,
                        "call_site": call_site,
                        "fetched_row_count": 120 if index == 1 else 8,
                        "fetch_elapsed_ms": 20.0,
                        "fetch_cpu_ms": 2.0,
                    },
                    {
                        "kind": "result_consumer",
                        "request_id": f"req-{index}",
                        "connection_id": 101 + index,
                        "statement_fingerprint": fingerprint,
                        "call_site": call_site,
                        "consumer_elapsed_ms": 22.0,
                        "nested_fetch_elapsed_ms": 20.0,
                        "non_nested_consumer_elapsed_ms": 2.0,
                        "immediate_projection_elapsed_ms": 1.0,
                        "worker_cpu_ms": 3.0,
                        "non_nested_worker_cpu_ms": 1.0,
                    },
                ]
            )

        summary = _summarize_trace(rows)

        self.assertEqual(summary["list_shifts_fingerprint"], changed)
        self.assertEqual(
            tuple(summary["target_fingerprints"]),
            (changed, *TARGET_RESULT_FINGERPRINTS[1:]),
        )
        self.assertFalse(summary["target_identification_ambiguous"])
        self.assertTrue(summary["targeted_timing_complete"])
        self.assertIn(changed, summary["targeted_result_timing"])
        self.assertNotIn(
            later_list_shifts_fetch,
            summary["targeted_result_timing"],
        )
        self.assertEqual(
            summary["targeted_result_timing"][changed]["fetched_row_count"][
                "p50"
            ],
            180.0,
        )

    def test_list_shifts_target_identification_fails_closed_when_ambiguous(self) -> None:
        rows = []
        for fingerprint in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
            rows.append(
                {
                    "kind": "dbapi_fetch",
                    "request_id": "req-shifts",
                    "connection_id": 101,
                    "statement_fingerprint": fingerprint,
                    "call_site": (
                        "app/infrastructure/sql/query_repository.py:"
                        "list_shifts:2346"
                    ),
                    "target_role": "list_shifts_initial",
                    "fetched_row_count": 180,
                    "fetch_elapsed_ms": 10.0,
                    "fetch_cpu_ms": 1.0,
                }
            )

        summary = _summarize_trace(rows)

        self.assertEqual(summary["list_shifts_fingerprint"], "")
        self.assertTrue(summary["target_identification_ambiguous"])
        self.assertFalse(summary["targeted_timing_complete"])

    def test_list_shifts_target_identification_fails_closed_when_missing(self) -> None:
        rows = [
            {
                "kind": "dbapi_fetch",
                "request_id": "req-shifts",
                "connection_id": 101,
                "statement_fingerprint": "9abe91efc0db1ae6",
                "call_site": (
                    "app/infrastructure/sql/query_repository.py:"
                    "list_shifts:2548"
                ),
                "fetched_row_count": 8,
                "fetch_elapsed_ms": 10.0,
                "fetch_cpu_ms": 1.0,
            }
        ]

        summary = _summarize_trace(rows)

        self.assertEqual(summary["list_shifts_fingerprint"], "")
        self.assertTrue(summary["target_identification_ambiguous"])
        self.assertFalse(summary["targeted_timing_complete"])

    def test_instrumented_trial_validation_fails_closed_when_target_timing_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "instrumented.json"
            report = _trial_report("instrumented")
            trace_summary = report["trace_summary"]
            trace_summary["targeted_result_timing"][
                TARGET_RESULT_FINGERPRINTS[0]
            ]["evidence_complete"] = False
            trace_summary["targeted_timing_complete"] = False
            output.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(ValueError):
                _validated_trial_report(
                    output,
                    expected_mode="instrumented",
                )

    def test_canonical_trial_rejects_detailed_result_tracing(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "canonical.json"
            report = _trial_report("canonical")
            report["trace_summary"]["event_counts"]["dbapi_fetch"] = 1
            output.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(ValueError):
                _validated_trial_report(
                    output,
                    expected_mode="canonical",
                )

    @staticmethod
    def _correlation_fixture(
        *,
        dropped: int = 0,
        truncated: bool = False,
        omit_direct_batch: bool = False,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        spid = 70
        connection_id = 100
        changed = "3d86d93500f16875"
        target_fingerprints = (
            changed,
            *TARGET_RESULT_FINGERPRINTS[1:],
        )
        trace_rows: list[dict[str, object]] = [
            {
                "kind": "spid_probe",
                "timestamp_utc": "2026-11-23T02:00:00.100+00:00",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 1,
            },
            {
                "kind": "statement_start",
                "timestamp_utc": "2026-11-23T02:00:00.200+00:00",
                "request_id": "request-noise-a",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 2,
                "statement_sequence": 1,
                "statement_fingerprint": "noise-a",
            },
            {
                "kind": "statement_start",
                "timestamp_utc": "2026-11-23T02:00:00.700+00:00",
                "request_id": "request-shifts",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 3,
                "statement_sequence": 2,
                "statement_fingerprint": changed,
            },
            {
                "kind": "statement_start",
                "timestamp_utc": "2026-11-23T02:00:01.200+00:00",
                "request_id": "request-noise-b",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 4,
                "statement_sequence": 3,
                "statement_fingerprint": "noise-b",
            },
            {
                "kind": "statement_start",
                "timestamp_utc": "2026-11-23T02:00:01.700+00:00",
                "request_id": "request-demand",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 5,
                "statement_sequence": 4,
                "statement_fingerprint": target_fingerprints[1],
            },
            {
                "kind": "statement_start",
                "timestamp_utc": "2026-11-23T02:00:02.700+00:00",
                "request_id": "request-children",
                "connection_id": connection_id,
                "spid": spid,
                "connection_execution_sequence": 6,
                "statement_sequence": 5,
                "statement_fingerprint": target_fingerprints[2],
            },
            {
                "kind": "dbapi_fetch",
                "timestamp_utc": "2026-11-23T02:00:01.100+00:00",
                "request_id": "request-shifts",
                "connection_id": connection_id,
                "spid": spid,
                "statement_sequence": 2,
                "connection_execution_sequence": 3,
                "statement_fingerprint": changed,
                "target_role": "list_shifts_initial",
                "fetch_elapsed_ms": 500.0,
                "fetched_row_count": 180,
            },
            {
                "kind": "dbapi_fetch",
                "timestamp_utc": "2026-11-23T02:00:02.100+00:00",
                "request_id": "request-demand",
                "connection_id": connection_id,
                "spid": spid,
                "statement_sequence": 4,
                "connection_execution_sequence": 5,
                "statement_fingerprint": target_fingerprints[1],
                "fetch_elapsed_ms": 500.0,
                "fetched_row_count": 120,
            },
            {
                "kind": "dbapi_fetch",
                "timestamp_utc": "2026-11-23T02:00:03.100+00:00",
                "request_id": "request-children",
                "connection_id": connection_id,
                "spid": spid,
                "statement_sequence": 5,
                "connection_execution_sequence": 6,
                "statement_fingerprint": target_fingerprints[2],
                "fetch_elapsed_ms": 500.0,
                "fetched_row_count": 120,
            },
            {
                "kind": "connection_return",
                "timestamp_utc": "2026-11-23T02:00:03.200+00:00",
                "connection_id": connection_id,
                "spid": spid,
            },
        ]
        server_events: list[dict[str, object]] = [
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T01:59:58.000+00:00",
                "session_id": spid,
                "duration_us": 100,
                "cpu_us": 1,
                "logical_reads": 0,
                "physical_reads": 0,
                "row_count": 999,
                "object_name": "sp_prepexec",
            },
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T02:00:00.020+00:00",
                "session_id": spid,
                "duration_us": 800,
                "cpu_us": 8,
                "logical_reads": 2,
                "physical_reads": 0,
                "row_count": 0,
                "object_name": "sp_prepexec",
            },
            {
                "event": "sql_batch_completed",
                "timestamp_utc": "2026-11-23T02:00:00.050+00:00",
                "session_id": spid,
                "duration_us": 900,
                "cpu_us": 9,
                "logical_reads": 0,
                "physical_reads": 0,
                "row_count": 0,
            },
            {
                "event": "sql_batch_completed",
                "timestamp_utc": "2026-11-23T02:00:00.090+00:00",
                "session_id": spid,
                "duration_us": 1000,
                "cpu_us": 10,
                "logical_reads": 1,
                "physical_reads": 0,
                "row_count": 1,
            },
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T02:00:00.300+00:00",
                "session_id": spid,
                "duration_us": 1100,
                "cpu_us": 11,
                "logical_reads": 2,
                "physical_reads": 0,
                "row_count": 0,
                "object_name": "sp_prepexec",
            },
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T02:00:01.000+00:00",
                "session_id": spid,
                "duration_us": 1200,
                "cpu_us": 12,
                "logical_reads": 100,
                "physical_reads": 0,
                "row_count": 180,
                "object_name": "sp_prepexec",
            },
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T02:00:01.001+00:00",
                "session_id": spid,
                "duration_us": 10,
                "cpu_us": 0,
                "logical_reads": 0,
                "physical_reads": 0,
                "row_count": 0,
                "object_name": "sp_unprepare",
            },
            {
                "event": "sql_batch_completed",
                "timestamp_utc": "2026-11-23T02:00:01.300+00:00",
                "session_id": spid,
                "duration_us": 1300,
                "cpu_us": 13,
                "logical_reads": 3,
                "physical_reads": 0,
                "row_count": 0,
            },
            {
                "event": "rpc_completed",
                "timestamp_utc": "2026-11-23T02:00:02.000+00:00",
                "session_id": spid,
                "duration_us": 1400,
                "cpu_us": 14,
                "logical_reads": 80,
                "physical_reads": 0,
                "row_count": 120,
                "object_name": "sp_execute",
            },
            {
                "event": "sql_batch_completed",
                "timestamp_utc": "2026-11-23T02:00:03.000+00:00",
                "session_id": spid,
                "duration_us": 1500,
                "cpu_us": 15,
                "logical_reads": 70,
                "physical_reads": 0,
                "row_count": 120,
            },
            {
                "event": "wait_info",
                "timestamp_utc": "2026-11-23T02:00:00.950+00:00",
                "session_id": spid,
                "wait_type": "ASYNC_NETWORK_IO",
                "duration_ms": 150.0,
                "opcode": "End",
            },
        ]
        if omit_direct_batch:
            server_events = [
                row
                for row in server_events
                if not (
                    row.get("event") == "sql_batch_completed"
                    and row.get("row_count") == 0
                )
            ]
        return trace_rows, {
            "events": server_events,
            "dropped_event_count": dropped,
            "truncated": truncated,
            "cleanup_succeeded": True,
        }

    def test_fetch_time_consistent_inside_interval_passes(self) -> None:
        self.assertTrue(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.800+00:00"},
            )
        )

    def test_fetch_time_consistent_uncertainty_boundary_passes(self) -> None:
        self.assertTrue(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.250+00:00"},
            )
        )
        self.assertTrue(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:01.250+00:00"},
            )
        )

    def test_fetch_time_consistent_before_boundary_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.249+00:00"},
            )
        )

    def test_fetch_time_consistent_after_boundary_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:01.251+00:00"},
            )
        )

    def test_fetch_time_consistent_malformed_application_time_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "not-a-time"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.800+00:00"},
            )
        )

    def test_fetch_time_consistent_malformed_server_time_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "not-a-time"},
            )
        )

    def test_fetch_time_consistent_missing_timing_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.800+00:00"},
            )
        )
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {},
                {"timestamp_utc": "2026-11-23T02:00:00.800+00:00"},
            )
        )
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:01.000+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {},
            )
        )

    def test_fetch_time_consistent_inverted_application_window_fails(self) -> None:
        self.assertFalse(
            _fetch_time_consistent(
                {"timestamp_utc": "2026-11-23T02:00:00.400+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.500+00:00"},
                {"timestamp_utc": "2026-11-23T02:00:00.450+00:00"},
            )
        )

    def test_probe_anchor_discards_same_spid_preprobe_executions(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        probe = next(
            row for row in trace_rows if row.get("kind") == "spid_probe"
        )
        events, _ignored, error = _server_execution_events(
            capture,
            spid=70,
            probe=probe,
            end_utc=connection_trial._parse_utc(
                "2026-11-23T02:00:03.200+00:00"
            ),
        )

        self.assertEqual(error, "")
        self.assertEqual(len(events), 6)
        self.assertEqual(events[0]["server_execution_sequence"], 1)
        self.assertEqual(events[0]["execution_category"], "direct_batch")
        self.assertEqual(events[0]["row_count"], 1)
        self.assertEqual(
            events[0]["timestamp_utc"],
            "2026-11-23T02:00:00.090+00:00",
        )

    def test_probe_anchor_chooses_latest_one_row_batch_before_probe(self) -> None:
        probe = {"timestamp_utc": "2026-11-23T02:00:00.100+00:00"}
        executions = [
            {
                "execution_category": "direct_batch",
                "timestamp_utc": "2026-11-23T02:00:00.020+00:00",
                "row_count": 1,
            },
            {
                "execution_category": "prepared_rpc",
                "timestamp_utc": "2026-11-23T02:00:00.060+00:00",
                "row_count": 1,
            },
            {
                "execution_category": "direct_batch",
                "timestamp_utc": "2026-11-23T02:00:00.090+00:00",
                "row_count": 1,
            },
        ]

        index, error = _resolve_probe_server_anchor(executions, probe)

        self.assertEqual(error, "")
        self.assertEqual(index, 2)

    def test_probe_anchor_missing_server_probe_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        capture["events"] = [
            row
            for row in capture["events"]
            if not (
                row.get("event") == "sql_batch_completed"
                and row.get("timestamp_utc")
                == "2026-11-23T02:00:00.090+00:00"
            )
        ]

        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(
            any(
                failure.get("reason")
                == "probe_server_execution_missing"
                for failure in report["sequence_alignment_failures"]
            )
        )

    def test_probe_anchor_multiple_latest_candidates_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        capture["events"].append({
            "event": "sql_batch_completed",
            "timestamp_utc": "2026-11-23T02:00:00.090+00:00",
            "session_id": 70,
            "duration_us": 500,
            "cpu_us": 5,
            "logical_reads": 0,
            "physical_reads": 0,
            "row_count": 1,
        })

        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(
            any(
                failure.get("reason")
                == "probe_server_execution_ambiguous"
                for failure in report["sequence_alignment_failures"]
            )
        )

    def test_probe_anchor_outside_uncertainty_window_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        for row in capture["events"]:
            if (
                row.get("event") == "sql_batch_completed"
                and row.get("timestamp_utc")
                == "2026-11-23T02:00:00.090+00:00"
            ):
                row["timestamp_utc"] = (
                    "2026-11-23T01:59:59.849+00:00"
                )

        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(
            any(
                failure.get("reason")
                == "probe_server_execution_missing"
                for failure in report["sequence_alignment_failures"]
            )
        )

    def test_server_client_correlation_maps_mixed_execution_stream(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        report = _server_client_correlation(trace_rows, capture)

        self.assertTrue(report["complete"])
        self.assertTrue(report["prepared_execution_observed"])
        self.assertTrue(report["direct_batch_execution_observed"])
        self.assertEqual(report["physical_connection_lifetime_count"], 1)
        self.assertEqual(report["unmatched_application_fetches"], 0)
        self.assertEqual(report["sequence_alignment_failures"], [])
        self.assertEqual(report["timing_inconsistencies"], [])
        self.assertEqual(
            len(
                connection_trial._application_execution_markers(
                    trace_rows,
                    connection_id=100,
                    spid=70,
                )
            ),
            6,
        )
        self.assertEqual(report["row_count_mismatches"], [])
        self.assertEqual(report["ignored_non_execution_rpc_events"], 1)
        self.assertEqual(
            tuple(report["target_fingerprints"]),
            ("3d86d93500f16875", *TARGET_RESULT_FINGERPRINTS[1:]),
        )
        for fingerprint in report["target_fingerprints"]:
            self.assertEqual(
                report["targets"][fingerprint][
                    "correlated_execution_count"
                ],
                1,
            )
        self.assertEqual(
            report["targets"]["3d86d93500f16875"]["row_count"]["p50"],
            180.0,
        )

    def test_execution_count_mismatch_finds_earliest_non_target_divergence(self) -> None:
        aligned_markers = [
            {
                "kind": "spid_probe",
                "connection_execution_sequence": 1,
                "statement_fingerprint": "",
                "timestamp_utc": "2026-11-23T02:00:00.000+00:00",
            },
            {
                "kind": "statement_start",
                "connection_execution_sequence": 2,
                "statement_fingerprint": "fingerprint-a",
                "timestamp_utc": "2026-11-23T02:00:00.100+00:00",
            },
            {
                "kind": "statement_start",
                "connection_execution_sequence": 3,
                "statement_fingerprint": "fingerprint-b",
                "timestamp_utc": "2026-11-23T02:00:00.200+00:00",
            },
            {
                "kind": "statement_start",
                "connection_execution_sequence": 4,
                "statement_fingerprint": "fingerprint-target",
                "timestamp_utc": "2026-11-23T02:00:00.300+00:00",
            },
        ]
        aligned_events = [
            {
                "server_execution_sequence": 1,
                "execution_category": "direct_batch",
                "event": "sql_batch_completed",
                "object_name": "",
                "row_count": 1,
                "timestamp_utc": "2026-11-23T02:00:00.000Z",
            },
            {
                "server_execution_sequence": 2,
                "execution_category": "prepared_rpc",
                "event": "rpc_completed",
                "object_name": "sp_prepexec",
                "row_count": 1,
                "timestamp_utc": "2026-11-23T02:00:00.101Z",
            },
            {
                "server_execution_sequence": 3,
                "execution_category": "prepared_rpc",
                "event": "rpc_completed",
                "object_name": "sp_prepexec",
                "row_count": 1,
                "timestamp_utc": "2026-11-23T02:00:00.201Z",
            },
            {
                "server_execution_sequence": 4,
                "execution_category": "direct_batch",
                "event": "sql_batch_completed",
                "object_name": "",
                "row_count": 120,
                "timestamp_utc": "2026-11-23T02:00:00.301Z",
            },
        ]
        mismatched_markers = [
            dict(row, connection_id=100, spid=70)
            for row in aligned_markers
        ]
        marker_by_connection = {
            100: mismatched_markers,
            200: [
                dict(row, connection_id=200, spid=80)
                for row in aligned_markers
            ],
            201: [
                dict(row, connection_id=201, spid=81)
                for row in aligned_markers
            ],
        }
        server_by_connection = {
            100: [
                dict(aligned_events[0], session_id=70),
                dict(aligned_events[1], session_id=70),
                dict(
                    aligned_events[3],
                    session_id=70,
                    server_execution_sequence=3,
                ),
            ],
            200: [
                dict(row, session_id=80)
                for row in aligned_events
            ],
            201: [
                dict(row, session_id=81)
                for row in aligned_events
            ],
        }
        trace_rows = []
        for marker in mismatched_markers[1:]:
            trace_rows.append(marker)
            trace_rows.append({
                "kind": "statement_end",
                "connection_id": 100,
                "statement_fingerprint": marker[
                    "statement_fingerprint"
                ],
                "timestamp_utc": marker["timestamp_utc"],
            })
        capture = {
            "events": [
                {
                    "event": "rpc_completed",
                    "session_id": 70,
                    "object_name": "sp_unprepare",
                    "row_count": 0,
                    "timestamp_utc": "2026-11-23T01:59:59.000Z",
                },
                {
                    "event": "rpc_completed",
                    "session_id": 70,
                    "object_name": "sp_unprepare",
                    "row_count": 0,
                    "timestamp_utc": "2026-11-23T02:00:00.205Z",
                },
                {
                    "event": "rpc_completed",
                    "session_id": 70,
                    "object_name": "sp_unprepare",
                    "row_count": 0,
                    "timestamp_utc": "2026-11-23T02:00:00.500Z",
                },
            ],
        }
        failures = [{
            "connection_id": 100,
            "spid": 70,
            "reason": "execution_count_mismatch",
            "application_execution_count": 4,
            "server_execution_count": 3,
        }]

        evidence = connection_trial._bounded_execution_divergence_evidence(
            trace_rows=trace_rows,
            capture=capture,
            app_fetches=[{
                "connection_id": 100,
                "connection_execution_sequence": 4,
                "timestamp_utc": "2026-11-23T02:00:00.350+00:00",
                "fetched_row_count": 120,
                "target_role": "synthetic-target",
            }],
            marker_by_connection=marker_by_connection,
            server_by_connection=server_by_connection,
            sequence_alignment_failures=failures,
            row_count_mismatches=[],
            timing_inconsistencies=[],
            unmatched_app=[],
        )

        self.assertEqual(len(evidence), 1)
        detail = evidence[0]
        self.assertEqual(detail["diagnostic_window_anchor_sequence"], 3)
        self.assertEqual(
            detail["classification"],
            "participating_server_execution_mismatch",
        )
        self.assertEqual(
            detail["preceding_reference_aligned_through_sequence"],
            2,
        )
        self.assertEqual(
            detail["candidate_aligned_reference"]["observation_count"],
            2,
        )
        self.assertTrue(
            detail["candidate_server_matches_next_reference"]
        )
        self.assertEqual(
            [
                row["connection_execution_sequence"]
                for row in detail["application_window"]
            ],
            [1, 2, 3, 4],
        )
        self.assertIn(
            "statement_completion_utc",
            detail["application_window"][2],
        )
        self.assertEqual(
            detail["excluded_rpc_window"][0][
                "timestamp_utc"
            ],
            "2026-11-23T02:00:00.205Z",
        )
        serialized = json.dumps(detail, sort_keys=True)
        self.assertNotIn("SELECT ", serialized)
        self.assertNotIn("parameters", serialized)
        self.assertNotIn("batch_text", serialized)

    @staticmethod
    def _assignment_driver(
        entry: str,
        terminal: str,
        rpc: str = "sp_prepexec",
    ) -> dict[str, object]:
        return {
            "entry_timestamp_utc": entry,
            "terminal_timestamp_utc": terminal,
            "expected_rpc": rpc,
        }

    @staticmethod
    def _assignment_pair(
        start: str,
        completion: str,
        start_sequence: int,
        completion_sequence: int,
        rpc: str = "sp_prepexec",
    ) -> dict[str, object]:
        return {
            "normalized_rpc_name": rpc,
            "start_timestamp_utc": start,
            "completion_timestamp_utc": completion,
            "start_event_sequence": start_sequence,
            "completion_event_sequence": completion_sequence,
        }

    def test_monotonic_pairing_does_not_reuse_earlier_rpc_pair(self) -> None:
        drivers = [
            self._assignment_driver(
                "2026-11-23T02:00:00.300Z",
                "2026-11-23T02:00:00.310Z",
            ),
            self._assignment_driver(
                "2026-11-23T02:00:00.400Z",
                "2026-11-23T02:00:00.410Z",
            ),
        ]
        pairs = [
            self._assignment_pair(
                "2026-11-23T02:00:00.190Z",
                "2026-11-23T02:00:00.200Z",
                10,
                11,
            ),
            self._assignment_pair(
                "2026-11-23T02:00:00.390Z",
                "2026-11-23T02:00:00.400Z",
                12,
                13,
            ),
        ]

        cardinality, count, mapping = (
            connection_trial._monotonic_rpc_assignments(
                drivers,
                pairs,
            )
        )

        self.assertEqual(cardinality, 2)
        self.assertEqual(count, 1)
        self.assertEqual(mapping, [0, 1])

    def test_monotonic_pairing_rejects_crossing_assignment(self) -> None:
        drivers = [
            self._assignment_driver(
                "2026-11-23T02:00:00.500Z",
                "2026-11-23T02:00:00.510Z",
                rpc="sp_execute",
            ),
            self._assignment_driver(
                "2026-11-23T02:00:00.600Z",
                "2026-11-23T02:00:00.610Z",
                rpc="sp_prepexec",
            ),
        ]
        pairs = [
            self._assignment_pair(
                "2026-11-23T02:00:00.590Z",
                "2026-11-23T02:00:00.600Z",
                10,
                11,
                rpc="sp_prepexec",
            ),
            self._assignment_pair(
                "2026-11-23T02:00:00.490Z",
                "2026-11-23T02:00:00.500Z",
                12,
                13,
                rpc="sp_execute",
            ),
        ]

        cardinality, _count, mapping = (
            connection_trial._monotonic_rpc_assignments(
                drivers,
                pairs,
            )
        )

        self.assertEqual(cardinality, 1)
        self.assertIsNone(mapping)

    def test_monotonic_pairing_multiple_optimal_mappings_stay_ambiguous(self) -> None:
        drivers = [
            self._assignment_driver(
                "2026-11-23T02:00:00.300Z",
                "2026-11-23T02:00:00.310Z",
            ),
            self._assignment_driver(
                "2026-11-23T02:00:00.320Z",
                "2026-11-23T02:00:00.330Z",
            ),
        ]
        pairs = [
            self._assignment_pair(
                "2026-11-23T02:00:00.290Z",
                "2026-11-23T02:00:00.300Z",
                10,
                11,
            ),
        ]

        cardinality, count, mapping = (
            connection_trial._monotonic_rpc_assignments(
                drivers,
                pairs,
            )
        )

        self.assertEqual(cardinality, 1)
        self.assertGreater(count, 1)
        self.assertIsNone(mapping)

    def test_monotonic_pairing_preserves_250ms_compatibility_bound(self) -> None:
        driver = self._assignment_driver(
            "2026-11-23T02:00:00.500Z",
            "2026-11-23T02:00:00.510Z",
        )
        inside = self._assignment_pair(
            "2026-11-23T02:00:00.250Z",
            "2026-11-23T02:00:00.260Z",
            10,
            11,
        )
        outside = self._assignment_pair(
            "2026-11-23T02:00:00.249Z",
            "2026-11-23T02:00:00.259Z",
            12,
            13,
        )

        inside_result = connection_trial._monotonic_rpc_assignments(
            [driver],
            [inside],
        )
        outside_result = connection_trial._monotonic_rpc_assignments(
            [driver],
            [outside],
        )

        self.assertEqual(inside_result, (1, 1, [0]))
        self.assertEqual(outside_result, (0, 1, [None]))

    @staticmethod
    def _lifecycle_fixture(
        *,
        driver: bool = True,
        server_start: bool = True,
        server_completion: bool = True,
        participating_completion: bool = True,
        repeated_start: bool = False,
    ):
        markers = [
            {
                "kind": "spid_probe",
                "connection_id": 100,
                "spid": 70,
                "connection_execution_sequence": 1,
                "timestamp_utc": "2026-11-23T02:00:00.000+00:00",
            },
            {
                "kind": "statement_start",
                "connection_id": 100,
                "spid": 70,
                "connection_execution_sequence": 2,
                "statement_fingerprint": "fp-prepared",
                "timestamp_utc": "2026-11-23T02:00:00.100+00:00",
            },
        ]
        aligned_markers = [
            dict(row, connection_id=connection_id, spid=spid)
            for connection_id, spid in ((200, 80), (201, 81))
            for row in markers
        ]
        marker_by_connection = {
            100: markers,
            200: aligned_markers[:2],
            201: aligned_markers[2:],
        }
        probe_event = {
            "server_execution_sequence": 1,
            "execution_category": "direct_batch",
            "event": "sql_batch_completed",
            "object_name": "",
            "row_count": 1,
            "event_sequence": 1,
            "timestamp_utc": "2026-11-23T02:00:00.000Z",
        }
        prepared_event = {
            "server_execution_sequence": 2,
            "execution_category": "prepared_rpc",
            "event": "rpc_completed",
            "object_name": "sp_prepexec",
            "row_count": 1,
            "event_sequence": 12,
            "timestamp_utc": "2026-11-23T02:00:00.150Z",
        }
        server_by_connection = {
            100: [
                dict(probe_event, session_id=70),
                *(
                    [dict(prepared_event, session_id=70)]
                    if participating_completion
                    else []
                ),
            ],
            200: [
                dict(probe_event, session_id=80),
                dict(prepared_event, session_id=80),
            ],
            201: [
                dict(probe_event, session_id=81),
                dict(prepared_event, session_id=81),
            ],
        }
        trace_rows = list(markers)
        if driver:
            trace_rows.extend([
                {
                    "kind": "driver_execute_entry",
                    "connection_id": 100,
                    "spid": 70,
                    "connection_execution_sequence": 2,
                    "statement_fingerprint": "fp-prepared",
                    "timestamp_utc": "2026-11-23T02:00:00.110+00:00",
                },
                {
                    "kind": "driver_execute_return",
                    "connection_id": 100,
                    "spid": 70,
                    "connection_execution_sequence": 2,
                    "statement_fingerprint": "fp-prepared",
                    "timestamp_utc": "2026-11-23T02:00:00.190+00:00",
                },
            ])
        events = []
        if server_start:
            events.append({
                "event": "rpc_starting",
                "session_id": 70,
                "client_connection_id": "client-70",
                "event_sequence": 10,
                "object_name": "sp_prepexec",
                "timestamp_utc": "2026-11-23T02:00:00.120Z",
            })
            if repeated_start:
                events.append({
                    "event": "rpc_starting",
                    "session_id": 70,
                    "client_connection_id": "client-70",
                    "event_sequence": 11,
                    "object_name": "sp_prepexec",
                    "timestamp_utc": "2026-11-23T02:00:00.130Z",
                })
        if server_completion:
            events.append({
                "event": "rpc_completed",
                "session_id": 70,
                "client_connection_id": "client-70",
                "event_sequence": 12,
                "object_name": "sp_prepexec",
                "row_count": 1,
                "timestamp_utc": "2026-11-23T02:00:00.150Z",
            })
        failures = [{
            "connection_id": 100,
            "spid": 70,
            "reason": "execution_count_mismatch",
            "application_execution_count": 2,
            "server_execution_count": 1,
        }]
        divergence = [{
            "connection_id": 100,
            "diagnostic_window_anchor_sequence": 2,
        }]
        evidence = connection_trial._execution_lifecycle_evidence(
            trace_rows=trace_rows,
            capture={"events": events},
            marker_by_connection=marker_by_connection,
            server_by_connection=server_by_connection,
            sequence_alignment_failures=failures,
            divergence_evidence=divergence,
        )
        return evidence[0]

    def test_lifecycle_application_marker_without_driver_entry(self) -> None:
        detail = self._lifecycle_fixture(driver=False)
        self.assertEqual(
            detail["candidate_classification"],
            "application_marker_without_driver_execute",
        )

    def test_lifecycle_driver_entry_without_server_start(self) -> None:
        detail = self._lifecycle_fixture(
            server_start=False,
            server_completion=False,
        )
        self.assertEqual(
            detail["candidate_classification"],
            "driver_execute_without_identifiable_server_start",
        )

    def test_lifecycle_server_start_without_completion(self) -> None:
        detail = self._lifecycle_fixture(server_completion=False)
        self.assertEqual(
            detail["candidate_classification"],
            "server_start_without_completion",
        )

    def test_lifecycle_start_completion_reconstruction_omission(self) -> None:
        detail = self._lifecycle_fixture(
            participating_completion=False,
        )
        self.assertEqual(
            detail["candidate_classification"],
            "server_start_completion_reconstruction_omission",
        )

    def test_lifecycle_normal_start_completion_pair(self) -> None:
        detail = self._lifecycle_fixture()
        self.assertEqual(
            detail["candidate_classification"],
            "server_start_completion_observed",
        )

    def test_lifecycle_repeated_signatures_remain_ambiguous(self) -> None:
        detail = self._lifecycle_fixture(repeated_start=True)
        self.assertEqual(
            detail["candidate_classification"],
            "execution_lifecycle_ambiguous",
        )
        self.assertTrue(detail["rpc_pairing_ambiguities"])

    def test_pre_lifetime_spid_execution_is_excluded(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        report = _server_client_correlation(trace_rows, capture)

        self.assertTrue(report["complete"])
        self.assertEqual(report["unmatched_server_events"], 3)

    def test_failed_lifetime_is_quarantined_from_target_aggregates(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        capture["events"] = [
            row
            for row in capture["events"]
            if not (
                row.get("event") == "sql_batch_completed"
                and row.get("timestamp_utc")
                == "2026-11-23T02:00:01.300+00:00"
            )
        ]

        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertEqual(report["quarantined_connection_count"], 1)
        self.assertEqual(
            report["quarantined_lifetimes"][0]["connection_id"],
            100,
        )
        self.assertEqual(
            report["quarantined_lifetimes"][0]["reason"],
            "execution_count_mismatch",
        )
        self.assertTrue(
            report[
                "interpretable_target_aggregates_exclude_quarantined"
            ]
        )
        for fingerprint in report["target_fingerprints"]:
            self.assertEqual(
                report["targets"][fingerprint][
                    "correlated_execution_count"
                ],
                0,
            )
        self.assertGreater(
            report["unmatched_application_fetches"],
            0,
        )

    def test_missing_direct_batch_execution_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture(
            omit_direct_batch=True,
        )
        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(report["sequence_alignment_failures"])

    def test_duplicate_or_reordered_mixed_execution_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        target_batch = next(
            row
            for row in capture["events"]
            if row.get("event") == "sql_batch_completed"
            and row.get("row_count") == 120
        )
        target_batch["timestamp_utc"] = (
            "2026-11-23T02:00:00.250+00:00"
        )
        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(
            report["timing_inconsistencies"]
            or report["row_count_mismatches"]
        )

    def test_row_count_mismatch_still_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        target = next(
            row
            for row in capture["events"]
            if row.get("event") == "rpc_completed"
            and row.get("row_count") == 180
        )
        target["row_count"] = 181
        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(report["row_count_mismatches"])

    def test_timing_inconsistency_still_fails_closed(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        target = next(
            row
            for row in capture["events"]
            if row.get("event") == "rpc_completed"
            and row.get("row_count") == 180
        )
        target["timestamp_utc"] = (
            "2026-11-23T02:00:00.300+00:00"
        )
        report = _server_client_correlation(trace_rows, capture)

        self.assertFalse(report["complete"])
        self.assertTrue(report["timing_inconsistencies"])

    def test_unprepare_interleaved_remains_excluded(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        report = _server_client_correlation(trace_rows, capture)

        self.assertTrue(report["complete"])
        self.assertEqual(report["ignored_non_execution_rpc_events"], 1)

    def test_server_client_correlation_fails_closed_on_dropped_events(self) -> None:
        trace_rows, capture = self._correlation_fixture(dropped=1)
        report = _server_client_correlation(trace_rows, capture)
        self.assertFalse(report["complete"])
        self.assertEqual(report["dropped_event_count"], 1)

    def test_server_client_correlation_fails_closed_on_truncation(self) -> None:
        trace_rows, capture = self._correlation_fixture(truncated=True)
        report = _server_client_correlation(trace_rows, capture)
        self.assertFalse(report["complete"])
        self.assertTrue(report["capture_truncated"])

    @staticmethod
    def _xe_event_xml(
        *,
        timestamp: str = "2026-11-23T02:00:01.000Z",
        session_id: int = 77,
        row_count: int = 1,
        object_name: str = "sp_execute",
        include_secret: bool = False,
    ) -> str:
        statement = (
            "<data name=\"statement\"><value>"
            "SELECT secret_parameter_894"
            "</value></data>"
            if include_secret
            else ""
        )
        return f"""
        <event name="rpc_completed" timestamp="{timestamp}">
          <data name="duration"><value>1000</value></data>
          <data name="cpu_time"><value>10</value></data>
          <data name="logical_reads"><value>2</value></data>
          <data name="physical_reads"><value>0</value></data>
          <data name="row_count"><value>{row_count}</value></data>
          <data name="object_name"><value>{object_name}</value></data>
          {statement}
          <action name="database_id"><value>5</value></action>
          <action name="session_id"><value>{session_id}</value></action>
        </event>
        """

    def test_xe_rpc_starting_parser_keeps_only_lifecycle_fields(self) -> None:
        xml = """
        <event name="rpc_starting" timestamp="2026-11-23T02:00:01.000Z">
          <data name="object_name"><value>sp_prepexec</value></data>
          <data name="statement"><value>SELECT secret_rpc_start</value></data>
          <action name="database_id"><value>5</value></action>
          <action name="session_id"><value>77</value></action>
          <action name="event_sequence"><value>42</value></action>
          <action name="client_connection_id"><value>client-guid</value></action>
        </event>
        """
        parsed = _parse_xe_event_xml(xml, database_id=5)
        serialized = json.dumps(parsed, sort_keys=True)

        self.assertEqual(parsed["event"], "rpc_starting")
        self.assertEqual(parsed["event_sequence"], 42)
        self.assertEqual(
            parsed["client_connection_id"],
            "client-guid",
        )
        self.assertEqual(parsed["object_name"], "sp_prepexec")
        self.assertNotIn("secret_rpc_start", serialized)
        self.assertNotIn("statement", serialized)

    def test_xe_batch_parser_sanitizes_batch_text(self) -> None:
        xml = """
        <event name="sql_batch_completed" timestamp="2026-11-23T02:00:01.000Z">
          <data name="duration"><value>1000</value></data>
          <data name="cpu_time"><value>10</value></data>
          <data name="logical_reads"><value>2</value></data>
          <data name="physical_reads"><value>0</value></data>
          <data name="row_count"><value>1</value></data>
          <data name="batch_text"><value>SELECT secret_batch_894</value></data>
          <action name="database_id"><value>5</value></action>
          <action name="session_id"><value>77</value></action>
        </event>
        """
        parsed = _parse_xe_event_xml(xml, database_id=5)
        serialized = json.dumps(parsed, sort_keys=True)
        self.assertEqual(parsed["execution_category"], "direct_batch")
        self.assertNotIn("secret_batch_894", serialized)
        self.assertNotIn("batch_text", serialized)

    def test_xe_event_parser_sanitizes_statement_and_parameter_text(self) -> None:
        parsed = _parse_xe_event_xml(
            self._xe_event_xml(include_secret=True),
            database_id=5,
        )
        serialized = json.dumps(parsed, sort_keys=True)
        self.assertNotIn("secret_parameter_894", serialized)
        self.assertNotIn("statement", serialized)
        self.assertEqual(parsed["object_name"], "sp_execute")

    def test_xe_start_creates_bounded_event_file_target(self) -> None:
        scalar = mock.MagicMock()
        scalar.scalar_one.return_value = 5
        connection = mock.MagicMock()
        connection.exec_driver_sql.return_value = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            scalar,
            mock.MagicMock(),
            mock.MagicMock(),
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=[],
        ):
            report = _xe_start(
                "database-url",
                Path(tmp) / "xe-start.json",
            )
        sql = "\n".join(
            str(call.args[0])
            for call in connection.exec_driver_sql.call_args_list
        )
        self.assertIn("ADD EVENT sqlserver.rpc_starting", sql)
        self.assertIn("ADD EVENT sqlserver.rpc_completed", sql)
        self.assertIn("ADD EVENT sqlserver.sql_batch_completed", sql)
        self.assertIn("collect_statement=(0)", sql)
        self.assertIn("collect_batch_text=(0)", sql)
        self.assertIn("package0.event_sequence", sql)
        self.assertIn("sqlserver.client_connection_id", sql)
        self.assertNotIn("sqlserver.sql_text", sql)
        self.assertIn("ADD TARGET package0.event_file", sql)
        self.assertIn("max_file_size=(8)", sql)
        self.assertIn("max_rollover_files=(8)", sql)
        self.assertEqual(report["target"], "event_file")
        self.assertEqual(report["max_file_size_mb"], 8)
        self.assertEqual(report["max_rollover_files"], 8)

    def test_xe_file_rows_are_ordered_by_rollover_then_offset(self) -> None:
        rows = [
            ("/var/opt/mssql/log/rp894_short_correlation_1.xel", 30, "<event/>"),
            ("/var/opt/mssql/log/rp894_short_correlation_0.xel", 50, "<event/>"),
            ("/var/opt/mssql/log/rp894_short_correlation_0.xel", 10, "<event/>"),
        ]
        ordered = _ordered_xe_file_rows(rows)
        self.assertEqual(
            [(Path(row[0]).name, row[1]) for row in ordered],
            [
                ("rp894_short_correlation_0.xel", 10),
                ("rp894_short_correlation_0.xel", 50),
                ("rp894_short_correlation_1.xel", 30),
            ],
        )

    def test_xe_stop_collect_reads_complete_multi_file_capture(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        read = mock.MagicMock()
        read.all.return_value = [
            (
                "/var/opt/mssql/log/rp894_short_correlation_1.xel",
                20,
                self._xe_event_xml(
                    timestamp="2026-11-23T02:00:02.000Z",
                    row_count=2,
                ),
            ),
            (
                "/var/opt/mssql/log/rp894_short_correlation_0.xel",
                10,
                self._xe_event_xml(
                    timestamp="2026-11-23T02:00:01.000Z",
                    row_count=1,
                ),
            ),
        ]
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            read,
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=[
                "rp894_short_correlation_0.xel",
                "rp894_short_correlation_1.xel",
            ],
        ), mock.patch.object(connection_trial.time, "sleep"):
            report = _xe_stop_collect(
                "database-url",
                Path(tmp) / "capture.json",
            )
        self.assertEqual(report["event_file_count"], 2)
        self.assertEqual(report["event_count"], 2)
        self.assertEqual(
            [row["row_count"] for row in report["events"]],
            [1, 2],
        )
        self.assertTrue(report["session_cleanup_succeeded"])
        self.assertFalse(report["cleanup_succeeded"])

    def test_xe_stop_collect_rollover_capacity_exhausted_fails_closed(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        files = [
            f"rp894_short_correlation_{index}.xel"
            for index in range(8)
        ]
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=files,
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(ValueError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )

    def test_xe_stop_collect_missing_rollover_file_fails_closed(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        read = mock.MagicMock()
        read.all.return_value = [
            (
                "/var/opt/mssql/log/rp894_short_correlation_0.xel",
                10,
                self._xe_event_xml(),
            ),
        ]
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            read,
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=[
                "rp894_short_correlation_0.xel",
                "rp894_short_correlation_1.xel",
            ],
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(ValueError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )

    def test_xe_stop_collect_unreadable_event_file_fails_closed(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            RuntimeError("event file unreadable"),
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=["rp894_short_correlation_0.xel"],
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(RuntimeError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )

    def test_xe_stop_collect_parse_failure_fails_closed(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        read = mock.MagicMock()
        read.all.return_value = [
            (
                "/var/opt/mssql/log/rp894_short_correlation_0.xel",
                10,
                "<event",
            ),
        ]
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            read,
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=["rp894_short_correlation_0.xel"],
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(ValueError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )

    def test_xe_stop_collect_dropped_events_fails_closed(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 1
        read = mock.MagicMock()
        read.all.return_value = [
            (
                "/var/opt/mssql/log/rp894_short_correlation_0.xel",
                10,
                self._xe_event_xml(),
            ),
        ]
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            read,
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=["rp894_short_correlation_0.xel"],
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(ValueError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )

    def test_xe_cleanup_stops_and_drops_session(self) -> None:
        connection = mock.MagicMock()
        engine = mock.MagicMock()
        with mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ):
            _xe_cleanup("database-url")
        sql_calls = [
            " ".join(str(call.args[0]).split())
            for call in connection.exec_driver_sql.call_args_list
        ]
        self.assertTrue(any("STATE=STOP" in sql for sql in sql_calls))
        self.assertTrue(
            any("DROP EVENT SESSION" in sql for sql in sql_calls)
        )
        connection.close.assert_called_once()
        engine.dispose.assert_called_once()

    def test_xe_verify_files_removed_fails_when_prefix_remains(self) -> None:
        connection = mock.MagicMock()
        engine = mock.MagicMock()
        with mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=["rp894_short_correlation_0.xel"],
        ):
            with self.assertRaises(ValueError):
                _xe_verify_files_removed("database-url")

    def test_xe_finalize_cleanup_marks_capture_only_after_file_removal(self) -> None:
        with TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.json"
            capture.write_text(
                json.dumps({
                    "session_cleanup_succeeded": True,
                    "cleanup_succeeded": False,
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                connection_trial,
                "_xe_verify_files_removed",
            ) as verify:
                report = _xe_finalize_capture_cleanup(
                    "database-url",
                    capture,
                )
        verify.assert_called_once_with("database-url")
        self.assertTrue(report["cleanup_succeeded"])
        self.assertTrue(report["event_files_removed"])

    def test_xe_cleanup_after_capture_failure_still_stops_and_drops(self) -> None:
        dbid = mock.MagicMock()
        dbid.scalar_one.return_value = 5
        dropped = mock.MagicMock()
        dropped.scalar_one.return_value = 0
        read = mock.MagicMock()
        read.all.side_effect = RuntimeError("read failed")
        connection = mock.MagicMock()
        connection.exec_driver_sql.side_effect = [
            dbid,
            dropped,
            mock.MagicMock(),
            read,
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        engine = mock.MagicMock()
        with TemporaryDirectory() as tmp, mock.patch.object(
            connection_trial,
            "_xe_connection",
            return_value=(engine, connection),
        ), mock.patch.object(
            connection_trial,
            "_xe_list_event_files",
            return_value=["rp894_short_correlation_0.xel"],
        ), mock.patch.object(connection_trial.time, "sleep"):
            with self.assertRaises(RuntimeError):
                _xe_stop_collect(
                    "database-url",
                    Path(tmp) / "capture.json",
                )
        sql_calls = [
            str(call.args[0])
            for call in connection.exec_driver_sql.call_args_list
        ]
        self.assertTrue(
            any("DROP EVENT SESSION" in sql for sql in sql_calls)
        )

    def test_workflow_removes_only_diagnostic_xe_file_prefix(self) -> None:
        workflow = Path(".github/workflows/syntax-check.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "rm -f /var/opt/mssql/log/"
            "rp894_short_correlation*.xel",
            workflow,
        )
        self.assertIn("xe-finalize-cleanup", workflow)
        self.assertNotIn("xp_delete_file", workflow)

    async def test_first_stall_snapshot_is_bounded_to_one_capture(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            recorder = ConnectionTraceRecorder(path)
            recorder.capture_first_stall(
                trigger="pool_checkout_wait",
                trigger_value_ms=1001.0,
            )
            recorder.capture_first_stall(
                trigger="pool_checkout_wait",
                trigger_value_ms=2500.0,
            )
            rows = _rows(path)

        snapshots = [
            row for row in rows if row["kind"] == "first_stall_snapshot"
        ]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["trigger"], "pool_checkout_wait")
        self.assertEqual(float(snapshots[0]["trigger_value_ms"]), 1001.0)


if __name__ == "__main__":
    unittest.main()
