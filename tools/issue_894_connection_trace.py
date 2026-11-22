from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


_DIAGNOSTIC_ID = ContextVar("rp894_diagnostic_request_id", default="")
_ROUTE_FAMILY = ContextVar("rp894_diagnostic_route_family", default="unknown")
_EXECUTOR_PHASE = ContextVar("rp894_diagnostic_executor_phase", default="other")
_ID_RE = re.compile(r"^[a-f0-9]{16,32}$")
_STALL_WAIT_SECONDS = 1.0


def safe_diagnostic_id(candidate: object | None = None) -> str:
    value = str(candidate or "").strip().casefold()
    if _ID_RE.fullmatch(value):
        return value
    return uuid4().hex[:20]


def route_family(path: str) -> str:
    text = str(path or "")
    if text.startswith("/api/v1/planning/"):
        return "planning_read"
    if text.startswith("/api/v1/projects"):
        return "project_read"
    if text.startswith("/api/v1/resources/"):
        return "resource_save"
    if text == "/api/v1/resources":
        return "resource_collection"
    if text.startswith("/api/v1/"):
        return "other_api"
    if text in {"/health", "/ready"}:
        return text[1:]
    return "other"


@dataclass(slots=True)
class _RequestAggregate:
    request_id: str
    route_family: str
    request_started: float
    first_statement_end: float | None = None
    last_statement_end: float | None = None
    orm_materialization_count: int = 0
    first_materialization: float | None = None
    last_materialization: float | None = None


