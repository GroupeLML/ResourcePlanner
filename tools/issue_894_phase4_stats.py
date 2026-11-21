from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


_MEM_RE = re.compile(r"^\s*([0-9.]+)\s*([KMGT]?i?B)\s*$", re.IGNORECASE)
_FACTORS = {
    "B": 1,
    "KB": 1000,
    "KIB": 1024,
    "MB": 1000**2,
    "MIB": 1024**2,
    "GB": 1000**3,
    "GIB": 1024**3,
    "TB": 1000**4,
    "TIB": 1024**4,
}


def _bytes(value: str) -> int:
    match = _MEM_RE.match(value.strip())
    if match is None:
        raise ValueError(f"Unsupported memory value: {value!r}")
    return int(float(match.group(1)) * _FACTORS[match.group(2).upper()])


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize #894 Docker load samples.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allocated-cpus", type=float, required=True)
    args = parser.parse_args()
    if args.allocated_cpus <= 0:
        raise SystemExit("--allocated-cpus must be positive")

    samples: list[dict[str, float | int]] = []
    for raw in args.input.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        epoch, cpu_text, memory_text = raw.split("\t", 2)
        usage = memory_text.split("/", 1)[0].strip()
        samples.append(
            {
                "epoch": float(epoch),
                "cpu_raw_percent": float(cpu_text.rstrip("%")),
                "cpu_allocated_percent": (
                    float(cpu_text.rstrip("%")) / args.allocated_cpus
                ),
                "memory_bytes": _bytes(usage),
            }
        )

    rolling: list[float] = []
    for index, sample in enumerate(samples):
        cutoff = float(sample["epoch"]) - 60.0
        window = [
            float(item["cpu_allocated_percent"])
            for item in samples[: index + 1]
            if float(item["epoch"]) >= cutoff
        ]
        if window:
            rolling.append(sum(window) / len(window))

    payload = {
        "schema": "issue-894-phase4-container-stats-v1",
        "allocated_cpus": args.allocated_cpus,
        "samples": len(samples),
        "max_cpu_allocated_percent_rolling_60s": max(rolling, default=0.0),
        "max_memory_bytes": max(
            (int(item["memory_bytes"]) for item in samples),
            default=0,
        ),
    }
    payload["cpu_acceptance_passed"] = (
        payload["max_cpu_allocated_percent_rolling_60s"] <= 70.0
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["cpu_acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
