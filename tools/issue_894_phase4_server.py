from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import statistics
import threading
import time

import uvicorn

from app.server.runtime import ServerSettings, create_configured_app


_INTERVAL_SECONDS = 0.05
_LAGS_MS: deque[float] = deque(maxlen=500_000)
_POOL_CHECKOUT_WAIT_MS: deque[float] = deque(maxlen=500_000)
_INGRESS_TOTAL_MS: deque[float] = deque(maxlen=500_000)
_INGRESS_AT_MONOTONIC: deque[float] = deque(maxlen=500_000)
_ADMISSION_WAIT_MS: deque[float] = deque(maxlen=500_000)
_PERMIT_SERVICE_MS: deque[float] = deque(maxlen=500_000)
_POOL_MAX = {"checked_out": 0, "overflow": 0}
_STATE_LOCK = threading.Lock()
_MONITOR = {
    "active": False,
    "starts": 0,
    "stops": 0,
    "resets": 0,
    "started_monotonic": 0.0,
    "stopped_monotonic": 0.0,
    "window_started_monotonic": 0.0,
    "last_sample_monotonic": 0.0,
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * percentile))),
    )
    return float(ordered[index])


def _distribution(values: deque[float]) -> dict[str, float | int]:
    copied = list(values)
    return {
        "samples": len(copied),
        "p50": _percentile(copied, 0.50),
        "p95": _percentile(copied, 0.95),
        "p99": _percentile(copied, 0.99),
        "max": max(copied, default=0.0),
        "mean": statistics.fmean(copied) if copied else 0.0,
    }


def _server_timing_total_ms(header: str) -> float | None:
    for item in str(header or "").split(","):
        name, *parameters = item.strip().split(";")
        if name.strip().casefold() != "total":
            continue
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if separator and key.casefold() == "dur":
                try:
                    return max(0.0, float(value))
                except ValueError:
                    return None
    return None


