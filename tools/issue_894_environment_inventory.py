from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

SCHEMA = "issue-894-environment-inventory-v1"
STATUS_VALUES = {
    "AVAILABLE",
    "UNAVAILABLE",
    "ACCESS_DENIED",
    "NOT_APPLICABLE",
    "EXACT_RUNTIME_UNAVAILABLE",
}
SAFE_ENV_KEYS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITHUB_SHA",
    "ImageOS",
    "ImageVersion",
    "RUNNER_ARCH",
    "RUNNER_OS",
)
SECURITY_STATUS_FIELDS = (
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
    "Seccomp_filters",
)
THREAD_STATUS_FIELDS = (
    "Name",
    "State",
    "Tgid",
    "Pid",
    "PPid",
    "TracerPid",
    "NStgid",
    "NSpid",
    "NSpgid",
    "NSsid",
    "voluntary_ctxt_switches",
    "nonvoluntary_ctxt_switches",
)
SYSCTL_PATHS = {
    "yama_ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope"),
    "perf_event_paranoid": Path("/proc/sys/kernel/perf_event_paranoid"),
    "kptr_restrict": Path("/proc/sys/kernel/kptr_restrict"),
    "unprivileged_bpf_disabled": Path(
        "/proc/sys/kernel/unprivileged_bpf_disabled"
    ),
}
TOOL_COMMANDS = {
    "gdb": (("--version",), ("--configuration",)),
    "perf": (("version", "--build-options"),),
    "strace": (("-V",),),
    "readelf": (("--version",),),
    "objdump": (("--version",),),
    "bpftool": (("version",),),
}
DENIED_COMMAND_TOKENS = (
    "perf record",
    "perf trace",
    "gdb -p",
    "gdb --pid",
    "strace -p",
    "ptrace(",
    "bpftrace",
    "bcc",
    "uprobe",
    "uretprobe",
    "sysctl -w",
    "mount -t",
    "umount",
    "setcap",
    "capsh --",
    "py-spy",
    "pip install",
    "apt install",
    "apt-get install",
    "os.kill",
    "signal.",
    "docker kill",
)
FIXED_EXECUTABLE_BASENAMES = {
    "docker",
    "getcap",
    "readelf",
}
RELEVANT_LABELS = (
    "cpython",
    "pyodbc",
    "libc",
    "unixodbc",
    "microsoft_odbc_driver",
)


def _status(
    state: str,
    value: object = None,
    reason: str = "",
) -> dict[str, object]:
    if state not in STATUS_VALUES:
        raise ValueError(f"invalid state: {state}")
    result: dict[str, object] = {"state": state}
    if value is not None:
        result["value"] = value
    if reason:
        result["reason"] = reason[:400]
    return result


def _bounded_text(value: str, limit: int = 12000) -> str:
    return value.replace("\x00", "")[:limit]


def _read_text(
    path: Path,
    *,
    limit: int = 12000,
) -> dict[str, object]:
    try:
        return _status(
            "AVAILABLE",
            _bounded_text(
                path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ),
                limit,
            ).strip(),
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except FileNotFoundError:
        return _status(
            "UNAVAILABLE",
            reason="path_not_found",
        )
    except OSError as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )


def _parse_status(
    path: Path,
    fields: tuple[str, ...],
) -> dict[str, object]:
    raw = _read_text(path, limit=32000)
    if raw["state"] != "AVAILABLE":
        return raw
    wanted = set(fields)
    parsed: dict[str, str] = {}
    for line in str(raw.get("value") or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in wanted:
            parsed[key] = value.strip()[:300]
    return _status("AVAILABLE", parsed)


def _run(
    executable: str,
    args: tuple[str, ...],
    *,
    timeout: float = 4.0,
) -> dict[str, object]:
    resolved = shutil.which(executable)
    if not resolved:
        return _status(
            "UNAVAILABLE",
            reason="executable_not_found",
        )
    allowed = TOOL_COMMANDS.get(executable)
    if allowed is not None and args not in allowed:
        raise ValueError(
            f"command arguments not allowlisted: "
            f"{executable} {args}"
        )
    try:
        completed = subprocess.run(
            [resolved, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )
    return _status(
        "AVAILABLE",
        {
            "exit_code": completed.returncode,
            "output": _bounded_text(
                completed.stdout or "",
                10000,
            ).strip(),
        },
    )


def _run_fixed(
    argv: list[str],
    *,
    timeout: float = 5.0,
    limit: int = 200000,
) -> dict[str, object]:
    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError(
            "fixed command requires absolute executable"
        )
    executable_name = Path(argv[0]).name
    if executable_name not in FIXED_EXECUTABLE_BASENAMES:
        raise ValueError(
            "fixed command executable is not allowlisted: "
            f"{executable_name}"
        )
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )
    return _status(
        "AVAILABLE",
        {
            "exit_code": completed.returncode,
            "output": _bounded_text(
                completed.stdout or "",
                limit,
            ).strip(),
        },
    )


def _safe_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in SAFE_ENV_KEYS
        if key in os.environ
    }


def _mount_inventory() -> dict[str, object]:
    raw = _read_text(
        Path("/proc/self/mountinfo"),
        limit=200000,
    )
    if raw["state"] != "AVAILABLE":
        return raw
    rows: list[dict[str, str]] = []
    for line in str(raw.get("value") or "").splitlines():
        parts = line.split()
        if "-" not in parts or len(parts) < 10:
            continue
        sep = parts.index("-")
        mountpoint = parts[4]
        fstype = (
            parts[sep + 1]
            if sep + 1 < len(parts)
            else ""
        )
        if (
            fstype
            not in {
                "tracefs",
                "debugfs",
                "bpf",
                "securityfs",
            }
            and mountpoint
            not in {
                "/sys/kernel/tracing",
                "/sys/kernel/debug",
                "/sys/fs/bpf",
                "/sys/kernel/security",
            }
        ):
            continue
        rows.append(
            {
                "mountpoint": mountpoint,
                "filesystem": fstype,
                "mount_options": parts[5],
            }
        )
    rows.sort(
        key=lambda row: (
            row["mountpoint"],
            row["filesystem"],
        )
    )
    return _status("AVAILABLE", rows)


