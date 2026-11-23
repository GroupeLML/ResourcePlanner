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
    _ordered_xe_file_rows,
    _parse_xe_event_xml,
    _server_client_correlation,
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
            "list_shifts_fingerprint": TARGET_RESULT_FINGERPRINTS[0],
            "target_fingerprints": TARGET_RESULT_FINGERPRINTS,
            "targeted_result_timing": targeted,
            "targeted_timing_complete": mode == "instrumented",
        },
        "server_client_correlation": (
            {"complete": True}
            if mode == "instrumented"
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

    def test_pre_lifetime_spid_execution_is_excluded(self) -> None:
        trace_rows, capture = self._correlation_fixture()
        report = _server_client_correlation(trace_rows, capture)

        self.assertTrue(report["complete"])
        self.assertEqual(report["unmatched_server_events"], 3)

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
