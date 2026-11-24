from __future__ import annotations

import importlib.util
from pathlib import Path
import inspect
import os
import subprocess
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "tools" / "issue_894_gdb_capability_preflight.py"
spec = importlib.util.spec_from_file_location("gdb_preflight", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ReceiverPipe:
    def __init__(self, payload: bytes = b"", *, read_size: int = 4096, keep_open: bool = False):
        self.read_fd, self.write_fd = os.pipe()
        if payload:
            os.write(self.write_fd, payload)
        if not keep_open:
            os.close(self.write_fd)
            self.write_fd = -1
        self.receiver = mod.MIRecordReceiver(self.read_fd, read_size=read_size)

    def close(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1
        os.close(self.read_fd)


class GdbCapabilityPreflightTests(unittest.TestCase):
    def test_mi_receiver_accepts_single_prompt_readiness(self):
        pipe = ReceiverPipe(b"(gdb)\n")
        try:
            record = pipe.receiver.wait_for(
                mod._is_mi_prompt,
                deadline=mod.time.monotonic() + 0.2,
                description="GDB readiness",
            )
            self.assertEqual(record, "(gdb)")
        finally:
            pipe.close()

    def test_mi_receiver_preserves_notification_before_coalesced_prompt(self):
        pipe = ReceiverPipe(b'=thread-group-added,id="i1"\n(gdb)\n')
        try:
            pipe.receiver.wait_for(
                mod._is_mi_prompt,
                deadline=mod.time.monotonic() + 0.2,
                description="GDB readiness",
            )
            self.assertEqual(
                pipe.receiver.queued_records,
                ('=thread-group-added,id="i1"',),
            )
        finally:
            pipe.close()

    def test_mi_receiver_accepts_prompt_trailing_whitespace(self):
        pipe = ReceiverPipe(b"(gdb)   \t\r\n")
        try:
            record = pipe.receiver.wait_for(
                mod._is_mi_prompt,
                deadline=mod.time.monotonic() + 0.2,
                description="GDB readiness",
            )
            self.assertTrue(mod._is_mi_prompt(record))
        finally:
            pipe.close()

    def test_mi_receiver_handles_fragmented_records_and_prompt(self):
        pipe = ReceiverPipe(
            b'=thread-created,id="1"\n(gdb) \n',
            read_size=2,
        )
        try:
            pipe.receiver.wait_for(
                mod._is_mi_prompt,
                deadline=mod.time.monotonic() + 0.2,
                description="GDB readiness",
            )
            self.assertEqual(
                pipe.receiver.queued_records,
                ('=thread-created,id="1"',),
            )
        finally:
            pipe.close()

    def test_mi_receiver_buffers_multiple_records_in_order(self):
        pipe = ReceiverPipe(b'*running,thread-id="all"\n7^done\n(gdb)\n')
        try:
            result = pipe.receiver.wait_for(
                lambda line: line.startswith("7^"),
                deadline=mod.time.monotonic() + 0.2,
                description="command",
            )
            self.assertEqual(result, "7^done")
            self.assertEqual(
                pipe.receiver.queued_records,
                ('*running,thread-id="all"', "(gdb)"),
            )
        finally:
            pipe.close()

    def test_mi_receiver_preserves_async_while_waiting_for_result(self):
        pipe = ReceiverPipe(b'*stopped,reason="signal-received"\n9^done\n')
        try:
            result = pipe.receiver.wait_for(
                lambda line: line.startswith("9^"),
                deadline=mod.time.monotonic() + 0.2,
                description="command",
            )
            event = pipe.receiver.wait_for(
                lambda line: line.startswith("*stopped"),
                deadline=mod.time.monotonic() + 0.2,
                description="async",
            )
            self.assertEqual(result, "9^done")
            self.assertTrue(event.startswith("*stopped"))
        finally:
            pipe.close()

    def test_mi_receiver_preserves_result_while_waiting_for_async(self):
        pipe = ReceiverPipe(b'11^done\n*running,thread-id="all"\n')
        try:
            event = pipe.receiver.wait_for(
                lambda line: line.startswith("*running"),
                deadline=mod.time.monotonic() + 0.2,
                description="async",
            )
            result = pipe.receiver.wait_for(
                lambda line: line.startswith("11^"),
                deadline=mod.time.monotonic() + 0.2,
                description="command",
            )
            self.assertTrue(event.startswith("*running"))
            self.assertEqual(result, "11^done")
        finally:
            pipe.close()

    def test_mi_receiver_preserves_async_event_ordering(self):
        pipe = ReceiverPipe(
            b'*running,thread-id="all"\n*stopped,reason="end-stepping-range"\n'
        )
        try:
            running = pipe.receiver.wait_for(
                lambda line: line.startswith("*running"),
                deadline=mod.time.monotonic() + 0.2,
                description="running",
            )
            stopped = pipe.receiver.wait_for(
                lambda line: line.startswith("*stopped"),
                deadline=mod.time.monotonic() + 0.2,
                description="stopped",
            )
            self.assertTrue(running.startswith("*running"))
            self.assertTrue(stopped.startswith("*stopped"))
        finally:
            pipe.close()

    def test_mi_receiver_fails_closed_on_eof(self):
        pipe = ReceiverPipe()
        try:
            with self.assertRaisesRegex(mod.MIError, "EOF"):
                pipe.receiver.wait_for(
                    lambda line: line.startswith("^done"),
                    deadline=mod.time.monotonic() + 0.2,
                    description="command",
                )
        finally:
            pipe.close()

    def test_mi_receiver_fails_closed_on_partial_line_deadline(self):
        pipe = ReceiverPipe(b"7^do", keep_open=True)
        try:
            with self.assertRaisesRegex(TimeoutError, "partial MI record"):
                pipe.receiver.wait_for(
                    lambda line: line.startswith("7^done"),
                    deadline=mod.time.monotonic() + 0.02,
                    description="command",
                )
        finally:
            pipe.close()

    def test_mi_receiver_genuinely_late_readiness_times_out(self):
        pipe = ReceiverPipe(keep_open=True)
        try:
            started = mod.time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "GDB readiness"):
                pipe.receiver.wait_for(
                    mod._is_mi_prompt,
                    deadline=started + 0.02,
                    description="GDB readiness",
                )
            self.assertLess(mod.time.monotonic() - started, 0.5)
            self.assertEqual(mod.READINESS_SECONDS, 2.0)
        finally:
            pipe.close()

    def test_gdb_constructor_failure_reaps_child(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)

        class FakePipe:
            def fileno(self):
                return read_fd

        class FakeProcess:
            def __init__(self):
                self.stdout = FakePipe()
                self.stdin = mock.Mock()
                self.terminated = False
                self.waited = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                self.waited = True
                return 0

            def kill(self):
                self.killed = True

        proc = FakeProcess()
        try:
            with mock.patch.object(mod.subprocess, "Popen", return_value=proc):
                with self.assertRaises(mod.MIError):
                    mod.GdbMI("gdb")
            self.assertTrue(proc.terminated)
            self.assertTrue(proc.waited)
        finally:
            os.close(read_fd)

    def test_gdb_mi_uses_one_shared_receiver_without_text_readline(self):
        source = inspect.getsource(mod.GdbMI)
        receiver_source = inspect.getsource(mod.MIRecordReceiver)
        self.assertNotIn("readline(", source)
        self.assertNotIn("select.select", source)
        self.assertEqual(receiver_source.count("select.select"), 1)
        self.assertIn("self.receiver.wait_for", source)

    def test_gdb_mi_receiver_does_not_persist_raw_transcript(self):
        source = (
            inspect.getsource(mod.GdbMI)
            + inspect.getsource(mod.MIRecordReceiver)
        ).casefold()
        self.assertNotIn("write_text", source)
        self.assertNotIn("_write_json", source)
        self.assertNotIn("transcript", source)

    def test_native_chain_requires_complete_order_without_gap(self):
        frames = [
            {"module": "libc.so.6"},
            {"module": "libmsodbcsql-18.5.so.1.1"},
            {"module": "libodbc.so.2"},
            {"module": "pyodbc.cpython-312-x86_64-linux-gnu.so"},
            {"module": "libpython3.12.so.1.0"},
        ]
        self.assertTrue(mod.validate_native_chain(frames)["complete"])
        self.assertFalse(mod.validate_native_chain(frames[:-1])["complete"])
        with_gap = frames[:2] + [{"module": "mystery.so"}] + frames[2:]
        self.assertEqual(mod.validate_native_chain(with_gap)["reason"], "attribution_gap")

    def test_scheduler_parser_uses_numeric_identity_and_separates_debugger_interval(self):
        text = "\n".join([
            '{"event":"sched_switch","time_ns":1000000000,"prev_pid":42,"next_pid":9,"prev_state":1}',
            '{"event":"sched_switch","time_ns":1100000000,"prev_pid":42,"next_pid":9,"prev_state":8}',
            '{"event":"sched_wakeup","time_ns":1200000000,"pid":42}',
        ])
        parsed = mod.parse_scheduler(text, 42, [(1_050_000_000, 1_150_000_000)])
        self.assertEqual(len(parsed["rows"]), 3)
        self.assertFalse(parsed["rows"][0]["debugger_induced"])
        self.assertTrue(parsed["rows"][1]["debugger_induced"])
        self.assertEqual(parsed["rejected_scheduler_records"], 0)

    def test_scheduler_parser_fails_closed_on_unrecognized_payload(self):
        parsed = mod.parse_scheduler("not-json", 42, [])
        self.assertEqual(parsed["rows"], [])
        self.assertEqual(parsed["rejected_scheduler_records"], 1)

    def test_privacy_filter_rejects_args_locals_transcript_memory_registers(self):
        safe = {"frames": [{"module": "libc.so.6", "function": "read"}]}
        self.assertTrue(mod._privacy_safe(safe))
        for key in ("args", "locals", "raw_debugger_output", "memory", "registers"):
            self.assertFalse(mod._privacy_safe({key: "x"}), key)

    def test_blocked_state_deadline_and_state_validation(self):
        with mock.patch.object(mod, "_proc_state", side_effect=["R", "S"]):
            state, stamp = mod._wait_blocked_state(1, 2, 0.2)
        self.assertEqual(state, "S")
        self.assertGreater(stamp, 0)
        with mock.patch.object(mod, "_proc_state", return_value="R"):
            with self.assertRaises(TimeoutError):
                mod._wait_blocked_state(1, 2, 0.01)

    def test_identity_mapping_rejects_missing_tid(self):
        with self.assertRaises(ValueError):
            mod.map_identity(99999999, 1, 2)

    def test_phase_metrics_preserve_synthetic_results(self):
        rows = []
        for index, phase in enumerate(mod.PHASES, start=1):
            rows += [
                {"phase": phase, "event": "start", "monotonic_ns": index * 1000},
                {
                    "phase": phase,
                    "event": "end",
                    "monotonic_ns": index * 1000 + 500,
                    "iterations": mod.CPU_ITERATIONS if phase == "runnable_control" else 1,
                },
            ]
        metrics = mod._phase_metrics(rows)
        self.assertTrue(all(metrics[phase]["complete"] for phase in mod.PHASES))
        self.assertEqual(metrics["runnable_control"]["completed_work"], mod.CPU_ITERATIONS)

    def test_pause_limit_and_no_automatic_snapshot_retry(self):
        self.assertEqual(mod.MAX_DEBUGGER_PAUSE_MS, 100.0)
        self.assertEqual(mod.READINESS_SECONDS, 2.0)
        self.assertEqual(mod.BLOCKED_PHASES, ("libc_block", "odbc_wait_read"))
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("snap = _snapshot("), 1)
        for forbidden in (
            "-stack-list-arguments",
            "-stack-list-locals",
            "-data-read-memory",
            "-data-list-register-values",
            "-break-insert",
            "-exec-call",
        ):
            self.assertNotIn(forbidden, source)

    def test_gdb_initialization_and_symbol_downloads_are_disabled(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for required in (
            "--nx",
            "--nh",
            "set auto-load off",
            "set debuginfod enabled off",
            "set non-stop on",
        ):
            self.assertIn(required, source)

    def test_scheduler_state_is_observed_before_interrupt(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        state_position = source.index("state, state_ns = _wait_blocked_state")
        interrupt_position = source.index('mi.command(\n        f"-exec-interrupt')
        self.assertLess(state_position, interrupt_position)

    def test_detach_recovery_is_required(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('mi.command("-target-detach"', source)
        self.assertIn("sentinel_progress_after_detach", source)
        self.assertIn("target recovery after GDB detach failed", source)

    def test_thread_info_parser_handles_nested_frame_payload(self):
        result = (
            '^done,threads=[{id="1",target-id="Thread 0x1 (LWP 42)",'
            'frame={level="0",addr="0x1",func="poll"},state="stopped"},'
            '{id="2",target-id="Thread 0x2 (LWP 43)",state="running"}]'
        )
        rows = mod._thread_rows(result)
        self.assertEqual(
            rows,
            [
                {"gdb_thread_id": 1, "host_tid": 42, "state": "stopped"},
                {"gdb_thread_id": 2, "host_tid": 43, "state": "running"},
            ],
        )

    def test_permitted_frame_metadata_excludes_arguments(self):
        result = '^done,stack=[frame={level="0",addr="0x1",func="read",from="/lib/libc.so.6"}]'
        rows = mod._frame_rows(result)
        self.assertEqual(
            rows,
            [{"level": "0", "address": "0x1", "function": "read", "module": "libc.so.6"}],
        )
        self.assertNotIn("args", rows[0])


if __name__ == "__main__":
    unittest.main()