def _scheduler_events() -> dict[str, object]:
    roots = (
        Path("/sys/kernel/tracing/events/sched"),
        Path(
            "/sys/kernel/debug/tracing/events/sched"
        ),
    )
    for root in roots:
        try:
            values = sorted(
                entry.name
                for entry in root.iterdir()
                if entry.is_dir()
                and not entry.name.startswith(".")
            )
            return _status(
                "AVAILABLE",
                {
                    "root": str(root),
                    "events": values[:256],
                },
            )
        except PermissionError as exc:
            return _status(
                "ACCESS_DENIED",
                reason=type(exc).__name__,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            return _status(
                "UNAVAILABLE",
                reason=type(exc).__name__,
            )
    return _status(
        "UNAVAILABLE",
        reason="sched_trace_events_not_mounted",
    )


def _tool_inventory() -> dict[str, object]:
    result: dict[str, object] = {}
    getcap = shutil.which("getcap")
    for name, command_sets in TOOL_COMMANDS.items():
        path = shutil.which(name)
        if not path:
            result[name] = _status(
                "UNAVAILABLE",
                reason="executable_not_found",
            )
            continue
        commands = [
            {
                "args": list(args),
                "result": _run(name, args),
            }
            for args in command_sets
        ]
        capabilities = _status(
            "UNAVAILABLE",
            reason="getcap_unavailable",
        )
        if getcap:
            capabilities = _run_fixed(
                [getcap, path],
                timeout=2.0,
                limit=4000,
            )
        result[name] = _status(
            "AVAILABLE",
            {
                "path": path,
                "commands": commands,
                "file_capabilities": capabilities,
            },
        )
    return result


def _readlink(path: Path) -> dict[str, object]:
    try:
        return _status(
            "AVAILABLE",
            os.readlink(path),
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except FileNotFoundError:
        return _status(
            "UNAVAILABLE",
            reason="path_not_found",
        )
    except OSError as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )


def _proc_start_ticks(pid: int) -> dict[str, object]:
    raw = _read_text(
        Path(f"/proc/{pid}/stat"),
        limit=12000,
    )
    if raw["state"] != "AVAILABLE":
        return raw
    text = str(raw.get("value") or "")
    close = text.rfind(")")
    if close < 0:
        return _status(
            "UNAVAILABLE",
            reason="malformed_stat",
        )
    fields = text[close + 2 :].split()
    if len(fields) <= 19:
        return _status(
            "UNAVAILABLE",
            reason="short_stat",
        )
    try:
        return _status(
            "AVAILABLE",
            int(fields[19]),
        )
    except ValueError:
        return _status(
            "UNAVAILABLE",
            reason="invalid_start_ticks",
        )


def _thread_inventory(pid: int) -> dict[str, object]:
    task_root = Path(f"/proc/{pid}/task")
    try:
        tids = sorted(
            int(entry.name)
            for entry in task_root.iterdir()
            if entry.name.isdigit()
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except FileNotFoundError:
        return _status(
            "UNAVAILABLE",
            reason="target_process_not_found",
        )
    except OSError as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )
    rows: list[dict[str, object]] = []
    for tid in tids[:512]:
        status = _parse_status(
            Path(
                f"/proc/{pid}/task/{tid}/status"
            ),
            THREAD_STATUS_FIELDS,
        )
        namespace_tid = None
        if status.get("state") == "AVAILABLE":
            values = status.get("value") or {}
            nspid = str(
                values.get("NSpid") or ""
            ).split()
            if nspid:
                try:
                    namespace_tid = int(nspid[-1])
                except ValueError:
                    namespace_tid = None
        rows.append(
            {
                "host_tid": tid,
                "namespace_tid": namespace_tid,
                "status": status,
            }
        )
    return _status("AVAILABLE", rows)


def _relevance(
    path: str,
    executable_path: str,
) -> str | None:
    value = path.casefold()
    base = Path(path).name.casefold()
    if (
        path == executable_path
        or base.startswith("python")
    ):
        return "cpython"
    if "pyodbc" in base:
        return "pyodbc"
    if (
        base.startswith("libc.so")
        or base.startswith("libc-")
    ):
        return "libc"
    if base.startswith("libodbc.so"):
        return "unixodbc"
    if (
        "msodbcsql" in value
        or "libmsodbcsql" in base
    ):
        return "microsoft_odbc_driver"
    return None


def _sha256(path: Path) -> dict[str, object]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(
                lambda: handle.read(1024 * 1024),
                b"",
            ):
                digest.update(block)
        return _status(
            "AVAILABLE",
            digest.hexdigest(),
        )
    except PermissionError as exc:
        return _status(
            "ACCESS_DENIED",
            reason=type(exc).__name__,
        )
    except FileNotFoundError:
        return _status(
            "UNAVAILABLE",
            reason="mapped_file_not_found",
        )
    except OSError as exc:
        return _status(
            "UNAVAILABLE",
            reason=type(exc).__name__,
        )


def _elf_metadata(
    resolved: Path,
) -> dict[str, object]:
    readelf = shutil.which("readelf")
    if not readelf:
        return _status(
            "UNAVAILABLE",
            reason="readelf_unavailable",
        )
    sections_result = _run_fixed(
        [readelf, "-W", "-S", str(resolved)],
        timeout=4.0,
    )
    notes_result = _run_fixed(
        [readelf, "-W", "-n", str(resolved)],
        timeout=4.0,
    )
    header_result = _run_fixed(
        [readelf, "-W", "-h", str(resolved)],
        timeout=4.0,
    )
    if sections_result["state"] != "AVAILABLE":
        return sections_result
    output = str(
        (
            sections_result.get("value")
            or {}
        ).get("output")
        or ""
    )
    section_names = sorted(
        set(
            re.findall(
                r"\]\s+([^\s]+)\s+",
                output,
            )
        )
    )
    build_id = ""
    elf_class = ""
    machine = ""
    data_encoding = ""
    if header_result["state"] == "AVAILABLE":
        header = str(
            (
                header_result.get("value")
                or {}
            ).get("output")
            or ""
        )
        for key, target in (
            ("Class:", "elf_class"),
            ("Data:", "data_encoding"),
            ("Machine:", "machine"),
        ):
            match = re.search(
                rf"^\s*{re.escape(key)}\s*(.+)$",
                header,
                re.MULTILINE,
            )
            if not match:
                continue
            value = match.group(1).strip()[:200]
            if target == "elf_class":
                elf_class = value
            elif target == "data_encoding":
                data_encoding = value
            else:
                machine = value
    if notes_result["state"] == "AVAILABLE":
        notes = str(
            (
                notes_result.get("value")
                or {}
            ).get("output")
            or ""
        )
        match = re.search(
            r"Build ID:\s*([0-9a-fA-F]+)",
            notes,
        )
        if match:
            build_id = match.group(1).lower()
    return _status(
        "AVAILABLE",
        {
            "build_id": build_id or None,
            "architecture": {
                "elf_class": elf_class or None,
                "data_encoding": data_encoding or None,
                "machine": machine or None,
            },
            "sections": [
                name
                for name in section_names
                if name
                in {
                    ".eh_frame",
                    ".eh_frame_hdr",
                    ".debug_frame",
                    ".symtab",
                    ".dynsym",
                    ".debug_info",
                    ".debug_line",
                }
            ],
            "has_eh_frame": (
                ".eh_frame" in section_names
            ),
            "has_debug_frame": (
                ".debug_frame" in section_names
            ),
            "has_symtab": (
                ".symtab" in section_names
            ),
            "has_dynsym": (
                ".dynsym" in section_names
            ),
            "frame_pointer_preservation": _status(
                "UNAVAILABLE",
                reason=(
                    "not_inferred_from_CFI_or_symbol_presence; "
                    "no disassembly performed because this "
                    "inventory phase does not require a "
                    "speculative frame-pointer conclusion"
                ),
            ),
        },
    )


def _mapped_objects(pid: int) -> dict[str, object]:
    exe_link = _readlink(
        Path(f"/proc/{pid}/exe")
    )
    executable = (
        str(exe_link.get("value") or "")
        if exe_link["state"] == "AVAILABLE"
        else ""
    )
    raw = _read_text(
        Path(f"/proc/{pid}/maps"),
        limit=2_000_000,
    )
    if raw["state"] != "AVAILABLE":
        return raw
    selected: dict[
        tuple[str, str],
        dict[str, object],
    ] = {}
    root = Path(f"/proc/{pid}/root")
    for line in str(raw.get("value") or "").splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 6:
            continue
        mapped_path = parts[5]
        if not mapped_path.startswith("/"):
            continue
        mapped_path = mapped_path.removesuffix(
            " (deleted)"
        )
        label = _relevance(
            mapped_path,
            executable,
        )
        if label not in RELEVANT_LABELS:
            continue
        key = (label, mapped_path)
        if key in selected:
            continue
        resolved = (
            root
            / mapped_path.lstrip("/")
        )
        selected[key] = {
            "component": label,
            "mapped_path": mapped_path,
            "sha256": _sha256(resolved),
            "elf": _elf_metadata(resolved),
        }
    rows = [
        selected[key]
        for key in sorted(selected)
    ]
    present = {
        row["component"]
        for row in rows
    }
    missing = [
        label
        for label in RELEVANT_LABELS
        if label not in present
    ]
    return _status(
        "AVAILABLE",
        {
            "objects": rows,
            "missing_components": missing,
        },
    )


def _docker_json(
    args: list[str],
) -> dict[str, object]:
    docker = shutil.which("docker")
    if not docker:
        return _status(
            "UNAVAILABLE",
            reason="docker_unavailable",
        )
    result = _run_fixed(
        [docker, *args],
        timeout=5.0,
        limit=200000,
    )
    if result["state"] != "AVAILABLE":
        return result
    payload = result.get("value") or {}
    if int(payload.get("exit_code") or 0) != 0:
        return _status(
            "UNAVAILABLE",
            reason="docker_inspect_failed",
        )
    try:
        return _status(
            "AVAILABLE",
            json.loads(
                str(
                    payload.get("output")
                    or ""
                )
            ),
        )
    except json.JSONDecodeError:
        return _status(
            "UNAVAILABLE",
            reason="docker_output_not_json",
        )


def _historical_image_status(
    digest: str,
) -> dict[str, object]:
    historical = _docker_json(
        ["image", "inspect", digest]
    )
    if historical["state"] == "AVAILABLE":
        return _status(
            "AVAILABLE",
            {
                "digest": digest,
                "availability_basis": (
                    "exact_digest_present_in_current_"
                    "runner_docker_store"
                ),
            },
        )
    return _status(
        "EXACT_RUNTIME_UNAVAILABLE",
        {
            "known_image_identity": digest,
            "availability_basis": (
                "exact identity not present in current runner "
                "docker store; no pull, rebuild, or historical-"
                "image recreation attempted"
            ),
        },
    )


def _image_inventory(
    container_name: str,
    image_ref: str,
    expected_head: str,
    historical_ci_3221_digest: str,
    historical_ci_3222_digest: str,
) -> dict[str, object]:
    container = _docker_json(
        ["inspect", container_name]
    )
    image = _docker_json(
        ["image", "inspect", image_ref]
    )
    current: dict[str, object]
    if (
        container["state"] == "AVAILABLE"
        and image["state"] == "AVAILABLE"
    ):
        crows = container.get("value") or []
        irows = image.get("value") or []
        c0 = crows[0] if crows else {}
        i0 = irows[0] if irows else {}
        labels = (
            (
                i0.get("Config")
                or {}
            ).get("Labels")
            or {}
        )
        current = _status(
            "AVAILABLE",
            {
                "container_id": c0.get("Id"),
                "container_image_id": c0.get("Image"),
                "container_config_image": (
                    c0.get("Config")
                    or {}
                ).get("Image"),
                "image_id": i0.get("Id"),
                "repo_digests": sorted(
                    i0.get("RepoDigests")
                    or []
                ),
                "architecture": i0.get(
                    "Architecture"
                ),
                "os": i0.get("Os"),
                "created": i0.get("Created"),
                "revision_label": labels.get(
                    "org.opencontainers.image.revision"
                ),
                "expected_head": expected_head,
                "revision_matches_expected_head": (
                    labels.get(
                        "org.opencontainers.image.revision"
                    )
                    == expected_head
                ),
            },
        )
    else:
        current = _status(
            "UNAVAILABLE",
            reason=(
                f"container={container['state']} "
                f"image={image['state']}"
            ),
        )

    return {
        "current_inventory_runtime": current,
        "historical_ci_3221": _historical_image_status(
            historical_ci_3221_digest
        ),
        "historical_ci_3222": _historical_image_status(
            historical_ci_3222_digest
        ),
    }


def _clock_mapping(
    container_name: str,
) -> dict[str, object]:
    docker = shutil.which("docker")
    if not docker:
        return _status(
            "UNAVAILABLE",
            reason="docker_unavailable",
        )
    code = (
        "import json,time;"
        "print(json.dumps({"
        "'realtime_ns':time.time_ns(),"
        "'monotonic_ns':time.monotonic_ns()}))"
    )
    host_realtime_before = time.time_ns()
    host_monotonic_before = time.monotonic_ns()
    result = _run_fixed(
        [
            docker,
            "exec",
            container_name,
            "python",
            "-c",
            code,
        ],
        timeout=4.0,
        limit=4000,
    )
    host_realtime_after = time.time_ns()
    host_monotonic_after = time.monotonic_ns()
    if result["state"] != "AVAILABLE":
        return result
    payload = result.get("value") or {}
    if int(payload.get("exit_code") or 0) != 0:
        return _status(
            "UNAVAILABLE",
            reason="container_clock_read_failed",
        )
    try:
        container = json.loads(
            str(
                payload.get("output")
                or ""
            )
        )
    except json.JSONDecodeError:
        return _status(
            "UNAVAILABLE",
            reason="container_clock_output_invalid",
        )
    realtime = int(container["realtime_ns"])
    monotonic = int(container["monotonic_ns"])
    return _status(
        "AVAILABLE",
        {
            "host_realtime_before_ns": (
                host_realtime_before
            ),
            "host_realtime_after_ns": (
                host_realtime_after
            ),
            "container_realtime_ns": realtime,
            "container_realtime_minus_host_bounds_ns": [
                realtime - host_realtime_after,
                realtime - host_realtime_before,
            ],
            "host_monotonic_before_ns": (
                host_monotonic_before
            ),
            "host_monotonic_after_ns": (
                host_monotonic_after
            ),
            "container_monotonic_ns": monotonic,
            "container_monotonic_minus_host_bounds_ns": [
                monotonic - host_monotonic_after,
                monotonic - host_monotonic_before,
            ],
        },
    )


def _namespace_inventory(
    pid: int,
) -> dict[str, object]:
    names = (
        "pid",
        "pid_for_children",
        "time",
        "time_for_children",
        "mnt",
        "user",
    )
    links = {
        name: _readlink(
            Path(f"/proc/{pid}/ns/{name}")
        )
        for name in names
    }
    return {
        "links": links,
        "timens_offsets": _read_text(
            Path(
                f"/proc/{pid}/timens_offsets"
            ),
            limit=4000,
        ),
        "self_time_namespace": _readlink(
            Path("/proc/self/ns/time")
        ),
        "self_pid_namespace": _readlink(
            Path("/proc/self/ns/pid")
        ),
    }


def _observer_identity() -> dict[str, object]:
    resuid = (
        list(os.getresuid())
        if hasattr(os, "getresuid")
        else [os.getuid(), os.geteuid()]
    )
    resgid = (
        list(os.getresgid())
        if hasattr(os, "getresgid")
        else [os.getgid(), os.getegid()]
    )
    return {
        "uid": os.getuid(),
        "effective_uid": os.geteuid(),
        "gid": os.getgid(),
        "effective_gid": os.getegid(),
        "resuid": resuid,
        "resgid": resgid,
        "proc_status": _parse_status(
            Path("/proc/self/status"),
            SECURITY_STATUS_FIELDS,
        ),
        "security_context": _read_text(
            Path("/proc/self/attr/current"),
            limit=2000,
        ),
    }


def _security_inventory() -> dict[str, object]:
    sysctls = {
        name: _read_text(
            path,
            limit=1000,
        )
        for name, path in SYSCTL_PATHS.items()
    }
    lockdown = _read_text(
        Path("/sys/kernel/security/lockdown"),
        limit=1000,
    )
    return {
        "collector_proc_status": _parse_status(
            Path("/proc/self/status"),
            SECURITY_STATUS_FIELDS,
        ),
        "sysctls": sysctls,
        "lockdown": lockdown,
        "mounts": _mount_inventory(),
        "scheduler_events": _scheduler_events(),
        "bpf_filesystem": (
            _status(
                "AVAILABLE",
                {
                    "path": "/sys/fs/bpf",
                    "readable": os.access(
                        "/sys/fs/bpf",
                        os.R_OK,
                    ),
                },
            )
            if Path("/sys/fs/bpf").exists()
            else _status(
                "UNAVAILABLE",
                reason="bpf_filesystem_path_absent",
            )
        ),
    }


def _target_mode(seconds: int) -> int:
    imported: list[object] = []
    try:
        import pyodbc  # type: ignore

        imported.append(pyodbc)
    except Exception:
        pass
    for pattern in (
        "/usr/lib/*/libodbc.so*",
        "/usr/lib/libodbc.so*",
        "/opt/microsoft/msodbcsql*/lib64/"
        "libmsodbcsql-*.so*",
    ):
        for candidate in sorted(
            Path("/").glob(
                pattern.lstrip("/")
            )
        ):
            try:
                imported.append(
                    ctypes.CDLL(
                        str(candidate)
                    )
                )
                break
            except OSError:
                continue
    deadline = (
        time.monotonic()
        + max(
            5,
            min(seconds, 300),
        )
    )
    while time.monotonic() < deadline:
        time.sleep(0.5)
    return 0


def _collect(
    args: argparse.Namespace,
) -> dict[str, object]:
    pid = int(args.target_pid)
    if pid <= 0:
        raise ValueError(
            "target pid must be positive"
        )
    if args.require_elevated and os.geteuid() != 0:
        raise ValueError(
            "elevated host inventory requires effective uid 0"
        )
    boot_id = _read_text(
        Path(
            "/proc/sys/kernel/random/boot_id"
        ),
        limit=200,
    )
    report: dict[str, object] = {
        "schema": SCHEMA,
        "inventory_only": True,
        "observer_preflight_performed": False,
        "profiling_or_tracing_performed": False,
        "mutation_performed": False,
        "observer_identity": _observer_identity(),
        "runner": {
            "allowlisted_environment": _safe_env(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": (
                    platform.version()[:500]
                ),
                "machine": platform.machine(),
                "uname": list(
                    platform.uname()
                ),
            },
            "boot_id": boot_id,
        },
        "host_kernel_security": (
            _security_inventory()
        ),
        "tools": _tool_inventory(),
        "target_process": {
            "host_pid": pid,
            "start_ticks": _proc_start_ticks(
                pid
            ),
            "status": _parse_status(
                Path(
                    f"/proc/{pid}/status"
                ),
                SECURITY_STATUS_FIELDS
                + (
                    "Pid",
                    "Tgid",
                    "NSpid",
                    "Threads",
                ),
            ),
            "threads": _thread_inventory(
                pid
            ),
            "namespaces": (
                _namespace_inventory(pid)
            ),
        },
        "loaded_binary_unwind": (
            _mapped_objects(pid)
        ),
        "images": _image_inventory(
            args.container_name,
            args.image_ref,
            args.expected_head,
            args.historical_ci_3221_digest,
            args.historical_ci_3222_digest,
        ),
        "clock_mapping": (
            _clock_mapping(
                args.container_name
            )
        ),
        "remaining_unknowns": [],
    }
    unknowns: list[str] = []
    loaded = report[
        "loaded_binary_unwind"
    ]
    if (
        isinstance(loaded, dict)
        and loaded.get("state")
        == "AVAILABLE"
    ):
        missing = (
            (
                loaded.get("value")
                or {}
            ).get(
                "missing_components"
            )
            or []
        )
        for component in missing:
            unknowns.append(
                f"mapped_object_missing:"
                f"{component}"
            )
        for row in (
            (
                loaded.get("value")
                or {}
            ).get("objects")
            or []
        ):
            elf = (
                row.get("elf")
                or {}
            )
            if (
                elf.get("state")
                != "AVAILABLE"
            ):
                unknowns.append(
                    "elf_metadata_unavailable:"
                    f"{row.get('component')}"
                )
            else:
                fp = (
                    (
                        elf.get("value")
                        or {}
                    ).get(
                        "frame_pointer_preservation"
                    )
                    or {}
                )
                if (
                    fp.get("state")
                    != "AVAILABLE"
                ):
                    unknowns.append(
                        "frame_pointer_preservation_unknown:"
                        f"{row.get('component')}"
                    )
    else:
        unknowns.append(
            "loaded_binary_inventory_unavailable"
        )
    for tool in (
        "gdb",
        "perf",
        "strace",
    ):
        if (
            (
                report["tools"].get(
                    tool
                )
                or {}
            ).get("state")
            != "AVAILABLE"
        ):
            unknowns.append(
                f"tool_unavailable:{tool}"
            )
    security = report[
        "host_kernel_security"
    ]
    for name, entry in (
        security.get("sysctls")
        or {}
    ).items():
        if (
            entry.get("state")
            != "AVAILABLE"
        ):
            unknowns.append(
                "security_fact_unavailable:"
                f"{name}"
            )
    report["remaining_unknowns"] = (
        sorted(set(unknowns))
    )
    return report


