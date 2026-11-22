from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import time
import unittest

from tools.issue_894_connection_trace import (
    ConnectionTraceRecorder,
    safe_diagnostic_id,
)
from tools.issue_894_connection_trial import (
    DIAGNOSTIC_LABEL,
    FAILURE_RATE_MATERIAL_DELTA,
    _compare,
    _correlate_timeouts,
    _summarize_trace,
)


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _comparison(
    canonical: dict[str, object],
    instrumented: dict[str, object],
) -> dict[str, object]:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical_path = root / "canonical.json"
        instrumented_path = root / "instrumented.json"
        output_path = root / "comparison.json"
        canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
        instrumented_path.write_text(json.dumps(instrumented), encoding="utf-8")
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
