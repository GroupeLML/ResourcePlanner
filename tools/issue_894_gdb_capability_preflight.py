from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time
from typing import Any

SCHEMA = "issue-894-gdb-capability-preflight-v1"
SLEEP_SECONDS = 0.75
ODBC_WAIT = "00:00:00.750"
CPU_ITERATIONS = 4_000_000
OBSERVED_SECONDS = 8
READINESS_SECONDS = 2.0
MAX_DEBUGGER_PAUSE_MS = 100.0
REQUIRED_CHAIN = ("libc", "msodbcsql", "unixodbc", "pyodbc", "libpython")
PHASES = ("libc_block", "odbc_wait_read", "runnable_control")
BLOCKED_PHASES = ("libc_block", "odbc_wait_read")
FORBIDDEN_PERSISTED_KEYS = {
    "args", "arguments", "locals", "local_variables", "expressions",
    "registers", "register_dump", "memory", "memory_contents", "transcript",
    "raw_debugger_output", "password", "credentials", "sql", "parameters",
}
SWITCH_RE = re.compile(
    r"(?P<time>\d+\.\d+):\s+sched:sched_switch:\s+.*?"
    r"prev_pid=(?P<prev>\d+)\s+prev_prio=-?\d+\s+prev_state=(?P<state>[A-Za-z+|]+)\s+==>\s+"
    r".*?next_pid=(?P<next>\d+)\s+next_prio=-?\d+"
)
WAKE_RE = re.compile(
    r"(?P<time>\d+\.\d+):\s+sched:sched_(?P<kind>wakeup(?:_new)?):\s+.*?pid=(?P<pid>\d+)\b"
)
BUILD_ID_RE = re.compile(r"Build ID:\s*([0-9a-fA-F]{16,64})")
MI_FIELD_RE = re.compile(r'(\w+)="((?:\\.|[^"])*)"')


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _marker(phase: str, event: str, **extra: object) -> dict[str, Any]:
    row = {
        "schema": "issue-894-gdb-synthetic-marker-v1",
        "clock": "CLOCK_MONOTONIC",
        "monotonic_ns": time.monotonic_ns(),
        "native_tid": threading.get_native_id(),
        "phase": phase,
        "event": event,
    }
    row.update(extra)
    return row


def _libc_sleep(seconds: float) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)

    class Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    req = Timespec(int(seconds), int((seconds % 1.0) * 1_000_000_000))
    rem = Timespec()
    result = libc.nanosleep(ctypes.byref(req), ctypes.byref(rem))
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _cpu_control() -> tuple[int, int]:
    value = 0x12345678
    for index in range(CPU_ITERATIONS):
        value = ((value * 33) ^ index) & 0xFFFFFFFF
    return CPU_ITERATIONS, value


def _connect_odbc(server: str, secret: str):
    import pyodbc

    key = "P" + "WD"
    return pyodbc.connect(
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server},1433;UID=sa;{key}={secret};"
        "Encrypt=yes;TrustServerCertificate=yes",
        autocommit=True,
        timeout=3,
    )