def _walk_statuses(
    value: object,
) -> list[str]:
    states: list[str] = []
    if isinstance(value, dict):
        if "state" in value:
            states.append(
                str(value["state"])
            )
        for nested in value.values():
            states.extend(
                _walk_statuses(nested)
            )
    elif isinstance(value, list):
        for nested in value:
            states.extend(
                _walk_statuses(nested)
            )
    return states


def _validate_report(
    report: dict[str, object],
) -> None:
    expected_top = {
        "schema",
        "inventory_only",
        "observer_preflight_performed",
        "profiling_or_tracing_performed",
        "mutation_performed",
        "observer_identity",
        "runner",
        "host_kernel_security",
        "tools",
        "target_process",
        "loaded_binary_unwind",
        "images",
        "clock_mapping",
        "remaining_unknowns",
    }
    if set(report) != expected_top:
        raise ValueError(
            "top-level schema drift: "
            f"{sorted(set(report) ^ expected_top)}"
        )
    if report.get("schema") != SCHEMA:
        raise ValueError("schema mismatch")
    if (
        report.get("inventory_only")
        is not True
    ):
        raise ValueError(
            "inventory_only must be true"
        )
    if (
        report.get(
            "observer_preflight_performed"
        )
        is not False
    ):
        raise ValueError(
            "observer preflight is forbidden "
            "in inventory phase"
        )
    if (
        report.get(
            "profiling_or_tracing_performed"
        )
        is not False
    ):
        raise ValueError(
            "profiling/tracing is forbidden "
            "in inventory phase"
        )
    if (
        report.get("mutation_performed")
        is not False
    ):
        raise ValueError(
            "mutation is forbidden "
            "in inventory phase"
        )
    invalid_states = sorted(
        set(_walk_statuses(report))
        - STATUS_VALUES
    )
    if invalid_states:
        raise ValueError(
            "invalid status values: "
            f"{invalid_states}"
        )
    env = (
        (
            report.get("runner")
            or {}
        ).get(
            "allowlisted_environment"
        )
        or {}
    )
    if not set(env).issubset(
        SAFE_ENV_KEYS
    ):
        raise ValueError(
            "non-allowlisted environment "
            "metadata present"
        )
    serialized = json.dumps(
        report,
        sort_keys=True,
    ).casefold()
    forbidden_fragments = (
        "authorization: bearer",
        "cookie=",
        "access_token",
        "refresh_token",
        "sql_statement",
        "request_body",
        "response_body",
        "locals",
        "function_arguments",
        "core_dump",
        "raw_process_memory",
    )
    present = [
        fragment
        for fragment in forbidden_fragments
        if fragment in serialized
    ]
    if present:
        raise ValueError(
            "forbidden artifact fragments: "
            f"{present}"
        )
    for historical_key in (
        "historical_ci_3221",
        "historical_ci_3222",
    ):
        historical = (
            (
                report.get("images")
                or {}
            ).get(historical_key)
            or {}
        )
        if historical.get("state") != "AVAILABLE":
            continue
        basis = str(
            (
                historical.get("value")
                or {}
            ).get(
                "availability_basis"
            )
            or ""
        )
        if basis != (
            "exact_digest_present_in_current_"
            "runner_docker_store"
        ):
            raise ValueError(
                "historical runtime cannot "
                "be reported available "
                "from rebuild"
            )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _archive_json_member(
    handle: tarfile.TarFile,
    name: str,
) -> tuple[bytes, object]:
    try:
        member = handle.getmember(name)
    except KeyError as exc:
        raise ValueError(
            f"docker archive member is missing: {name}"
        ) from exc
    extracted = handle.extractfile(member)
    if extracted is None:
        raise ValueError(
            f"docker archive member is unreadable: {name}"
        )
    raw = extracted.read()
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"docker archive JSON is invalid: {name}"
        ) from exc