def _request_state_ms(request, name: str) -> float:
    try:
        seconds = float(getattr(request.state, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, seconds * 1000.0)


def create_phase4_app():
    app = create_configured_app(ServerSettings.from_environment())

    factory = app.state.session_factory
    engine = factory.kw.get("bind")
    pool = engine.pool if engine is not None else None
    original_lifespan = app.router.lifespan_context
    original_do_get = getattr(pool, "_do_get", None) if pool is not None else None

    def measured_do_get():
        assert original_do_get is not None
        started = time.perf_counter()
        try:
            return original_do_get()
        finally:
            _POOL_CHECKOUT_WAIT_MS.append(
                max(0.0, (time.perf_counter() - started) * 1000.0)
            )

    async def monitor_event_loop() -> None:
        expected = time.perf_counter() + _INTERVAL_SECONDS
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            now = time.perf_counter()
            _LAGS_MS.append(max(0.0, (now - expected) * 1000.0))
            with _STATE_LOCK:
                _MONITOR["last_sample_monotonic"] = now
            expected = now + _INTERVAL_SECONDS
            if pool is not None:
                checked_out = int(pool.checkedout())
                overflow = max(0, int(pool.overflow()))
                _POOL_MAX["checked_out"] = max(
                    _POOL_MAX["checked_out"],
                    checked_out,
                )
                _POOL_MAX["overflow"] = max(
                    _POOL_MAX["overflow"],
                    overflow,
                )

    @asynccontextmanager
    async def phase4_lifespan(phase4_app):
        async with original_lifespan(phase4_app):
            if pool is not None and original_do_get is not None:
                pool._do_get = measured_do_get
            now = time.perf_counter()
            with _STATE_LOCK:
                _MONITOR["active"] = True
                _MONITOR["starts"] = int(_MONITOR["starts"]) + 1
                _MONITOR["started_monotonic"] = now
                _MONITOR["window_started_monotonic"] = now
                _MONITOR["last_sample_monotonic"] = 0.0
            monitor_task = asyncio.create_task(
                monitor_event_loop(),
                name="issue-894-phase4-loop-lag",
            )
            app.state.issue_894_phase4_monitor = monitor_task
            try:
                yield
            finally:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task
                if pool is not None and original_do_get is not None:
                    pool._do_get = original_do_get
                now = time.perf_counter()
                with _STATE_LOCK:
                    _MONITOR["active"] = False
                    _MONITOR["stops"] = int(_MONITOR["stops"]) + 1
                    _MONITOR["stopped_monotonic"] = now

    app.router.lifespan_context = phase4_lifespan

    @app.middleware("http")
    async def phase4_outer_request_timing(request, call_next):
        path = request.url.path
        feature_path = (
            path == "/api/v1/runtime/status"
            or path.startswith("/api/v1/runtime/broadcasts/")
            or path.startswith("/api/v1/admin/runtime")
        )
        if not feature_path:
            return await call_next(request)

        ingress_started = time.perf_counter()
        response = await call_next(request)
        ingress_ms = max(0.0, (time.perf_counter() - ingress_started) * 1000.0)
        service_ms = _server_timing_total_ms(response.headers.get("Server-Timing", ""))
        admission_wait_ms = (
            max(0.0, ingress_ms - service_ms)
            if service_ms is not None
            else ingress_ms
        )

        _INGRESS_TOTAL_MS.append(ingress_ms)
        _INGRESS_AT_MONOTONIC.append(ingress_started)
        _ADMISSION_WAIT_MS.append(admission_wait_ms)
        if service_ms is not None:
            _PERMIT_SERVICE_MS.append(service_ms)

        response.headers["X-RP894-Ingress-Ms"] = f"{ingress_ms:.3f}"
        response.headers["X-RP894-Admission-Wait-Ms"] = f"{admission_wait_ms:.3f}"
        if service_ms is not None:
            response.headers["X-RP894-Permit-Service-Ms"] = f"{service_ms:.3f}"
        response.headers["X-RP894-Auth-Resolver-Ms"] = (
            f"{_request_state_ms(request, 'issue_894_auth_resolver_seconds'):.3f}"
        )
        response.headers["X-RP894-Csrf-Ms"] = (
            f"{_request_state_ms(request, 'issue_894_csrf_seconds'):.3f}"
        )
        response.headers["X-RP894-Ack-Repository-Ms"] = (
            f"{_request_state_ms(request, 'issue_894_ack_repository_seconds'):.3f}"
        )
        return response

    @app.get("/__phase4__/metrics", include_in_schema=False)
    async def phase4_metrics(reset: bool = False) -> dict[str, object]:
        now = time.perf_counter()
        with _STATE_LOCK:
            monitor = dict(_MONITOR)
        window_started = float(monitor["window_started_monotonic"] or 0.0)
        coverage_seconds = max(0.0, now - window_started) if window_started else 0.0
        expected_samples = (
            coverage_seconds / _INTERVAL_SECONDS
            if coverage_seconds > 0.0
            else 0.0
        )
        lag_values = list(_LAGS_MS)
        coverage_ratio = (
            min(1.0, len(lag_values) / expected_samples)
            if expected_samples > 0.0
            else 0.0
        )
        last_sample = float(monitor["last_sample_monotonic"] or 0.0)
        payload = {
            "samples": len(lag_values),
            "monitor": {
                "active": bool(monitor["active"]),
                "starts": int(monitor["starts"]),
                "stops": int(monitor["stops"]),
                "resets": int(monitor["resets"]),
                "coverage_seconds": coverage_seconds,
                "coverage_ratio": coverage_ratio,
                "last_sample_age_ms": (
                    max(0.0, (now - last_sample) * 1000.0)
                    if last_sample
                    else None
                ),
            },
            "event_loop_lag_ms": {
                "p50": _percentile(lag_values, 0.50),
                "p95": _percentile(lag_values, 0.95),
                "p99": _percentile(lag_values, 0.99),
                "max": max(lag_values, default=0.0),
                "mean": statistics.fmean(lag_values) if lag_values else 0.0,
            },
            "pool": {
                "size": int(pool.size()) if pool is not None else 0,
                "checked_out": int(pool.checkedout()) if pool is not None else 0,
                "overflow": max(0, int(pool.overflow())) if pool is not None else 0,
                "max_checked_out": _POOL_MAX["checked_out"],
                "max_overflow": _POOL_MAX["overflow"],
                "checkout_wait_ms": _distribution(_POOL_CHECKOUT_WAIT_MS),
            },
            "admission": {
                "ingress_total_ms": _distribution(_INGRESS_TOTAL_MS),
                "arrival": {
                    "samples": len(_INGRESS_AT_MONOTONIC),
                    "spread_ms": (
                        (max(_INGRESS_AT_MONOTONIC) - min(_INGRESS_AT_MONOTONIC))
                        * 1000.0
                        if _INGRESS_AT_MONOTONIC
                        else 0.0
                    ),
                },
                "wait_ms": _distribution(_ADMISSION_WAIT_MS),
                "permit_service_ms": _distribution(_PERMIT_SERVICE_MS),
            },
        }
        if reset:
            _LAGS_MS.clear()
            _POOL_CHECKOUT_WAIT_MS.clear()
            _INGRESS_TOTAL_MS.clear()
            _INGRESS_AT_MONOTONIC.clear()
            _ADMISSION_WAIT_MS.clear()
            _PERMIT_SERVICE_MS.clear()
            _POOL_MAX["checked_out"] = 0
            _POOL_MAX["overflow"] = 0
            reset_at = time.perf_counter()
            with _STATE_LOCK:
                _MONITOR["resets"] = int(_MONITOR["resets"]) + 1
                _MONITOR["window_started_monotonic"] = reset_at
                _MONITOR["last_sample_monotonic"] = 0.0
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