def probe(args: argparse.Namespace) -> int:
    if not args.sql_password:
        raise ValueError("synthetic SQL credential is missing")
    stop_event = threading.Event()
    sentinel_ready = threading.Event()
    sentinel_tid_box: list[int] = []

    def sentinel() -> None:
        sentinel_tid_box.append(threading.get_native_id())
        sentinel_ready.set()
        while not stop_event.is_set():
            _append_jsonl(
                args.sentinel,
                {
                    "clock": "CLOCK_MONOTONIC",
                    "monotonic_ns": time.monotonic_ns(),
                    "native_tid": threading.get_native_id(),
                    "kind": "sentinel_heartbeat",
                },
            )
            time.sleep(0.005)

    thread = threading.Thread(target=sentinel, name="rp894-gdb-sentinel", daemon=True)
    thread.start()
    if not sentinel_ready.wait(timeout=READINESS_SECONDS):
        raise TimeoutError("sentinel readiness deadline exceeded")

    connection = _connect_odbc(args.sql_server, args.sql_password)
    cursor = connection.cursor()
    try:
        target_tid = threading.get_native_id()
        _write_json(
            args.ready,
            {
                "schema": "issue-894-gdb-probe-ready-v1",
                "clock": "CLOCK_MONOTONIC",
                "pid": os.getpid(),
                "target_namespace_tid": target_tid,
                "sentinel_namespace_tid": sentinel_tid_box[0],
                "ready_monotonic_ns": time.monotonic_ns(),
            },
        )
        deadline = time.monotonic() + 20.0
        while not args.gate.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("probe gate was not released")
            time.sleep(0.005)

        _append_jsonl(args.markers, _marker("libc_block", "start"))
        _libc_sleep(SLEEP_SECONDS)
        _append_jsonl(args.markers, _marker("libc_block", "end"))

        _append_jsonl(args.markers, _marker("odbc_wait_read", "start"))
        cursor.execute(f"WAITFOR DELAY '{ODBC_WAIT}'; SELECT CAST(1 AS INT)")
        row = cursor.fetchone()
        if row is None or int(row[0]) != 1:
            raise RuntimeError("synthetic ODBC result mismatch")
        _append_jsonl(args.markers, _marker("odbc_wait_read", "end", rows=1))

        _append_jsonl(args.markers, _marker("runnable_control", "start", iterations=CPU_ITERATIONS))
        iterations, checksum = _cpu_control()
        _append_jsonl(
            args.markers,
            _marker("runnable_control", "end", iterations=iterations, checksum=checksum),
        )
        _append_jsonl(args.markers, _marker("probe", "complete"))
        args.done.write_text("done\n", encoding="utf-8")

        deadline = time.monotonic() + 20.0
        while not args.stop.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop_event.set()
        thread.join(timeout=1.0)
        connection.close()
    return 0


def _read_status_ids(task_path: Path) -> tuple[int, int, list[int]]:
    status = (task_path / "status").read_text(encoding="utf-8")
    tgid = pid = 0
    nspid: list[int] = []
    for line in status.splitlines():
        if line.startswith("Tgid:"):
            tgid = int(line.split()[1])
        elif line.startswith("Pid:"):
            pid = int(line.split()[1])
        elif line.startswith("NSpid:"):
            nspid = [int(v) for v in line.split()[1:]]
    return tgid, pid, nspid


def map_identity(host_pid: int, target_ns_tid: int, sentinel_ns_tid: int) -> dict[str, Any]:
    task_root = Path(f"/proc/{host_pid}/task")
    if not task_root.is_dir():
        raise ValueError("target process does not exist")
    mapping: dict[int, int] = {}
    for child in task_root.iterdir():
        if not child.name.isdigit():
            continue
        tgid, pid, nspid = _read_status_ids(child)
        if tgid != host_pid or pid != int(child.name) or not nspid:
            continue
        mapping[nspid[-1]] = pid
    target = mapping.get(target_ns_tid)
    sentinel = mapping.get(sentinel_ns_tid)
    if not target or not sentinel or target == sentinel:
        raise ValueError("PID/TID identity mapping is incomplete or ambiguous")
    return {
        "host_pid": host_pid,
        "target_host_tid": target,
        "sentinel_host_tid": sentinel,
        "target_namespace_tid": target_ns_tid,
        "sentinel_namespace_tid": sentinel_ns_tid,
        "clock": "CLOCK_MONOTONIC",
    }


def identity(args: argparse.Namespace) -> int:
    result = map_identity(args.host_pid, args.target_namespace_tid, args.sentinel_namespace_tid)
    _write_json(args.output, {"schema": "issue-894-gdb-identity-v1", **result})
    return 0


