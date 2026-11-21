from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import random
import socket
import statistics
import time
from typing import Any
from uuid import UUID, uuid5

import httpx
from sqlalchemy import func, inspect, select, text, update

from app.application.security import ROLE_ADMIN, ROLE_TECHNICIAN
from app.infrastructure.sql import (
    AppUser,
    Base,
    SqlAuthSessionRepository,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.models import (
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
)
from app.infrastructure.sql.emergency_broadcast_models import (
    EmergencyBroadcast,
    EmergencyBroadcastAcknowledgment,
)
from app.infrastructure.sql.emergency_broadcast_repository import (
    SqlEmergencyBroadcastRepository,
)
from tools.benchmark_v2_api import DATASETS, _seed


ISOLATION_MARKER = "ISSUE_894_DISPOSABLE_SQLSERVER"
CLIENT_COUNTS = (20, 50, 100, 250, 500)
STATES = ("no_active", "pending", "mostly_acknowledged")
DEFAULT_WARMUP_SECONDS = 120
DEFAULT_MEASURE_SECONDS = 600
DEFAULT_ACK_REPEATS = 20
ORDINARY_RPS = 10.0
NAMESPACE = UUID("6db35556-15ef-4e58-af35-90d69bb23966")


@dataclass(frozen=True, slots=True)
class Credential:
    user_id: str
    session: str
    csrf: str
    admin: bool = False


def _require_isolated_database(database_url: str) -> None:
    if os.environ.get("RESOURCEPLANNER_PHASE4_ISOLATED_LOAD") != ISOLATION_MARKER:
        raise SystemExit("Refusing #894 load without the disposable-load isolation marker.")
    if "ResourcePlanner894Load" not in database_url:
        raise SystemExit("Refusing #894 load outside the ResourcePlanner894Load database.")


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return float(ordered[index])


def _latencies(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values, default=0.0),
        "mean_ms": statistics.fmean(values) if values else 0.0,
    }


