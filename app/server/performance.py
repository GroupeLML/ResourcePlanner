from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Iterator
from weakref import WeakSet

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, event
from starlette.routing import Match


_PERFORMANCE_LOG_MAX_BYTES = 1_000_000
_PERFORMANCE_LOG_BACKUPS = 3
_DB_SLOW_QUERY_SECONDS = 0.250
_DB_N_PLUS_ONE_REPEAT_THRESHOLD = 5
_ALLOWED_PHASES = {"auth", "compute", "serialization", "external"}


@dataclass(frozen=True, slots=True)
class ServerPerformanceSample:
    operation: str
    status: str
    total_seconds: float
    auth_seconds: float = 0.0
    api_seconds: float = 0.0
    db_seconds: float = 0.0
    compute_seconds: float = 0.0
    serialization_seconds: float = 0.0
    external_seconds: float = 0.0
    db_query_count: int = 0
    db_select_count: int = 0
    db_repeated_query_max: int = 0
    db_slow_query_count: int = 0
    db_slowest_query_seconds: float = 0.0
    db_n_plus_one_suspected: bool = False
    external_call_count: int = 0
    external_item_count: int = 0
    engine: str = "fastapi-v2"
    error_type: str = ""
    timestamp_utc: str = ""

    def normalized(self) -> "ServerPerformanceSample":
        return ServerPerformanceSample(
            operation=str(self.operation or "unknown"),
            status=str(self.status or "unknown"),
            total_seconds=_seconds(self.total_seconds),
            auth_seconds=_seconds(self.auth_seconds),
            api_seconds=_seconds(self.api_seconds),
            db_seconds=_seconds(self.db_seconds),
            compute_seconds=_seconds(self.compute_seconds),
            serialization_seconds=_seconds(self.serialization_seconds),
            external_seconds=_seconds(self.external_seconds),
            db_query_count=max(int(self.db_query_count), 0),
            db_select_count=max(int(self.db_select_count), 0),
            db_repeated_query_max=max(int(self.db_repeated_query_max), 0),
            db_slow_query_count=max(int(self.db_slow_query_count), 0),
            db_slowest_query_seconds=_seconds(self.db_slowest_query_seconds),
            db_n_plus_one_suspected=bool(self.db_n_plus_one_suspected),
            external_call_count=max(int(self.external_call_count), 0),
            external_item_count=max(int(self.external_item_count), 0),
            engine="fastapi-v2",
            error_type=str(self.error_type or ""),
            timestamp_utc=self.timestamp_utc
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())