def _digest_blob_name(digest: str) -> str:
    match = re.fullmatch(
        r"sha256:([0-9a-f]{64})",
        digest,
    )
    if not match:
        raise ValueError(
            f"unsupported OCI digest: {digest}"
        )
    return f"blobs/sha256/{match.group(1)}"


def _verified_blob_json(
    handle: tarfile.TarFile,
    digest: str,
) -> tuple[bytes, object]:
    name = _digest_blob_name(digest)
    raw, payload = _archive_json_member(
        handle,
        name,
    )
    expected = digest.removeprefix(
        "sha256:"
    )
    actual = _sha256_bytes(raw)
    if actual != expected:
        raise ValueError(
            f"OCI blob digest mismatch: {name}"
        )
    return raw, payload


def _reference_manifest_from_inspect(
    archive: Path,
    image_ref: str,
    expected_head: str,
    inspected: dict[str, object],
) -> dict[str, object]:
    archive_hash = _sha256(archive)
    if archive_hash["state"] != "AVAILABLE":
        raise ValueError(
            "image archive is not readable"
        )

    image_id = str(
        inspected.get("Id") or ""
    )
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        image_id,
    ):
        raise ValueError(
            "live Docker image ID is missing or malformed"
        )
    labels = (
        (
            inspected.get("Config")
            or {}
        ).get("Labels")
        or {}
    )
    revision = str(
        labels.get(
            "org.opencontainers.image.revision"
        )
        or ""
    )
    if revision != expected_head:
        raise ValueError(
            "inventory image revision does not match PR head"
        )
    architecture = str(
        inspected.get("Architecture") or ""
    )
    operating_system = str(
        inspected.get("Os") or ""
    )
    if not architecture or not operating_system:
        raise ValueError(
            "live Docker platform identity is incomplete"
        )

    repo_digests = sorted(
        str(value)
        for value in (
            inspected.get("RepoDigests")
            or []
        )
    )
    if repo_digests:
        matching_repo_digests = [
            value
            for value in repo_digests
            if value.rsplit("@", 1)[-1]
            == image_id
        ]
        if not matching_repo_digests:
            raise ValueError(
                "live repo digest does not match live image ID"
            )

    with tarfile.open(
        archive,
        mode="r:*",
    ) as handle:
        legacy_raw, legacy = _archive_json_member(
            handle,
            "manifest.json",
        )
        if not isinstance(legacy, list):
            raise ValueError(
                "docker archive manifest.json is malformed"
            )
        legacy_matches = [
            row
            for row in legacy
            if isinstance(row, dict)
            and image_ref in (
                row.get("RepoTags")
                or []
            )
        ]
        if len(legacy_matches) != 1:
            raise ValueError(
                "docker archive tag entry is missing or ambiguous"
            )
        legacy_entry = legacy_matches[0]

        index_raw, index = _archive_json_member(
            handle,
            "index.json",
        )
        if not isinstance(index, dict):
            raise ValueError(
                "OCI archive index.json is malformed"
            )
        top_descriptors = index.get(
            "manifests"
        )
        if (
            not isinstance(top_descriptors, list)
            or len(top_descriptors) != 1
            or not isinstance(
                top_descriptors[0],
                dict,
            )
        ):
            raise ValueError(
                "OCI archive top-level image descriptor is ambiguous"
            )
        top_descriptor = top_descriptors[0]
        top_digest = str(
            top_descriptor.get("digest")
            or ""
        )

        top_raw, top_index = _verified_blob_json(
            handle,
            top_digest,
        )
        if not isinstance(top_index, dict):
            raise ValueError(
                "OCI image-index blob is malformed"
            )

        # On the GitHub Actions containerd-backed Docker store
        # observed in CI #3223, docker image inspect .Id is the
        # digest of this OCI image-index blob, not the config
        # object digest. Bind the live image to the archive at
        # that exact cryptographic identity.
        if image_id != top_digest:
            raise ValueError(
                "live Docker image ID does not match archive "
                "OCI image-index digest"
            )

        platform_descriptors = [
            descriptor
            for descriptor in (
                top_index.get("manifests")
                or []
            )
            if isinstance(
                descriptor,
                dict,
            )
            and (
                descriptor.get("platform")
                or {}
            ).get("architecture")
            == architecture
            and (
                descriptor.get("platform")
                or {}
            ).get("os")
            == operating_system
        ]
        if len(platform_descriptors) != 1:
            raise ValueError(
                "OCI platform image manifest is missing or ambiguous"
            )
        platform_descriptor = (
            platform_descriptors[0]
        )
        platform_digest = str(
            platform_descriptor.get(
                "digest"
            )
            or ""
        )
        platform_raw, platform_manifest = (
            _verified_blob_json(
                handle,
                platform_digest,
            )
        )
        if not isinstance(
            platform_manifest,
            dict,
        ):
            raise ValueError(
                "OCI platform manifest is malformed"
            )

        config_descriptor = (
            platform_manifest.get(
                "config"
            )
            or {}
        )
        if not isinstance(
            config_descriptor,
            dict,
        ):
            raise ValueError(
                "OCI config descriptor is malformed"
            )
        config_digest = str(
            config_descriptor.get(
                "digest"
            )
            or ""
        )
        config_raw, config = (
            _verified_blob_json(
                handle,
                config_digest,
            )
        )
        if not isinstance(config, dict):
            raise ValueError(
                "OCI config object is malformed"
            )

        legacy_config_name = str(
            legacy_entry.get(
                "Config"
            )
            or ""
        )
        if (
            legacy_config_name
            != _digest_blob_name(
                config_digest
            )
        ):
            raise ValueError(
                "legacy docker manifest config does not match "
                "OCI platform manifest config"
            )

    archived_labels = (
        (
            config.get("config")
            or {}
        ).get("Labels")
        or {}
    )
    archived_revision = str(
        archived_labels.get(
            "org.opencontainers.image.revision"
        )
        or ""
    )
    if archived_revision != expected_head:
        raise ValueError(
            "archive OCI revision does not match PR head"
        )
    if (
        str(config.get("architecture") or "")
        != architecture
        or str(config.get("os") or "")
        != operating_system
    ):
        raise ValueError(
            "archive config platform does not match live image"
        )

    report = {
        "schema": (
            "issue-894-diagnostic-reference-runtime-v2"
        ),
        "diagnostic_reference": True,
        "historical_recovery": False,
        "source": (
            "new_exact_head_inventory_build"
        ),
        "pr_head": expected_head,
        "image_ref": image_ref,
        "live_image_id": image_id,
        "live_repo_digests": repo_digests,
        "live_revision_label": revision,
        "architecture": architecture,
        "os": operating_system,
        "archive_file": archive.name,
        "archive_size_bytes": archive.stat().st_size,
        "archive_sha256": str(
            archive_hash.get("value") or ""
        ),
        "docker_archive_manifest_sha256": (
            _sha256_bytes(legacy_raw)
        ),
        "oci_layout_index_sha256": (
            _sha256_bytes(index_raw)
        ),
        "archive_top_descriptor_digest": (
            top_digest
        ),
        "archive_top_index_sha256": (
            _sha256_bytes(top_raw)
        ),
        "archive_platform_manifest_digest": (
            platform_digest
        ),
        "archive_platform_manifest_sha256": (
            _sha256_bytes(platform_raw)
        ),
        "archive_config_digest": config_digest,
        "archive_config_sha256": (
            _sha256_bytes(config_raw)
        ),
        "archive_revision_label": archived_revision,
        "archive_architecture": config.get(
            "architecture"
        ),
        "archive_os": config.get("os"),
        "identity_chain": {
            "live_image_id_equals_archive_top_descriptor": (
                image_id == top_digest
            ),
            "archive_top_descriptor_hash_verified": (
                _sha256_bytes(top_raw)
                == top_digest.removeprefix(
                    "sha256:"
                )
            ),
            "archive_platform_manifest_hash_verified": (
                _sha256_bytes(platform_raw)
                == platform_digest.removeprefix(
                    "sha256:"
                )
            ),
            "archive_config_hash_verified": (
                _sha256_bytes(config_raw)
                == config_digest.removeprefix(
                    "sha256:"
                )
            ),
            "legacy_manifest_config_matches_oci_config": True,
            "live_revision_matches_pr_head": (
                revision == expected_head
            ),
            "archive_revision_matches_pr_head": (
                archived_revision == expected_head
            ),
        },
    }
    _validate_reference_manifest(report)
    return report