def _headers(credential: Credential, *, csrf: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = credential.csrf
        headers["Origin"] = "http://frontend:8080"
    return headers


def _cookies(credential: Credential) -> dict[str, str]:
    return {
        "resourceplanner_session": credential.session,
        "resourceplanner_csrf": credential.csrf,
    }


def _load_credentials(path: Path) -> tuple[Credential, list[Credential]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    admin = Credential(**payload["admin"])
    users = [Credential(**item) for item in payload["users"]]
    return admin, users


def _seed_sessions(database_url: str, output: Path, count: int) -> None:
    _require_isolated_database(database_url)
    _seed(database_url, DATASETS["medium"])
    engine = create_sql_engine(database_url)
    factory = create_session_factory(engine)
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    admin_user_id = str(uuid5(NAMESPACE, "admin"))
    admin_token = "rp894-admin-session"
    admin_csrf = "rp894-admin-csrf"
    credentials: list[Credential] = []
    try:
        with factory.begin() as session:
            session.add(
                AppUser(
                    id=admin_user_id,
                    issuer="urn:resourceplanner:phase4",
                    subject="admin",
                    display_name="Administrateur charge 894",
                    email=None,
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )
            session.flush()
            SqlAuthSessionRepository(session).create_session(
                raw_token=admin_token,
                csrf_token=admin_csrf,
                user_id=admin_user_id,
                expires_at=expires,
                auth_mode="oidc",
            )
            for index in range(count):
                user_id = str(uuid5(NAMESPACE, f"user-{index:04d}"))
                token = f"rp894-user-session-{index:04d}"
                csrf = f"rp894-user-csrf-{index:04d}"
                session.add(
                    AppUser(
                        id=user_id,
                        issuer="urn:resourceplanner:phase4",
                        subject=f"user-{index:04d}",
                        display_name=f"Utilisateur charge {index:04d}",
                        email=None,
                        roles_json=json.dumps([ROLE_TECHNICIAN]),
                        active=True,
                    )
                )
                session.flush()
                SqlAuthSessionRepository(session).create_session(
                    raw_token=token,
                    csrf_token=csrf,
                    user_id=user_id,
                    expires_at=expires,
                    auth_mode="oidc",
                )
                credentials.append(
                    Credential(user_id=user_id, session=token, csrf=csrf)
                )
    finally:
        engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "admin": {
                    "user_id": admin_user_id,
                    "session": admin_token,
                    "csrf": admin_csrf,
                    "admin": True,
                },
                "users": [
                    {
                        "user_id": item.user_id,
                        "session": item.session,
                        "csrf": item.csrf,
                        "admin": item.admin,
                    }
                    for item in credentials
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _sql_snapshot(database_url: str) -> dict[str, object]:
    engine = create_sql_engine(database_url)
    try:
        with engine.connect() as connection:
            database = connection.execute(
                text(
                    "SELECT snapshot_isolation_state_desc, is_read_committed_snapshot_on "
                    "FROM sys.databases WHERE name = DB_NAME()"
                )
            ).one()
            deadlocks = connection.scalar(
                text(
                    "SELECT COALESCE(SUM(cntr_value), 0) "
                    "FROM sys.dm_os_performance_counters "
                    "WHERE counter_name = 'Number of Deadlocks/sec'"
                )
            )
            sessions = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM sys.dm_exec_sessions "
                    "WHERE is_user_process = 1 AND database_id = DB_ID()"
                )
            )
            xact_abort = connection.scalar(
                text("SELECT CASE WHEN (16384 & @@OPTIONS) = 16384 THEN 1 ELSE 0 END")
            )
            shift_count = connection.scalar(select(func.count()).select_from(Shift))
            demand_count = connection.scalar(
                select(func.count()).select_from(WorkforceRequest)
            )
            version = connection.scalar(text("SELECT CAST(@@VERSION AS nvarchar(4000))"))
        return {
            "sql_server_version": str(version),
            "snapshot_isolation_state": str(database[0]),
            "read_committed_snapshot_on": bool(database[1]),
            "xact_abort": int(xact_abort or 0),
            "deadlocks": int(deadlocks or 0),
            "user_sessions": int(sessions or 0),
            "shift_count": int(shift_count or 0),
            "demand_count": int(demand_count or 0),
        }
    finally:
        engine.dispose()


def _xact_abort_recovery(database_url: str) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for mode in ("OFF", "ON"):
        # pyodbc/ODBC may reuse the same physical SQL Server session even after
        # SQLAlchemy closes/disposes an engine. Give each XACT_ABORT proof its
        # own session-local temp-table identity so that lower-level pooling
        # cannot collide the OFF and ON cases.
        table_name = (
            "#rp894_xact_off"
            if mode == "OFF"
            else "#rp894_xact_on"
        )
        engine = create_sql_engine(database_url)
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute(f"SET XACT_ABORT {mode}")
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
            cursor.execute(
                f"CREATE TABLE {table_name} (id int NOT NULL PRIMARY KEY)"
            )
            raw.commit()
            try:
                cursor.execute("BEGIN TRANSACTION")
                cursor.execute(
                    f"INSERT INTO {table_name} (id) VALUES (1)"
                )
                cursor.execute(
                    f"INSERT INTO {table_name} (id) VALUES (1)"
                )
                raw.commit()
            except Exception:
                raw.rollback()
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            remaining = int(cursor.fetchone()[0] or 0)
            results[mode.lower()] = remaining == 0
        finally:
            raw.close()
            engine.dispose()
    return results


async def _request_timed(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: object | None = None,
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    response = await client.request(
        method,
        path,
        headers=headers,
        json=json_body,
    )
    return response, (time.perf_counter() - started) * 1000.0


async def _active_broadcast(
    client: httpx.AsyncClient,
    admin: Credential,
) -> dict[str, object] | None:
    response = await client.get(
        "/api/v1/admin/runtime/status",
        headers=_headers(admin),
    )
    response.raise_for_status()
    broadcast = response.json().get("broadcast")
    return broadcast if isinstance(broadcast, dict) and broadcast.get("state") == "ACTIVE" else None


async def _end_active(client: httpx.AsyncClient, admin: Credential) -> None:
    active = await _active_broadcast(client, admin)
    if active is None:
        return
    response = await client.post(
        f"/api/v1/admin/runtime/broadcasts/{active['id']}/end",
        headers=_headers(admin, csrf=True),
        cookies=_cookies(admin),
        json={},
    )
    response.raise_for_status()


async def _create_broadcast(
    client: httpx.AsyncClient,
    admin: Credential,
    command_suffix: str,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/admin/runtime/broadcasts",
        headers=_headers(admin, csrf=True),
        cookies=_cookies(admin),
        json={
            "creation_command_id": str(uuid5(NAMESPACE, command_suffix)),
            "title": f"Charge #894 {command_suffix}",
            "message": "Message synthetique de validation de charge #894.",
            "confirmed": True,
        },
    )
    response.raise_for_status()
    return response.json()["broadcast"]


async def _ack(
    client: httpx.AsyncClient,
    credential: Credential,
    broadcast_id: str,
) -> tuple[httpx.Response, float]:
    return await _request_timed(
        client,
        "POST",
        f"/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment",
        headers=_headers(credential, csrf=True),
        json_body={},
    )


async def _preack(
    base_url: str,
    users: list[Credential],
    broadcast_id: str,
) -> None:
    semaphore = asyncio.Semaphore(32)

    async def one(credential: Credential) -> None:
        async with semaphore:
            async with httpx.AsyncClient(
                base_url=base_url,
                cookies=_cookies(credential),
                timeout=10.0,
            ) as client:
                response, _elapsed = await _ack(client, credential, broadcast_id)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"pre-ACK failed: {response.status_code} {response.text[:200]}"
                    )

    await asyncio.gather(*(one(item) for item in users))


async def _ensure_synthetic_resource(
    client: httpx.AsyncClient,
    admin: Credential,
) -> str:
    response = await client.post(
        "/api/v1/resources",
        headers={
            **_headers(admin, csrf=True),
            "Idempotency-Key": "issue-894-phase4-resource",
        },
        cookies=_cookies(admin),
        json={"name": "Ressource synthetique Phase 4 #894"},
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"resource create failed: {response.status_code} {response.text[:300]}")
    body = response.json()
    resource_id = body.get("resource_id") or body.get("id")
    if not resource_id:
        raise RuntimeError("resource create response omitted its identity")
    return str(resource_id)


async def _ordinary_driver(
    client: httpx.AsyncClient,
    admin: Credential,
    resource_id: str,
    stop_at: float,
    latencies: list[float],
    statuses: dict[str, int],
) -> dict[str, object]:
    interval = 1.0 / ORDINARY_RPS
    started = time.perf_counter()
    next_release = started
    issued = 0
    completed = 0
    scheduling_lag_ms: list[float] = []
    operation_counts = {
        "planning_reads": 0,
        "project_reads": 0,
        "save_mutations": 0,
    }
    semaphore = asyncio.Semaphore(32)
    tasks: set[asyncio.Task[None]] = set()

    async def one(counter: int, scheduled_at: float) -> None:
        nonlocal completed
        async with semaphore:
            actual_start = time.perf_counter()
            scheduling_lag_ms.append(
                max(0.0, (actual_start - scheduled_at) * 1000.0)
            )
            if counter % 10 == 0:
                operation = "save_mutations"
                method = "PATCH"
                path = f"/api/v1/resources/{resource_id}"
                headers = _headers(admin, csrf=True)
                body = {
                    "name": (
                        "Ressource synthetique Phase 4 #894 "
                        f"{counter % 1000:03d}"
                    )
                }
            elif counter % 2 == 0:
                operation = "planning_reads"
                method = "GET"
                path = (
                    "/api/v1/planning/snapshot"
                    "?start=2026-01-05&end=2026-01-09"
                )
                headers = _headers(admin)
                body = None
            else:
                operation = "project_reads"
                method = "GET"
                path = "/api/v1/projects?active_only=true"
                headers = _headers(admin)
                body = None
            response, elapsed = await _request_timed(
                client,
                method,
                path,
                headers=headers,
                json_body=body,
            )
            latencies.append(elapsed)
            key = str(response.status_code)
            statuses[key] = statuses.get(key, 0) + 1
            operation_counts[operation] += 1
            completed += 1

    while next_release < stop_at:
        await asyncio.sleep(max(0.0, next_release - time.perf_counter()))
        issued += 1
        task = asyncio.create_task(one(issued, next_release))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        next_release += interval

    pending = tuple(tasks)
    if pending:
        await asyncio.gather(*pending)

    configured_window = max(0.001, stop_at - started)
    completed_at = time.perf_counter()
    completion_window = max(configured_window, completed_at - started)
    return {
        "configured_target_rps": ORDINARY_RPS,
        "issued": issued,
        "completed": completed,
        "achieved_issue_rps": issued / configured_window,
        "achieved_completion_rps": completed / completion_window,
        "operation_counts": operation_counts,
        "scheduling_lag_ms": _latencies(scheduling_lag_ms),
    }


async def _poller(
    base_url: str,
    credential: Credential,
    *,
    start_measurement: asyncio.Event,
    stop_at: float,
    status_latencies: list[float],
    status_codes: dict[str, int],
    delivery_latencies: list[float],
    activation_monotonic: list[float],
) -> None:
    known_id: str | None = None
    known_version: int | None = None
    delivered: set[str] = set()
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(credential),
        timeout=5.0,
    ) as client:
        while time.perf_counter() < stop_at:
            params: dict[str, str] = {}
            if known_id is not None and known_version is not None:
                params = {
                    "known_broadcast_id": known_id,
                    "known_broadcast_version": str(known_version),
                }
            started = time.perf_counter()
            response = await client.get(
                "/api/v1/runtime/status",
                headers=_headers(credential),
                params=params,
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            if start_measurement.is_set():
                status_latencies.append(elapsed)
                key = str(response.status_code)
                status_codes[key] = status_codes.get(key, 0) + 1
            if response.status_code == 200:
                active = response.json().get("active_broadcast")
                if isinstance(active, dict):
                    known_id = str(active["id"])
                    known_version = int(active["version"])
                    if (
                        activation_monotonic
                        and known_id not in delivered
                        and start_measurement.is_set()
                    ):
                        delivered.add(known_id)
                        delivery_latencies.append(
                            (time.perf_counter() - activation_monotonic[0]) * 1000.0
                        )
                else:
                    known_id = None
                    known_version = None
            await asyncio.sleep(random.uniform(4.0, 6.0))


async def _loop_metrics(backend_url: str, *, reset: bool) -> dict[str, object]:
    async with httpx.AsyncClient(base_url=backend_url, timeout=5.0) as client:
        response = await client.get(
            "/__phase4__/metrics",
            params={"reset": str(reset).lower()},
        )
        response.raise_for_status()
        return response.json()


def _require_phase4_telemetry(
    metrics: dict[str, object],
    *,
    minimum_coverage_seconds: float = 0.0,
) -> None:
    monitor = metrics.get("monitor")
    event_loop = metrics.get("event_loop_lag_ms")
    pool = metrics.get("pool")
    if not isinstance(monitor, dict) or not bool(monitor.get("active")):
        raise RuntimeError("Phase 4 monitor is not active.")
    samples = int(metrics.get("samples") or 0)
    coverage = float(monitor.get("coverage_seconds") or 0.0)
    ratio = float(monitor.get("coverage_ratio") or 0.0)
    if samples <= 0:
        raise RuntimeError("Phase 4 event-loop telemetry has no samples.")
    if coverage < max(0.0, minimum_coverage_seconds * 0.95):
        raise RuntimeError(
            "Phase 4 monitor did not cover the required measurement window: "
            f"{coverage:.3f}s < {minimum_coverage_seconds:.3f}s."
        )
    if ratio < 0.50:
        raise RuntimeError(
            f"Phase 4 event-loop telemetry coverage ratio is only {ratio:.3f}."
        )
    if not isinstance(event_loop, dict):
        raise RuntimeError("Phase 4 event-loop distribution is missing.")
    if not isinstance(pool, dict):
        raise RuntimeError("Phase 4 SQL pool telemetry is missing.")
    checkout = pool.get("checkout_wait_ms")
    if not isinstance(checkout, dict) or int(checkout.get("samples") or 0) <= 0:
        raise RuntimeError("Phase 4 SQL pool checkout telemetry has no samples.")


async def _run_baseline(
    base_url: str,
    admin: Credential,
    *,
    warmup_seconds: int,
    measure_seconds: int,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as client:
        await _end_active(client, admin)
        resource_id = await _ensure_synthetic_resource(client, admin)
        warmup_until = time.perf_counter() + warmup_seconds
        while time.perf_counter() < warmup_until:
            response = await client.get(
                "/api/v1/planning/snapshot?start=2026-01-05&end=2026-01-09",
                headers=_headers(admin),
            )
            response.raise_for_status()
            await asyncio.sleep(0.1)
        latencies: list[float] = []
        statuses: dict[str, int] = {}
        stop_at = time.perf_counter() + measure_seconds
        await _ordinary_driver(
            client,
            admin,
            resource_id,
            stop_at,
            latencies,
            statuses,
        )
        return {
            "ordinary_api": _latencies(latencies),
            "statuses": statuses,
        }


async def _run_scenario(
    base_url: str,
    backend_url: str,
    admin: Credential,
    users: list[Credential],
    *,
    clients_count: int,
    state: str,
    warmup_seconds: int,
    measure_seconds: int,
    sequence: int,
) -> dict[str, object]:
    selected = users[:clients_count]
    start_measurement = asyncio.Event()
    status_latencies: list[float] = []
    status_codes: dict[str, int] = {}
    ordinary_latencies: list[float] = []
    ordinary_statuses: dict[str, int] = {}
    delivery_latencies: list[float] = []
    activation_monotonic: list[float] = []

    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as admin_client:
        await _end_active(admin_client, admin)
        resource_id = await _ensure_synthetic_resource(admin_client, admin)
        if state == "mostly_acknowledged":
            created = await _create_broadcast(
                admin_client,
                admin,
                f"scenario-{sequence}-{clients_count}-{state}",
            )
            await _preack(
                base_url,
                selected[: max(1, math.floor(clients_count * 0.90))],
                str(created["id"]),
            )

        stop_at = time.perf_counter() + warmup_seconds + measure_seconds + 15.0
        pollers = [
            asyncio.create_task(
                _poller(
                    base_url,
                    credential,
                    start_measurement=start_measurement,
                    stop_at=stop_at,
                    status_latencies=status_latencies,
                    status_codes=status_codes,
                    delivery_latencies=delivery_latencies,
                    activation_monotonic=activation_monotonic,
                )
            )
            for credential in selected
        ]
        await asyncio.sleep(warmup_seconds)
        await _loop_metrics(backend_url, reset=True)
        before_sql = _sql_snapshot(os.environ["RESOURCEPLANNER_DATABASE_URL"])
        if state == "pending":
            activation_monotonic.append(time.perf_counter())
            await _create_broadcast(
                admin_client,
                admin,
                f"scenario-{sequence}-{clients_count}-{state}",
            )

        start_measurement.set()
        measurement_stop = time.perf_counter() + measure_seconds
        ordinary = asyncio.create_task(
            _ordinary_driver(
                admin_client,
                admin,
                resource_id,
                measurement_stop,
                ordinary_latencies,
                ordinary_statuses,
            )
        )
        await asyncio.sleep(measure_seconds)
        await ordinary
        for task in pollers:
            task.cancel()
        await asyncio.gather(*pollers, return_exceptions=True)
        loop = await _loop_metrics(backend_url, reset=False)
        after_sql = _sql_snapshot(os.environ["RESOURCEPLANNER_DATABASE_URL"])
        await _end_active(admin_client, admin)

    return {
        "clients": clients_count,
        "state": state,
        "warmup_seconds": warmup_seconds,
        "measurement_seconds": measure_seconds,
        "status": _latencies(status_latencies),
        "status_codes": status_codes,
        "ordinary_api": _latencies(ordinary_latencies),
        "ordinary_statuses": ordinary_statuses,
        "foreground_delivery": _latencies(delivery_latencies),
        "event_loop": loop,
        "sql_before": before_sql,
        "sql_after": after_sql,
    }


async def _raw_lost_response(
    base_url: str,
    credential: Credential,
    broadcast_id: str,
) -> None:
    parsed = httpx.URL(base_url)
    host = parsed.host
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    authority = f"{parsed.host}:{port}"
    request = (
        f"POST /api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 2\r\n"
        f"Cookie: resourceplanner_session={credential.session}; "
        f"resourceplanner_csrf={credential.csrf}\r\n"
        f"X-CSRF-Token: {credential.csrf}\r\n"
        f"Origin: {parsed.scheme}://{authority}\r\n"
        "Connection: close\r\n\r\n{}"
    ).encode("ascii")
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()
    await asyncio.sleep(0.05)
    writer.close()
    await writer.wait_closed()
    del reader


def _age_ended_broadcast_for_cleanup(
    database_url: str,
    broadcast_id: str,
) -> None:
    engine = create_sql_engine(database_url)
    historical_activation = datetime.now(timezone.utc) - timedelta(days=92)
    historical_end = datetime.now(timezone.utc) - timedelta(days=91)
    try:
        with engine.begin() as connection:
            connection.execute(
                update(EmergencyBroadcast)
                .where(EmergencyBroadcast.id == broadcast_id)
                .values(
                    created_at=historical_activation,
                    activated_at=historical_activation,
                    ended_at=historical_end,
                )
            )
    finally:
        engine.dispose()


def _run_retention_cleanup(database_url: str) -> tuple[int, int]:
    engine = create_sql_engine(database_url)
    factory = create_session_factory(engine)
    try:
        repository = SqlEmergencyBroadcastRepository(factory)
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        receipts = repository.delete_acknowledgments_for_expired(
            ended_before=cutoff,
            limit=1000,
        )
        parents = repository.delete_empty_expired_broadcasts(
            ended_before=cutoff,
            limit=100,
        )
        return receipts, parents
    finally:
        engine.dispose()


def _wait_for_receipt(
    database_url: str,
    broadcast_id: str,
    user_id: str,
    *,
    timeout_seconds: float = 3.0,
) -> bool:
    engine = create_sql_engine(database_url)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                exists = connection.scalar(
                    select(func.count())
                    .select_from(EmergencyBroadcastAcknowledgment)
                    .where(
                        EmergencyBroadcastAcknowledgment.broadcast_id == broadcast_id,
                        EmergencyBroadcastAcknowledgment.user_id == user_id,
                    )
                )
            if int(exists or 0) == 1:
                return True
            time.sleep(0.025)
        return False
    finally:
        engine.dispose()


async def _admin_runtime_diagnostics(
    client: httpx.AsyncClient,
    admin: Credential,
) -> dict[str, object]:
    response = await client.get(
        "/api/v1/admin/runtime/status",
        headers=_headers(admin),
    )
    response.raise_for_status()
    diagnostics = response.json().get("diagnostics")
    return diagnostics if isinstance(diagnostics, dict) else {}


def _record_http_failure(
    response: httpx.Response,
    error_codes: dict[str, int],
    retry_after: dict[str, int],
) -> None:
    code = f"http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code"):
            code = str(error["code"])
    error_codes[code] = error_codes.get(code, 0) + 1
    retry_value = str(response.headers.get("Retry-After") or "").strip()
    if retry_value:
        retry_after[retry_value] = retry_after.get(retry_value, 0) + 1


async def _ack_wave(
    base_url: str,
    admin: Credential,
    users: list[Credential],
    *,
    distinct: int,
    duplicates: int,
    suffix: str,
) -> dict[str, object]:
    database_url = os.environ["RESOURCEPLANNER_DATABASE_URL"]
    cleanup_user = users[-1]
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as admin_client:
        await _end_active(admin_client, admin)
        old = await _create_broadcast(admin_client, admin, f"{suffix}-cleanup")
        old_id = str(old["id"])
        async with httpx.AsyncClient(
            base_url=base_url,
            cookies=_cookies(cleanup_user),
            timeout=10.0,
        ) as cleanup_client:
            cleanup_ack, _cleanup_elapsed = await _ack(
                cleanup_client,
                cleanup_user,
                old_id,
            )
            cleanup_ack.raise_for_status()
        await _end_active(admin_client, admin)
        _age_ended_broadcast_for_cleanup(database_url, old_id)

        broadcast = await _create_broadcast(admin_client, admin, suffix)
        broadcast_id = str(broadcast["id"])
        diagnostics_before = await _admin_runtime_diagnostics(
            admin_client,
            admin,
        )

    selected = users[:distinct]
    duplicate_users = [selected[index % distinct] for index in range(duplicates)]
    targets = selected + duplicate_users
    random.Random(894).shuffle(targets)
    barrier = asyncio.Event()
    latencies: list[float] = []
    statuses: list[int] = []
    error_codes: dict[str, int] = {}
    retry_after: dict[str, int] = {}
    timestamps: dict[str, set[str]] = {}

    async def one(credential: Credential) -> None:
        async with httpx.AsyncClient(
            base_url=base_url,
            cookies=_cookies(credential),
            timeout=10.0,
        ) as client:
            await barrier.wait()
            response, elapsed = await _ack(client, credential, broadcast_id)
            latencies.append(elapsed)
            statuses.append(response.status_code)
            if response.status_code == 200:
                payload = response.json()["acknowledgment"]
                timestamps.setdefault(credential.user_id, set()).add(
                    str(payload["acknowledged_at"])
                )
            else:
                _record_http_failure(
                    response,
                    error_codes,
                    retry_after,
                )

    tasks = [asyncio.create_task(one(item)) for item in targets]
    barrier.set()
    cleanup_task = asyncio.create_task(
        asyncio.to_thread(_run_retention_cleanup, database_url)
    )
    await asyncio.gather(*tasks)
    cleanup_deleted_receipts, cleanup_deleted_parents = await cleanup_task

    lost_user = users[distinct]
    await _raw_lost_response(base_url, lost_user, broadcast_id)
    lost_response_committed = await asyncio.to_thread(
        _wait_for_receipt,
        database_url,
        broadcast_id,
        lost_user.user_id,
    )
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(lost_user),
        timeout=10.0,
    ) as client:
        retry, retry_elapsed = await _ack(client, lost_user, broadcast_id)
        latencies.append(retry_elapsed)
        statuses.append(retry.status_code)
        if retry.status_code == 200:
            timestamps.setdefault(lost_user.user_id, set()).add(
                str(retry.json()["acknowledgment"]["acknowledged_at"])
            )
        else:
            _record_http_failure(
                retry,
                error_codes,
                retry_after,
            )

    engine = create_sql_engine(database_url)
    try:
        with engine.connect() as connection:
            receipt_count = connection.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(EmergencyBroadcastAcknowledgment.broadcast_id == broadcast_id)
            )
    finally:
        engine.dispose()

    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as admin_client:
        diagnostics_after = await _admin_runtime_diagnostics(
            admin_client,
            admin,
        )
        await _end_active(admin_client, admin)

    admission_overload_delta = (
        int(diagnostics_after.get("status_overloads") or 0)
        - int(diagnostics_before.get("status_overloads") or 0)
    )
    expected_receipts = distinct + 1
    stable = all(len(values) == 1 for values in timestamps.values())
    return {
        "distinct_users": distinct,
        "duplicate_requests": duplicates,
        "target_requests": len(targets),
        "lost_response_retry": True,
        "lost_response_committed_before_retry": lost_response_committed,
        "latency": _latencies(latencies),
        "http_codes": {
            str(code): statuses.count(code)
            for code in sorted(set(statuses))
        },
        "http_error_codes": dict(sorted(error_codes.items())),
        "retry_after": dict(sorted(retry_after.items())),
        "admission_overload_delta": admission_overload_delta,
        "receipt_count": int(receipt_count or 0),
        "expected_receipt_count": expected_receipts,
        "canonical_timestamps_stable": stable,
        "retention_interleave": {
            "deleted_receipts": cleanup_deleted_receipts,
            "deleted_parents": cleanup_deleted_parents,
        },
        "passed": (
            all(code == 200 for code in statuses)
            and int(receipt_count or 0) == expected_receipts
            and stable
            and lost_response_committed
            and cleanup_deleted_receipts >= 1
            and cleanup_deleted_parents >= 1
        ),
    }


