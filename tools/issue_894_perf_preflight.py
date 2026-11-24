from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import re
import select
import tempfile
import threading
import time
from typing import Any

SCHEMA = "issue-894-perf-dwarf-preflight-v1"
REFERENCE_HEAD = "1cb01861ddba17babb0b6b3b09d062bd15107f20"
REFERENCE_IMAGE_ID = "sha256:194211a0c3a5a5113388ff876b7154edfb297cac0ebb4be6d11c4aa431bd51db"
REFERENCE_PLATFORM = "sha256:40e271b9ec31384a0b2289906a5b8d7e888faced3c626ab538871b3ad5dfba7d"
REFERENCE_CONFIG = "sha256:799bad6afa00d775059d3d6d43f2e1fa7defe34e56074686e53c4101bf3e2768"
REFERENCE_ARCHIVE_SHA256 = "483b9ae6cc3ada5ad5918d68f1f812de649ffeb120d3f82eb4c60ea49ae0af09"
LIBPYTHON_BUILD_ID = "a86be2f94c7ff304fc3aac60b00b763640234afc"
CPU_ITERATIONS = 4_000_000
SLEEP_SECONDS = 0.75
ODBC_WAIT = "00:00:00.750"
RAW_STACK_BYTES = 8192
OBSERVED_SECONDS = 8
ACTIVATION_TIMEOUT_SECONDS = 2.0
PREFLIGHT_EXECUTION_BEGIN = "# RP894_PREFLIGHT_EXECUTION_BEGIN"
PREFLIGHT_EXECUTION_END = "# RP894_PREFLIGHT_EXECUTION_END"
FORBIDDEN_EXECUTABLE_TOKENS = (
    "py-spy",
    "gdb -p",
    "gdb --pid",
    "strace -p",
    "bpftrace",
    "bcc",
    "uprobe",
    "uretprobe",
    "--duration-seconds 30",
    "--duration-seconds 600",
    "issue_894_connection_trial.py trial",
    "docker build",
    "apt-get install",
    "apt install",
    "pip install",
)


def _preflight_execution_region(text: str) -> str:
    if text.count(PREFLIGHT_EXECUTION_BEGIN) != 1:
        raise ValueError("preflight execution begin marker must appear exactly once")
    if text.count(PREFLIGHT_EXECUTION_END) != 1:
        raise ValueError("preflight execution end marker must appear exactly once")
    start = text.index(PREFLIGHT_EXECUTION_BEGIN) + len(PREFLIGHT_EXECUTION_BEGIN)
    end = text.index(PREFLIGHT_EXECUTION_END, start)
    if end <= start:
        raise ValueError("preflight execution markers are misordered")
    return text[start:end]


def _validate_workflow_text(text: str) -> None:
    region = _preflight_execution_region(text).casefold()
    hits = [
        token
        for token in FORBIDDEN_EXECUTABLE_TOKENS
        if token in region
    ]
    if hits:
        raise ValueError(
            "forbidden preflight executable operation(s): "
            f"{hits}"
        )
    required = (
        "issue_894_perf_preflight.py command",
        "raw_dir=",
        "trap cleanup exit",
        'rm -rf "$raw_dir"',
        "issue-894-perf-preflight-evidence/",
        "docker load",
    )
    missing = [
        token
        for token in required
        if token not in region
    ]
    if missing:
        raise ValueError(
            "preflight executable surface is missing required guardrail(s): "
            f"{missing}"
        )
    generated = " ".join(
        perf_argv(
            [11, 22],
            Path("/tmp/perf.data"),
            control_fifo=Path("/tmp/perf-control.fifo"),
            ack_fifo=Path("/tmp/perf-ack.fifo"),
        )
    ).casefold()
    generated_hits = [
        token
        for token in FORBIDDEN_EXECUTABLE_TOKENS
        if token in generated
    ]
    if generated_hits:
        raise ValueError(
            "forbidden generated perf operation(s): "
            f"{generated_hits}"
        )
    generated_required = (
        "perf record",
        "sched:sched_switch/call-graph=dwarf,stack-size=8192/",
        "sched:sched_wakeup",
        "sched:sched_wakeup_new",
        "--clockid mono",
        "--delay -1",
        "--control fifo:",
    )
    workflow_required = (
        'perf_executable="$(command -v perf)"',
        '--perf-executable "$perf_executable"',
        '--parser-argv-lines "$raw_dir/perf-parser-argv.txt"',
        'sudo "${parser_args[@]}"',
        'test "${parser_args[0]}" = "$perf_executable"',
        'sudo "${perf_args[@]}"',
        'test "${perf_args[0]}" = "$perf_executable"',
        'mkfifo "$raw_dir/perf-control.fifo" "$raw_dir/perf-ack.fifo"',
        'issue_894_perf_preflight.py activate',
        '--readiness "$evidence/perf-readiness.json"',
        'perf-channel-samples.jsonl',
        'perf-header.txt',
    )
    if any(token not in region for token in workflow_required):
        raise ValueError("perf gate/recorder execution context is not aligned")
    if "sleep 0.5" in region:
        raise ValueError("fixed-delay recorder readiness is forbidden")
    if any(token not in generated for token in generated_required):
        raise ValueError("generated perf command drifted from the authorized event model")
    if generated.count("call-graph=dwarf") != 1:
        raise ValueError("DWARF call graph must be enabled on exactly one event channel")
    if "actions/upload-artifact@v4" not in region:
        raise ValueError("bounded derived evidence upload is missing")
    upload_start = region.index("actions/upload-artifact@v4")
    upload_region = region[upload_start:upload_start + 1200]
    if "issue-894-perf-preflight-evidence/" not in upload_region:
        raise ValueError("upload path is not the bounded evidence directory")
    if "perf.data" in upload_region or "runner_temp" in upload_region or "raw_dir" in upload_region:
        raise ValueError("raw perf capture path is reachable from artifact upload")
    lower = text.casefold()
    if "rp894_perf_preflight_only" not in lower:
        raise ValueError("preflight-only routing marker is missing")
    phase4_start = lower.index("  issue-894-phase4:")
    phase4 = lower[phase4_start:phase4_start + 900]
    if "issue_894_perf_preflight_only != 'true'" not in phase4:
        raise ValueError("legacy phase 4 job is not gated off")
    inventory_start = lower.index("  issue-894-environment-inventory:")
    inventory = lower[inventory_start:inventory_start + 700]
    if "issue_894_inventory_only == 'true'" not in inventory:
        raise ValueError("inventory-only routing changed unexpectedly")


def validate_workflow(args: argparse.Namespace) -> int:
    _validate_workflow_text(
        args.workflow.read_text(encoding="utf-8")
    )
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "workflow_validation": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_marker(
    path: Path,
    phase: str,
    event: str,
    **extra: object,
) -> None:
    import threading

    row = {
        "schema": "issue-894-synthetic-probe-marker-v1",
        "clock": "CLOCK_MONOTONIC",
        "monotonic_ns": time.monotonic_ns(),
        "native_tid": threading.get_native_id(),
        "phase": phase,
        "event": event,
    }
    row.update(extra)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _libc_sleep(seconds: float) -> None:
    libc_name = ctypes.util.find_library("c")
    libc = ctypes.CDLL(libc_name or None, use_errno=True)

    class Timespec(ctypes.Structure):
        _fields_ = [
            ("tv_sec", ctypes.c_long),
            ("tv_nsec", ctypes.c_long),
        ]

    req = Timespec(
        int(seconds),
        int((seconds % 1.0) * 1_000_000_000),
    )
    remain = Timespec()
    result = libc.nanosleep(
        ctypes.byref(req),
        ctypes.byref(remain),
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _cpu_control() -> tuple[int, int]:
    value = 0x12345678
    for index in range(CPU_ITERATIONS):
        value = (
            ((value * 33) ^ index)
            & 0xFFFFFFFF
        )
    return CPU_ITERATIONS, value


def _connect_odbc(
    server: str,
    credential_value: str,
):
    import pyodbc

    key = "P" + "WD"
    return pyodbc.connect(
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server},1433;UID=sa;"
        f"{key}={credential_value};"
        "Encrypt=yes;TrustServerCertificate=yes",
        autocommit=True,
        timeout=3,
    )


def probe(args: argparse.Namespace) -> int:
    marker = args.marker
    gate = args.gate
    stop = args.stop
    marker.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    marker.write_text("", encoding="utf-8")
    if not args.sql_password:
        raise ValueError("synthetic SQL password is missing")
    connection = _connect_odbc(
        args.sql_server,
        args.sql_password,
    )
    cursor = connection.cursor()
    try:
        _append_marker(
            marker,
            "probe",
            "ready",
            pid=os.getpid(),
        )
        args.ready.write_text(
            "ready\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + 20
        while not gate.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "probe gate was not released"
                )
            time.sleep(0.01)

        _append_marker(
            marker,
            "libc_block",
            "start",
        )
        _libc_sleep(SLEEP_SECONDS)
        _append_marker(
            marker,
            "libc_block",
            "end",
        )

        _append_marker(
            marker,
            "odbc_wait_read",
            "start",
        )
        cursor.execute(
            f"WAITFOR DELAY '{ODBC_WAIT}'; "
            "SELECT CAST(1 AS INT)"
        )
        row = cursor.fetchone()
        if row is None or int(row[0]) != 1:
            raise RuntimeError(
                "synthetic ODBC result mismatch"
            )
        _append_marker(
            marker,
            "odbc_wait_read",
            "end",
            rows=1,
        )

        _append_marker(
            marker,
            "runnable_control",
            "start",
            iterations=CPU_ITERATIONS,
        )
        iterations, checksum = _cpu_control()
        _append_marker(
            marker,
            "runnable_control",
            "end",
            iterations=iterations,
            checksum=checksum,
        )
        _append_marker(
            marker,
            "probe",
            "complete",
        )
        args.done.write_text(
            "done\n",
            encoding="utf-8",
        )

        deadline = time.monotonic() + 20
        while not stop.exists():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    finally:
        connection.close()
    return 0


def _tid_filter(
    field: str,
    tids: list[int],
) -> str:
    if not tids:
        raise ValueError(
            "at least one TID is required"
        )
    if any(tid <= 0 for tid in tids):
        raise ValueError(
            "TIDs must be positive"
        )
    return " || ".join(
        f"{field} == {tid}"
        for tid in sorted(set(tids))
    )


