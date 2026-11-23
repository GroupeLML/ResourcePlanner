from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import re
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
    )
    workflow_required = (
        'perf_executable="$(command -v perf)"',
        '--perf-executable "$perf_executable"',
        '--parser-argv-lines "$raw_dir/perf-parser-argv.txt"',
        'sudo "${parser_args[@]}"',
        'sudo "${perf_args[@]}"',
    )
    if any(token not in region for token in workflow_required):
        raise ValueError("perf gate/recorder execution context is not aligned")
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
    return [
        perf_executable,
        "record",
        "-o",
        str(raw_path),
        "-a",
        "--clockid",
        "mono",
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
FRAME_RE = re.compile(
    r"^\s*([0-9a-fA-F]+)\s+"
    r"(.+?)\s+\((.+)\)\s*$"
)
BUILDID_RE = re.compile(
    r"^([0-9a-fA-F]{40,64})\s+(.+)$"
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


def _parse_perf_script(
    path: Path,
    tids: set[int],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    events: list[
        dict[str, Any]
    ] = []
    stacks: list[
        dict[str, Any]
    ] = []
    current_stack: (
        dict[str, Any]
        | None
    ) = None
    for raw in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        match = EVENT_RE.search(
            raw
        )
        if match:
            payload = dict(
                FIELD_RE.findall(
                    match.group(
                        "payload"
                    )
                )
            )
            event = match.group(
                "event"
            )
            row = {
                "time_seconds": float(
                    match.group(
                        "time"
                    )
                ),
                "event": event,
                "payload": payload,
            }
            events.append(row)
            current_stack = None
            if (
                event
                == "sched_switch"
            ):
                try:
                    prev_pid = int(
                        payload.get(
                            "prev_pid",
                            "0",
                        )
                    )
                except ValueError:
                    prev_pid = 0
                if prev_pid in tids:
                    current_stack = {
                        "time_seconds": (
                            row[
                                "time_seconds"
                            ]
                        ),
                        "tid": prev_pid,
                        "frames": [],
                    }
                    stacks.append(
                        current_stack
                    )
            continue
        if (
            current_stack
            is not None
        ):
            frame = FRAME_RE.match(
                raw
            )
            if frame:
                current_stack[
                    "frames"
                ].append(
                    {
                        "ip": (
                            "0x"
                            + frame.group(
                                1
                            ).lower()
                        ),
                        "symbol": (
                            frame.group(
                                2
                            ).strip()[
                                :240
                            ]
                        ),
                        "module": (
                            frame.group(
                                3
                            ).strip()[
                                :400
                            ]
                        ),
                    }
                )
    return events, stacks


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
            prev = int(
                payload.get(
                    "prev_pid",
                    "0",
                )
                or 0
            )
            nxt = int(
                payload.get(
                    "next_pid",
                    "0",
                )
                or 0
            )
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
            tid = int(
                payload.get(
                    "pid",
                    "0",
                )
                or 0
            )
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
    events, stacks = (
        _parse_perf_script(
            args.perf_script,
            tids,
        )
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
            <= int(float(row["time_seconds"]) * 1_000_000_000)
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
            "timeline": phase_timeline[:80],
        }
    controlled_timeline = (
        phase_timelines["libc_block"]
        + phase_timelines["odbc_wait_read"]
        + phase_timelines["runnable_control"]
    )
    unresolved = sum(
        int(value["unresolved_transition_count"])
        for value in phase_scheduler.values()
    )
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
        if odbc_start_ns
        <= int(float(stack["time_seconds"]) * 1_000_000_000)
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
        if not stack.get("frames")
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
        "control_phases": control,
        "observed_phases": observed,
        "scheduler": {
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
    sample_workflow = """
jobs:
  issue-894-environment-inventory:
    if: issue_894_inventory_only == 'true'
  issue-894-perf-preflight:
    # RP894_PREFLIGHT_EXECUTION_BEGIN
    run: |
      docker load
      python tools/issue_894_perf_preflight.py command --tids 11,22 --raw "$raw_dir/perf.data" --output out.json
      raw_dir="$RUNNER_TEMP/raw"
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
    if args.command == "identity":
        return identity(args)
    if args.command == "summarize":
        return summarize(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
