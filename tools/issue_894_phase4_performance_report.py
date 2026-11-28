from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from app.server.performance import read_performance_samples


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate #894 bounded performance samples.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    samples = read_performance_samples(path=args.input, limit=500_000)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("operation") or "unknown")].append(sample)

    operations: dict[str, object] = {}
    for operation, rows in sorted(grouped.items()):
        totals = [float(row.get("total_seconds") or 0.0) * 1000.0 for row in rows]
        auth = [float(row.get("auth_seconds") or 0.0) * 1000.0 for row in rows]
        db = [float(row.get("db_seconds") or 0.0) * 1000.0 for row in rows]
        query_counts = [int(row.get("db_query_count") or 0) for row in rows]
        statuses: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        operations[operation] = {
            "samples": len(rows),
            "statuses": statuses,
            "total_ms": {
                "p50": _percentile(totals, 0.50),
                "p95": _percentile(totals, 0.95),
                "p99": _percentile(totals, 0.99),
            },
            "auth_ms": {
                "p95": _percentile(auth, 0.95),
                "p99": _percentile(auth, 0.99),
            },
            "db_ms": {
                "p95": _percentile(db, 0.95),
                "p99": _percentile(db, 0.99),
            },
            "db_query_count": {
                "sum": sum(query_counts),
                "max": max(query_counts, default=0),
                "mean": (
                    sum(query_counts) / len(query_counts)
                    if query_counts
                    else 0.0
                ),
            },
        }

    payload = {
        "schema": "issue-894-phase4-performance-samples-v1",
        "sample_count": len(samples),
        "operations": operations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"sample_count": len(samples), "operations": len(operations)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