@dataclass(slots=True)
class ConnectionTraceRecorder:
    path: Path
    started: float = field(default_factory=time.perf_counter)
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    _state_lock: threading.Lock = field(default_factory=threading.Lock)
    _first_stall_lock: threading.Lock = field(default_factory=threading.Lock)
    _first_stall_captured: bool = False
    _executor_pending: int = 0
    _executor_running: int = 0
    _holders: dict[int, dict[str, object]] = field(default_factory=dict)
    _requests: dict[str, _RequestAggregate] = field(default_factory=dict)

    def _elapsed_ms(self) -> float:
        return max(0.0, (time.perf_counter() - self.started) * 1000.0)

    def record(self, kind: str, **payload: object) -> None:
        now = time.perf_counter()
        row = {
            "schema": "issue-894-connection-trace-v1",
            "kind": str(kind),
            "monotonic": now,
            "elapsed_ms": max(0.0, (now - self.started) * 1000.0),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True))
                handle.write("\n")

    def begin_request(self, request_id: str, family: str) -> tuple[object, object]:
        request_token = _DIAGNOSTIC_ID.set(request_id)
        family_token = _ROUTE_FAMILY.set(family)
        now = time.perf_counter()
        with self._state_lock:
            self._requests[request_id] = _RequestAggregate(
                request_id=request_id,
                route_family=family,
                request_started=now,
            )
        self.record(
            "request_start",
            request_id=request_id,
            route_family=family,
            thread_id=threading.get_ident(),
        )
        return request_token, family_token

    def end_request(
        self,
        request_id: str,
        *,
        status: str,
        error_type: str = "",
    ) -> None:
        now = time.perf_counter()
        with self._state_lock:
            aggregate = self._requests.pop(request_id, None)
        payload: dict[str, object] = {
            "request_id": request_id,
            "route_family": (
                aggregate.route_family if aggregate is not None else _ROUTE_FAMILY.get()
            ),
            "status": status,
            "error_type": error_type,
            "thread_id": threading.get_ident(),
        }
        if aggregate is not None:
            payload["server_request_ms"] = max(
                0.0,
                (now - aggregate.request_started) * 1000.0,
            )
            payload["orm_materialization_count"] = aggregate.orm_materialization_count
            if aggregate.first_materialization is not None:
                payload["first_materialization_ms"] = max(
                    0.0,
                    (aggregate.first_materialization - aggregate.request_started) * 1000.0,
                )
                if aggregate.first_statement_end is not None:
                    payload["statement_end_to_first_materialization_ms"] = max(
                        0.0,
                        (
                            aggregate.first_materialization
                            - aggregate.first_statement_end
                        )
                        * 1000.0,
                    )
            if aggregate.last_materialization is not None:
                payload["last_materialization_ms"] = max(
                    0.0,
                    (aggregate.last_materialization - aggregate.request_started) * 1000.0,
                )
                if aggregate.first_materialization is not None:
                    payload["materialization_span_ms"] = max(
                        0.0,
                        (
                            aggregate.last_materialization
                            - aggregate.first_materialization
                        )
                        * 1000.0,
                    )
                payload["last_materialization_to_request_end_ms"] = max(
                    0.0,
                    (now - aggregate.last_materialization) * 1000.0,
                )
            if aggregate.last_statement_end is not None:
                payload["last_statement_end_to_request_end_ms"] = max(
                    0.0,
                    (now - aggregate.last_statement_end) * 1000.0,
                )
        self.record("request_end", **payload)

    def reset_request_context(self, request_token: object, family_token: object) -> None:
        _DIAGNOSTIC_ID.reset(request_token)
        _ROUTE_FAMILY.reset(family_token)

    def set_executor_phase(self, phase: str) -> object:
        return _EXECUTOR_PHASE.set(str(phase))

    def reset_executor_phase(self, token: object) -> None:
        _EXECUTOR_PHASE.reset(token)

    async def run_sync(
        self,
        original: Callable[..., Any],
        func: Callable[..., Any],
        /,
        *args: object,
        **kwargs: object,
    ) -> Any:
        request_id = _DIAGNOSTIC_ID.get()
        family = _ROUTE_FAMILY.get()
        phase = _EXECUTOR_PHASE.get()
        submitted = time.perf_counter()
        with self._state_lock:
            self._executor_pending += 1
            pending = self._executor_pending
            running = self._executor_running
        self.record(
            "executor_submit",
            request_id=request_id,
            route_family=family,
            phase=phase,
            pending=pending,
            running=running,
            thread_id=threading.get_ident(),
        )

        def wrapped() -> Any:
            worker_started = time.perf_counter()
            with self._state_lock:
                self._executor_pending -= 1
                self._executor_running += 1
                pending_now = self._executor_pending
                running_now = self._executor_running
            self.record(
                "executor_worker_start",
                request_id=request_id,
                route_family=family,
                phase=phase,
                queue_wait_ms=max(0.0, (worker_started - submitted) * 1000.0),
                pending=pending_now,
                running=running_now,
                thread_id=threading.get_ident(),
            )
            try:
                return func(*args, **kwargs)
            finally:
                worker_done = time.perf_counter()
                with self._state_lock:
                    self._executor_running -= 1
                    pending_done = self._executor_pending
                    running_done = self._executor_running
                self.record(
                    "executor_worker_complete",
                    request_id=request_id,
                    route_family=family,
                    phase=phase,
                    worker_ms=max(0.0, (worker_done - worker_started) * 1000.0),
                    submitted_to_complete_ms=max(
                        0.0,
                        (worker_done - submitted) * 1000.0,
                    ),
                    pending=pending_done,
                    running=running_done,
                    thread_id=threading.get_ident(),
                )

        try:
            value = await original(wrapped)
        except asyncio.CancelledError:
            self.record(
                "executor_coroutine_cancelled_after_drain",
                request_id=request_id,
                route_family=family,
                phase=phase,
                coroutine_ms=max(0.0, (time.perf_counter() - submitted) * 1000.0),
                thread_id=threading.get_ident(),
            )
            raise
        self.record(
            "executor_coroutine_resume",
            request_id=request_id,
            route_family=family,
            phase=phase,
            coroutine_ms=max(0.0, (time.perf_counter() - submitted) * 1000.0),
            thread_id=threading.get_ident(),
        )
        return value

    def pool_checkout_request(self) -> float:
        started = time.perf_counter()
        self.record(
            "pool_checkout_request",
            request_id=_DIAGNOSTIC_ID.get(),
            route_family=_ROUTE_FAMILY.get(),
            phase=_EXECUTOR_PHASE.get(),
            thread_id=threading.get_ident(),
        )
        return started

    def pool_checkout_complete(self, started: float) -> float:
        wait_seconds = max(0.0, time.perf_counter() - started)
        self.record(
            "pool_checkout_complete",
            request_id=_DIAGNOSTIC_ID.get(),
            route_family=_ROUTE_FAMILY.get(),
            phase=_EXECUTOR_PHASE.get(),
            wait_ms=wait_seconds * 1000.0,
            thread_id=threading.get_ident(),
        )
        if wait_seconds >= _STALL_WAIT_SECONDS:
            self.capture_first_stall(
                trigger="pool_checkout_wait",
                trigger_value_ms=wait_seconds * 1000.0,
            )
        return wait_seconds

    def on_checkout(self, connection_record: object) -> None:
        now = time.perf_counter()
        request_id = _DIAGNOSTIC_ID.get()
        holder = {
            "request_id": request_id,
            "route_family": _ROUTE_FAMILY.get(),
            "phase": _EXECUTOR_PHASE.get(),
            "acquired": now,
            "thread_id": threading.get_ident(),
            "last_statement_end": None,
            "last_statement_fingerprint": "",
            "current_statement_fingerprint": "",
            "statement_count": 0,
        }
        with self._state_lock:
            self._holders[id(connection_record)] = holder
        self.record(
            "connection_acquired",
            request_id=request_id,
            route_family=holder["route_family"],
            phase=holder["phase"],
            connection_id=id(connection_record),
            thread_id=holder["thread_id"],
        )

    def on_statement_start(self, connection: object, statement: object) -> None:
        request_id = _DIAGNOSTIC_ID.get()
        fingerprint = _statement_fingerprint(statement)
        connection_id = _connection_record_id(connection)
        with self._state_lock:
            holder = self._holders.get(connection_id)
            if holder is not None:
                holder["current_statement_fingerprint"] = fingerprint
        self.record(
            "statement_start",
            request_id=request_id,
            route_family=_ROUTE_FAMILY.get(),
            phase=_EXECUTOR_PHASE.get(),
            connection_id=connection_id,
            statement_fingerprint=fingerprint,
            thread_id=threading.get_ident(),
        )

    def on_statement_end(
        self,
        connection: object,
        statement: object,
        started: float,
        *,
        error: bool = False,
    ) -> None:
        now = time.perf_counter()
        connection_id = _connection_record_id(connection)
        request_id = _DIAGNOSTIC_ID.get()
        with self._state_lock:
            holder = self._holders.get(connection_id)
            if holder is not None:
                holder["last_statement_end"] = now
                holder["last_statement_fingerprint"] = _statement_fingerprint(statement)
                holder["current_statement_fingerprint"] = ""
                holder["statement_count"] = int(holder.get("statement_count") or 0) + 1
            aggregate = self._requests.get(request_id)
            if aggregate is not None:
                if aggregate.first_statement_end is None:
                    aggregate.first_statement_end = now
                aggregate.last_statement_end = now
        self.record(
            "statement_end",
            request_id=request_id,
            route_family=_ROUTE_FAMILY.get(),
            phase=_EXECUTOR_PHASE.get(),
            connection_id=connection_id,
            statement_fingerprint=_statement_fingerprint(statement),
            statement_ms=max(0.0, (now - started) * 1000.0),
            error=bool(error),
            thread_id=threading.get_ident(),
        )

    def on_transaction_end(self, connection: object, kind: str) -> None:
        self.record(
            "transaction_end",
            request_id=_DIAGNOSTIC_ID.get(),
            route_family=_ROUTE_FAMILY.get(),
            phase=_EXECUTOR_PHASE.get(),
            connection_id=_connection_record_id(connection),
            transaction_kind=str(kind),
            thread_id=threading.get_ident(),
        )

    def on_checkin(self, connection_record: object) -> None:
        now = time.perf_counter()
        connection_id = id(connection_record)
        with self._state_lock:
            holder = self._holders.pop(connection_id, None)
        if holder is None:
            self.record(
                "connection_return",
                request_id="",
                route_family="unknown",
                connection_id=connection_id,
                holding_ms=None,
                thread_id=threading.get_ident(),
            )
            return
        acquired = float(holder["acquired"])
        last_statement_end = holder.get("last_statement_end")
        payload = {
            "request_id": str(holder.get("request_id") or ""),
            "route_family": str(holder.get("route_family") or "unknown"),
            "phase": str(holder.get("phase") or "other"),
            "connection_id": connection_id,
            "holding_ms": max(0.0, (now - acquired) * 1000.0),
            "statement_count": int(holder.get("statement_count") or 0),
            "thread_id": threading.get_ident(),
        }
        if isinstance(last_statement_end, float):
            payload["post_statement_connection_residence_ms"] = max(
                0.0,
                (now - last_statement_end) * 1000.0,
            )
        self.record("connection_return", **payload)

    def on_materialized(self) -> None:
        request_id = _DIAGNOSTIC_ID.get()
        if not request_id:
            return
        now = time.perf_counter()
        with self._state_lock:
            aggregate = self._requests.get(request_id)
            if aggregate is None:
                return
            aggregate.orm_materialization_count += 1
            if aggregate.first_materialization is None:
                aggregate.first_materialization = now
            aggregate.last_materialization = now

    def capture_first_stall(self, *, trigger: str, trigger_value_ms: float) -> None:
        with self._first_stall_lock:
            if self._first_stall_captured:
                return
            self._first_stall_captured = True
        now = time.perf_counter()
        with self._state_lock:
            holders = [
                {
                    "connection_id": connection_id,
                    "request_id": str(holder.get("request_id") or ""),
                    "route_family": str(holder.get("route_family") or "unknown"),
                    "phase": str(holder.get("phase") or "other"),
                    "holding_ms": max(
                        0.0,
                        (now - float(holder["acquired"])) * 1000.0,
                    ),
                    "statement_count": int(holder.get("statement_count") or 0),
                    "current_statement_fingerprint": str(
                        holder.get("current_statement_fingerprint") or ""
                    ),
                    "last_statement_fingerprint": str(
                        holder.get("last_statement_fingerprint") or ""
                    ),
                }
                for connection_id, holder in self._holders.items()
            ]
            executor = {
                "pending": self._executor_pending,
                "running": self._executor_running,
            }
        holders.sort(key=lambda row: float(row["holding_ms"]), reverse=True)
        frames = sys._current_frames()
        thread_dump = []
        for thread in list(threading.enumerate())[:64]:
            frame = frames.get(thread.ident or -1)
            if frame is None:
                continue
            thread_dump.append(
                {
                    "thread_id": thread.ident,
                    "thread_name": thread.name,
                    "stack": traceback.format_stack(frame, limit=12),
                }
            )
        self.record(
            "first_stall_snapshot",
            trigger=str(trigger),
            trigger_value_ms=max(0.0, float(trigger_value_ms)),
            executor=executor,
            holders=holders[:32],
            thread_dump=thread_dump,
        )

    def summary(self) -> dict[str, object]:
        with self._state_lock:
            holders = len(self._holders)
            pending = self._executor_pending
            running = self._executor_running
        return {
            "trace_path": str(self.path),
            "first_stall_captured": self._first_stall_captured,
            "active_holders": holders,
            "executor_pending": pending,
            "executor_running": running,
        }