def _degradation_ok(baseline: float, loaded: float) -> bool:
    return loaded - baseline <= max(baseline * 0.10, 50.0)


async def _run_certification(args: argparse.Namespace) -> dict[str, object]:
    database_url = os.environ["RESOURCEPLANNER_DATABASE_URL"]
    _require_isolated_database(database_url)
    admin, users = _load_credentials(args.credentials)
    if len(users) < 500:
        raise SystemExit("Phase 4 certification requires at least 500 synthetic users.")

    sql_initial = _sql_snapshot(database_url)
    xact_abort_recovery = _xact_abort_recovery(database_url)
    baseline = await _run_baseline(
        args.base_url,
        admin,
        warmup_seconds=args.warmup_seconds,
        measure_seconds=args.measure_seconds,
    )
    scenarios: list[dict[str, object]] = []
    sequence = 0
    for clients_count in CLIENT_COUNTS:
        for state in STATES:
            sequence += 1
            scenarios.append(
                await _run_scenario(
                    args.base_url,
                    args.backend_url,
                    admin,
                    users,
                    clients_count=clients_count,
                    state=state,
                    warmup_seconds=args.warmup_seconds,
                    measure_seconds=args.measure_seconds,
                    sequence=sequence,
                )
            )

    waves: list[dict[str, object]] = []
    sql_before_waves = _sql_snapshot(database_url)
    for repeat in range(args.ack_repeats):
        for distinct, duplicates in ((75, 25), (187, 63), (250, 63)):
            waves.append(
                await _ack_wave(
                    args.base_url,
                    admin,
                    users,
                    distinct=distinct,
                    duplicates=duplicates,
                    suffix=f"wave-{repeat:02d}-{distinct}-{duplicates}",
                )
            )
    sql_after_waves = _sql_snapshot(database_url)
    sql_final = _sql_snapshot(database_url)

    baseline_p95 = float(baseline["ordinary_api"]["p95_ms"])
    baseline_p99 = float(baseline["ordinary_api"]["p99_ms"])
    scenario_checks: list[dict[str, object]] = []
    for scenario in scenarios:
        ordinary = scenario["ordinary_api"]
        status = scenario["status"]
        loop = scenario["event_loop"]["event_loop_lag_ms"]
        pool = scenario["event_loop"]["pool"]
        delivery = scenario["foreground_delivery"]
        deadlock_delta = (
            int(scenario["sql_after"]["deadlocks"])
            - int(scenario["sql_before"]["deadlocks"])
        )
        status_codes = scenario["status_codes"]
        check = {
            "clients": scenario["clients"],
            "state": scenario["state"],
            "ordinary_p95_ok": _degradation_ok(
                baseline_p95, float(ordinary["p95_ms"])
            ),
            "ordinary_all_200": set(scenario["ordinary_statuses"]) <= {"200"}
                and int(ordinary["count"]) > 0,
            "ordinary_p99_ok": _degradation_ok(
                baseline_p99, float(ordinary["p99_ms"])
            ),
            "status_p95_ok": float(status["p95_ms"]) <= 250.0,
            "status_p99_ok": float(status["p99_ms"]) <= 750.0,
            "status_all_200": set(status_codes) <= {"200"}
                and int(status["count"]) > 0,
            "event_loop_p99_ok": float(loop["p99"]) <= 100.0,
            "pool_ceiling_ok": (
                int(pool["max_checked_out"]) <= int(pool["size"]) + 10
                and int(pool["max_overflow"]) <= 10
                and int(pool["checked_out"]) == 0
            ),
            "deadlocks_zero": deadlock_delta == 0,
            "delivery_p95_ok": (
                scenario["state"] != "pending"
                or (
                    int(delivery["count"]) == int(scenario["clients"])
                    and float(delivery["p95_ms"]) <= 7000.0
                )
            ),
        }
        check["passed"] = all(
            bool(value)
            for key, value in check.items()
            if key.endswith("_ok")
            or key in {"status_all_200", "ordinary_all_200", "deadlocks_zero"}
        )
        scenario_checks.append(check)

    wave_p95 = max((float(w["latency"]["p95_ms"]) for w in waves), default=0.0)
    wave_p99 = max((float(w["latency"]["p99_ms"]) for w in waves), default=0.0)
    wave_check = {
        "repeats": args.ack_repeats,
        "waves": len(waves),
        "all_functional": all(bool(w["passed"]) for w in waves),
        "p95_ms_max": wave_p95,
        "p99_ms_max": wave_p99,
        "p95_ok": wave_p95 <= 2000.0,
        "p99_ok": wave_p99 <= 5000.0,
        "deadlock_delta": int(sql_after_waves["deadlocks"]) - int(sql_before_waves["deadlocks"]),
    }
    wave_check["passed"] = (
        wave_check["all_functional"]
        and wave_check["p95_ok"]
        and wave_check["p99_ok"]
        and wave_check["deadlock_delta"] == 0
    )

    business_unchanged = (
        int(sql_initial["shift_count"]) == int(sql_final["shift_count"])
        and int(sql_initial["demand_count"]) == int(sql_final["demand_count"])
    )
    baseline_all_200 = (
        set(baseline["statuses"]) <= {"200"}
        and int(baseline["ordinary_api"]["count"]) > 0
    )
    passed = (
        baseline_all_200
        and all(bool(item["passed"]) for item in scenario_checks)
        and bool(wave_check["passed"])
        and business_unchanged
        and all(xact_abort_recovery.values())
    )
    return {
        "schema": "issue-894-phase4-load-v1",
        "head_sha": args.head_sha,
        "environment": {
            "stack": "nginx -> one uvicorn process -> sql server",
            "synthetic_users": len(users),
            "dataset": DATASETS["medium"].shape(),
            "warmup_seconds": args.warmup_seconds,
            "measurement_seconds": args.measure_seconds,
            "ordinary_requests_per_second": ORDINARY_RPS,
            "client_counts": list(CLIENT_COUNTS),
            "states": list(STATES),
        },
        "sql_initial": sql_initial,
        "xact_abort_recovery": xact_abort_recovery,
        "baseline": baseline,
        "baseline_all_200": baseline_all_200,
        "scenarios": scenarios,
        "scenario_acceptance": scenario_checks,
        "ack_waves": waves,
        "ack_acceptance": wave_check,
        "business_rows_unchanged": business_unchanged,
        "sql_final": sql_final,
        "passed": passed,
    }


