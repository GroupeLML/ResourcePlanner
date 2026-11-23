from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from tools.issue_894_connection_trial import (
    DIAGNOSTIC_LABEL,
    FAILURE_RATE_MATERIAL_DELTA,
)

PYSPY_VERSION = "0.4.4"
MAX_START_UNCERTAINTY_MS = 250.0
_THREAD_RE = re.compile(r"Thread\s+(\d+)\b")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_speedscope(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("py-spy speedscope payload is not an object")
    profiles = payload.get("profiles")
    shared = payload.get("shared")
    if not isinstance(profiles, list) or not isinstance(shared, dict):
        raise ValueError("py-spy speedscope payload is malformed")
    frames = shared.get("frames")
    if not isinstance(frames, list):
        raise ValueError("py-spy speedscope frames are missing")
    return payload


def _native_tid(profile: dict[str, object]) -> int:
    match = _THREAD_RE.search(str(profile.get("name") or ""))
    return int(match.group(1)) if match else 0


def _frame_text(frame: dict[str, object]) -> str:
    return " ".join(
        (
            str(frame.get("name") or ""),
            str(frame.get("file") or ""),
        )
    ).casefold()


def _is_native_frame(frame: dict[str, object]) -> bool:
    file_name = str(frame.get("file") or "").casefold()
    if file_name.endswith((".py", ".pyc", ".pyo")):
        return False
    text = _frame_text(frame)
    return bool(
        ".so" in file_name
        or "/lib" in file_name
        or "libpython" in text
        or "libodbc" in text
        or "msodbcsql" in text
        or "pyodbc" in text
        or "pthread_" in text
        or "futex" in text
        or "epoll" in text
        or " poll" in f" {text}"
        or "recv" in text
        or "select" in text
        or file_name.startswith("[")
    )


def _is_wait_frame(frame: dict[str, object]) -> bool:
    text = _frame_text(frame)
    return any(
        token in text
        for token in (
            "futex",
            "pthread_cond",
            "pthread_mutex",
            "sem_wait",
            "epoll_wait",
            "poll",
            "select",
            "recv",
            "nanosleep",
        )
    )


def _stack_category(frames: list[dict[str, object]]) -> str:
    texts = [_frame_text(frame) for frame in frames]
    joined = "\n".join(texts)
    has_driver = any(
        token in joined
        for token in (
            "pyodbc",
            "libodbc",
            "msodbcsql",
            "sqlfetch",
            "sqlgetdata",
            "sqlmore",
            "odbc",
        )
    )
    has_sync = any(
        token in joined
        for token in (
            "futex",
            "pthread_cond",
            "pthread_mutex",
            "sem_wait",
        )
    )
    has_socket = any(
        token in joined
        for token in (
            "recv",
            "recvfrom",
            "recvmmsg",
            "poll",
            "ppoll",
            "epoll_wait",
            "select",
        )
    )
    has_gil = any(
        token in joined
        for token in (
            "take_gil",
            "_pyeval_acquirelock",
            "pythread_acquire_lock",
            "_pythread_acquire_lock_timed",
        )
    )
    has_conversion = any(
        token in joined
        for token in (
            "convert",
            "decode",
            "unicode",
            "sqlgetdata",
            "textenc",
        )
    )

    if has_gil and has_sync:
        return "cpython_gil_wait"
    if has_driver and has_socket:
        return "socket_or_result_delivery_path"
    if has_driver and has_sync:
        return "pyodbc_odbc_synchronization"
    if has_driver and has_conversion:
        return "native_driver_conversion_or_processing"
    if has_driver:
        return "native_driver_path"
    if has_sync:
        return "other_native_synchronization"
    return "unresolved"


def _profile_duration_seconds(profile: dict[str, object]) -> float:
    value = profile.get("endValue")
    return max(0.0, float(value or 0.0))


def _timing_window(
    profile_payload: dict[str, object],
    *,
    command_start_ns: int,
    command_end_ns: int,
) -> tuple[int, int, float]:
    profiles = profile_payload.get("profiles", [])
    duration = max(
        (
            _profile_duration_seconds(profile)
            for profile in profiles
            if isinstance(profile, dict)
        ),
        default=0.0,
    )
    duration_ns = int(duration * 1_000_000_000)
    latest_start = command_end_ns - duration_ns
    lower = min(command_start_ns, latest_start)
    upper = max(command_start_ns, latest_start)
    return lower, upper, (upper - lower) / 1_000_000.0


def _preflight(args: argparse.Namespace) -> dict[str, object]:
    payload = _load_speedscope(args.profile)
    profiles = [
        profile
        for profile in payload.get("profiles", [])
        if isinstance(profile, dict)
    ]
    frames = [
        frame
        for frame in (payload.get("shared") or {}).get("frames", [])
        if isinstance(frame, dict)
    ]
    tids = sorted({_native_tid(profile) for profile in profiles} - {0})
    referenced: set[int] = set()
    waiting_samples = 0
    native_samples = 0
    for profile in profiles:
        for sample in profile.get("samples", []) or []:
            if not isinstance(sample, list):
                continue
            sample_frames = [
                frames[index]
                for index in sample
                if isinstance(index, int) and 0 <= index < len(frames)
            ]
            referenced.update(
                index
                for index in sample
                if isinstance(index, int) and 0 <= index < len(frames)
            )
            if any(_is_native_frame(frame) for frame in sample_frames):
                native_samples += 1
            if any(_is_wait_frame(frame) for frame in sample_frames):
                waiting_samples += 1

    forbidden_keys = {
        str(key).casefold()
        for frame in frames
        for key in frame
        if str(key).casefold() in {
            "locals",
            "arguments",
            "args",
            "values",
            "buffer",
        }
    }
    lower, upper, uncertainty_ms = _timing_window(
        payload,
        command_start_ns=args.command_start_ns,
        command_end_ns=args.command_end_ns,
    )
    version_ok = str(args.observer_version).strip() == (
        f"py-spy {PYSPY_VERSION}"
    )
    passed = bool(
        args.attach_status == 0
        and args.native_requested
        and args.idle_requested
        and not args.locals_requested
        and version_ok
        and tids
        and native_samples > 0
        and waiting_samples > 0
        and not forbidden_keys
        and uncertainty_ms <= MAX_START_UNCERTAINTY_MS
    )
    report = {
        "schema": "issue-894-native-profile-preflight-v1",
        "label": DIAGNOSTIC_LABEL,
        "observer": "py-spy",
        "observer_version": str(args.observer_version).strip(),
        "pinned_version": PYSPY_VERSION,
        "attach_status": args.attach_status,
        "native_requested": bool(args.native_requested),
        "idle_requested": bool(args.idle_requested),
        "locals_requested": bool(args.locals_requested),
        "native_thread_ids": tids,
        "thread_ids_distinguished": bool(tids),
        "native_userspace_sample_count": native_samples,
        "waiting_native_sample_count": waiting_samples,
        "native_userspace_unwinding_available": native_samples > 0,
        "idle_waiting_threads_observed": waiting_samples > 0,
        "payload_fields_present": sorted(forbidden_keys),
        "profile_start_lower_epoch_ns": lower,
        "profile_start_upper_epoch_ns": upper,
        "profile_start_uncertainty_ms": uncertainty_ms,
        "maximum_allowed_start_uncertainty_ms": (
            MAX_START_UNCERTAINTY_MS
        ),
        "sample_timing_bounded": (
            uncertainty_ms <= MAX_START_UNCERTAINTY_MS
        ),
        "passed": passed,
    }
    _write_json(args.output, report)
    return report


def _parse_utc_ns(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing UTC timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _target_name(row: dict[str, object]) -> str:
    if str(row.get("target_role") or "") == "list_shifts_initial":
        return "list_shifts_initial"
    fingerprint = str(row.get("statement_fingerprint") or "")
    if fingerprint == "bb0e459d480f9470":
        return "demand_materialization"
    if fingerprint == "44f262328b901259":
        return "aggregate_children"
    return "unresolved_target"


def _fetch_spans(trace: Path) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("kind") != "dbapi_fetch":
            continue
        tid = int(row.get("native_thread_id") or 0)
        elapsed_ms = float(row.get("fetch_elapsed_ms") or 0.0)
        if tid <= 0 or elapsed_ms <= 0.0:
            continue
        end_ns = _parse_utc_ns(row.get("timestamp_utc"))
        spans.append({
            "target": _target_name(row),
            "native_thread_id": tid,
            "start_ns": end_ns - int(elapsed_ms * 1_000_000),
            "end_ns": end_ns,
            "connection_id": int(row.get("connection_id") or 0),
            "connection_execution_sequence": int(
                row.get("connection_execution_sequence") or 0
            ),
        })
    return spans


def _summarize(args: argparse.Namespace) -> dict[str, object]:
    preflight = json.loads(
        args.preflight.read_text(encoding="utf-8")
    )
    if preflight.get("passed") is not True:
        report = {
            "schema": "issue-894-native-profile-summary-v1",
            "label": DIAGNOSTIC_LABEL,
            "interpretation_allowed": False,
            "reason": "preflight_failed",
            "targets": {},
        }
        _write_json(args.output, report)
        return report

    payload = _load_speedscope(args.profile)
    (
        profile_start_lower,
        profile_start_upper,
        profile_uncertainty_ms,
    ) = _timing_window(
        payload,
        command_start_ns=args.command_start_ns,
        command_end_ns=args.command_end_ns,
    )
    if profile_uncertainty_ms > MAX_START_UNCERTAINTY_MS:
        report = {
            "schema": "issue-894-native-profile-summary-v1",
            "label": DIAGNOSTIC_LABEL,
            "interpretation_allowed": False,
            "reason": "profile_timing_uncertainty_too_large",
            "profile_start_uncertainty_ms": profile_uncertainty_ms,
            "targets": {},
        }
        _write_json(args.output, report)
        return report
    frames = [
        frame
        for frame in (payload.get("shared") or {}).get("frames", [])
        if isinstance(frame, dict)
    ]
    lower_start = profile_start_lower
    upper_start = profile_start_upper
    spans = _fetch_spans(args.trace)
    by_tid: dict[int, list[dict[str, object]]] = {}
    for span in spans:
        by_tid.setdefault(int(span["native_thread_id"]), []).append(span)

    target_counts: dict[str, Counter[str]] = {}
    target_samples: Counter[str] = Counter()
    target_spans: dict[str, set[tuple[int, int, int]]] = {}
    timing_unresolved = 0
    total_samples = 0

    for profile in payload.get("profiles", []) or []:
        if not isinstance(profile, dict):
            continue
        tid = _native_tid(profile)
        if tid <= 0:
            continue
        offset_seconds = 0.0
        samples = profile.get("samples", []) or []
        weights = profile.get("weights", []) or []
        for index, sample in enumerate(samples):
            if not isinstance(sample, list):
                continue
            weight = (
                float(weights[index])
                if index < len(weights)
                else 0.0
            )
            sample_low = (
                lower_start
                + int(offset_seconds * 1_000_000_000)
            )
            sample_high = (
                upper_start
                + int((offset_seconds + weight) * 1_000_000_000)
            )
            offset_seconds += weight
            total_samples += 1
            candidates = [
                span
                for span in by_tid.get(tid, [])
                if int(span["start_ns"]) <= sample_low
                and sample_high <= int(span["end_ns"])
            ]
            if len(candidates) != 1:
                if any(
                    int(span["start_ns"]) <= sample_high
                    and sample_low <= int(span["end_ns"])
                    for span in by_tid.get(tid, [])
                ):
                    timing_unresolved += 1
                continue
            span = candidates[0]
            target = str(span["target"])
            sample_frames = [
                frames[frame_index]
                for frame_index in sample
                if isinstance(frame_index, int)
                and 0 <= frame_index < len(frames)
            ]
            category = _stack_category(sample_frames)
            target_counts.setdefault(target, Counter())[category] += 1
            target_samples[target] += 1
            target_spans.setdefault(target, set()).add((
                int(span["native_thread_id"]),
                int(span["connection_id"]),
                int(span["connection_execution_sequence"]),
            ))

    targets: dict[str, object] = {}
    for target in (
        "list_shifts_initial",
        "demand_materialization",
        "aggregate_children",
    ):
        counts = target_counts.get(target, Counter())
        samples = int(target_samples[target])
        unresolved = int(counts.get("unresolved", 0))
        resolved_counts = [
            (name, count)
            for name, count in counts.items()
            if name != "unresolved"
        ]
        resolved_counts.sort(key=lambda item: (-item[1], item[0]))
        dominant = (
            resolved_counts[0][0]
            if resolved_counts
            and resolved_counts[0][1] > samples / 2
            else "mixed_or_unresolved"
        )
        targets[target] = {
            "profiled_fetch_spans": len(target_spans.get(target, set())),
            "associated_external_samples": samples,
            "native_categories": dict(sorted(counts.items())),
            "unresolved_fraction": (
                unresolved / samples if samples else 1.0
            ),
            "dominant_category": dominant,
        }

    report = {
        "schema": "issue-894-native-profile-summary-v1",
        "label": DIAGNOSTIC_LABEL,
        "observer": "py-spy",
        "observer_version": preflight.get("observer_version"),
        "interpretation_allowed": True,
        "profile_start_uncertainty_ms": profile_uncertainty_ms,
        "total_external_samples": total_samples,
        "timing_ambiguous_samples": timing_unresolved,
        "targets": targets,
        "privacy": {
            "locals_captured": False,
            "arguments_captured": False,
            "payload_capture": False,
        },
    }
    _write_json(args.output, report)
    return report


def _compare(args: argparse.Namespace) -> dict[str, object]:
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    profiled = json.loads(args.profiled.read_text(encoding="utf-8"))

    baseline_rate = float(
        baseline.get("achieved_request_start_rps") or 0.0
    )
    profiled_rate = float(
        profiled.get("achieved_request_start_rps") or 0.0
    )
    baseline_p95 = float(
        (baseline.get("latency") or {}).get("p95_ms") or 0.0
    )
    profiled_p95 = float(
        (profiled.get("latency") or {}).get("p95_ms") or 0.0
    )
    baseline_completed = max(1, int(baseline.get("completed") or 0))
    profiled_completed = max(1, int(profiled.get("completed") or 0))
    baseline_failure = (
        int(baseline.get("failed") or 0) / baseline_completed
    )
    profiled_failure = (
        int(profiled.get("failed") or 0) / profiled_completed
    )
    rate_floor = baseline_rate * 0.90
    latency_ceiling = max(
        baseline_p95 * 1.10,
        baseline_p95 + 50.0,
    )
    failure_ceiling = min(
        1.0,
        baseline_failure + FAILURE_RATE_MATERIAL_DELTA,
    )
    material = (
        profiled_rate < rate_floor
        or profiled_p95 > latency_ceiling
        or profiled_failure > failure_ceiling
    )
    report = {
        "schema": "issue-894-native-profile-comparison-v1",
        "label": DIAGNOSTIC_LABEL,
        "baseline": {
            "achieved_request_start_rps": baseline_rate,
            "latency_p95_ms": baseline_p95,
            "failure_rate": baseline_failure,
        },
        "externally_profiled": {
            "achieved_request_start_rps": profiled_rate,
            "latency_p95_ms": profiled_p95,
            "failure_rate": profiled_failure,
        },
        "required_rate_floor": rate_floor,
        "latency_p95_ceiling_ms": latency_ceiling,
        "failure_rate_ceiling": failure_ceiling,
        "delta": {
            "request_start_rps": profiled_rate - baseline_rate,
            "latency_p95_ms": profiled_p95 - baseline_p95,
            "failure_rate": profiled_failure - baseline_failure,
        },
        "material_native_profiling_deterioration": material,
        "interpretation_allowed": not material,
    }
    _write_json(args.output, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--profile", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--observer-version", required=True)
    preflight.add_argument("--attach-status", type=int, required=True)
    preflight.add_argument("--command-start-ns", type=int, required=True)
    preflight.add_argument("--command-end-ns", type=int, required=True)
    preflight.add_argument("--native-requested", action="store_true")
    preflight.add_argument("--idle-requested", action="store_true")
    preflight.add_argument("--locals-requested", action="store_true")

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--profile", type=Path, required=True)
    summarize.add_argument("--preflight", type=Path, required=True)
    summarize.add_argument("--trace", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument(
        "--command-start-ns",
        type=int,
        required=True,
    )
    summarize.add_argument(
        "--command-end-ns",
        type=int,
        required=True,
    )

    compare = sub.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--profiled", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "preflight":
        report = _preflight(args)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    if args.command == "summarize":
        report = _summarize(args)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["interpretation_allowed"] else 1
    if args.command == "compare":
        report = _compare(args)
        print(json.dumps(report, sort_keys=True))
        return (
            0
            if report["material_native_profiling_deterioration"] is False
            and report["interpretation_allowed"] is True
            else 1
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