def _statement_fingerprint(statement: object) -> str:
    import hashlib

    normalized = " ".join(str(statement or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _connection_record_id(connection: object) -> int:
    try:
        fairy = connection.connection
        record = fairy._connection_record
        return id(record)
    except Exception:
        return 0


def install_connection_trace(app: Any, engine: Engine, recorder: ConnectionTraceRecorder) -> Callable[[], None]:
    from app.server import security as security_module

    original_resolve = security_module.resolve_principal
    original_csrf = security_module._csrf_allowed
    original_run_sync = security_module.run_sync_cancellation_safe

    async def traced_resolve(*args: object, **kwargs: object) -> Any:
        token = recorder.set_executor_phase("auth")
        try:
            return await original_resolve(*args, **kwargs)
        finally:
            recorder.reset_executor_phase(token)

    async def traced_csrf(*args: object, **kwargs: object) -> Any:
        token = recorder.set_executor_phase("csrf")
        try:
            return await original_csrf(*args, **kwargs)
        finally:
            recorder.reset_executor_phase(token)

    async def traced_run_sync(func: Callable[..., Any], /, *args: object, **kwargs: object) -> Any:
        return await recorder.run_sync(original_run_sync, func, *args, **kwargs)

    security_module.resolve_principal = traced_resolve
    security_module._csrf_allowed = traced_csrf
    security_module.run_sync_cancellation_safe = traced_run_sync

    @event.listens_for(engine.pool, "checkout")
    def _checkout(_dbapi_connection, connection_record, _connection_proxy) -> None:
        recorder.on_checkout(connection_record)

    @event.listens_for(engine.pool, "checkin")
    def _checkin(_dbapi_connection, connection_record) -> None:
        recorder.on_checkin(connection_record)

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn,
        _cursor,
        statement,
        _parameters,
        execution_context,
        _executemany,
    ) -> None:
        started = time.perf_counter()
        execution_context._rp894_trace_started = started
        recorder.on_statement_start(conn, statement)

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn,
        _cursor,
        statement,
        _parameters,
        execution_context,
        _executemany,
    ) -> None:
        started = getattr(execution_context, "_rp894_trace_started", time.perf_counter())
        recorder.on_statement_end(conn, statement, started)
        execution_context._rp894_trace_started = None

    @event.listens_for(engine, "handle_error")
    def _handle_error(exception_context) -> None:
        execution_context = exception_context.execution_context
        if execution_context is None:
            return
        started = getattr(execution_context, "_rp894_trace_started", time.perf_counter())
        recorder.on_statement_end(
            exception_context.connection,
            exception_context.statement or "",
            started,
            error=True,
        )
        execution_context._rp894_trace_started = None

    @event.listens_for(engine, "commit")
    def _commit(conn) -> None:
        recorder.on_transaction_end(conn, "commit")

    @event.listens_for(engine, "rollback")
    def _rollback(conn) -> None:
        recorder.on_transaction_end(conn, "rollback")

    @event.listens_for(Session, "loaded_as_persistent")
    def _loaded_as_persistent(_session, _instance) -> None:
        recorder.on_materialized()

    def uninstall() -> None:
        security_module.resolve_principal = original_resolve
        security_module._csrf_allowed = original_csrf
        security_module.run_sync_cancellation_safe = original_run_sync
        event.remove(engine.pool, "checkout", _checkout)
        event.remove(engine.pool, "checkin", _checkin)
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
        event.remove(engine, "after_cursor_execute", _after_cursor_execute)
        event.remove(engine, "handle_error", _handle_error)
        event.remove(engine, "commit", _commit)
        event.remove(engine, "rollback", _rollback)
        event.remove(Session, "loaded_as_persistent", _loaded_as_persistent)

    return uninstall


def request_context(recorder: ConnectionTraceRecorder, request_id: str, family: str):
    class _Context:
        def __enter__(self):
            self.tokens = recorder.begin_request(request_id, family)
            return self

        def __exit__(self, exc_type, exc, _tb):
            status = "500" if exc is not None else "completed"
            recorder.end_request(
                request_id,
                status=status,
                error_type=type(exc).__name__ if exc is not None else "",
            )
            recorder.reset_request_context(*self.tokens)
            return False

    return _Context()