async def _restart_prepare(args: argparse.Namespace) -> None:
    admin, users = _load_credentials(args.credentials)
    async with httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as client:
        await _end_active(client, admin)
        broadcast = await _create_broadcast(client, admin, "restart-proof")
    user = users[0]
    async with httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(user),
        timeout=10.0,
    ) as client:
        response, _elapsed = await _ack(client, user, str(broadcast["id"]))
        response.raise_for_status()
        status = await client.get("/api/v1/runtime/status", headers=_headers(user))
        status.raise_for_status()
    args.output.write_text(
        json.dumps(
            {
                "broadcast_id": broadcast["id"],
                "acknowledged_at": response.json()["acknowledgment"]["acknowledged_at"],
                "observation_epoch": status.json()["observation_epoch"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def _restart_verify(args: argparse.Namespace) -> None:
    state = json.loads(args.state.read_text(encoding="utf-8"))
    admin, users = _load_credentials(args.credentials)
    user = users[0]
    async with httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(user),
        timeout=10.0,
    ) as client:
        status = await client.get("/api/v1/runtime/status", headers=_headers(user))
        status.raise_for_status()
        body = status.json()
        active = body["active_broadcast"]
        if active is None or active["id"] != state["broadcast_id"]:
            raise SystemExit("restart lost the active broadcast")
        if active["acknowledged_at"] != state["acknowledged_at"]:
            raise SystemExit("restart changed/lost the canonical acknowledgment")
        if body["observation_epoch"] == state["observation_epoch"]:
            raise SystemExit("restart did not reset ephemeral observation epoch")
    async with httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as client:
        await _end_active(client, admin)
    print(json.dumps({"status": "passed", "restart": "durable_state_preserved_ephemeral_reset"}))


def _seed_predecessor(database_url: str) -> None:
    engine = create_sql_engine(database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            project = Project(
                id="RP894-MIG-PROJECT",
                number="RP894-MIG",
                name="Projet migration Phase 4",
                client="Synthetic",
                project_manager_name="Synthetic",
                status="Actif",
            )
            resource = Resource(
                id="RP894-MIG-RESOURCE",
                external_id="RP894-MIG-EMP",
                name="Ressource migration Phase 4",
                active=True,
                sort_order=1,
            )
            demand = WorkforceRequest(
                id="RP894-MIG-DEMAND",
                legacy_demand_number="RP894-MIG-DEMAND",
                project_id=project.id,
                requester_name="Synthetic",
                request_type="Projet",
                priority="Normale",
                confirmation="Confirmée",
                desired_start=date(2026, 1, 5),
                desired_end=date(2026, 1, 9),
                description="Representative predecessor data",
                resource_count=1,
                estimated_hours=Decimal("8"),
                estimated_days=Decimal("1"),
                proposed_resource_id=resource.id,
                status="En planification",
            )
            session.add_all([project, resource, demand])
            session.flush()
            line = RequestLine(
                id="RP894-MIG-LINE",
                workforce_request_id=demand.id,
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=demand.desired_start,
                desired_end=demand.desired_end,
                desired_active_days=1,
                estimated_hours=Decimal("8"),
                confirmation=demand.confirmation,
                proposed_resource_id=resource.id,
                active=True,
            )
            requirement = ResourceRequirement(
                id="RP894-MIG-SEGMENT",
                legacy_segment_id="RP894-MIG-SEGMENT",
                project_id=project.id,
                workforce_request_id=demand.id,
                source_request_line_id=line.id,
                assigned_resource_id=resource.id,
                start_date=date(2026, 1, 5),
                end_date=date(2026, 1, 5),
                planned_hours=Decimal("8"),
                desired_active_days=1,
                load_profile="UNIFORM",
                status="Planifié",
                planning_type="Flexible",
                priority="Normale",
                confirmation="Confirmée",
                origin="REQUEST",
            )
            session.add_all([line, requirement])
            session.flush()
            session.add(
                Shift(
                    id="RP894-MIG-SHIFT",
                    legacy_allocation_id="RP894-MIG-SHIFT",
                    resource_requirement_id=requirement.id,
                    resource_id=resource.id,
                    work_date=date(2026, 1, 5),
                    hours=Decimal("8"),
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                )
            )
    finally:
        engine.dispose()


def _verify_migration(database_url: str) -> dict[str, object]:
    engine = create_sql_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            project = connection.scalar(
                select(func.count()).select_from(Project).where(Project.id == "RP894-MIG-PROJECT")
            )
            demand = connection.scalar(
                select(func.count()).select_from(WorkforceRequest).where(
                    WorkforceRequest.id == "RP894-MIG-DEMAND"
                )
            )
            shift = connection.scalar(
                select(func.count()).select_from(Shift).where(Shift.id == "RP894-MIG-SHIFT")
            )
        passed = (
            int(project or 0) == 1
            and int(demand or 0) == 1
            and int(shift or 0) == 1
            and "emergency_broadcasts" in tables
            and "emergency_broadcast_acknowledgments" in tables
        )
        return {
            "representative_project_preserved": int(project or 0) == 1,
            "representative_demand_preserved": int(demand or 0) == 1,
            "representative_shift_preserved": int(shift or 0) == 1,
            "emergency_tables_present": (
                "emergency_broadcasts" in tables
                and "emergency_broadcast_acknowledgments" in tables
            ),
            "passed": passed,
        }
    finally:
        engine.dispose()


async def _rollback_smoke(args: argparse.Namespace) -> dict[str, object]:
    admin, _users = _load_credentials(args.credentials)
    async with httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    ) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
        me = await client.get("/api/v1/auth/me")
        planning = await client.get(
            "/api/v1/planning/snapshot?start=2026-01-05&end=2026-01-09",
            headers=_headers(admin),
        )
        resource_id = await _ensure_synthetic_resource(client, admin)
        save = await client.patch(
            f"/api/v1/resources/{resource_id}",
            headers=_headers(admin, csrf=True),
            json={"name": "Ressource synthetique rollback #894"},
        )
    checks = {
        "health": health.status_code == 200,
        "ready": ready.status_code == 200,
        "auth": me.status_code == 200,
        "ordinary_read": planning.status_code == 200,
        "ordinary_save": save.status_code == 200,
    }
    return {"checks": checks, "passed": all(checks.values())}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated Phase 4 certification for issue #894.")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed.add_argument("--database-url", required=True)
    seed.add_argument("--output", type=Path, required=True)
    seed.add_argument("--users", type=int, default=500)

    run = sub.add_parser("run")
    run.add_argument("--base-url", default="http://frontend:8080")
    run.add_argument("--backend-url", default="http://backend:8000")
    run.add_argument("--credentials", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--head-sha", required=True)
    run.add_argument("--warmup-seconds", type=int, default=DEFAULT_WARMUP_SECONDS)
    run.add_argument("--measure-seconds", type=int, default=DEFAULT_MEASURE_SECONDS)
    run.add_argument("--ack-repeats", type=int, default=DEFAULT_ACK_REPEATS)

    restart_prepare = sub.add_parser("restart-prepare")
    restart_prepare.add_argument("--base-url", default="http://frontend:8080")
    restart_prepare.add_argument("--credentials", type=Path, required=True)
    restart_prepare.add_argument("--output", type=Path, required=True)

    restart_verify = sub.add_parser("restart-verify")
    restart_verify.add_argument("--base-url", default="http://frontend:8080")
    restart_verify.add_argument("--credentials", type=Path, required=True)
    restart_verify.add_argument("--state", type=Path, required=True)

    predecessor = sub.add_parser("seed-predecessor")
    predecessor.add_argument("--database-url", required=True)

    verify = sub.add_parser("verify-migration")
    verify.add_argument("--database-url", required=True)
    verify.add_argument("--output", type=Path, required=True)

    rollback = sub.add_parser("rollback-smoke")
    rollback.add_argument("--base-url", default="http://frontend:8080")
    rollback.add_argument("--credentials", type=Path, required=True)
    rollback.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "seed":
        _seed_sessions(args.database_url, args.output, args.users)
        return 0
    if args.command == "run":
        report = asyncio.run(_run_certification(args))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0 if report["passed"] else 1
    if args.command == "restart-prepare":
        asyncio.run(_restart_prepare(args))
        return 0
    if args.command == "restart-verify":
        asyncio.run(_restart_verify(args))
        return 0
    if args.command == "seed-predecessor":
        _seed_predecessor(args.database_url)
        return 0
    if args.command == "verify-migration":
        report = _verify_migration(args.database_url)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0 if report["passed"] else 1
    if args.command == "rollback-smoke":
        report = asyncio.run(_rollback_smoke(args))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0 if report["passed"] else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