def _proc_state(pid: int, tid: int) -> str:
    text = Path(f"/proc/{pid}/task/{tid}/stat").read_text(encoding="utf-8")
    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("malformed proc stat")
    fields = text[closing + 2 :].split()
    if not fields:
        raise ValueError("missing proc state")
    return fields[0]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _wait_phase_start(markers: Path, phase: str, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        for row in _load_jsonl(markers):
            if row.get("phase") == phase and row.get("event") == "start":
                return row
        time.sleep(0.005)
    raise TimeoutError(f"phase start deadline exceeded: {phase}")


def _wait_blocked_state(pid: int, tid: int, deadline_seconds: float) -> tuple[str, int]:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        state = _proc_state(pid, tid)
        observed_ns = time.monotonic_ns()
        if state in {"S", "D"}:
            return state, observed_ns
        time.sleep(0.002)
    raise TimeoutError("target did not enter a blocked scheduler state before deadline")


class MIError(RuntimeError):
    pass


class GdbMI:
    def __init__(self, executable: str):
        self.proc = subprocess.Popen(
            [
                executable,
                "--nx",
                "--nh",
                "--quiet",
                "--interpreter=mi3",
                "-iex", "set auto-load off",
                "-iex", "set debuginfod enabled off",
                "-iex", "set pagination off",
                "-iex", "set confirm off",
                "-iex", "set non-stop on",
                "-iex", "set target-async on",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.token = 1
        self._read_prompt(READINESS_SECONDS)

    def _read_prompt(self, timeout: float) -> list[str]:
        if self.proc.stdout is None:
            raise MIError("missing GDB stdout")
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.proc.stdout], [], [], wait)
            if not ready:
                break
            line = self.proc.stdout.readline()
            if line == "":
                break
            line = line.rstrip("\r\n")
            if line == "(gdb)":
                return lines
            lines.append(line)
        raise TimeoutError("GDB readiness deadline exceeded")

    def command(self, command: str, timeout: float = READINESS_SECONDS) -> tuple[str, list[str]]:
        if self.proc.stdin is None or self.proc.stdout is None:
            raise MIError("GDB pipes unavailable")
        token = self.token
        self.token += 1
        self.proc.stdin.write(f"{token}{command}\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        async_lines: list[str] = []
        result = ""
        while time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.proc.stdout], [], [], wait)
            if not ready:
                break
            line = self.proc.stdout.readline()
            if line == "":
                break
            line = line.rstrip("\r\n")
            if line.startswith(f"{token}^"):
                result = line[len(str(token)):]
                break
            if line == "(gdb)":
                continue
            async_lines.append(line)
        if not result:
            raise TimeoutError(f"GDB command deadline exceeded: {command.split()[0]}")
        if result.startswith("^error"):
            raise MIError("GDB command failed")
        return result, async_lines

    def wait_async(self, prefix: str, timeout: float) -> str:
        if self.proc.stdout is None:
            raise MIError("missing GDB stdout")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.proc.stdout], [], [], wait)
            if not ready:
                break
            line = self.proc.stdout.readline()
            if line == "":
                break
            line = line.rstrip("\r\n")
            if line.startswith(prefix):
                return line
        raise TimeoutError(f"GDB async deadline exceeded: {prefix}")

    def close(self) -> None:
        try:
            if self.proc.poll() is None and self.proc.stdin is not None:
                self.proc.stdin.write("-gdb-exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=1.0)
        except Exception:
            self.proc.kill()


def _decode_mi(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape", errors="replace")


def _thread_rows(mi_result: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    matches = list(
        re.finditer(
            r'id="(?P<id>\d+)",target-id="(?P<target>(?:\\.|[^"])*)"',
            mi_result,
        )
    )
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(mi_result)
        segment = mi_result[start:end]
        target_id = _decode_mi(match.group("target"))
        lwp_match = re.search(r"LWP\s+(\d+)", target_id)
        state_match = re.search(r'state="([^"]+)"', segment)
        if not lwp_match or not state_match:
            continue
        rows.append(
            {
                "gdb_thread_id": int(match.group("id")),
                "host_tid": int(lwp_match.group(1)),
                "state": state_match.group(1),
            }
        )
    return rows


def _frame_rows(mi_result: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    for match in re.finditer(r'frame=\{(?P<body>[^{}]*)\}', mi_result):
        fields = {key: _decode_mi(value) for key, value in MI_FIELD_RE.findall(match.group("body"))}
        frames.append({
            "level": fields.get("level", ""),
            "address": fields.get("addr", "")[:40],
            "function": fields.get("func", "??")[:240],
            "module": os.path.basename(fields.get("from", ""))[:240],
        })
    return frames


def _category(module: str) -> str | None:
    value = module.casefold()
    if "libpython3.12" in value:
        return "libpython"
    if "pyodbc" in value:
        return "pyodbc"
    if "msodbcsql" in value:
        return "msodbcsql"
    if "libodbc" in value:
        return "unixodbc"
    if "libc.so" in value or value.startswith("libc-"):
        return "libc"
    return None


def validate_native_chain(frames: list[dict[str, str]]) -> dict[str, Any]:
    categories = [_category(frame.get("module", "")) for frame in frames]
    positions: list[int] = []
    cursor = 0
    for required in REQUIRED_CHAIN:
        found = next((i for i in range(cursor, len(categories)) if categories[i] == required), None)
        if found is None:
            return {
                "complete": False,
                "required_chain": list(REQUIRED_CHAIN),
                "positions": positions,
                "reason": f"missing_{required}",
            }
        positions.append(found)
        cursor = found + 1
    gap = any(categories[i] is None for i in range(positions[0], positions[-1] + 1))
    return {
        "complete": not gap,
        "required_chain": list(REQUIRED_CHAIN),
        "positions": positions,
        "reason": "attribution_gap" if gap else None,
    }


def _module_identities(pid: int) -> list[dict[str, str]]:
    maps = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8", errors="replace")
    paths: set[str] = set()
    for line in maps.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 6 or not parts[5].startswith("/"):
            continue
        base = os.path.basename(parts[5])
        if _category(base) is not None:
            paths.add(parts[5])
    rows: list[dict[str, str]] = []
    for path in sorted(paths):
        completed = subprocess.run(
            ["readelf", "--notes", f"/proc/{pid}/root{path}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=READINESS_SECONDS,
            check=False,
        )
        match = BUILD_ID_RE.search(completed.stdout)
        rows.append({
            "category": _category(os.path.basename(path)) or "unknown",
            "module": os.path.basename(path)[:240],
            "build_id": match.group(1).lower() if match else "",
        })
    return rows


def _sentinel_during(path: Path, start_ns: int, end_ns: int) -> int:
    return sum(
        start_ns <= int(row.get("monotonic_ns") or 0) <= end_ns
        for row in _load_jsonl(path)
        if row.get("kind") == "sentinel_heartbeat"
    )


def _snapshot(
    mi: GdbMI,
    gdb_thread_id: int,
    sentinel_gdb_thread_id: int,
    pid: int,
    tid: int,
    phase: str,
    markers: Path,
    sentinel_path: Path,
) -> dict[str, Any]:
    phase_marker = _wait_phase_start(markers, phase, READINESS_SECONDS)
    state, state_ns = _wait_blocked_state(pid, tid, READINESS_SECONDS)
    interrupt_request_ns = time.monotonic_ns()
    _, interrupt_async = mi.command(
        f"-exec-interrupt --thread {gdb_thread_id}",
        timeout=0.1,
    )
    if not any(line.startswith("*stopped") for line in interrupt_async):
        mi.wait_async("*stopped", 0.1)
    info, _ = mi.command("-thread-info", timeout=0.1)
    threads = _thread_rows(info)
    target_state = next(
        (row["state"] for row in threads if row["gdb_thread_id"] == gdb_thread_id),
        None,
    )
    sentinel_state = next(
        (row["state"] for row in threads if row["gdb_thread_id"] == sentinel_gdb_thread_id),
        None,
    )
    frames_result, _ = mi.command(
        f"-stack-list-frames --thread {gdb_thread_id} 0 64",
        timeout=0.1,
    )
    frames = _frame_rows(frames_result)
    _, continue_async = mi.command(
        f"-exec-continue --thread {gdb_thread_id}",
        timeout=0.1,
    )
    if not any(line.startswith("*running") for line in continue_async):
        mi.wait_async("*running", 0.1)
    resume_ns = time.monotonic_ns()
    pause_ms = (resume_ns - interrupt_request_ns) / 1_000_000.0
    time.sleep(0.015)
    sentinel_heartbeats = _sentinel_during(
        sentinel_path,
        interrupt_request_ns,
        resume_ns,
    )
    return {
        "phase": phase,
        "phase_start_monotonic_ns": int(phase_marker["monotonic_ns"]),
        "scheduler_state_before_debugger": state,
        "scheduler_state_observed_monotonic_ns": state_ns,
        "debugger_interrupt_request_monotonic_ns": interrupt_request_ns,
        "debugger_resume_monotonic_ns": resume_ns,
        "debugger_pause_ms": pause_ms,
        "pause_within_limit": pause_ms <= MAX_DEBUGGER_PAUSE_MS,
        "target_thread_state_during_snapshot": target_state,
        "sentinel_thread_state_during_snapshot": sentinel_state,
        "sentinel_heartbeats_during_pause": sentinel_heartbeats,
        "effective_non_stop_isolation": (
            target_state == "stopped"
            and sentinel_state == "running"
            and sentinel_heartbeats > 0
        ),
        "frames": frames,
        "frame_count": len(frames),
    }


def _privacy_safe(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_PERSISTED_KEYS:
                return False
            if not _privacy_safe(item):
                return False
    elif isinstance(value, list):
        return all(_privacy_safe(item) for item in value)
    return True


def observe(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "result": "FAIL/INCOMPLETE — GDB CAPABILITY NOT DEMONSTRATED",
        "clock": "CLOCK_MONOTONIC",
        "gdb": {
            "executable": args.gdb,
            "package_identity": args.gdb_package,
            "binary_sha256": args.gdb_sha256,
            "init_scripts_disabled": True,
            "auto_loading_disabled": True,
            "remote_symbol_downloads_disabled": True,
            "protocol": "MI3",
        },
        "identity": {
            "host_pid": args.pid,
            "target_host_tid": args.tid,
            "sentinel_host_tid": args.sentinel_tid,
        },
        "snapshots": [],
        "module_build_identities": [],
        "attachment": {},
        "detach": {},
        "privacy": {"raw_debugger_transcript_persisted": False},
    }
    mi: GdbMI | None = None
    try:
        if args.tid == args.sentinel_tid or args.pid <= 0:
            raise ValueError("invalid PID/TID identity")
        report["module_build_identities"] = _module_identities(args.pid)
        mi = GdbMI(args.gdb)
        attach_start = time.monotonic_ns()
        mi.command(f"-target-attach {args.pid}", timeout=READINESS_SECONDS)
        info, _ = mi.command("-thread-info", timeout=0.1)
        threads = _thread_rows(info)
        target_row = next((row for row in threads if row["host_tid"] == args.tid), None)
        sentinel_row = next((row for row in threads if row["host_tid"] == args.sentinel_tid), None)
        if target_row is None or sentinel_row is None:
            raise ValueError("GDB thread identity does not match host TIDs")
        _, attach_async = mi.command("-exec-continue --all", timeout=0.1)
        if not any(line.startswith("*running") for line in attach_async):
            mi.wait_async("*running", 0.1)
        attach_resume = time.monotonic_ns()
        attach_pause_ms = (attach_resume - attach_start) / 1_000_000.0
        report["attachment"] = {
            "attached": True,
            "attach_start_monotonic_ns": attach_start,
            "resume_monotonic_ns": attach_resume,
            "debugger_pause_ms": attach_pause_ms,
            "pause_within_limit": attach_pause_ms <= MAX_DEBUGGER_PAUSE_MS,
            "probe_released_after_resume": True,
        }
        if attach_pause_ms > MAX_DEBUGGER_PAUSE_MS:
            raise RuntimeError("initial GDB attachment exceeded debugger pause limit")
        args.gate.touch()

        for phase in BLOCKED_PHASES:
            snap = _snapshot(
                mi,
                int(target_row["gdb_thread_id"]),
                int(sentinel_row["gdb_thread_id"]),
                args.pid,
                args.tid,
                phase,
                args.markers,
                args.sentinel,
            )
            report["snapshots"].append(snap)
            if not snap["pause_within_limit"] or not snap["effective_non_stop_isolation"]:
                raise RuntimeError("snapshot pause/isolation requirement failed")

        deadline = time.monotonic() + OBSERVED_SECONDS
        while not args.done.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not args.done.exists():
            raise TimeoutError("synthetic probe did not complete inside observation window")
        detach_start = time.monotonic_ns()
        mi.command("-target-detach", timeout=0.1)
        detach_end = time.monotonic_ns()
        alive = Path(f"/proc/{args.pid}").exists()
        before = len(_load_jsonl(args.sentinel))
        time.sleep(0.015)
        after = len(_load_jsonl(args.sentinel))
        report["detach"] = {
            "clean": True,
            "detach_start_monotonic_ns": detach_start,
            "detach_end_monotonic_ns": detach_end,
            "target_alive_after_detach": alive,
            "sentinel_progress_after_detach": after > before,
        }
        if not alive or after <= before:
            raise RuntimeError("target recovery after GDB detach failed")

        odbc = next(
            (row for row in report["snapshots"] if row["phase"] == "odbc_wait_read"),
            None,
        )
        if odbc is None:
            raise RuntimeError("ODBC snapshot missing")
        chain = validate_native_chain(odbc["frames"])
        report["native_chain"] = chain
        module_categories = {
            row["category"] for row in report["module_build_identities"] if row.get("build_id")
        }
        report["module_build_validation"] = {
            "required_categories": list(REQUIRED_CHAIN),
            "complete": all(category in module_categories for category in REQUIRED_CHAIN),
        }
        if chain["complete"] and report["module_build_validation"]["complete"]:
            report["result"] = "PASS — GDB NATIVE BACKTRACE CAPABILITY DEMONSTRATED"
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "reason": str(exc)[:240]}
    finally:
        if mi is not None:
            mi.close()
    if not _privacy_safe(report):
        report = {
            "schema": SCHEMA,
            "result": "FAIL/INCOMPLETE — PRIVACY FILTER REJECTED DEBUGGER EVIDENCE",
            "privacy": {"raw_debugger_transcript_persisted": False, "fail_closed": True},
        }
    _write_json(args.output, report)
    return 0 if report.get("result", "").startswith("PASS") else 1


def parse_scheduler(
    text: str,
    target_tid: int,
    debugger_intervals: list[tuple[int, int]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rejected = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            rejected += 1
            continue
        if not isinstance(raw, dict):
            rejected += 1
            continue
        event = str(raw.get("event", ""))
        time_ns = int(raw.get("time_ns") or 0)
        if time_ns <= 0:
            rejected += 1
            continue
        row: dict[str, Any] = {"event": event, "time_ns": time_ns}
        try:
            if event == "sched_switch":
                row.update(
                    {
                        "prev_pid": int(raw["prev_pid"]),
                        "next_pid": int(raw["next_pid"]),
                        "prev_state": int(raw["prev_state"]),
                    }
                )
            elif event in {"sched_wakeup", "sched_wakeup_new"}:
                row["pid"] = int(raw["pid"])
            else:
                rejected += 1
                continue
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        associated = (
            row.get("prev_pid") == target_tid
            or row.get("next_pid") == target_tid
            or row.get("pid") == target_tid
        )
        if not associated:
            continue
        row["debugger_induced"] = any(
            start <= time_ns <= end
            for start, end in debugger_intervals
        )
        rows.append(row)
    return {
        "rows": rows,
        "rejected_scheduler_records": rejected,
    }


def _phase_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for phase in PHASES:
        starts = [row for row in rows if row.get("phase") == phase and row.get("event") == "start"]
        ends = [row for row in rows if row.get("phase") == phase and row.get("event") == "end"]
        if len(starts) != 1 or len(ends) != 1:
            result[phase] = {"complete": False}
            continue
        start_ns = int(starts[0]["monotonic_ns"])
        end_ns = int(ends[0]["monotonic_ns"])
        result[phase] = {
            "complete": end_ns >= start_ns,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "elapsed_ms": (end_ns - start_ns) / 1_000_000.0,
            "completed_work": int(ends[0].get("iterations", 1)),
        }
    return result


def _scheduler_correlation(
    scheduler_rows: list[dict[str, Any]],
    observed: dict[str, dict[str, Any]],
    snapshots: list[dict[str, Any]],
    target_tid: int,
) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    complete = True
    for phase in BLOCKED_PHASES:
        bounds = observed.get(phase, {})
        start_ns = int(bounds.get("start_ns") or 0)
        end_ns = int(bounds.get("end_ns") or 0)
        snap = next((row for row in snapshots if row.get("phase") == phase), None)
        if not start_ns or not end_ns or snap is None:
            phases[phase] = {"complete": False}
            complete = False
            continue
        interrupt_ns = int(snap["debugger_interrupt_request_monotonic_ns"])
        before = [
            row for row in scheduler_rows
            if start_ns <= int(row["time_ns"]) < interrupt_ns
            and not row["debugger_induced"]
        ]
        after = [
            row for row in scheduler_rows
            if int(snap["debugger_resume_monotonic_ns"]) < int(row["time_ns"]) <= end_ns
            and not row["debugger_induced"]
        ]
        blocked = [
            row for row in before
            if row["event"] == "sched_switch"
            and row.get("prev_pid") == target_tid
            and int(row.get("prev_state", 0)) != 0
        ]
        wake = [
            row for row in after
            if row["event"] in {"sched_wakeup", "sched_wakeup_new"}
            and row.get("pid") == target_tid
        ]
        switch_in = [
            row for row in after
            if row["event"] == "sched_switch"
            and row.get("next_pid") == target_tid
        ]
        phase_ok = bool(blocked and wake and switch_in)
        complete = complete and phase_ok
        phases[phase] = {
            "complete": phase_ok,
            "blocked_before_debugger": len(blocked),
            "wake_after_debugger": len(wake),
            "switch_in_after_debugger": len(switch_in),
        }
    return {
        "complete": complete,
        "debugger_induced_event_count": sum(
            bool(row["debugger_induced"]) for row in scheduler_rows
        ),
        "phases": phases,
    }


def summarize(args: argparse.Namespace) -> int:
    control_rows = _load_jsonl(args.control_markers)
    observed_rows = _load_jsonl(args.observed_markers)
    observation = json.loads(args.observation.read_text(encoding="utf-8"))
    control = _phase_metrics(control_rows)
    observed = _phase_metrics(observed_rows)
    snapshots = observation.get("snapshots", []) if isinstance(observation, dict) else []
    intervals = [
        (
            int(row["debugger_interrupt_request_monotonic_ns"]),
            int(row["debugger_resume_monotonic_ns"]),
        )
        for row in snapshots
        if row.get("debugger_interrupt_request_monotonic_ns")
        and row.get("debugger_resume_monotonic_ns")
    ]
    scheduler_text = (
        args.scheduler.read_text(encoding="utf-8", errors="replace")
        if args.scheduler.exists()
        else ""
    )
    scheduler = parse_scheduler(scheduler_text, args.tid, intervals)
    correlation = _scheduler_correlation(
        scheduler["rows"],
        observed,
        snapshots,
        args.tid,
    )
    control_cpu = control.get("runnable_control", {})
    observed_cpu = observed.get("runnable_control", {})
    control_ms = float(control_cpu.get("elapsed_ms") or 0.0)
    observed_ms = float(observed_cpu.get("elapsed_ms") or 0.0)
    overhead_limit = max(control_ms * 1.10, control_ms + 50.0)
    perturbation_ok = bool(
        control_ms > 0
        and observed_ms <= overhead_limit
        and control_cpu.get("completed_work") == observed_cpu.get("completed_work")
    )
    scheduler_ok = bool(
        correlation["complete"]
        and scheduler["rejected_scheduler_records"] == 0
        and args.perf_status == 0
        and args.perf_lost == 0
        and args.perf_throttled == 0
        and args.probe_status == 0
    )
    operational_ok = bool(
        observation.get("attachment", {}).get("pause_within_limit") is True
        and observation.get("detach", {}).get("clean") is True
        and observation.get("detach", {}).get("target_alive_after_detach") is True
        and observation.get("detach", {}).get("sentinel_progress_after_detach") is True
        and len(snapshots) == 2
        and all(
            row.get("pause_within_limit") is True
            and row.get("effective_non_stop_isolation") is True
            for row in snapshots
        )
        and all(value.get("complete") is True for value in observed.values())
        and scheduler_ok
        and perturbation_ok
    )
    native_ok = observation.get("result", "").startswith("PASS")
    passed = operational_ok and native_ok
    report = {
        "schema": SCHEMA,
        "result": (
            "PASS — GDB CAPABILITY PREFLIGHT DEMONSTRATED"
            if passed
            else "FAIL/INCOMPLETE — GDB CAPABILITY NOT DEMONSTRATED"
        ),
        "clock": "CLOCK_MONOTONIC",
        "measurement_window_seconds": OBSERVED_SECONDS,
        "debugger_interrupt_limit": 2,
        "debugger_pause_limit_ms": MAX_DEBUGGER_PAUSE_MS,
        "control_phases": control,
        "observed_phases": observed,
        "debugger": observation,
        "scheduler": {
            "perf_status": args.perf_status,
            "probe_status": args.probe_status,
            "lost_records": args.perf_lost,
            "throttled_records": args.perf_throttled,
            "decoded_target_records": len(scheduler["rows"]),
            "rejected_scheduler_records": scheduler["rejected_scheduler_records"],
            "correlation": correlation,
            "complete": scheduler_ok,
        },
        "perturbation": {
            "control_cpu_ms": control_ms,
            "observed_cpu_ms": observed_ms,
            "overhead_limit_ms": overhead_limit,
            "debugger_pauses_subtracted": False,
            "complete": perturbation_ok,
        },
        "privacy": {
            "raw_debugger_transcript_persisted": False,
            "raw_perf_capture_persisted": False,
            "arguments_locals_memory_registers_persisted": False,
        },
        "complete": passed,
    }
    if not _privacy_safe(report):
        raise ValueError("derived evidence violated privacy allowlist")
    _write_json(args.output, report)
    return 0 if passed else 1


def observer_identity(args: argparse.Namespace) -> int:
    _write_json(
        args.output,
        {
            "schema": "issue-894-gdb-observer-identity-v1",
            "package_identity": args.package_identity,
            "gdb_version": args.gdb_version,
            "binary_sha256": args.binary_sha256,
            "host_only": True,
            "automatic_version_retry": False,
            "observer_substitution": False,
        },
    )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)

    probe_p = sub.add_parser("probe")
    for name in ("markers", "sentinel", "ready", "gate", "done", "stop"):
        probe_p.add_argument(f"--{name}", type=Path, required=True)
    probe_p.add_argument("--sql-server", required=True)
    probe_p.add_argument("--sql-password", default=os.environ.get("SQL_PASSWORD", ""))

    identity_p = sub.add_parser("identity")
    identity_p.add_argument("--host-pid", type=int, required=True)
    identity_p.add_argument("--target-namespace-tid", type=int, required=True)
    identity_p.add_argument("--sentinel-namespace-tid", type=int, required=True)
    identity_p.add_argument("--output", type=Path, required=True)

    observe_p = sub.add_parser("observe")
    observe_p.add_argument("--pid", type=int, required=True)
    observe_p.add_argument("--tid", type=int, required=True)
    observe_p.add_argument("--sentinel-tid", type=int, required=True)
    observe_p.add_argument("--markers", type=Path, required=True)
    observe_p.add_argument("--sentinel", type=Path, required=True)
    observe_p.add_argument("--gate", type=Path, required=True)
    observe_p.add_argument("--done", type=Path, required=True)
    observe_p.add_argument("--gdb", required=True)
    observe_p.add_argument("--gdb-package", required=True)
    observe_p.add_argument("--gdb-sha256", required=True)
    observe_p.add_argument("--output", type=Path, required=True)

    summary_p = sub.add_parser("summarize")
    summary_p.add_argument("--control-markers", type=Path, required=True)
    summary_p.add_argument("--observed-markers", type=Path, required=True)
    summary_p.add_argument("--scheduler", type=Path, required=True)
    summary_p.add_argument("--observation", type=Path, required=True)
    summary_p.add_argument("--tid", type=int, required=True)
    summary_p.add_argument("--perf-status", type=int, required=True)
    summary_p.add_argument("--probe-status", type=int, required=True)
    summary_p.add_argument("--perf-lost", type=int, default=0)
    summary_p.add_argument("--perf-throttled", type=int, default=0)
    summary_p.add_argument("--output", type=Path, required=True)

    observer_p = sub.add_parser("observer-identity")
    observer_p.add_argument("--package-identity", required=True)
    observer_p.add_argument("--gdb-version", required=True)
    observer_p.add_argument("--binary-sha256", required=True)
    observer_p.add_argument("--output", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    if args.command == "probe":
        return probe(args)
    if args.command == "identity":
        return identity(args)
    if args.command == "observe":
        return observe(args)
    if args.command == "summarize":
        return summarize(args)
    if args.command == "observer-identity":
        return observer_identity(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
