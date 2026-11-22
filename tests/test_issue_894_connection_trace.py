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
    safe_diagnostic_id,
)
from tools.issue_894_connection_trial import (
    DIAGNOSTIC_LABEL,
    FAILURE_RATE_MATERIAL_DELTA,
    TARGET_RESULT_FINGERPRINTS,
    _compare,
    _correlate_timeouts,
    _summarize_trace,
    _validated_trial_report,
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
    targeted = {
        fingerprint: {
            "evidence_complete": mode == "instrumented",
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
                {"dbapi_fetch": 3, "result_consumer": 3}
                if mode == "instrumented"
                else {}
            ),
            "targeted_result_timing": targeted,
            "targeted_timing_complete": mode == "instrumented",
        },
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
            connection_record = object()
            connection = SimpleNamespace(
                connection=SimpleNamespace(
                    _connection_record=connection_record,
                )
            )
            checkout_started = recorder.pool_checkout_request()
            recorder.on_checkout(connection_record)
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
        returned = next(row for row in rows if row["kind"] == "connection_return")
        self.assertGreaterEqual(float(returned["holding_ms"]), 0.0)
        self.assertIn("post_statement_connection_residence_ms", returned)
        ended = next(row for row in rows if row["kind"] == "request_end")
        self.assertEqual(ended["orm_materialization_count"], 1)

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
            float(consumer["immediate_projection_elapsed_ms"]),
            0.0,
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