def switch_out_event() -> str:
    return (
        "sched:sched_switch/"
        f"call-graph=dwarf,stack-size={RAW_STACK_BYTES}/"
    )


def perf_parser_argv(
    perf_executable: str = "perf",
) -> list[str]:
    return [
        perf_executable,
        "record",
        "--dry-run",
        "-e",
        switch_out_event(),
        "--",
        "true",
    ]


def perf_argv(
    tids: list[int],
    raw_path: Path,
    perf_executable: str = "perf",
    control_fifo: Path | None = None,
    ack_fifo: Path | None = None,
) -> list[str]:
    switch_out = _tid_filter(
        "prev_pid",
        tids,
    )
    timeline = " || ".join(
        [
            f"prev_pid == {tid}"
            for tid in sorted(set(tids))
        ]
        + [
            f"next_pid == {tid}"
            for tid in sorted(set(tids))
        ]
    )
    wake = _tid_filter(
        "pid",
        tids,
    )
    if (control_fifo is None) != (ack_fifo is None):
        raise ValueError("perf control and acknowledgement FIFOs must be paired")
    control_args: list[str] = []
    if control_fifo is not None and ack_fifo is not None:
        control_args = [
            "--delay",
            "-1",
            "--control",
            f"fifo:{control_fifo},{ack_fifo}",
        ]
    return [
        perf_executable,
        "record",
        "-o",
        str(raw_path),
        "-a",
        "--clockid",
        "mono",
        *control_args,
        "-e",
        switch_out_event(),
        "--filter",
        switch_out,
        "-e",
        "sched:sched_switch",
        "--filter",
        timeline,
        "-e",
        "sched:sched_wakeup",
        "--filter",
        wake,
        "-e",
        "sched:sched_wakeup_new",
        "--filter",
        wake,
        "--",
        "sleep",
        str(OBSERVED_SECONDS),
    ]


def command(args: argparse.Namespace) -> int:
    tids = [
        int(value)
        for value in args.tids.split(",")
        if value.strip()
    ]
    argv = perf_argv(
        tids,
        args.raw,
        args.perf_executable,
        args.control_fifo,
        args.ack_fifo,
    )
    parser_argv = perf_parser_argv(
        args.perf_executable,
    )
    report = {
        "schema": "issue-894-perf-command-v1",
        "clock": "CLOCK_MONOTONIC",
        "raw_stack_bytes": RAW_STACK_BYTES,
        "observed_seconds": OBSERVED_SECONDS,
        "tids": sorted(set(tids)),
        "recording_activation": {
            "mode": "perf-control-ack",
            "clock": "CLOCK_MONOTONIC",
            "deadline_seconds": ACTIVATION_TIMEOUT_SECONDS,
            "control_transport": "fifo",
        },
        "argv": argv,
        "parser_check": {
            "argv": parser_argv,
            "perf_executable": args.perf_executable,
            "execution_context": "host-sudo",
            "result_evidence": "perf-parser-check.json",
        },
        "channels": {
            "switch_out": {
                "event": "sched:sched_switch",
                "filter": _tid_filter(
                    "prev_pid",
                    tids,
                ),
                "dwarf": True,
            },
            "timeline": {
                "event": "sched:sched_switch",
                "filter": " || ".join(
                    [
                        f"prev_pid == {tid}"
                        for tid in sorted(
                            set(tids)
                        )
                    ]
                    + [
                        f"next_pid == {tid}"
                        for tid in sorted(
                            set(tids)
                        )
                    ]
                ),
                "dwarf": False,
            },
            "wakeup": {
                "events": [
                    "sched:sched_wakeup",
                    "sched:sched_wakeup_new",
                ],
                "filter": _tid_filter(
                    "pid",
                    tids,
                ),
                "dwarf": False,
            },
        },
    }
    _write_json(
        args.output,
        report,
    )
    if args.argv_lines:
        args.argv_lines.write_text(
            "\n".join(argv) + "\n",
            encoding="utf-8",
        )
    if args.parser_argv_lines:
        args.parser_argv_lines.write_text(
            "\n".join(parser_argv) + "\n",
            encoding="utf-8",
        )
    return 0