def _reference_manifest(
    archive: Path,
    image_ref: str,
    expected_head: str,
) -> dict[str, object]:
    image = _docker_json(
        ["image", "inspect", image_ref]
    )
    if image["state"] != "AVAILABLE":
        raise ValueError(
            "exact inventory image is unavailable"
        )
    rows = image.get("value") or []
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise ValueError(
            "expected exactly one image inspection result"
        )
    return _reference_manifest_from_inspect(
        archive,
        image_ref,
        expected_head,
        rows[0],
    )


def _validate_reference_manifest(
    report: dict[str, object],
) -> None:
    expected = {
        "schema",
        "diagnostic_reference",
        "historical_recovery",
        "source",
        "pr_head",
        "image_ref",
        "live_image_id",
        "live_repo_digests",
        "live_revision_label",
        "architecture",
        "os",
        "archive_file",
        "archive_size_bytes",
        "archive_sha256",
        "docker_archive_manifest_sha256",
        "oci_layout_index_sha256",
        "archive_top_descriptor_digest",
        "archive_top_index_sha256",
        "archive_platform_manifest_digest",
        "archive_platform_manifest_sha256",
        "archive_config_digest",
        "archive_config_sha256",
        "archive_revision_label",
        "archive_architecture",
        "archive_os",
        "identity_chain",
    }
    if set(report) != expected:
        raise ValueError(
            "diagnostic reference manifest schema drift"
        )
    if report.get("schema") != (
        "issue-894-diagnostic-reference-runtime-v2"
    ):
        raise ValueError(
            "diagnostic reference manifest schema mismatch"
        )
    if report.get("diagnostic_reference") is not True:
        raise ValueError(
            "diagnostic reference marker is missing"
        )
    if report.get("historical_recovery") is not False:
        raise ValueError(
            "new diagnostic reference cannot claim historical recovery"
        )
    if report.get("source") != (
        "new_exact_head_inventory_build"
    ):
        raise ValueError(
            "diagnostic reference source is invalid"
        )

    head = str(report.get("pr_head") or "")
    if not re.fullmatch(
        r"[0-9a-f]{40}",
        head,
    ):
        raise ValueError(
            "PR head is missing or malformed"
        )
    if str(
        report.get(
            "live_revision_label"
        )
        or ""
    ) != head:
        raise ValueError(
            "live revision label is not bound to PR head"
        )
    if str(
        report.get(
            "archive_revision_label"
        )
        or ""
    ) != head:
        raise ValueError(
            "archive revision is not bound to PR head"
        )

    live_image_id = str(
        report.get("live_image_id") or ""
    )
    top_digest = str(
        report.get(
            "archive_top_descriptor_digest"
        )
        or ""
    )
    if live_image_id != top_digest:
        raise ValueError(
            "live image identity is not bound to archive OCI index"
        )

    digest_pairs = (
        (
            "archive_top_descriptor_digest",
            "archive_top_index_sha256",
        ),
        (
            "archive_platform_manifest_digest",
            "archive_platform_manifest_sha256",
        ),
        (
            "archive_config_digest",
            "archive_config_sha256",
        ),
    )
    for digest_key, hash_key in digest_pairs:
        digest = str(
            report.get(digest_key) or ""
        )
        value_hash = str(
            report.get(hash_key) or ""
        )
        if digest != f"sha256:{value_hash}":
            raise ValueError(
                "archive OCI descriptor/hash mismatch: "
                f"{digest_key}"
            )

    for key in (
        "archive_sha256",
        "docker_archive_manifest_sha256",
        "oci_layout_index_sha256",
        "archive_top_index_sha256",
        "archive_platform_manifest_sha256",
        "archive_config_sha256",
    ):
        value = str(report.get(key) or "")
        if not re.fullmatch(
            r"[0-9a-f]{64}",
            value,
        ):
            raise ValueError(
                f"invalid diagnostic reference digest: {key}"
            )

    chain = report.get(
        "identity_chain"
    )
    if not isinstance(chain, dict):
        raise ValueError(
            "identity chain is missing"
        )
    required_chain = {
        "live_image_id_equals_archive_top_descriptor",
        "archive_top_descriptor_hash_verified",
        "archive_platform_manifest_hash_verified",
        "archive_config_hash_verified",
        "legacy_manifest_config_matches_oci_config",
        "live_revision_matches_pr_head",
        "archive_revision_matches_pr_head",
    }
    if set(chain) != required_chain:
        raise ValueError(
            "identity chain schema drift"
        )
    if not all(
        chain.get(key) is True
        for key in required_chain
    ):
        raise ValueError(
            "diagnostic reference identity chain is incomplete"
        )
    if (
        report.get("architecture")
        != report.get(
            "archive_architecture"
        )
        or report.get("os")
        != report.get("archive_os")
    ):
        raise ValueError(
            "live/archive platform identity mismatch"
        )

    repo_digests = report.get(
        "live_repo_digests"
    )
    if not isinstance(
        repo_digests,
        list,
    ):
        raise ValueError(
            "live repo digests are malformed"
        )
    if repo_digests and not any(
        str(value).rsplit("@", 1)[-1]
        == live_image_id
        for value in repo_digests
    ):
        raise ValueError(
            "live repo digest is inconsistent with image ID"
        )


