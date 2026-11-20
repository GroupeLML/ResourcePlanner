from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import statistics
import time

import uvicorn

from app.server.runtime import ServerSettings, create_configured_app


_INTERVAL_SECONDS = 0.05
_LAGS_MS: deque[float] = deque(maxlen=500_000)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * percentile))),
    )
    return float(ordered[index])


def create_phase4_app():
    app = create_configured_app(ServerSettings.from_environment())

    async def monitor_event_loop() -> None:
        expected = time.perf_counter() + _INTERVAL_SECONDS
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            now = time.perf_counter()
            _LAGS_MS.append(max(0.0, (now - expected) * 1000.0))
            expected = now + _INTERVAL_SECONDS

    @app.on_event("startup")
    async def start_phase4_monitor() -> None:
        app.state.issue_894_phase4_monitor = asyncio.create_task(
            monitor_event_loop(),
            name="issue-894-phase4-loop-lag",
        )

    @app.on_event("shutdown")
    async def stop_phase4_monitor() -> None:
        task = getattr(app.state, "issue_894_phase4_monitor", None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @app.get("/__phase4__/metrics", include_in_schema=False)
    async def phase4_metrics(reset: bool = False) -> dict[str, object]:
        values = list(_LAGS_MS)
        payload = {
            "samples": len(values),
            "event_loop_lag_ms": {
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
                "max": max(values, default=0.0),
                "mean": statistics.fmean(values) if values else 0.0,
            },
        }
        if reset:
            _LAGS_MS.clear()
        return payload

    return app


def main() -> None:
    settings = ServerSettings.from_environment()
    uvicorn.run(
        create_phase4_app(),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