def _await_perf_activation(
    control_fifo: Path,
    ack_fifo: Path,
    deadline_seconds: float,
) -> dict[str, Any]:
    request_ns = time.monotonic_ns()
    deadline_ns = request_ns + int(deadline_seconds * 1_000_000_000)
    ack_ns: int | None = None
    ack_value = ""
    control_fd = os.open(control_fifo, os.O_RDWR | os.O_NONBLOCK)
    ack_fd = os.open(ack_fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(control_fd, b"enable\n")
        data = b""
        while time.monotonic_ns() < deadline_ns:
            remaining = max(
                0.0,
                (deadline_ns - time.monotonic_ns()) / 1_000_000_000,
            )
            readable, _, _ = select.select([ack_fd], [], [], remaining)
            if not readable:
                break
            try:
                chunk = os.read(ack_fd, 128)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            data += chunk
            if b"\n" in data:
                break
        if b"\n" in data:
            ack_value = data.split(b"\n", 1)[0].decode(
                "ascii",
                errors="replace",
            ).strip()
            ack_ns = time.monotonic_ns()
    finally:
        os.close(control_fd)
        os.close(ack_fd)
    accepted = bool(
        ack_value == "ack"
        and ack_ns is not None
        and ack_ns <= deadline_ns
    )
    return {
        "activation_request_monotonic_ns": request_ns,
        "activation_ack_monotonic_ns": ack_ns,
        "ack_received": ack_value == "ack",
        "accepted": accepted,
        "deadline_seconds": deadline_seconds,
        "timed_out": ack_ns is None,
        "clock": "CLOCK_MONOTONIC",
    }


def _activate_perf_gate(
    control_fifo: Path,
    ack_fifo: Path,
    output: Path,
    probe_gate: Path,
    perf_executable: str,
    launcher_pid: int,
    deadline_seconds: float = ACTIVATION_TIMEOUT_SECONDS,
) -> bool:
    evidence = _await_perf_activation(
        control_fifo,
        ack_fifo,
        deadline_seconds,
    )
    evidence.update(
        {
            "schema": "issue-894-perf-activation-v1",
            "perf_executable": perf_executable,
            "perf_launcher_pid": launcher_pid,
            "control_transport": "fifo",
            "control_fifo_name": control_fifo.name,
            "ack_fifo_name": ack_fifo.name,
            "probe_released": False,
            "probe_release_monotonic_ns": None,
        }
    )
    if evidence["accepted"]:
        release_ns = time.monotonic_ns()
        evidence["probe_release_monotonic_ns"] = release_ns
        probe_gate.touch()
        evidence["probe_released"] = True
    _write_json(output, evidence)
    return bool(evidence["accepted"])


def activate(args: argparse.Namespace) -> int:
    return 0 if _activate_perf_gate(
        args.control_fifo,
        args.ack_fifo,
        args.output,
        args.probe_gate,
        args.perf_executable,
        args.launcher_pid,
    ) else 1


def identity(args: argparse.Namespace) -> int:
    pid = args.host_pid
    status = Path(
        f"/proc/{pid}/status"
    ).read_text(
        encoding="utf-8",
        errors="replace",
    )
    nspid = []
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            nspid = [
                int(value)
                for value in line.split()[1:]
            ]
            break
    stat = Path(
        f"/proc/{pid}/stat"
    ).read_text(
        encoding="utf-8",
        errors="replace",
    )
    tail = stat[
        stat.rfind(")") + 2 :
    ].split()
    start_ticks = int(tail[19])
    tasks = []
    for entry in sorted(
        Path(
            f"/proc/{pid}/task"
        ).iterdir(),
        key=lambda value: int(value.name),
    ):
        if not entry.name.isdigit():
            continue
        text = (
            entry / "status"
        ).read_text(
            encoding="utf-8",
            errors="replace",
        )
        task_nspid = []
        state = ""
        for line in text.splitlines():
            if line.startswith("NSpid:"):
                task_nspid = [
                    int(value)
                    for value
                    in line.split()[1:]
                ]
            elif line.startswith("State:"):
                state = line.split(
                    ":",
                    1,
                )[1].strip()
        tasks.append(
            {
                "host_tid": int(
                    entry.name
                ),
                "nspid": task_nspid,
                "state": state,
            }
        )
    report = {
        "schema": (
            "issue-894-perf-identity-v1"
        ),
        "boot_id": Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text().strip(),
        "host_pid": pid,
        "container_pid": (
            nspid[-1]
            if nspid
            else None
        ),
        "process_nspid": nspid,
        "process_start_ticks": start_ticks,
        "host_tids": [
            row["host_tid"]
            for row in tasks
        ],
        "threads": tasks,
        "pid_namespace": os.readlink(
            f"/proc/{pid}/ns/pid"
        ),
        "time_namespace": os.readlink(
            f"/proc/{pid}/ns/time"
        ),
        "timens_offsets": Path(
            f"/proc/{pid}/timens_offsets"
        ).read_text().strip(),
        "observer_clock": (
            "CLOCK_MONOTONIC"
        ),
        "observer_monotonic_ns": (
            time.monotonic_ns()
        ),
    }
    _write_json(
        args.output,
        report,
    )
    return 0


EVENT_RE = re.compile(
    r"(?P<time>\d+\.\d+):\s+"
    r"sched:(?P<event>"
    r"sched_switch|sched_wakeup(?:_new)?"
    r"):\s+(?P<payload>.*)$"
)
FIELD_RE = re.compile(
    r"([A-Za-z_]+)=([^ ]+)"
)
KEY_VALUE_SWITCH_RE = re.compile(
    r"^prev_comm=(?P<prev_comm>.*?)\s+"
    r"prev_pid=(?P<prev_pid>\d+)\s+"
    r"prev_prio=(?P<prev_prio>-?\d+)\s+"
    r"prev_state=(?P<prev_state>[A-Za-z+|]+)\s+==>\s+"
    r"next_comm=(?P<next_comm>.*?)\s+"
    r"next_pid=(?P<next_pid>\d+)\s+"
    r"next_prio=(?P<next_prio>-?\d+)\s*$"
)
KEY_VALUE_WAKE_RE = re.compile(
    r"^comm=(?P<comm>.*?)\s+"
    r"pid=(?P<pid>\d+)\s+"
    r"prio=(?P<prio>-?\d+)"
    r"(?:(?:\s+success=(?P<success_before>[01]))?"
    r"(?:\s+target_cpu=(?P<target_cpu>\d+))?"
    r"|(?:\s+target_cpu=(?P<target_cpu_before>\d+))"
    r"(?:\s+success=(?P<success_after>[01]))?)\s*$"
)
COMPACT_SWITCH_RE = re.compile(
    r"^(?P<prev_comm>.+):(?P<prev_pid>\d+)\s+"
    r"\[(?P<prev_prio>-?\d+)\]\s+"
    r"(?P<prev_state>[A-Za-z+|]+)\s+==>\s+"
    r"(?P<next_comm>.+):(?P<next_pid>\d+)\s+"
    r"\[(?P<next_prio>-?\d+)\]\s*$"
)
COMPACT_WAKE_RE = re.compile(
    r"^(?P<comm>.+):(?P<pid>\d+)\s+"
    r"\[(?P<prio>-?\d+)\]"
    r"(?:\s+success=(?P<success>[01]))?"
    r"(?:\s+CPU:\s*\d+)?\s*$"
)
PERF_EVENT_SUFFIX_RE = re.compile(
    r"^(?P<payload>.+?)\s+"
    r"(?P<ip>(?:0x)?[0-9a-fA-F]+)\s+"
    r"(?P<symbol>[^()\r\n]+?)\s+"
    r"\((?P<module>[^()\r\n]+)\)\s*$"
)
FRAME_RE = re.compile(
    r"^\s*([0-9a-fA-F]+)\s+"
    r"(.+?)\s+\((.+)\)\s*$"
)
BUILDID_RE = re.compile(
    r"^([0-9a-fA-F]{40,64})\s+(.+)$"
)
HEADER_EVENT_RE = re.compile(
    r"^# event : name = (?P<name>[^,]+),.*?"
    r"id = \{(?P<ids>[^}]*)\}.*?"
    r"sample_type = (?P<sample_type>[^,]+),"
)


def _prev_state_kind(
    value: str,
) -> str:
    state = value.strip().rstrip("+")
    if state == "R":
        return "RUNNABLE_PREEMPTION"
    if state in {
        "S",
        "D",
        "I",
        "K",
        "W",
        "P",
        "N",
    }:
        return "BLOCKED_SWITCH_OUT"
    return "UNRESOLVED"


def _load_markers(
    path: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(
            value,
            dict,
        ):
            rows.append(value)
    return rows


def _phase_metrics(
    markers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[
        str,
        dict[str, Any],
    ] = {}
    for phase in (
        "libc_block",
        "odbc_wait_read",
        "runnable_control",
    ):
        starts = [
            row
            for row in markers
            if row.get("phase") == phase
            and row.get("event")
            == "start"
        ]
        ends = [
            row
            for row in markers
            if row.get("phase") == phase
            and row.get("event")
            == "end"
        ]
        if (
            len(starts) != 1
            or len(ends) != 1
        ):
            output[phase] = {
                "complete": False
            }
            continue
        start = int(
            starts[0][
                "monotonic_ns"
            ]
        )
        end = int(
            ends[0][
                "monotonic_ns"
            ]
        )
        output[phase] = {
            "complete": end >= start,
            "start_ns": start,
            "end_ns": end,
            "elapsed_ms": (
                end - start
            )
            / 1_000_000.0,
            "native_tid": int(
                starts[0][
                    "native_tid"
                ]
            ),
            "completed_work": int(
                ends[0].get(
                    "iterations",
                    1,
                )
            ),
        }
    return output


def _decode_scheduler_payload_only(
    event: str,
    text: str,
) -> tuple[
    dict[str, str] | None,
    str | None,
    str | None,
]:
    stripped = text.strip()
    fields = dict(FIELD_RE.findall(stripped))
    if event == "sched_switch":
        key_value = KEY_VALUE_SWITCH_RE.fullmatch(stripped)
        if key_value is not None:
            return {
                "prev_pid": key_value.group("prev_pid"),
                "next_pid": key_value.group("next_pid"),
                "prev_state": key_value.group("prev_state"),
            }, "key_value", None
        required = ("prev_pid", "next_pid", "prev_state")
        present = [name for name in required if fields.get(name)]
        if present:
            if len(present) != len(required):
                return None, None, "missing_required_field"
            for name in ("prev_pid", "next_pid"):
                try:
                    int(fields[name])
                except ValueError:
                    return None, None, "invalid_numeric_field"
            return None, None, "malformed_key_value_payload"
        compact = COMPACT_SWITCH_RE.fullmatch(stripped)
        if compact is None:
            return None, None, "malformed_compact_payload"
        return {
            "prev_pid": compact.group("prev_pid"),
            "next_pid": compact.group("next_pid"),
            "prev_state": compact.group("prev_state"),
        }, "compact", None
    if event in {"sched_wakeup", "sched_wakeup_new"}:
        key_value = KEY_VALUE_WAKE_RE.fullmatch(stripped)
        if key_value is not None:
            payload = {"pid": key_value.group("pid")}
            success = (
                key_value.group("success_before")
                or key_value.group("success_after")
            )
            if success is not None:
                payload["success"] = success
            return payload, "key_value", None
        compact = COMPACT_WAKE_RE.fullmatch(stripped)
        if compact is not None:
            payload = {"pid": compact.group("pid")}
            if compact.group("success") is not None:
                payload["success"] = compact.group("success")
            return payload, "compact", None
        if fields:
            if not fields.get("pid"):
                return None, None, "missing_required_field"
            try:
                int(fields["pid"])
            except ValueError:
                return None, None, "invalid_numeric_field"
            return None, None, "malformed_key_value_payload"
        return None, None, "malformed_compact_payload"
    return None, None, "unsupported_scheduler_event"


def _decode_scheduler_payload(
    event: str,
    text: str,
) -> tuple[
    dict[str, str] | None,
    str | None,
    str | None,
    bool,
]:
    stripped = text.strip()
    suffix = PERF_EVENT_SUFFIX_RE.fullmatch(stripped)
    if suffix is not None:
        payload, payload_format, suffix_reason = (
            _decode_scheduler_payload_only(
                event,
                suffix.group("payload").strip(),
            )
        )
        if payload is None:
            return None, None, suffix_reason, True
        return payload, payload_format, None, True
    payload, payload_format, reason = (
        _decode_scheduler_payload_only(
            event,
            stripped,
        )
    )
    if payload is not None:
        return payload, payload_format, None, False
    if "(" in stripped or ")" in stripped:
        return None, None, "unsupported_perf_suffix", False
    return None, None, reason, False


def _empty_decoder_accounting() -> dict[str, Any]:
    return {
        "recognized_scheduler_event_lines": 0,
        "decoded_scheduler_event_lines": 0,
        "rejected_scheduler_event_lines": 0,
        "target_associated_decoded_events": 0,
        "decoded_with_perf_suffix": 0,
        "rejection_reasons": {
            "missing_required_field": 0,
            "malformed_compact_payload": 0,
            "malformed_key_value_payload": 0,
            "unsupported_perf_suffix": 0,
            "invalid_numeric_field": 0,
            "unsupported_scheduler_event": 0,
        },
        "by_event": {
            event: {
                "recognized": 0,
                "decoded": 0,
                "rejected": 0,
                "target_associated": 0,
                "key_value": 0,
                "compact": 0,
                "with_perf_suffix": 0,
            }
            for event in (
                "sched_switch",
                "sched_wakeup",
                "sched_wakeup_new",
            )
        },
    }


def _time_text_to_ns(value: str) -> int:
    whole, dot, fraction = value.partition(".")
    if not dot:
        return int(whole) * 1_000_000_000
    nanos = (fraction + "000000000")[:9]
    return int(whole) * 1_000_000_000 + int(nanos)


def _parse_perf_header_text(text: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = HEADER_EVENT_RE.match(line.strip())
        if match is None:
            continue
        name = match.group("name").strip()
        if name not in {
            "sched:sched_switch",
            "sched:sched_wakeup",
            "sched:sched_wakeup_new",
        }:
            continue
        ids = [
            int(value.strip())
            for value in match.group("ids").split(",")
            if value.strip().isdigit()
        ]
        sample_type = {
            token.strip()
            for token in match.group("sample_type").split("|")
            if token.strip()
        }
        entries.append(
            {"name": name, "ids": ids, "sample_type": sorted(sample_type)}
        )
    switches = [row for row in entries if row["name"] == "sched:sched_switch"]
    dwarf_switch = [
        row
        for row in switches
        if {"REGS_USER", "STACK_USER"}.issubset(set(row["sample_type"]))
    ]
    timeline_switch = [
        row
        for row in switches
        if "STACK_USER" not in set(row["sample_type"])
        and "REGS_USER" not in set(row["sample_type"])
    ]
    wakeup = [row for row in entries if row["name"] == "sched:sched_wakeup"]
    wakeup_new = [
        row for row in entries if row["name"] == "sched:sched_wakeup_new"
    ]
    groups = (dwarf_switch, timeline_switch, wakeup, wakeup_new)
    complete = all(len(group) == 1 and group[0]["ids"] for group in groups)
    channels: dict[str, dict[str, Any]] = {}
    if complete:
        for label, row, stack_expected in (
            ("switch_out_dwarf", dwarf_switch[0], True),
            ("timeline_switch", timeline_switch[0], False),
            ("wakeup", wakeup[0], False),
            ("wakeup_new", wakeup_new[0], False),
        ):
            channels[label] = {
                "event": row["name"],
                "ids": sorted(row["ids"]),
                "sample_type": row["sample_type"],
                "stack_expected": stack_expected,
            }
    id_to_channel: dict[int, str] = {}
    collision = False
    for label, channel in channels.items():
        for event_id in channel["ids"]:
            if event_id in id_to_channel:
                collision = True
            id_to_channel[event_id] = label
    complete = bool(complete and not collision)
    return {
        "complete": complete,
        "channels": channels,
        "id_to_channel": id_to_channel,
        "ambiguous_or_missing_configuration": not complete,
    }


def _load_channel_samples(
    path: Path,
    header: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    counts = {
        label: 0
        for label in (
            "switch_out_dwarf",
            "timeline_switch",
            "wakeup",
            "wakeup_new",
            "unknown",
        )
    }
    unknown_ids = 0
    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            continue
        event_name = str(raw.get("event", ""))
        event = (
            event_name.split(":", 1)[1]
            if event_name.startswith("sched:")
            else event_name.rsplit("__", 1)[-1]
        )
        if event not in {
            "sched_switch",
            "sched_wakeup",
            "sched_wakeup_new",
        }:
            continue
        event_id = int(raw.get("id") or 0)
        channel = header["id_to_channel"].get(event_id, "unknown")
        if channel == "unknown":
            unknown_ids += 1
        counts[channel] += 1
        samples.append(
            {
                "event": event,
                "id": event_id,
                "stream_id": int(raw.get("stream_id") or 0),
                "time_ns": int(raw.get("time_ns") or 0),
                "tid": int(raw.get("tid") or 0),
                "channel": channel,
            }
        )
    times = [row["time_ns"] for row in samples if row["time_ns"] > 0]
    complete = bool(header["complete"] and samples and unknown_ids == 0)
    return samples, {
        "complete": complete,
        "header_configuration_complete": header["complete"],
        "unknown_event_id_count": unknown_ids,
        "sample_count": len(samples),
        "first_sample_monotonic_ns": min(times) if times else None,
        "last_sample_monotonic_ns": max(times) if times else None,
        "per_channel_sample_count": counts,
        "channels": header["channels"],
    }


def _channel_sample_queues(
    samples: list[dict[str, Any]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    queues: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for sample in samples:
        queues.setdefault(
            (str(sample["event"]), int(sample["time_ns"])),
            [],
        ).append(sample)
    return queues


def _parse_perf_script_text(
    text: str,
    tids: set[int],
    channel_samples: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    events: list[dict[str, Any]] = []
    stacks: list[dict[str, Any]] = []
    accounting = _empty_decoder_accounting()
    accounting["switch_stack_accounting"] = {
        "target_switch_stack_records": 0,
        "dwarf_expected_stack_records": 0,
        "timeline_only_overlap_records": 0,
        "unknown_channel_target_switch_records": 0,
        "dwarf_expected_empty_stack_records": 0,
    }
    accounting["unknown_channel_scheduler_records"] = 0
    channel_queues = _channel_sample_queues(channel_samples)
    current_stack: dict[str, Any] | None = None
    for raw in text.splitlines():
        match = EVENT_RE.search(raw)
        if match:
            event = match.group("event")
            counters = accounting["by_event"][event]
            accounting["recognized_scheduler_event_lines"] += 1
            counters["recognized"] += 1
            current_stack = None
            (
                payload,
                payload_format,
                rejection_reason,
                had_perf_suffix,
            ) = _decode_scheduler_payload(
                event,
                match.group("payload"),
            )
            if payload is None:
                accounting["rejected_scheduler_event_lines"] += 1
                counters["rejected"] += 1
                reason = rejection_reason or "malformed_compact_payload"
                accounting["rejection_reasons"][reason] += 1
                continue
            target_associated = False
            if event == "sched_switch":
                prev_pid = int(payload["prev_pid"])
                next_pid = int(payload["next_pid"])
                target_associated = prev_pid in tids or next_pid in tids
            else:
                target_associated = int(payload["pid"]) in tids
            time_text = match.group("time")
            time_ns = _time_text_to_ns(time_text)
            queue = channel_queues.get((event, time_ns), [])
            sample_meta = queue.pop(0) if queue else None
            channel = (
                str(sample_meta["channel"])
                if sample_meta is not None
                else "unknown"
            )
            if channel == "unknown":
                accounting["unknown_channel_scheduler_records"] += 1
            row = {
                "time_seconds": float(time_text),
                "time_ns": time_ns,
                "event": event,
                "payload": payload,
                "target_associated": target_associated,
                "channel": channel,
                "sample_id": (
                    int(sample_meta["id"])
                    if sample_meta is not None
                    else None
                ),
            }
            events.append(row)
            accounting["decoded_scheduler_event_lines"] += 1
            counters["decoded"] += 1
            counters[payload_format] += 1
            if had_perf_suffix:
                accounting["decoded_with_perf_suffix"] += 1
                counters["with_perf_suffix"] += 1
            if target_associated:
                accounting["target_associated_decoded_events"] += 1
                counters["target_associated"] += 1
            if event == "sched_switch":
                prev_pid = int(payload["prev_pid"])
                if prev_pid in tids:
                    switch_accounting = accounting["switch_stack_accounting"]
                    switch_accounting["target_switch_stack_records"] += 1
                    if channel == "switch_out_dwarf":
                        switch_accounting["dwarf_expected_stack_records"] += 1
                        current_stack = {
                            "time_seconds": row["time_seconds"],
                            "time_ns": row["time_ns"],
                            "tid": prev_pid,
                            "sample_id": row["sample_id"],
                            "channel": channel,
                            "frames": [],
                            "dwarf_expected": True,
                            "timeline_only_overlap": False,
                        }
                        stacks.append(current_stack)
                    elif channel == "timeline_switch":
                        switch_accounting["timeline_only_overlap_records"] += 1
                    else:
                        switch_accounting[
                            "unknown_channel_target_switch_records"
                        ] += 1
            continue
        if current_stack is not None:
            frame = FRAME_RE.match(raw)
            if frame:
                current_stack["frames"].append(
                    {
                        "ip": "0x" + frame.group(1).lower(),
                        "symbol": frame.group(2).strip()[:240],
                        "module": frame.group(3).strip()[:400],
                    }
                )
    accounting["switch_stack_accounting"][
        "dwarf_expected_empty_stack_records"
    ] = sum(1 for stack in stacks if not stack.get("frames"))
    accounting["unmatched_channel_sample_count"] = sum(
        len(values)
        for values in channel_queues.values()
    )
    return events, stacks, accounting


def _parse_perf_script(
    path: Path,
    tids: set[int],
    channel_samples: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    return _parse_perf_script_text(
        path.read_text(
            encoding="utf-8",
            errors="replace",
        ),
        tids,
        channel_samples,
    )


def _transitions(
    events: list[dict[str, Any]],
    tids: set[int],
) -> tuple[
    list[dict[str, Any]],
    int,
]:
    timeline: list[
        dict[str, Any]
    ] = []
    unresolved = 0
    pending_wakeup: dict[
        int,
        float,
    ] = {}
    pending_block: dict[
        int,
        float,
    ] = {}
    for row in sorted(
        events,
        key=lambda item: item[
            "time_seconds"
        ],
    ):
        event = row["event"]
        payload = row["payload"]
        if event == "sched_switch":
            prev = int(payload["prev_pid"])
            nxt = int(payload["next_pid"])
            if prev in tids:
                kind = (
                    _prev_state_kind(
                        str(
                            payload.get(
                                "prev_state",
                                "",
                            )
                        )
                    )
                )
                timeline.append(
                    {
                        "time_seconds": (
                            row[
                                "time_seconds"
                            ]
                        ),
                        "tid": prev,
                        "kind": kind,
                    }
                )
                if (
                    kind
                    == "BLOCKED_SWITCH_OUT"
                ):
                    pending_block[
                        prev
                    ] = row[
                        "time_seconds"
                    ]
                elif (
                    kind
                    == "UNRESOLVED"
                ):
                    unresolved += 1
            if nxt in tids:
                wake = (
                    pending_wakeup.pop(
                        nxt,
                        None,
                    )
                )
                timeline.append(
                    {
                        "time_seconds": (
                            row[
                                "time_seconds"
                            ]
                        ),
                        "tid": nxt,
                        "kind": (
                            "SWITCH_IN"
                        ),
                        "runnable_delay_ms": (
                            None
                            if wake
                            is None
                            else (
                                row[
                                    "time_seconds"
                                ]
                                - wake
                            )
                            * 1000.0
                        ),
                    }
                )
        else:
            tid = int(payload["pid"])
            if tid in tids:
                block = (
                    pending_block.pop(
                        tid,
                        None,
                    )
                )
                pending_wakeup[
                    tid
                ] = row[
                    "time_seconds"
                ]
                timeline.append(
                    {
                        "time_seconds": (
                            row[
                                "time_seconds"
                            ]
                        ),
                        "tid": tid,
                        "kind": "WAKEUP",
                        "blocked_wait_ms": (
                            None
                            if block
                            is None
                            else (
                                row[
                                    "time_seconds"
                                ]
                                - block
                            )
                            * 1000.0
                        ),
                    }
                )
    unresolved += (
        len(pending_block)
        + len(pending_wakeup)
    )
    return timeline, unresolved


def _parse_buildids(
    path: Path,
) -> list[dict[str, str]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        match = BUILDID_RE.match(
            line.strip()
        )
        if match:
            rows.append(
                {
                    "build_id": (
                        match.group(
                            1
                        ).lower()
                    ),
                    "module": (
                        match.group(
                            2
                        )[:500]
                    ),
                }
            )
    return rows


def _essential_chain(
    stacks: list[dict[str, Any]],
    buildids: list[
        dict[str, str]
    ],
) -> dict[str, Any]:
    categories = {
        "libpython": False,
        "pyodbc": False,
        "unixodbc": False,
        "msodbcsql": False,
        "libc": False,
    }
    evidence: dict[
        str,
        list[dict[str, str]],
    ] = {
        key: []
        for key in categories
    }
    for stack in stacks:
        for frame in stack.get(
            "frames",
            [],
        ):
            text = (
                str(
                    frame.get(
                        "module",
                        "",
                    )
                )
                + " "
                + str(
                    frame.get(
                        "symbol",
                        "",
                    )
                )
            ).casefold()
            key = None
            if (
                "libpython3.12"
                in text
                or "/python3.12"
                in text
            ):
                key = "libpython"
            elif "pyodbc" in text:
                key = "pyodbc"
            elif "libodbc" in text:
                key = "unixodbc"
            elif "msodbcsql" in text:
                key = "msodbcsql"
            elif (
                "libc.so" in text
                or "libc-" in text
            ):
                key = "libc"
            if key:
                categories[
                    key
                ] = True
                if (
                    len(
                        evidence[
                            key
                        ]
                    )
                    < 8
                ):
                    evidence[
                        key
                    ].append(
                        {
                            "ip": str(
                                frame.get(
                                    "ip",
                                    "",
                                )
                            ),
                            "symbol": str(
                                frame.get(
                                    "symbol",
                                    "",
                                )
                            ),
                            "module": str(
                                frame.get(
                                    "module",
                                    "",
                                )
                            ),
                        }
                    )
    libpython_build = [
        row
        for row in buildids
        if (
            "libpython3.12.so.1.0"
            in row["module"]
        )
    ]
    libpython_id_ok = any(
        row["build_id"]
        == LIBPYTHON_BUILD_ID
        for row in libpython_build
    )
    relevant_build_ids = [
        row
        for row in buildids
        if any(
            token in row["module"].casefold()
            for token in (
                "libpython3.12",
                "pyodbc",
                "libodbc",
                "msodbcsql",
                "libc.so",
            )
        )
    ][:32]
    return {
        "boundaries": categories,
        "evidence": evidence,
        "module_build_ids": relevant_build_ids,
        "libpython_build_id_expected": (
            LIBPYTHON_BUILD_ID
        ),
        "libpython_build_id_matches": (
            libpython_id_ok
        ),
        "complete": (
            all(
                categories.values()
            )
            and libpython_id_ok
        ),
    }


def summarize(
    args: argparse.Namespace,
) -> int:
    identity_report = json.loads(
        args.identity.read_text(
            encoding="utf-8"
        )
    )
    tids = set(
        int(value)
        for value in identity_report[
            "host_tids"
        ]
    )
    readiness = json.loads(
        args.readiness.read_text(encoding="utf-8")
    )
    header = _parse_perf_header_text(
        args.perf_header.read_text(
            encoding="utf-8",
            errors="replace",
        )
    )
    channel_samples, channel_accounting = _load_channel_samples(
        args.channel_samples,
        header,
    )
    events, stacks, decoder_accounting = _parse_perf_script(
        args.perf_script,
        tids,
        channel_samples,
    )
    decoder_accounting["channel_accounting"] = channel_accounting
    control_markers = (
        _load_markers(
            args.control_markers
        )
    )
    observed_markers = (
        _load_markers(
            args.observed_markers
        )
    )
    control = _phase_metrics(
        control_markers
    )
    observed = _phase_metrics(
        observed_markers
    )
    # Perf uses CLOCK_MONOTONIC, matching the synthetic phase markers.
    # Restrict scheduler interpretation to the controlled phase windows so
    # the post-probe keepalive loop cannot create artificial unresolved tails.
    phase_scheduler: dict[str, dict[str, Any]] = {}
    phase_timelines: dict[str, list[dict[str, Any]]] = {}
    for phase in (
        "libc_block",
        "odbc_wait_read",
        "runnable_control",
    ):
        metrics = observed.get(phase, {})
        start_ns = int(metrics.get("start_ns") or 0)
        end_ns = int(metrics.get("end_ns") or 0)
        selected_events = [
            row
            for row in events
            if start_ns
            <= int(row["time_ns"])
            <= end_ns
        ]
        phase_timeline, phase_unresolved = _transitions(
            selected_events,
            tids,
        )
        phase_timelines[phase] = phase_timeline
        phase_scheduler[phase] = {
            "blocked_switch_out_count": sum(
                row["kind"] == "BLOCKED_SWITCH_OUT"
                for row in phase_timeline
            ),
            "wakeup_count": sum(
                row["kind"] == "WAKEUP"
                for row in phase_timeline
            ),
            "switch_in_count": sum(
                row["kind"] == "SWITCH_IN"
                for row in phase_timeline
            ),
            "runnable_preemption_count": sum(
                row["kind"] == "RUNNABLE_PREEMPTION"
                for row in phase_timeline
            ),
            "unresolved_transition_count": phase_unresolved,
            "decoded_event_count": len(selected_events),
            "target_associated_event_count": sum(
                bool(row.get("target_associated"))
                for row in selected_events
            ),
            "timeline": phase_timeline[:80],
        }
    phase_starts = [
        int(observed[phase].get("start_ns") or 0)
        for phase in (
            "libc_block",
            "odbc_wait_read",
            "runnable_control",
        )
        if observed[phase].get("complete") is True
    ]
    phase_ends = [
        int(observed[phase].get("end_ns") or 0)
        for phase in (
            "libc_block",
            "odbc_wait_read",
            "runnable_control",
        )
        if observed[phase].get("complete") is True
    ]
    first_phase_ns = min(phase_starts) if phase_starts else 0
    last_phase_ns = max(phase_ends) if phase_ends else 0
    timing_counts = {
        "before_controlled_phases": 0,
        "inside_controlled_phases": 0,
        "after_controlled_phases": 0,
    }
    for row in events:
        if not row.get("target_associated"):
            continue
        event_ns = int(float(row["time_seconds"]) * 1_000_000_000)
        inside = any(
            int(observed[phase].get("start_ns") or 0)
            <= event_ns
            <= int(observed[phase].get("end_ns") or 0)
            for phase in (
                "libc_block",
                "odbc_wait_read",
                "runnable_control",
            )
            if observed[phase].get("complete") is True
        )
        if inside:
            timing_counts["inside_controlled_phases"] += 1
        elif first_phase_ns and event_ns < first_phase_ns:
            timing_counts["before_controlled_phases"] += 1
        elif last_phase_ns and event_ns > last_phase_ns:
            timing_counts["after_controlled_phases"] += 1
    decoder_accounting["retained_before_phase_window_filtering"] = len(events)
    decoder_accounting["target_associated_before_phase_window_filtering"] = sum(
        bool(row.get("target_associated"))
        for row in events
    )
    decoder_accounting["retained_after_phase_window_filtering"] = sum(
        value["decoded_event_count"]
        for value in phase_scheduler.values()
    )
    decoder_accounting["retained_after_phase_window_filtering_by_event"] = {
        event: sum(
            1
            for phase in (
                "libc_block",
                "odbc_wait_read",
                "runnable_control",
            )
            for row in events
            if row["event"] == event
            and int(observed[phase].get("start_ns") or 0)
            <= int(float(row["time_seconds"]) * 1_000_000_000)
            <= int(observed[phase].get("end_ns") or 0)
            and observed[phase].get("complete") is True
        )
        for event in (
            "sched_switch",
            "sched_wakeup",
            "sched_wakeup_new",
        )
    }
    decoder_accounting["target_associated_after_phase_window_filtering"] = sum(
        value["target_associated_event_count"]
        for value in phase_scheduler.values()
    )
    decoder_accounting["target_event_timing"] = timing_counts
    controlled_timeline = (
        phase_timelines["libc_block"]
        + phase_timelines["odbc_wait_read"]
        + phase_timelines["runnable_control"]
    )
    unresolved = sum(
        int(value["unresolved_transition_count"])
        for value in phase_scheduler.values()
    )
    buildids = _parse_buildids(
        args.buildids
    )
    odbc_start_ns = int(
        observed.get("odbc_wait_read", {}).get("start_ns") or 0
    )
    odbc_end_ns = int(
        observed.get("odbc_wait_read", {}).get("end_ns") or 0
    )
    odbc_stacks = [
        stack
        for stack in stacks
        if stack.get("dwarf_expected") is True
        and odbc_start_ns
        <= int(stack["time_ns"])
        <= odbc_end_ns
    ]
    chain = _essential_chain(
        odbc_stacks,
        buildids,
    )

    cpu_control = control[
        "runnable_control"
    ]
    cpu_observed = observed[
        "runnable_control"
    ]
    control_ms = float(
        cpu_control.get(
            "elapsed_ms",
            0.0,
        )
    )
    observed_ms = float(
        cpu_observed.get(
            "elapsed_ms",
            0.0,
        )
    )
    overhead_limit = max(
        control_ms * 1.10,
        control_ms + 50.0,
    )
    perturbation_ok = bool(
        control_ms > 0
        and observed_ms
        <= overhead_limit
        and cpu_control.get(
            "completed_work"
        )
        == cpu_observed.get(
            "completed_work"
        )
    )

    perf_time = (
        args.perf_time.read_text(
            encoding="utf-8",
            errors="replace",
        )
        if args.perf_time.exists()
        else ""
    )

    def find_metric(
        label: str,
    ) -> float | None:
        match = re.search(
            rf"{re.escape(label)}:"
            r"\s*([0-9.]+)",
            perf_time,
        )
        return (
            float(match.group(1))
            if match
            else None
        )

    script_text = (
        args.perf_script.read_text(
            encoding="utf-8",
            errors="replace",
        )
    )
    lost = sum(
        1
        for line in script_text.splitlines()
        if "LOST" in line.upper()
    )
    throttled = (
        sum(
            1
            for line
            in args.perf_stderr.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            if "thrott"
            in line.casefold()
        )
        if args.perf_stderr.exists()
        else 0
    )
    truncated = sum(
        1
        for stack in stacks
        if stack.get("dwarf_expected") is True
        and not stack.get("frames")
    )

    blocked = [
        row
        for row in controlled_timeline
        if row["kind"] == "BLOCKED_SWITCH_OUT"
    ]
    wakeups = [
        row
        for row in controlled_timeline
        if row["kind"] == "WAKEUP"
    ]
    switchins = [
        row
        for row in controlled_timeline
        if row["kind"] == "SWITCH_IN"
    ]
    runnable = [
        row
        for row in controlled_timeline
        if row["kind"] == "RUNNABLE_PREEMPTION"
    ]
    activation_ack_ns = int(
        readiness.get("activation_ack_monotonic_ns") or 0
    )
    probe_release_ns = int(
        readiness.get("probe_release_monotonic_ns") or 0
    )
    activation_ok = bool(
        readiness.get("accepted") is True
        and readiness.get("probe_released") is True
        and readiness.get("clock") == "CLOCK_MONOTONIC"
        and activation_ack_ns > 0
        and probe_release_ns >= activation_ack_ns
        and (not first_phase_ns or probe_release_ns <= first_phase_ns)
    )
    channel_attribution_ok = bool(
        channel_accounting["complete"]
        and decoder_accounting["unknown_channel_scheduler_records"] == 0
        and decoder_accounting["unmatched_channel_sample_count"] == 0
        and decoder_accounting["switch_stack_accounting"][
            "unknown_channel_target_switch_records"
        ] == 0
    )
    transitions_complete = bool(
        all(
            phase_scheduler[phase]["blocked_switch_out_count"] > 0
            and phase_scheduler[phase]["wakeup_count"] > 0
            and phase_scheduler[phase]["switch_in_count"] > 0
            and phase_scheduler[phase]["unresolved_transition_count"] == 0
            for phase in ("libc_block", "odbc_wait_read")
        )
    )

    passed = all(
        [
            chain["complete"],
            transitions_complete,
            activation_ok,
            channel_attribution_ok,
            lost == 0,
            throttled == 0,
            truncated == 0,
            perturbation_ok,
            all(
                value.get(
                    "complete"
                )
                is True
                for value
                in observed.values()
            ),
        ]
    )
    result = (
        "PASS — PERF/DWARF CAPABILITY DEMONSTRATED"
        if passed
        else (
            "FAIL/INCOMPLETE — "
            "CAPABILITY NOT DEMONSTRATED"
        )
    )

    report = {
        "schema": SCHEMA,
        "result": result,
        "pr_head": REFERENCE_HEAD,
        "diagnostic_reference": {
            "image_id": (
                REFERENCE_IMAGE_ID
            ),
            "platform_manifest": (
                REFERENCE_PLATFORM
            ),
            "config": (
                REFERENCE_CONFIG
            ),
            "archive_sha256": (
                REFERENCE_ARCHIVE_SHA256
            ),
        },
        "perf": {
            "version": (
                args.perf_version
            ),
            "build_options": (
                args.perf_build_options
            ),
            "clock": (
                "CLOCK_MONOTONIC"
            ),
            "raw_stack_bytes": (
                RAW_STACK_BYTES
            ),
            "observer_cpu_user_seconds": (
                find_metric(
                    "User time (seconds)"
                )
            ),
            "observer_cpu_system_seconds": (
                find_metric(
                    "System time (seconds)"
                )
            ),
            "observer_max_rss_kb": (
                find_metric(
                    "Maximum resident set size (kbytes)"
                )
            ),
        },
        "identity": identity_report,
        "recording_activation": {
            **readiness,
            "validated_before_controlled_phase": activation_ok,
            "controlled_phase_bounds_monotonic_ns": {
                phase: {
                    "start_ns": observed[phase].get("start_ns"),
                    "end_ns": observed[phase].get("end_ns"),
                }
                for phase in (
                    "libc_block",
                    "odbc_wait_read",
                    "runnable_control",
                )
            },
            "first_scheduler_sample_monotonic_ns": (
                channel_accounting["first_sample_monotonic_ns"]
            ),
            "last_scheduler_sample_monotonic_ns": (
                channel_accounting["last_sample_monotonic_ns"]
            ),
        },
        "control_phases": control,
        "observed_phases": observed,
        "scheduler": {
            "decoder_accounting": decoder_accounting,
            "channel_attribution_complete": channel_attribution_ok,
            "blocked_switch_out_count": (
                len(blocked)
            ),
            "wakeup_count": (
                len(wakeups)
            ),
            "switch_in_count": (
                len(switchins)
            ),
            "runnable_preemption_count": (
                len(runnable)
            ),
            "unresolved_transition_count": (
                unresolved
            ),
            "controlled_phases": phase_scheduler,
            "timeline": controlled_timeline[:200],
            "complete": transitions_complete,
        },
        "native_unwind": {
            **chain,
            "odbc_switch_out_stack_count": len(odbc_stacks),
            "switch_stack_accounting": decoder_accounting[
                "switch_stack_accounting"
            ],
        },
        "capture_quality": {
            "lost_event_count": lost,
            "throttling_count": (
                throttled
            ),
            "truncated_stack_count": (
                truncated
            ),
        },
        "perturbation": {
            "control_runnable_elapsed_ms": (
                control_ms
            ),
            "observed_runnable_elapsed_ms": (
                observed_ms
            ),
            "allowed_elapsed_ms": (
                overhead_limit
            ),
            "completed_work_equal": (
                cpu_control.get(
                    "completed_work"
                )
                == cpu_observed.get(
                    "completed_work"
                )
            ),
            "material_perturbation": (
                not perturbation_ok
            ),
        },
        "raw_perf_data_uploaded": False,
        "cleanup_required": True,
    }
    _write_json(
        args.output,
        report,
    )
    return (
        0
        if passed
        else 1
    )


def _self_test() -> None:
    argv = perf_argv(
        [11, 22],
        Path("/tmp/perf.data"),
        control_fifo=Path("/tmp/perf-control.fifo"),
        ack_fifo=Path("/tmp/perf-ack.fifo"),
    )
    text = " ".join(argv)
    assert (
        "sched:sched_switch/"
        "call-graph=dwarf,stack-size=8192/"
        in text
    )
    assert "call-graph=dwarf,8192" not in text
    parser_text = " ".join(
        perf_parser_argv("/usr/bin/perf")
    )
    assert "/usr/bin/perf record --dry-run" in parser_text
    assert switch_out_event() in parser_text
    assert (
        text.count(
            "call-graph=dwarf"
        )
        == 1
    )
    assert (
        "prev_pid == 11 || "
        "prev_pid == 22"
        in text
    )
    assert (
        "next_pid == 11"
        in text
        and "next_pid == 22"
        in text
    )
    assert (
        "pid == 11 || pid == 22"
        in text
    )
    assert "--clockid mono" in text
    assert "--delay -1" in text
    assert "--control fifo:/tmp/perf-control.fifo,/tmp/perf-ack.fifo" in text
    assert (
        _prev_state_kind("R")
        == "RUNNABLE_PREEMPTION"
    )
    assert (
        _prev_state_kind("S")
        == "BLOCKED_SWITCH_OUT"
    )
    assert (
        _prev_state_kind("D")
        == "BLOCKED_SWITCH_OUT"
    )
    assert (
        _prev_state_kind("X")
        == "UNRESOLVED"
    )
    events = [
        {
            "time_seconds": 1.0,
            "event": "sched_switch",
            "payload": {
                "prev_pid": "11",
                "prev_state": "S",
                "next_pid": "99",
            },
        },
        {
            "time_seconds": 1.1,
            "event": "sched_wakeup",
            "payload": {
                "pid": "11"
            },
        },
        {
            "time_seconds": 1.15,
            "event": "sched_switch",
            "payload": {
                "prev_pid": "99",
                "prev_state": "R",
                "next_pid": "11",
            },
        },
    ]
    timeline, unresolved = (
        _transitions(
            events,
            {11},
        )
    )
    assert unresolved == 0
    assert [
        row["kind"]
        for row in timeline
    ] == [
        "BLOCKED_SWITCH_OUT",
        "WAKEUP",
        "SWITCH_IN",
    ]
    assert abs(
        float(
            timeline[-1][
                "runnable_delay_ms"
            ]
        )
        - 50.0
    ) < 0.001
    incomplete, unresolved = (
        _transitions(
            events[:2],
            {11},
        )
    )
    assert unresolved > 0
    assert incomplete
    assert OBSERVED_SECONDS <= 10
    assert _essential_chain([], [])["complete"] is False
    assert RAW_STACK_BYTES == 8192
    assert REFERENCE_HEAD == "1cb01861ddba17babb0b6b3b09d062bd15107f20"

    def readiness_case(
        delay_seconds: float | None,
        expect_success: bool,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_fifo = root / "control"
            ack_fifo = root / "ack"
            gate = root / "gate"
            output = root / "readiness.json"
            os.mkfifo(control_fifo)
            os.mkfifo(ack_fifo)
            worker: threading.Thread | None = None
            if delay_seconds is not None:
                def responder() -> None:
                    control_fd = os.open(
                        control_fifo,
                        os.O_RDWR | os.O_NONBLOCK,
                    )
                    ack_fd = os.open(
                        ack_fifo,
                        os.O_RDWR | os.O_NONBLOCK,
                    )
                    try:
                        deadline = time.monotonic() + 1.0
                        data = b""
                        while time.monotonic() < deadline:
                            readable, _, _ = select.select(
                                [control_fd],
                                [],
                                [],
                                0.05,
                            )
                            if not readable:
                                continue
                            try:
                                data += os.read(control_fd, 128)
                            except BlockingIOError:
                                continue
                            if b"enable\n" in data:
                                time.sleep(delay_seconds)
                                os.write(ack_fd, b"ack\n")
                                return
                    finally:
                        os.close(control_fd)
                        os.close(ack_fd)
                worker = threading.Thread(target=responder, daemon=True)
                worker.start()
            accepted = _activate_perf_gate(
                control_fifo,
                ack_fifo,
                output,
                gate,
                "/usr/bin/perf",
                1234,
                deadline_seconds=0.15,
            )
            if worker is not None:
                worker.join(timeout=1.0)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            assert accepted is expect_success
            assert gate.exists() is expect_success
            assert evidence["probe_released"] is expect_success
            return evidence

    immediate = readiness_case(0.0, True)
    delayed = readiness_case(0.03, True)
    missing = readiness_case(None, False)
    assert immediate["activation_ack_monotonic_ns"] <= immediate[
        "probe_release_monotonic_ns"
    ]
    assert delayed["activation_ack_monotonic_ns"] <= delayed[
        "probe_release_monotonic_ns"
    ]
    assert missing["timed_out"] is True
    assert missing["probe_release_monotonic_ns"] is None

    header_text = """
# event : name = sched:sched_switch, , id = { 10 }, type = 2, sample_type = IP|TID|TIME|ID|CPU|RAW|REGS_USER|STACK_USER,
# event : name = sched:sched_switch, , id = { 20 }, type = 2, sample_type = IP|TID|TIME|ID|CPU|RAW,
# event : name = sched:sched_wakeup, , id = { 30 }, type = 2, sample_type = IP|TID|TIME|ID|CPU|RAW,
# event : name = sched:sched_wakeup_new, , id = { 40 }, type = 2, sample_type = IP|TID|TIME|ID|CPU|RAW,
"""
    header_model = _parse_perf_header_text(header_text)
    assert header_model["complete"] is True
    assert header_model["id_to_channel"][10] == "switch_out_dwarf"
    assert header_model["id_to_channel"][20] == "timeline_switch"

    key_value_script = """
worker 11 1.000000: sched:sched_switch: prev_comm=worker prev_pid=11 prev_prio=120 prev_state=S ==> next_comm=idle next_pid=99 next_prio=120
  7fff0000 blocked_call (/lib/libc.so.6)
worker 11 1.100000: sched:sched_wakeup: comm=worker pid=11 prio=120 target_cpu=001
idle 99 1.150000: sched:sched_switch: prev_comm=idle prev_pid=99 prev_prio=120 prev_state=R ==> next_comm=worker next_pid=11 next_prio=120
"""
    compact_switch_payload = "worker:11 [120] S ==> idle:99 [120]"
    compact_switch_annotated = (
        compact_switch_payload
        + " 7fff0000 schedule+0x10 (/lib/libc.so.6)"
    )
    plain_switch, plain_switch_format, plain_switch_reason, plain_switch_suffix = (
        _decode_scheduler_payload(
            "sched_switch",
            compact_switch_payload,
        )
    )
    annotated_switch, annotated_switch_format, annotated_switch_reason, annotated_switch_suffix = (
        _decode_scheduler_payload(
            "sched_switch",
            compact_switch_annotated,
        )
    )
    assert plain_switch == annotated_switch == {
        "prev_pid": "11",
        "next_pid": "99",
        "prev_state": "S",
    }
    assert plain_switch_format == annotated_switch_format == "compact"
    assert plain_switch_reason is annotated_switch_reason is None
    assert plain_switch_suffix is False
    assert annotated_switch_suffix is True

    for event in ("sched_wakeup", "sched_wakeup_new"):
        wake_payload = "worker:11 [120] CPU:001"
        wake_success_zero = "worker:11 [120] success=0 CPU:001"
        wake_success = "worker:11 [120] success=1 CPU:001"
        for payload_text, expected_success in (
            (wake_payload, None),
            (wake_success_zero, "0"),
            (wake_success, "1"),
        ):
            decoded = _decode_scheduler_payload(
                event,
                payload_text,
            )
            assert decoded[0] is not None
            assert decoded[0]["pid"] == "11"
            assert decoded[0].get("success") == expected_success
            assert decoded[1] == "compact"
            assert decoded[2] is None
            assert decoded[3] is False
        wake_annotated = (
            wake_success
            + " 7fff0000 wake_up+0x20 (/lib/libc.so.6)"
        )
        annotated_wake, annotated_format, annotated_reason, annotated_suffix = (
            _decode_scheduler_payload(event, wake_annotated)
        )
        assert annotated_wake is not None
        assert annotated_wake["pid"] == "11"
        assert annotated_wake["success"] == "1"
        assert annotated_format == "compact"
        assert annotated_reason is None
        assert annotated_suffix is True

    for event in ("sched_wakeup", "sched_wakeup_new"):
        malformed_canonical = _decode_scheduler_payload(
            event,
            "comm=worker prio=120 target_cpu=001",
        )
        assert malformed_canonical[0] is None
        assert malformed_canonical[2] == "missing_required_field"
        malformed_compact = _decode_scheduler_payload(
            event,
            "worker [120] CPU:001",
        )
        assert malformed_compact[0] is None
        assert malformed_compact[2] == "malformed_compact_payload"
        mixed = _decode_scheduler_payload(
            event,
            "worker:11 [120] pid=11 success=1 CPU:001",
        )
        assert mixed[0] is None
        assert mixed[2] == "malformed_key_value_payload"
    rejected_suffix = _decode_scheduler_payload(
        "sched_switch",
        compact_switch_payload + " arbitrary trailing text",
    )
    assert rejected_suffix[0] is None
    assert rejected_suffix[2] == "malformed_compact_payload"
    rejected_bad_annotation = _decode_scheduler_payload(
        "sched_switch",
        compact_switch_payload + " symbol (unterminated",
    )
    assert rejected_bad_annotation[0] is None
    assert rejected_bad_annotation[2] == "unsupported_perf_suffix"
    canonical_arbitrary = _decode_scheduler_payload(
        "sched_switch",
        (
            "prev_comm=worker prev_pid=11 prev_prio=120 "
            "prev_state=S ==> next_comm=idle next_pid=99 "
            "next_prio=120 arbitrary"
        ),
    )
    assert canonical_arbitrary[0] is None
    assert canonical_arbitrary[2] == "malformed_key_value_payload"
    compact_script = """
worker 11 1.000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
  7fff0000 blocked_call (/lib/libc.so.6)
worker 11 1.100000: sched:sched_wakeup: worker:11 [120] CPU:001
idle 99 1.150000: sched:sched_switch: idle:99 [120] R ==> worker:11 [120]
"""
    base_channel_samples = [
        {
            "event": "sched_switch",
            "id": 10,
            "stream_id": 10,
            "time_ns": 1_000_000_000,
            "tid": 11,
            "channel": "switch_out_dwarf",
        },
        {
            "event": "sched_wakeup",
            "id": 30,
            "stream_id": 30,
            "time_ns": 1_100_000_000,
            "tid": 11,
            "channel": "wakeup",
        },
        {
            "event": "sched_switch",
            "id": 20,
            "stream_id": 20,
            "time_ns": 1_150_000_000,
            "tid": 99,
            "channel": "timeline_switch",
        },
    ]
    key_events, key_stacks, key_accounting = _parse_perf_script_text(
        key_value_script,
        {11},
        base_channel_samples,
    )
    compact_events, compact_stacks, compact_accounting = _parse_perf_script_text(
        compact_script,
        {11},
        base_channel_samples,
    )
    assert [row["kind"] for row in _transitions(key_events, {11})[0]] == [
        "BLOCKED_SWITCH_OUT",
        "WAKEUP",
        "SWITCH_IN",
    ]
    assert [row["kind"] for row in _transitions(compact_events, {11})[0]] == [
        "BLOCKED_SWITCH_OUT",
        "WAKEUP",
        "SWITCH_IN",
    ]
    assert key_events[0]["payload"]["prev_pid"] == "11"
    assert key_events[0]["payload"]["next_pid"] == "99"
    assert key_events[0]["payload"]["prev_state"] == "S"
    assert compact_events[0]["payload"] == {
        "prev_pid": "11",
        "next_pid": "99",
        "prev_state": "S",
    }
    assert key_events[1]["payload"]["pid"] == "11"
    assert compact_events[1]["payload"]["pid"] == "11"
    for event in ("sched_wakeup", "sched_wakeup_new"):
        key_wakeup = _decode_scheduler_payload(
            event,
            "comm=worker pid=11 prio=120 target_cpu=001 success=1",
        )
        compact_wakeup = _decode_scheduler_payload(
            event,
            "worker:11 [120] success=1 CPU:001",
        )
        assert key_wakeup[0] is not None
        assert compact_wakeup[0] is not None
        assert key_wakeup[0]["pid"] == compact_wakeup[0]["pid"] == "11"
        assert key_wakeup[0]["success"] == compact_wakeup[0]["success"] == "1"
        assert key_wakeup[1] == "key_value"
        assert compact_wakeup[1] == "compact"
    assert len(key_stacks) == len(compact_stacks) == 1
    assert key_stacks[0]["tid"] == compact_stacks[0]["tid"] == 11
    assert len(key_stacks[0]["frames"]) == len(compact_stacks[0]["frames"]) == 1
    assert key_accounting["rejected_scheduler_event_lines"] == 0
    assert compact_accounting["rejected_scheduler_event_lines"] == 0
    annotated_compact_script = """
worker 11 1.000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120] 7fff0000 schedule+0x10 (/lib/libc.so.6)
  7fff0001 blocked_call (/lib/libc.so.6)
worker 11 1.100000: sched:sched_wakeup: worker:11 [120] success=1 CPU:001 7fff0002 wake_up+0x20 (/lib/libc.so.6)
idle 99 1.150000: sched:sched_switch: idle:99 [120] R ==> worker:11 [120] 7fff0003 finish_task_switch+0x30 (/lib/libc.so.6)
"""
    annotated_events, annotated_stacks, annotated_accounting = (
        _parse_perf_script_text(
            annotated_compact_script,
            {11},
            base_channel_samples,
        )
    )
    assert [
        row["kind"]
        for row in _transitions(
            annotated_events,
            {11},
        )[0]
    ] == [
        "BLOCKED_SWITCH_OUT",
        "WAKEUP",
        "SWITCH_IN",
    ]
    assert annotated_accounting["decoded_with_perf_suffix"] == 3
    assert annotated_accounting["rejected_scheduler_event_lines"] == 0
    assert len(annotated_stacks) == 1
    assert annotated_stacks[0]["dwarf_expected"] is True
    assert len(annotated_stacks[0]["frames"]) == 1

    overlap_script = """
worker 11 2.000000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
worker 11 2.000000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
  7fff0010 blocked_call (/lib/libc.so.6)
"""
    overlap_samples = [
        {
            "event": "sched_switch",
            "id": 20,
            "stream_id": 20,
            "time_ns": 2_000_000_000,
            "tid": 11,
            "channel": "timeline_switch",
        },
        {
            "event": "sched_switch",
            "id": 10,
            "stream_id": 10,
            "time_ns": 2_000_000_000,
            "tid": 11,
            "channel": "switch_out_dwarf",
        },
    ]
    _, overlap_stacks, overlap_accounting = _parse_perf_script_text(
        overlap_script,
        {11},
        overlap_samples,
    )
    assert len(overlap_stacks) == 1
    assert overlap_stacks[0]["dwarf_expected"] is True
    assert overlap_accounting["switch_stack_accounting"] == {
        "target_switch_stack_records": 2,
        "dwarf_expected_stack_records": 1,
        "timeline_only_overlap_records": 1,
        "unknown_channel_target_switch_records": 0,
        "dwarf_expected_empty_stack_records": 0,
    }

    separated_script = """
worker 11 2.000000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
worker 11 2.000000100: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
  7fff0010 blocked_call (/lib/libc.so.6)
"""
    separated_samples = [
        {
            "event": "sched_switch",
            "id": 20,
            "stream_id": 20,
            "time_ns": 2_000_000_000,
            "tid": 11,
            "channel": "timeline_switch",
        },
        {
            "event": "sched_switch",
            "id": 10,
            "stream_id": 10,
            "time_ns": 2_000_000_100,
            "tid": 11,
            "channel": "switch_out_dwarf",
        },
    ]
    _, separated_stacks, separated_accounting = _parse_perf_script_text(
        separated_script,
        {11},
        separated_samples,
    )
    assert len(separated_stacks) == 1
    assert separated_stacks[0]["time_ns"] == 2_000_000_100
    assert separated_accounting["switch_stack_accounting"][
        "timeline_only_overlap_records"
    ] == 1
    assert separated_accounting["switch_stack_accounting"][
        "dwarf_expected_stack_records"
    ] == 1

    empty_expected_script = """
worker 11 3.000000: sched:sched_switch: worker:11 [120] S ==> idle:99 [120]
"""
    empty_samples = [
        {
            "event": "sched_switch",
            "id": 10,
            "stream_id": 10,
            "time_ns": 3_000_000_000,
            "tid": 11,
            "channel": "switch_out_dwarf",
        },
    ]
    _, empty_stacks, empty_accounting = _parse_perf_script_text(
        empty_expected_script,
        {11},
        empty_samples,
    )
    assert len(empty_stacks) == 1
    assert empty_stacks[0]["dwarf_expected"] is True
    assert empty_stacks[0]["frames"] == []
    assert empty_accounting["switch_stack_accounting"][
        "dwarf_expected_empty_stack_records"
    ] == 1
    malformed_script = """
worker 11 1.000000: sched:sched_switch: prev_comm=worker prev_state=S ==> next_comm=idle next_pid=99
worker 11 1.100000: sched:sched_wakeup: worker [120] CPU:001
"""
    malformed_events, malformed_stacks, malformed_accounting = (
        _parse_perf_script_text(
            malformed_script,
            {11},
            [],
        )
    )
    assert malformed_events == []
    assert malformed_stacks == []
    assert malformed_accounting["recognized_scheduler_event_lines"] == 2
    assert malformed_accounting["decoded_scheduler_event_lines"] == 0
    assert malformed_accounting["rejected_scheduler_event_lines"] == 2
    assert malformed_accounting["rejection_reasons"][
        "missing_required_field"
    ] == 1
    assert malformed_accounting["rejection_reasons"][
        "malformed_compact_payload"
    ] == 1
    assert "payload" not in malformed_accounting
    assert "raw" not in malformed_accounting
    assert "symbol" not in malformed_accounting
    assert "module" not in malformed_accounting
    synthetic_markers = []
    for index, phase in enumerate(
        (
            "libc_block",
            "odbc_wait_read",
            "runnable_control",
        ),
        start=1,
    ):
        start_ns = index * 1_000_000_000
        synthetic_markers.extend(
            [
                {
                    "phase": phase,
                    "event": "start",
                    "monotonic_ns": start_ns,
                    "native_tid": 11,
                },
                {
                    "phase": phase,
                    "event": "end",
                    "monotonic_ns": start_ns + 10_000_000,
                    "native_tid": 11,
                    "iterations": (
                        CPU_ITERATIONS
                        if phase == "runnable_control"
                        else 1
                    ),
                },
            ]
        )
    synthetic_observed = _phase_metrics(
        synthetic_markers
    )
    assert tuple(synthetic_observed) == (
        "libc_block",
        "odbc_wait_read",
        "runnable_control",
    )
    assert all(
        synthetic_observed[phase]["complete"]
        for phase in (
            "libc_block",
            "odbc_wait_read",
            "runnable_control",
        )
    )
    for phase in (
        "libc_block",
        "odbc_wait_read",
        "runnable_control",
    ):
        metrics = synthetic_observed.get(
            phase,
            {},
        )
        assert int(metrics.get("start_ns") or 0) > 0
        assert int(metrics.get("end_ns") or 0) >= int(
            metrics.get("start_ns") or 0
        )
    sample_workflow = """
jobs:
  issue-894-environment-inventory:
    if: issue_894_inventory_only == 'true'
  issue-894-perf-preflight:
    # RP894_PREFLIGHT_EXECUTION_BEGIN
    run: |
      docker load
      raw_dir="$RUNNER_TEMP/raw"
      evidence="$GITHUB_WORKSPACE/issue-894-perf-preflight-evidence"
      perf_executable="$(command -v perf)"
      mkfifo "$raw_dir/perf-control.fifo" "$raw_dir/perf-ack.fifo"
      python tools/issue_894_perf_preflight.py command \
        --tids 11,22 \
        --perf-executable "$perf_executable" \
        --control-fifo "$raw_dir/perf-control.fifo" \
        --ack-fifo "$raw_dir/perf-ack.fifo" \
        --raw "$raw_dir/perf.data" \
        --output out.json \
        --argv-lines "$raw_dir/perf-argv.txt" \
        --parser-argv-lines "$raw_dir/perf-parser-argv.txt"
      mapfile -t parser_args < "$raw_dir/perf-parser-argv.txt"
      test "${parser_args[0]}" = "$perf_executable"
      sudo "${parser_args[@]}"
      mapfile -t perf_args < "$raw_dir/perf-argv.txt"
      test "${perf_args[0]}" = "$perf_executable"
      sudo "${perf_args[@]}" &
      perf_pid=$!
      python tools/issue_894_perf_preflight.py activate \
        --control-fifo "$raw_dir/perf-control.fifo" \
        --ack-fifo "$raw_dir/perf-ack.fifo" \
        --output "$evidence/perf-readiness.json" \
        --probe-gate "$evidence/observed-gate" \
        --perf-executable "$perf_executable" \
        --launcher-pid "$perf_pid"
      printf '{}\n' > "$raw_dir/perf-header.txt"
      printf '{}\n' > "$raw_dir/perf-channel-samples.jsonl"
      python tools/issue_894_perf_preflight.py summarize \
        --readiness "$evidence/perf-readiness.json" \
        --perf-header "$raw_dir/perf-header.txt" \
        --channel-samples "$raw_dir/perf-channel-samples.jsonl"
      trap cleanup EXIT
      rm -rf "$raw_dir"
    uses: actions/upload-artifact@v4
    with:
      path: issue-894-perf-preflight-evidence/
    # RP894_PREFLIGHT_EXECUTION_END
  issue-894-phase4:
    if: issue_894_perf_preflight_only != 'true'
outputs:
  rp894_perf_preflight_only: true
"""
    _validate_workflow_text(sample_workflow)

    rejected_non_sudo_gate = sample_workflow.replace(
        'sudo "${parser_args[@]}"',
        '"${parser_args[@]}"',
    )
    try:
        _validate_workflow_text(rejected_non_sudo_gate)
    except ValueError as exc:
        assert "execution context" in str(exc)
    else:
        raise AssertionError(
            "synthetic non-sudo parser gate was accepted"
        )

    rejected_parser_executable = sample_workflow.replace(
        'test "${parser_args[0]}" = "$perf_executable"',
        'test "${parser_args[0]}" = "/different/perf"',
    )
    try:
        _validate_workflow_text(rejected_parser_executable)
    except ValueError as exc:
        assert "execution context" in str(exc)
    else:
        raise AssertionError(
            "synthetic parser executable mismatch was accepted"
        )

    rejected = sample_workflow.replace(
        "docker load",
        "docker load\n      py-spy record",
    )
    try:
        _validate_workflow_text(rejected)
    except ValueError as exc:
        assert "py-spy" in str(exc)
    else:
        raise AssertionError(
            "synthetic prohibited executable operation was accepted"
        )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(
        dest="command",
        required=True,
    )
    sub.add_parser("self-test")

    workflow_p = sub.add_parser(
        "validate-workflow"
    )
    workflow_p.add_argument(
        "--workflow",
        type=Path,
        required=True,
    )

    probe_p = sub.add_parser(
        "probe"
    )
    for name in (
        "marker",
        "ready",
        "gate",
        "done",
        "stop",
    ):
        probe_p.add_argument(
            f"--{name}",
            type=Path,
            required=True,
        )
    probe_p.add_argument(
        "--sql-server",
        required=True,
    )
    probe_p.add_argument(
        "--sql-password",
        default=os.environ.get("SQL_PASSWORD", ""),
    )

    command_p = sub.add_parser(
        "command"
    )
    command_p.add_argument(
        "--tids",
        required=True,
    )
    command_p.add_argument(
        "--perf-executable",
        required=True,
    )
    command_p.add_argument(
        "--control-fifo",
        type=Path,
        required=True,
    )
    command_p.add_argument(
        "--ack-fifo",
        type=Path,
        required=True,
    )
    command_p.add_argument(
        "--raw",
        type=Path,
        required=True,
    )
    command_p.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    command_p.add_argument(
        "--argv-lines",
        type=Path,
    )
    command_p.add_argument(
        "--parser-argv-lines",
        type=Path,
    )

    activate_p = sub.add_parser("activate")
    activate_p.add_argument("--control-fifo", type=Path, required=True)
    activate_p.add_argument("--ack-fifo", type=Path, required=True)
    activate_p.add_argument("--output", type=Path, required=True)
    activate_p.add_argument("--probe-gate", type=Path, required=True)
    activate_p.add_argument("--perf-executable", required=True)
    activate_p.add_argument("--launcher-pid", type=int, required=True)

    identity_p = sub.add_parser(
        "identity"
    )
    identity_p.add_argument(
        "--host-pid",
        type=int,
        required=True,
    )
    identity_p.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    summary = sub.add_parser(
        "summarize"
    )
    for name in (
        "identity",
        "readiness",
        "perf-header",
        "channel-samples",
        "perf-script",
        "buildids",
        "control-markers",
        "observed-markers",
        "perf-time",
        "perf-stderr",
        "output",
    ):
        summary.add_argument(
            f"--{name}",
            type=Path,
            required=True,
        )
    summary.add_argument(
        "--perf-version",
        required=True,
    )
    summary.add_argument(
        "--perf-build-options",
        required=True,
    )
    return p


def main() -> int:
    args = parser().parse_args()
    if args.command == "self-test":
        _self_test()
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "self_test": "PASS",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-workflow":
        return validate_workflow(args)
    if args.command == "probe":
        return probe(args)
    if args.command == "command":
        return command(args)
    if args.command == "activate":
        return activate(args)
    if args.command == "identity":
        return identity(args)
    if args.command == "summarize":
        return summarize(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