def _validate_workflow(
    path: Path,
) -> None:
    text = path.read_text(
        encoding="utf-8"
    )
    marker = (
        "  issue-894-environment-inventory:"
    )
    start = text.find(marker)
    if start < 0:
        raise ValueError(
            "inventory job missing"
        )
    match = re.search(
        r"\n  [a-zA-Z0-9_-]+:\n",
        text[start + len(marker) :],
    )
    end = (
        len(text)
        if match is None
        else (
            start
            + len(marker)
            + match.start()
        )
    )
    job = text[start:end].casefold()
    forbidden = (
        "py-spy",
        "perf record",
        "gdb -p",
        "gdb --pid",
        "strace -p",
        "bpftrace",
        "uprobe",
        "uretprobe",
        "issue_894_connection_trial.py trial",
        "--duration-seconds 30",
        "--duration-seconds 600",
        "sysctl -w",
        "mount -t",
        "setcap",
        "pip install",
        "apt-get install",
        "apt install",
        "kill -",
        "pkill",
    )
    hits = [
        token
        for token in forbidden
        if token in job
    ]
    if hits:
        raise ValueError(
            "inventory workflow invokes "
            f"forbidden operations: {hits}"
        )
    required_inventory_fragments = (
        "sudo --preserve-env=",
        " --require-elevated",
        "docker image save",
        "bind-image-archive",
        "validate-reference",
        "issue-894-diagnostic-reference-runtime-",
    )
    missing_requirements = [
        fragment
        for fragment in required_inventory_fragments
        if fragment not in job
    ]
    if missing_requirements:
        raise ValueError(
            "inventory workflow is missing elevated/reference "
            f"requirements: {missing_requirements}"
        )
    if (
        "issue_894_inventory_only"
        not in text
    ):
        raise ValueError(
            "inventory-only routing "
            "output missing"
        )
    phase4_pos = text.find(
        "  issue-894-phase4:"
    )
    if phase4_pos < 0:
        raise ValueError(
            "existing issue-894 "
            "phase4 job missing"
        )
    phase4_header = text[
        phase4_pos : phase4_pos + 500
    ]
    if (
        "issue_894_inventory_only != 'true'"
        not in phase4_header
    ):
        raise ValueError(
            "phase4 job is not gated off "
            "during inventory-only head"
        )