def _seconds(value: object) -> float:
    try:
        return round(max(float(value or 0.0), 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _default_performance_log_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    root = (
        Path(local_app_data) / "RessourcePlanner"
        if local_app_data
        else Path.home() / ".resourceplanner"
    )
    return root / "logs" / "performance.jsonl"


def _rotate_log(path: Path) -> None:
    if not path.exists():
        return
    try:
        if path.stat().st_size < _PERFORMANCE_LOG_MAX_BYTES:
            return
    except OSError:
        return
    oldest = path.with_name(f"{path.name}.{_PERFORMANCE_LOG_BACKUPS}")
    try:
        if oldest.exists():
            oldest.unlink()
    except OSError:
        pass
    for index in range(_PERFORMANCE_LOG_BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if not source.exists():
            continue
        try:
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
        except OSError:
            pass
    try:
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def _append_performance_sample(
    sample: ServerPerformanceSample, *, path: Path | None = None
) -> None:
    target = path or _default_performance_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    except OSError:
        return


@dataclass(slots=True)
class RequestPerformanceContext:
    durations: dict[str, float] = field(default_factory=dict)
    db_query_count: int = 0
    db_select_count: int = 0
    db_slow_query_count: int = 0
    db_slowest_query_seconds: float = 0.0
    select_fingerprints: Counter[str] = field(default_factory=Counter)
    external_call_count: int = 0
    external_item_count: int = 0

    @property
    def recorded_seconds(self) -> float:
        return sum(self.durations.values())

    def add_duration(self, phase: str, seconds: float) -> None:
        self.durations[phase] = self.durations.get(phase, 0.0) + max(float(seconds), 0.0)

    def record_query(self, statement: str, seconds: float) -> None:
        duration = max(float(seconds), 0.0)
        self.db_query_count += 1
        self.add_duration("db", duration)
        self.db_slowest_query_seconds = max(self.db_slowest_query_seconds, duration)
        if duration >= _DB_SLOW_QUERY_SECONDS:
            self.db_slow_query_count += 1

        normalized = " ".join(str(statement or "").split()).casefold()
        if not normalized.startswith("select"):
            return
        self.db_select_count += 1
        # Keep only a one-way technical fingerprint. SQL text/parameters never enter logs.
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        self.select_fingerprints[fingerprint] += 1

    @property
    def db_repeated_query_max(self) -> int:
        return max(self.select_fingerprints.values(), default=0)

    @property
    def db_n_plus_one_suspected(self) -> bool:
        return self.db_repeated_query_max >= _DB_N_PLUS_ONE_REPEAT_THRESHOLD

    def to_sample(
        self,
        *,
        operation: str,
        status: str,
        total_seconds: float,
        error_type: str = "",
    ) -> ServerPerformanceSample:
        total = max(float(total_seconds), 0.0)
        auth = self.durations.get("auth", 0.0)
        db = self.durations.get("db", 0.0)
        compute = self.durations.get("compute", 0.0)
        serialization = self.durations.get("serialization", 0.0)
        external = self.durations.get("external", 0.0)
        api = max(total - auth - db - compute - serialization - external, 0.0)
        return ServerPerformanceSample(
            operation=operation,
            status=status,
            total_seconds=total,
            auth_seconds=auth,
            api_seconds=api,
            db_seconds=db,
            compute_seconds=compute,
            serialization_seconds=serialization,
            external_seconds=external,
            db_query_count=self.db_query_count,
            db_select_count=self.db_select_count,
            db_repeated_query_max=self.db_repeated_query_max,
            db_slow_query_count=self.db_slow_query_count,
            db_slowest_query_seconds=self.db_slowest_query_seconds,
            db_n_plus_one_suspected=self.db_n_plus_one_suspected,
            external_call_count=self.external_call_count,
            external_item_count=self.external_item_count,
            engine="fastapi-v2",
            error_type=error_type,
        )


_CURRENT_REQUEST: ContextVar[RequestPerformanceContext | None] = ContextVar(
    "resourceplanner_request_performance",
    default=None,
)
_INSTRUMENTED_ENGINES: WeakSet[Engine] = WeakSet()


def current_request_performance() -> RequestPerformanceContext | None:
    return _CURRENT_REQUEST.get()


@contextmanager
def performance_phase(phase: str) -> Iterator[None]:
    """Measure exclusive request time for a known technical phase.

    Durations already recorded by nested phases (for example DB inside compute) are
    subtracted so the request buckets can be added without double-counting.
    """
    if phase not in _ALLOWED_PHASES:
        raise ValueError(f"Unsupported performance phase: {phase}")
    context = current_request_performance()
    if context is None:
        yield
        return

    nested_before = context.recorded_seconds
    started = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - started
        nested_seconds = max(context.recorded_seconds - nested_before, 0.0)
        context.add_duration(phase, max(elapsed - nested_seconds, 0.0))


def record_external_call() -> None:
    context = current_request_performance()
    if context is not None:
        context.external_call_count += 1


def record_external_items(item_count: int) -> None:
    context = current_request_performance()
    if context is not None:
        context.external_item_count += max(int(item_count), 0)


def _safe_operation(request: Request, app: Any) -> str:
    raw_path = request.url.path
    if raw_path in {"/health", "/ready"}:
        return f"http {request.method.upper()} {raw_path}"

    route = request.scope.get("route")
    template = str(getattr(route, "path", "") or "")
    if not template.startswith("/api/v1/"):
        # Authorization can short-circuit before Starlette stores the matched route in
        # the scope. Resolve the route pattern without persisting path parameters.
        for candidate in getattr(app, "routes", ()):
            try:
                match, _child_scope = candidate.matches(request.scope)
            except (AttributeError, TypeError):
                continue
            if match is Match.FULL:
                candidate_path = str(getattr(candidate, "path", "") or "")
                if candidate_path.startswith("/api/v1/"):
                    template = candidate_path
                    break
    if not template.startswith("/api/v1/"):
        # Never persist unmatched raw API paths: they may contain business identifiers.
        template = "/api/v1/<unmatched>"
    return f"http {request.method.upper()} {template}"


def _server_timing(sample: ServerPerformanceSample) -> str:
    timings = (
        ("auth", sample.auth_seconds),
        ("api", sample.api_seconds),
        ("db", sample.db_seconds),
        ("compute", sample.compute_seconds),
        ("serialization", sample.serialization_seconds),
        ("external", sample.external_seconds),
        ("total", sample.total_seconds),
    )
    return ", ".join(f"{name};dur={seconds * 1000.0:.3f}" for name, seconds in timings)


def install_performance_middleware(app: Any, *, log_path: Path | None = None) -> None:
    @app.middleware("http")
    async def collect_performance(request: Request, call_next):
        raw_path = request.url.path
        if raw_path not in {"/health", "/ready"} and not raw_path.startswith("/api/v1/"):
            return await call_next(request)

        context = RequestPerformanceContext()
        token = _CURRENT_REQUEST.set(context)
        started = perf_counter()
        status = "500"
        error_type = ""
        response = None
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            total = perf_counter() - started
            sample = context.to_sample(
                operation=_safe_operation(request, app),
                status=status,
                total_seconds=total,
                error_type=error_type,
            ).normalized()
            if response is not None:
                response.headers["Server-Timing"] = _server_timing(sample)
            _append_performance_sample(sample, path=log_path)
            _CURRENT_REQUEST.reset(token)


class InstrumentedJSONResponse(JSONResponse):
    """Measure JSON encoding separately from endpoint/domain work."""

    def render(self, content: Any) -> bytes:
        with performance_phase("serialization"):
            return super().render(content)


def install_sql_performance_instrumentation(engine: Engine) -> None:
    """Attach request-aware SQL timings without logging SQL text or parameters."""
    if engine in _INSTRUMENTED_ENGINES:
        return
    _INSTRUMENTED_ENGINES.add(engine)

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        _conn,
        _cursor,
        _statement,
        _parameters,
        execution_context,
        _executemany,
    ) -> None:
        if current_request_performance() is not None:
            execution_context._resourceplanner_perf_started = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        _conn,
        _cursor,
        statement,
        _parameters,
        execution_context,
        _executemany,
    ) -> None:
        context = current_request_performance()
        started = getattr(execution_context, "_resourceplanner_perf_started", None)
        if context is None or started is None:
            return
        context.record_query(statement, perf_counter() - started)
        execution_context._resourceplanner_perf_started = None

    @event.listens_for(engine, "handle_error")
    def _handle_error(exception_context) -> None:
        context = current_request_performance()
        execution_context = exception_context.execution_context
        started = getattr(execution_context, "_resourceplanner_perf_started", None)
        if context is None or started is None:
            return
        context.record_query(exception_context.statement or "", perf_counter() - started)
        execution_context._resourceplanner_perf_started = None


def read_performance_samples(
    *,
    path: Path | None = None,
    limit: int = 20,
    backups: int = _PERFORMANCE_LOG_BACKUPS,
) -> list[dict[str, Any]]:
    """Read recent technical samples across the active JSONL file and rotations."""

    target = path or _default_performance_log_path()
    if limit <= 0:
        return []

    # Rotation uses .1 as the newest archived file. Read oldest -> newest so
    # slicing the tail preserves chronological order across a rotation boundary.
    candidates = [
        target.with_name(f"{target.name}.{index}")
        for index in range(max(int(backups), 0), 0, -1)
    ]
    candidates.append(target)

    lines: list[str] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            lines.extend(candidate.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue

    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            result.append(parsed)
    return result[-limit:]


def _percentile(values: Iterable[object], percentile: float) -> float:
    numeric = sorted(_seconds(value) for value in values)
    if not numeric:
        return 0.0
    if len(numeric) == 1:
        return numeric[0]
    rank = (len(numeric) - 1) * min(max(float(percentile), 0.0), 1.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return numeric[lower]
    weight = rank - lower
    return round(numeric[lower] * (1.0 - weight) + numeric[upper] * weight, 6)


def format_http_percentile_report(samples: Iterable[dict[str, Any]]) -> str:
    """Aggregate safe HTTP latency percentiles by route template."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in samples:
        operation = str(row.get("operation") or "")
        if not operation.startswith("http "):
            continue
        groups.setdefault(operation, []).append(row)
    if not groups:
        return ""

    lines = [
        "FastAPI V2 — percentiles HTTP",
        "operation | n | p50 | p95 | p99 | auth p95 | api p95 | db p95 | compute p95 | serialization p95 | external p95 | db queries p95 | n+1 suspect",
    ]
    for operation in sorted(groups):
        rows = groups[operation]
        total = [row.get("total_seconds", 0.0) for row in rows]
        lines.append(
            "{operation} | {count} | {p50:.3f}s | {p95:.3f}s | {p99:.3f}s | "
            "{auth:.3f}s | {api:.3f}s | {db:.3f}s | {compute:.3f}s | {serialization:.3f}s | "
            "{external:.3f}s | {queries:.1f} | {n_plus_one}".format(
                operation=operation,
                count=len(rows),
                p50=_percentile(total, 0.50),
                p95=_percentile(total, 0.95),
                p99=_percentile(total, 0.99),
                auth=_percentile((row.get("auth_seconds", 0.0) for row in rows), 0.95),
                api=_percentile((row.get("api_seconds", 0.0) for row in rows), 0.95),
                db=_percentile((row.get("db_seconds", 0.0) for row in rows), 0.95),
                compute=_percentile((row.get("compute_seconds", 0.0) for row in rows), 0.95),
                serialization=_percentile((row.get("serialization_seconds", 0.0) for row in rows), 0.95),
                external=_percentile((row.get("external_seconds", 0.0) for row in rows), 0.95),
                queries=_percentile((row.get("db_query_count", 0.0) for row in rows), 0.95),
                n_plus_one=sum(bool(row.get("db_n_plus_one_suspected")) for row in rows),
            )
        )
    return "\n".join(lines)