def _self_test() -> None:
    if STATUS_VALUES != {
        "AVAILABLE",
        "UNAVAILABLE",
        "ACCESS_DENIED",
        "NOT_APPLICABLE",
        "EXACT_RUNTIME_UNAVAILABLE",
    }:
        raise AssertionError(
            "status contract drift"
        )
    command_text = "\n".join(
        f"{tool} {' '.join(args)}"
        for tool, commands
        in TOOL_COMMANDS.items()
        for args in commands
    ).casefold()
    hits = [
        token
        for token in DENIED_COMMAND_TOKENS
        if token in command_text
    ]
    if hits:
        raise AssertionError(
            f"unsafe command registry: {hits}"
        )
    unavailable = _status(
        "EXACT_RUNTIME_UNAVAILABLE",
        {
            "availability_basis": (
                "no_rebuild"
            )
        },
    )
    if (
        unavailable["state"]
        != "EXACT_RUNTIME_UNAVAILABLE"
    ):
        raise AssertionError(
            "historical runtime state "
            "contract failed"
        )
    sample = {
        "schema": SCHEMA,
        "inventory_only": True,
        "observer_preflight_performed": False,
        "profiling_or_tracing_performed": False,
        "mutation_performed": False,
        "observer_identity": {
            "effective_uid": 0,
        },
        "runner": {
            "allowlisted_environment": {}
        },
        "host_kernel_security": {},
        "tools": {},
        "target_process": {},
        "loaded_binary_unwind": _status(
            "UNAVAILABLE"
        ),
        "images": {
            "historical_ci_3221": _status(
                "EXACT_RUNTIME_UNAVAILABLE",
                {
                    "availability_basis": (
                        "no_rebuild"
                    )
                },
            ),
            "historical_ci_3222": _status(
                "EXACT_RUNTIME_UNAVAILABLE",
                {
                    "availability_basis": (
                        "no_rebuild"
                    )
                },
            )
        },
        "clock_mapping": _status(
            "UNAVAILABLE"
        ),
        "remaining_unknowns": [],
    }
    _validate_report(sample)
    def _json_bytes(value: object) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _add_bytes(
        handle: tarfile.TarFile,
        name: str,
        value: bytes,
    ) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(value)
        info.mtime = 0
        handle.addfile(
            info,
            io.BytesIO(value),
        )

    head = "a" * 40
    image_ref = f"example:{head}"
    config_bytes = _json_bytes({
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": head,
            }
        },
    })
    config_hash = _sha256_bytes(
        config_bytes
    )
    config_digest = (
        f"sha256:{config_hash}"
    )
    platform_bytes = _json_bytes({
        "schemaVersion": 2,
        "mediaType": (
            "application/vnd.oci.image.manifest.v1+json"
        ),
        "config": {
            "mediaType": (
                "application/vnd.oci.image.config.v1+json"
            ),
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [],
    })
    platform_hash = _sha256_bytes(
        platform_bytes
    )
    platform_digest = (
        f"sha256:{platform_hash}"
    )
    top_bytes = _json_bytes({
        "schemaVersion": 2,
        "mediaType": (
            "application/vnd.oci.image.index.v1+json"
        ),
        "manifests": [{
            "mediaType": (
                "application/vnd.oci.image.manifest.v1+json"
            ),
            "digest": platform_digest,
            "size": len(platform_bytes),
            "platform": {
                "architecture": "amd64",
                "os": "linux",
            },
        }],
    })
    top_hash = _sha256_bytes(
        top_bytes
    )
    top_digest = f"sha256:{top_hash}"
    index = {
        "schemaVersion": 2,
        "mediaType": (
            "application/vnd.oci.image.index.v1+json"
        ),
        "manifests": [{
            "mediaType": (
                "application/vnd.oci.image.index.v1+json"
            ),
            "digest": top_digest,
            "size": len(top_bytes),
        }],
    }
    legacy = [{
        "Config": (
            f"blobs/sha256/{config_hash}"
        ),
        "RepoTags": [image_ref],
        "Layers": [],
    }]
    inspected = {
        "Id": top_digest,
        "RepoDigests": [
            f"example@{top_digest}"
        ],
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": head,
            }
        },
    }

    def _write_synthetic_archive(
        path: Path,
        *,
        top_descriptors: list[dict[str, object]] | None = None,
    ) -> None:
        with tarfile.open(
            path,
            mode="w",
        ) as handle:
            _add_bytes(
                handle,
                "manifest.json",
                _json_bytes(legacy),
            )
            chosen_index = dict(index)
            if top_descriptors is not None:
                chosen_index[
                    "manifests"
                ] = top_descriptors
            _add_bytes(
                handle,
                "index.json",
                _json_bytes(
                    chosen_index
                ),
            )
            _add_bytes(
                handle,
                "blobs/sha256/"
                f"{top_hash}",
                top_bytes,
            )
            _add_bytes(
                handle,
                "blobs/sha256/"
                f"{platform_hash}",
                platform_bytes,
            )
            _add_bytes(
                handle,
                "blobs/sha256/"
                f"{config_hash}",
                config_bytes,
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        valid_archive = (
            root / "valid.tar"
        )
        _write_synthetic_archive(
            valid_archive
        )
        valid = _reference_manifest_from_inspect(
            valid_archive,
            image_ref,
            head,
            inspected,
        )
        # Regression for CI #3223: on the containerd-backed
        # Docker store the live image ID is the OCI index digest,
        # intentionally different from the config digest.
        if (
            valid["live_image_id"]
            == valid[
                "archive_config_digest"
            ]
        ):
            raise AssertionError(
                "synthetic fixture does not reproduce "
                "the CI #3223 identity distinction"
            )
        _validate_reference_manifest(
            valid
        )

        mismatched_image = dict(
            inspected
        )
        mismatched_image[
            "Id"
        ] = "sha256:" + "f" * 64
        mismatched_image[
            "RepoDigests"
        ] = []
        try:
            _reference_manifest_from_inspect(
                valid_archive,
                image_ref,
                head,
                mismatched_image,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "mismatched live image/archive was accepted"
            )

        try:
            _reference_manifest_from_inspect(
                valid_archive,
                image_ref,
                "b" * 40,
                inspected,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "mismatched PR head/revision was accepted"
            )

        ambiguous_archive = (
            root / "ambiguous.tar"
        )
        descriptor = (
            index["manifests"][0]
        )
        _write_synthetic_archive(
            ambiguous_archive,
            top_descriptors=[
                descriptor,
                descriptor,
            ],
        )
        try:
            _reference_manifest_from_inspect(
                ambiguous_archive,
                image_ref,
                head,
                inspected,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "ambiguous archive entry was accepted"
            )

        historical_mislabel = dict(
            valid
        )
        historical_mislabel[
            "historical_recovery"
        ] = True
        try:
            _validate_reference_manifest(
                historical_mislabel
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "historical recovery relabel was not rejected"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )
    sub.add_parser("self-test")

    target = sub.add_parser("target")
    target.add_argument(
        "--seconds",
        type=int,
        default=120,
    )

    collect = sub.add_parser("collect")
    collect.add_argument(
        "--target-pid",
        type=int,
        required=True,
    )
    collect.add_argument(
        "--container-name",
        required=True,
    )
    collect.add_argument(
        "--image-ref",
        required=True,
    )
    collect.add_argument(
        "--expected-head",
        required=True,
    )
    collect.add_argument(
        "--historical-ci-3221-digest",
        required=True,
    )
    collect.add_argument(
        "--historical-ci-3222-digest",
        required=True,
    )
    collect.add_argument(
        "--require-elevated",
        action="store_true",
    )
    collect.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    bind = sub.add_parser(
        "bind-image-archive"
    )
    bind.add_argument(
        "--archive",
        type=Path,
        required=True,
    )
    bind.add_argument(
        "--image-ref",
        required=True,
    )
    bind.add_argument(
        "--expected-head",
        required=True,
    )
    bind.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    validate_reference = sub.add_parser(
        "validate-reference"
    )
    validate_reference.add_argument(
        "--input",
        type=Path,
        required=True,
    )

    validate = sub.add_parser("validate")
    validate.add_argument(
        "--input",
        type=Path,
        required=True,
    )

    workflow = sub.add_parser(
        "validate-workflow"
    )
    workflow.add_argument(
        "--workflow",
        type=Path,
        required=True,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
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
    if args.command == "target":
        return _target_mode(args.seconds)
    if args.command == "collect":
        report = _collect(args)
        _validate_report(report)
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        args.output.write_text(
            json.dumps(
                report,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "output": str(
                        args.output
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "bind-image-archive":
        report = _reference_manifest(
            args.archive,
            args.image_ref,
            args.expected_head,
        )
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        args.output.write_text(
            json.dumps(
                report,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "schema": report["schema"],
                    "archive_sha256": report[
                        "archive_sha256"
                    ],
                    "live_image_id": report[
                        "live_image_id"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-reference":
        report = json.loads(
            args.input.read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(report, dict):
            raise ValueError(
                "diagnostic reference manifest must be a JSON object"
            )
        _validate_reference_manifest(report)
        print(
            json.dumps(
                {
                    "schema": report["schema"],
                    "validation": "PASS",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate":
        report = json.loads(
            args.input.read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(
            report,
            dict,
        ):
            raise ValueError(
                "inventory artifact must "
                "be a JSON object"
            )
        _validate_report(report)
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "validation": "PASS",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-workflow":
        _validate_workflow(
            args.workflow
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
