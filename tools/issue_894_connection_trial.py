from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine

from tools.issue_894_phase4_load import (
    ORDINARY_RPS,
    Credential,
    _cookies,
    _ensure_synthetic_resource,
    _headers,
    _latencies,
    _load_credentials,
    _sql_wait_snapshot,
)


DIAGNOSTIC_LABEL = "DIAGNOSTIC — NOT CERTIFICATION"
DEFAULT_DURATION_SECONDS = 30.0
DEFAULT_CONCURRENCY = 32
FAILURE_RATE_MATERIAL_DELTA = 0.10
LEGACY_LIST_SHIFTS_FINGERPRINT = "6361f6c1e9d43777"
UNCHANGED_RESULT_FINGERPRINTS = (
    "bb0e459d480f9470",
    "44f262328b901259",
)
TARGET_RESULT_FINGERPRINTS = (
    LEGACY_LIST_SHIFTS_FINGERPRINT,
    *UNCHANGED_RESULT_FINGERPRINTS,
)
XE_SESSION_NAME = "rp894_short_correlation"
XE_CLOCK_UNCERTAINTY_MS = 250.0
XE_EVENT_FILE_DIRECTORY = "/var/opt/mssql/log"
XE_EVENT_FILE_BASENAME = "rp894_short_correlation"
XE_EVENT_FILE_SIZE_MB = 8
XE_EVENT_FILE_ROLLOVER_COUNT = 4
_PREPARED_EXECUTION_RPC_NAMES = frozenset(
    {"sp_prepexec", "sp_execute", "sp_executesql"}
)
_PREPARED_NON_EXECUTION_RPC_NAMES = frozenset(
    {"sp_prepare", "sp_unprepare"}
)


def _normalized_rpc_name(value: object) -> str:
    text = str(value or "").casefold().replace("[", "").replace("]", "")
    return text.rsplit(".", 1)[-1]


def _current_result_fingerprints(
    rows: list[dict[str, object]],
) -> tuple[str, ...]:
    role_fingerprints = sorted(
        {
            str(row.get("statement_fingerprint") or "")
            for row in rows
            if row.get("kind") == "dbapi_fetch"
            and row.get("target_role") == "list_shifts_initial"
            and str(row.get("statement_fingerprint") or "")
        }
    )
    if len(role_fingerprints) == 1:
        return (role_fingerprints[0], *UNCHANGED_RESULT_FINGERPRINTS)
    if len(role_fingerprints) > 1:
        return (*UNCHANGED_RESULT_FINGERPRINTS,)

    legacy_observed = any(
        row.get("kind") == "dbapi_fetch"
        and row.get("statement_fingerprint") == LEGACY_LIST_SHIFTS_FINGERPRINT
        for row in rows
    )
    if legacy_observed:
        return (
            LEGACY_LIST_SHIFTS_FINGERPRINT,
            *UNCHANGED_RESULT_FINGERPRINTS,
        )
    return (*UNCHANGED_RESULT_FINGERPRINTS,)



def _xe_connection(database_url: str):
    engine = create_engine(database_url, pool_pre_ping=True)
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    return engine, connection


def _xe_event_file_pattern() -> str:
    return (
        f"{XE_EVENT_FILE_DIRECTORY}/{XE_EVENT_FILE_BASENAME}*.xel"
    )


def _xe_event_file_base() -> str:
    return f"{XE_EVENT_FILE_DIRECTORY}/{XE_EVENT_FILE_BASENAME}.xel"


def _xe_delete_event_files(connection: object) -> None:
    connection.exec_driver_sql(
        f"""
        EXEC master.dbo.xp_delete_file
            0,
            N'{XE_EVENT_FILE_DIRECTORY}',
            N'xel',
            N'2099-12-31T23:59:59',
            1
        """
    )


def _xe_list_event_files(connection: object) -> list[str]:
    rows = connection.exec_driver_sql(
        f"""
        CREATE TABLE #rp894_files (
            subdirectory nvarchar(512),
            depth int,
            isfile bit
        );
        INSERT INTO #rp894_files
        EXEC master.sys.xp_dirtree
            N'{XE_EVENT_FILE_DIRECTORY}',
            1,
            1;
        SELECT subdirectory
        FROM #rp894_files
        WHERE isfile = 1
          AND subdirectory LIKE N'{XE_EVENT_FILE_BASENAME}%.xel'
        ORDER BY subdirectory;
        DROP TABLE #rp894_files;
        """
    ).all()
    return [str(row[0]) for row in rows if row and row[0]]


def _xe_cleanup(database_url: str) -> None:
    engine, connection = _xe_connection(database_url)
    try:
        connection.exec_driver_sql(
            f"""
            IF EXISTS (
                SELECT 1 FROM sys.dm_xe_sessions
                WHERE name = N'{XE_SESSION_NAME}'
            )
            ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=STOP
            """
        )
        connection.exec_driver_sql(
            f"""
            IF EXISTS (
                SELECT 1 FROM sys.server_event_sessions
                WHERE name = N'{XE_SESSION_NAME}'
            )
            DROP EVENT SESSION [{XE_SESSION_NAME}] ON SERVER
            """
        )
        _xe_delete_event_files(connection)
        remaining = _xe_list_event_files(connection)
        if remaining:
            raise ValueError(
                "SQL Server Extended Events files were not removed"
            )
    finally:
        connection.close()
        engine.dispose()


def _xe_start(database_url: str, output: Path) -> dict[str, object]:
    engine, connection = _xe_connection(database_url)
    try:
        database_id = int(
            connection.exec_driver_sql("SELECT DB_ID()").scalar_one()
        )
        connection.exec_driver_sql(
            f"""
            IF EXISTS (
                SELECT 1 FROM sys.dm_xe_sessions
                WHERE name = N'{XE_SESSION_NAME}'
            )
            ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=STOP
            """
        )
        connection.exec_driver_sql(
            f"""
            IF EXISTS (
                SELECT 1 FROM sys.server_event_sessions
                WHERE name = N'{XE_SESSION_NAME}'
            )
            DROP EVENT SESSION [{XE_SESSION_NAME}] ON SERVER
            """
        )
        _xe_delete_event_files(connection)
        if _xe_list_event_files(connection):
            raise ValueError(
                "stale SQL Server Extended Events files remain"
            )
        connection.exec_driver_sql(
            f"""
            CREATE EVENT SESSION [{XE_SESSION_NAME}] ON SERVER
            ADD EVENT sqlserver.rpc_completed(
                ACTION(sqlserver.database_id, sqlserver.session_id)
            ),
            ADD EVENT sqlos.wait_info(
                ACTION(sqlserver.database_id, sqlserver.session_id)
                WHERE ([duration] > (1000))
            )
            ADD TARGET package0.event_file(
                SET filename=N'{_xe_event_file_base()}',
                    max_file_size=({XE_EVENT_FILE_SIZE_MB}),
                    max_rollover_files=({XE_EVENT_FILE_ROLLOVER_COUNT})
            )
            WITH (
                MAX_MEMORY=4096 KB,
                EVENT_RETENTION_MODE=ALLOW_SINGLE_EVENT_LOSS,
                MAX_DISPATCH_LATENCY=1 SECONDS,
                TRACK_CAUSALITY=OFF,
                STARTUP_STATE=OFF
            )
            """
        )
        connection.exec_driver_sql(
            f"ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=START"
        )
        started_utc = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        report = {
            "schema": "issue-894-sqlserver-xe-start-v2",
            "label": DIAGNOSTIC_LABEL,
            "session_name": XE_SESSION_NAME,
            "database_id": database_id,
            "target": "event_file",
            "event_file_pattern": _xe_event_file_pattern(),
            "max_file_size_mb": XE_EVENT_FILE_SIZE_MB,
            "max_rollover_files": XE_EVENT_FILE_ROLLOVER_COUNT,
            "started_utc": started_utc,
            "bounded": True,
            "raw_sql_captured_in_evidence": False,
        }
        _write_json(output, report)
        return report
    except Exception:
        try:
            connection.exec_driver_sql(
                f"""
                IF EXISTS (
                    SELECT 1 FROM sys.dm_xe_sessions
                    WHERE name = N'{XE_SESSION_NAME}'
                )
                ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=STOP
                """
            )
            connection.exec_driver_sql(
                f"""
                IF EXISTS (
                    SELECT 1 FROM sys.server_event_sessions
                    WHERE name = N'{XE_SESSION_NAME}'
                )
                DROP EVENT SESSION [{XE_SESSION_NAME}] ON SERVER
                """
            )
            _xe_delete_event_files(connection)
        except Exception:
            pass
        raise
    finally:
        connection.close()
        engine.dispose()


def _xe_value(event: ET.Element, name: str) -> str:
    node = event.find(f"./data[@name='{name}']")
    if node is None:
        node = event.find(f"./action[@name='{name}']")
    if node is None:
        return ""
    text_node = node.find("./text")
    if text_node is not None and text_node.text:
        return str(text_node.text)
    value_node = node.find("./value")
    if value_node is not None and value_node.text:
        return str(value_node.text)
    return ""


def _safe_int(value: object) -> int:
    try:
        return int(str(value or "0"))
    except (TypeError, ValueError):
        return 0


def _parse_xe_event_xml(
    event_xml: str,
    *,
    database_id: int,
) -> dict[str, object] | None:
    node = ET.fromstring(event_xml)
    event_name = str(node.attrib.get("name") or "")
    timestamp = str(node.attrib.get("timestamp") or "")
    event_database_id = _safe_int(_xe_value(node, "database_id"))
    if event_database_id and event_database_id != database_id:
        return None
    session_id = _safe_int(_xe_value(node, "session_id"))
    if not session_id:
        return None
    if event_name == "rpc_completed":
        return {
            "event": "rpc_completed",
            "timestamp_utc": timestamp,
            "session_id": session_id,
            "database_id": event_database_id,
            "duration_us": _safe_int(_xe_value(node, "duration")),
            "cpu_us": _safe_int(_xe_value(node, "cpu_time")),
            "logical_reads": _safe_int(_xe_value(node, "logical_reads")),
            "physical_reads": _safe_int(_xe_value(node, "physical_reads")),
            "row_count": _safe_int(_xe_value(node, "row_count")),
            "object_name": str(_xe_value(node, "object_name") or ""),
        }
    if event_name == "wait_info":
        return {
            "event": "wait_info",
            "timestamp_utc": timestamp,
            "session_id": session_id,
            "database_id": event_database_id,
            "wait_type": str(_xe_value(node, "wait_type") or "UNKNOWN"),
            "duration_ms": float(
                _safe_int(_xe_value(node, "duration"))
            ),
            "opcode": str(_xe_value(node, "opcode") or ""),
        }
    return None


def _xe_stop_collect(database_url: str, output: Path) -> dict[str, object]:
    engine, connection = _xe_connection(database_url)
    capture: dict[str, object] | None = None
    cleanup_error = ""
    try:
        database_id = int(
            connection.exec_driver_sql("SELECT DB_ID()").scalar_one()
        )
        session_dropped = _safe_int(
            connection.exec_driver_sql(
                f"""
                SELECT dropped_event_count
                FROM sys.dm_xe_sessions
                WHERE name = N'{XE_SESSION_NAME}'
                """
            ).scalar_one()
        )
        connection.exec_driver_sql(
            f"ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=STOP"
        )
        time.sleep(1.2)
        files = _xe_list_event_files(connection)
        if not files:
            raise ValueError(
                "SQL Server Extended Events event file is missing"
            )
        if len(files) >= XE_EVENT_FILE_ROLLOVER_COUNT:
            raise ValueError(
                "SQL Server Extended Events bounded file retention exhausted"
            )

        rows = connection.exec_driver_sql(
            f"""
            SELECT
                file_name,
                file_offset,
                CAST(event_data AS nvarchar(max)) AS event_xml
            FROM sys.fn_xe_file_target_read_file(
                N'{_xe_event_file_pattern()}',
                NULL,
                NULL,
                NULL
            )
            ORDER BY file_name, file_offset
            """
        ).all()
        if not rows:
            raise ValueError(
                "SQL Server Extended Events event files contain no events"
            )

        read_files: list[str] = []
        events: list[dict[str, object]] = []
        parse_failures = 0
        for row in rows:
            file_name = str(row[0] or "")
            if file_name and file_name not in read_files:
                read_files.append(file_name)
            try:
                parsed = _parse_xe_event_xml(
                    str(row[2] or ""),
                    database_id=database_id,
                )
            except (ET.ParseError, ValueError):
                parse_failures += 1
                continue
            if parsed is not None:
                events.append(parsed)

        listed_names = {
            name.casefold() for name in files
        }
        read_names = {
            Path(name).name.casefold() for name in read_files
        }
        missing_rollover_files = sorted(listed_names - read_names)
        unexpected_read_files = sorted(read_names - listed_names)
        if (
            parse_failures
            or missing_rollover_files
            or unexpected_read_files
        ):
            raise ValueError(
                "SQL Server Extended Events event-file collection "
                "is incomplete"
            )

        events.sort(
            key=lambda row: str(row.get("timestamp_utc") or "")
        )
        capture = {
            "schema": "issue-894-sqlserver-xe-capture-v2",
            "label": DIAGNOSTIC_LABEL,
            "session_name": XE_SESSION_NAME,
            "database_id": database_id,
            "target": "event_file",
            "event_file_count": len(files),
            "event_files": files,
            "read_event_files": [
                Path(name).name for name in read_files
            ],
            "max_file_size_mb": XE_EVENT_FILE_SIZE_MB,
            "max_rollover_files": XE_EVENT_FILE_ROLLOVER_COUNT,
            "event_count": len(events),
            "dropped_event_count": session_dropped,
            "session_dropped_event_count": session_dropped,
            "file_parse_failure_count": parse_failures,
            "missing_rollover_files": missing_rollover_files,
            "unexpected_read_files": unexpected_read_files,
            "truncated": False,
            "raw_sql_captured_in_evidence": False,
            "events": events,
        }
    finally:
        try:
            connection.exec_driver_sql(
                f"""
                IF EXISTS (
                    SELECT 1 FROM sys.dm_xe_sessions
                    WHERE name = N'{XE_SESSION_NAME}'
                )
                ALTER EVENT SESSION [{XE_SESSION_NAME}] ON SERVER STATE=STOP
                """
            )
            connection.exec_driver_sql(
                f"""
                IF EXISTS (
                    SELECT 1 FROM sys.server_event_sessions
                    WHERE name = N'{XE_SESSION_NAME}'
                )
                DROP EVENT SESSION [{XE_SESSION_NAME}] ON SERVER
                """
            )
            _xe_delete_event_files(connection)
            if _xe_list_event_files(connection):
                raise ValueError(
                    "SQL Server Extended Events files remain after cleanup"
                )
        except Exception as exc:
            cleanup_error = type(exc).__name__
        connection.close()
        engine.dispose()
    if capture is None:
        raise ValueError(
            "SQL Server Extended Events capture was not collected"
        )
    capture["cleanup_succeeded"] = not cleanup_error
    capture["cleanup_error_type"] = cleanup_error
    _write_json(output, capture)
    return capture


def _parse_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _count_distribution(values: list[float]) -> dict[str, float | int]:
    latency_shape = _latencies(values)
    return {
        "count": int(latency_shape["count"]),
        "p50": float(latency_shape["p50_ms"]),
        "p95": float(latency_shape["p95_ms"]),
        "max": float(latency_shape["max_ms"]),
        "mean": float(latency_shape["mean_ms"]),
    }


def _application_execution_markers(
    trace_rows: list[dict[str, object]],
    *,
    spid: int,
) -> list[dict[str, object]]:
    markers = [
        dict(row)
        for row in trace_rows
        if _safe_int(row.get("spid")) == spid
        and row.get("kind") in {"spid_probe", "statement_start"}
        and _safe_int(row.get("connection_execution_sequence")) > 0
    ]
    markers.sort(
        key=lambda row: _safe_int(
            row.get("connection_execution_sequence")
        )
    )
    expected = list(range(1, len(markers) + 1))
    observed = [
        _safe_int(row.get("connection_execution_sequence"))
        for row in markers
    ]
    if observed != expected:
        return []
    return markers


def _server_execution_events(
    capture: dict[str, object],
    *,
    spid: int,
) -> tuple[list[dict[str, object]], int]:
    executions: list[dict[str, object]] = []
    ignored = 0
    for row in capture.get("events", []):
        if (
            not isinstance(row, dict)
            or row.get("event") != "rpc_completed"
            or _safe_int(row.get("session_id")) != spid
        ):
            continue
        name = _normalized_rpc_name(row.get("object_name"))
        if name in _PREPARED_EXECUTION_RPC_NAMES:
            executions.append(dict(row))
        else:
            ignored += 1
    executions.sort(key=lambda row: str(row.get("timestamp_utc") or ""))
    for sequence, event in enumerate(executions, start=1):
        event["server_execution_sequence"] = sequence
    return executions, ignored


def _fetch_time_consistent(
    app_fetch: dict[str, object],
    app_statement: dict[str, object],
    server_event: dict[str, object],
) -> bool:
    statement_start = _parse_utc(app_statement.get("timestamp_utc"))
    fetch_end = _parse_utc(app_fetch.get("timestamp_utc"))
    margin = timedelta(milliseconds=XE_CLOCK_UNCERTAINTY_MS)
    event_time = _parse_utc(server_event.get("timestamp_utc"))
    return (
        statement_start - margin
        <= event_time
        <= fetch_end + margin
    )


def _server_client_correlation(
    trace_rows: list[dict[str, object]],
    capture: dict[str, object],
) -> dict[str, object]:
    target_fingerprints = _current_result_fingerprints(trace_rows)
    app_fetches = [
        dict(row)
        for row in trace_rows
        if row.get("kind") == "dbapi_fetch"
        and str(row.get("statement_fingerprint") or "")
        in target_fingerprints
        and _safe_int(row.get("spid")) > 0
        and _safe_int(row.get("connection_execution_sequence")) > 0
    ]
    wait_events = [
        dict(row)
        for row in capture.get("events", [])
        if isinstance(row, dict) and row.get("event") == "wait_info"
    ]
    observed_spids = sorted(
        {_safe_int(row.get("spid")) for row in app_fetches}
    )

    marker_by_spid: dict[int, list[dict[str, object]]] = {}
    server_by_spid: dict[int, list[dict[str, object]]] = {}
    ignored_rpc_events = 0
    sequence_alignment_failures: list[dict[str, object]] = []
    for spid in observed_spids:
        markers = _application_execution_markers(trace_rows, spid=spid)
        server_events, ignored = _server_execution_events(
            capture,
            spid=spid,
        )
        marker_by_spid[spid] = markers
        server_by_spid[spid] = server_events
        ignored_rpc_events += ignored
        if not markers:
            sequence_alignment_failures.append({
                "spid": spid,
                "reason": "application_sequence_missing_or_non_contiguous",
                "application_execution_count": 0,
                "server_execution_count": len(server_events),
            })
            continue
        if len(markers) != len(server_events):
            sequence_alignment_failures.append({
                "spid": spid,
                "reason": "execution_count_mismatch",
                "application_execution_count": len(markers),
                "server_execution_count": len(server_events),
            })

    server_lookup: dict[tuple[int, int], dict[str, object]] = {}
    for spid, events in server_by_spid.items():
        for event in events:
            key = (
                spid,
                _safe_int(event.get("server_execution_sequence")),
            )
            if key in server_lookup:
                sequence_alignment_failures.append({
                    "spid": spid,
                    "reason": "duplicate_server_execution_sequence",
                    "server_execution_sequence": key[1],
                })
            server_lookup[key] = event

    app_statement_lookup: dict[tuple[int, int], dict[str, object]] = {}
    for spid, markers in marker_by_spid.items():
        for marker in markers:
            sequence = _safe_int(
                marker.get("connection_execution_sequence")
            )
            key = (spid, sequence)
            if marker.get("kind") == "statement_start":
                if key in app_statement_lookup:
                    sequence_alignment_failures.append({
                        "spid": spid,
                        "reason": "duplicate_application_execution_sequence",
                        "connection_execution_sequence": sequence,
                    })
                app_statement_lookup[key] = marker

    used_server_keys: set[tuple[int, int]] = set()
    matches: list[dict[str, object]] = []
    unmatched_app: list[dict[str, object]] = []
    timing_inconsistencies: list[dict[str, object]] = []
    duplicate_mappings = 0
    row_count_mismatches: list[dict[str, object]] = []
    for app in sorted(
        app_fetches,
        key=lambda row: (
            _safe_int(row.get("spid")),
            _safe_int(row.get("connection_execution_sequence")),
        ),
    ):
        spid = _safe_int(app.get("spid"))
        sequence = _safe_int(
            app.get("connection_execution_sequence")
        )
        key = (spid, sequence)
        marker = app_statement_lookup.get(key)
        event = server_lookup.get(key)
        if (
            marker is None
            or event is None
            or str(marker.get("statement_fingerprint") or "")
            != str(app.get("statement_fingerprint") or "")
        ):
            unmatched_app.append(app)
            continue
        if key in used_server_keys:
            duplicate_mappings += 1
            unmatched_app.append(app)
            continue
        if not _fetch_time_consistent(app, marker, event):
            timing_inconsistencies.append({
                "spid": spid,
                "connection_execution_sequence": sequence,
                "statement_fingerprint": str(
                    app.get("statement_fingerprint") or ""
                ),
                "application_fetch_end_utc": str(
                    app.get("timestamp_utc") or ""
                ),
                "server_completion_utc": str(
                    event.get("timestamp_utc") or ""
                ),
            })
            unmatched_app.append(app)
            continue
        app_rows = _safe_int(app.get("fetched_row_count"))
        server_rows = _safe_int(event.get("row_count"))
        if app_rows != server_rows:
            row_count_mismatches.append({
                "spid": spid,
                "connection_execution_sequence": sequence,
                "statement_fingerprint": str(
                    app.get("statement_fingerprint") or ""
                ),
                "application_row_count": app_rows,
                "server_row_count": server_rows,
            })
            unmatched_app.append(app)
            continue

        used_server_keys.add(key)
        event_end = _parse_utc(event.get("timestamp_utc"))
        event_start = event_end - timedelta(
            microseconds=_safe_int(event.get("duration_us"))
        )
        waits: dict[str, list[float]] = {}
        for wait in wait_events:
            if _safe_int(wait.get("session_id")) != spid:
                continue
            wait_time = _parse_utc(wait.get("timestamp_utc"))
            if event_start <= wait_time <= event_end:
                waits.setdefault(
                    str(wait.get("wait_type") or "UNKNOWN"),
                    [],
                ).append(float(wait.get("duration_ms") or 0.0))

        matches.append({
            "statement_fingerprint": str(
                app.get("statement_fingerprint") or ""
            ),
            "request_id": str(app.get("request_id") or ""),
            "connection_id": _safe_int(app.get("connection_id")),
            "spid": spid,
            "statement_sequence": _safe_int(
                app.get("statement_sequence")
            ),
            "connection_execution_sequence": sequence,
            "server_execution_sequence": _safe_int(
                event.get("server_execution_sequence")
            ),
            "fetch_elapsed_ms": float(
                app.get("fetch_elapsed_ms") or 0.0
            ),
            "server_duration_ms": (
                _safe_int(event.get("duration_us")) / 1000.0
            ),
            "server_cpu_ms": (
                _safe_int(event.get("cpu_us")) / 1000.0
            ),
            "logical_reads": _safe_int(event.get("logical_reads")),
            "physical_reads": _safe_int(event.get("physical_reads")),
            "row_count": _safe_int(event.get("row_count")),
            "rpc_object_name": str(
                event.get("object_name") or ""
            ),
            "waits_ms": {
                wait_type: _latencies(values)
                for wait_type, values in sorted(waits.items())
            },
            "_wait_durations_ms": waits,
        })

    unmatched_server = [
        event
        for spid, events in server_by_spid.items()
        for event in events
        if (
            spid,
            _safe_int(event.get("server_execution_sequence")),
        )
        not in used_server_keys
    ]

    by_fingerprint: dict[str, dict[str, object]] = {}
    for fingerprint in target_fingerprints:
        values = [
            match
            for match in matches
            if match["statement_fingerprint"] == fingerprint
        ]
        waits: dict[str, list[float]] = {}
        for match in values:
            for wait_type, durations in match[
                "_wait_durations_ms"
            ].items():
                waits.setdefault(wait_type, []).extend(
                    float(value) for value in durations
                )
        by_fingerprint[fingerprint] = {
            "correlated_execution_count": len(values),
            "server_duration_ms": _latencies(
                [
                    float(row["server_duration_ms"])
                    for row in values
                ]
            ),
            "server_cpu_ms": _latencies(
                [float(row["server_cpu_ms"]) for row in values]
            ),
            "logical_reads": _count_distribution(
                [float(row["logical_reads"]) for row in values]
            ),
            "physical_reads": _count_distribution(
                [float(row["physical_reads"]) for row in values]
            ),
            "row_count": _count_distribution(
                [float(row["row_count"]) for row in values]
            ),
            "waits_ms": {
                wait_type: _latencies(durations)
                for wait_type, durations in sorted(waits.items())
            },
            "spids": sorted(
                {int(row["spid"]) for row in values}
            ),
            "prepared_rpc_names": sorted({
                str(row["rpc_object_name"])
                for row in values
                if str(row["rpc_object_name"])
            }),
        }

    prepared_execution_observed = any(
        _normalized_rpc_name(match.get("rpc_object_name"))
        in _PREPARED_EXECUTION_RPC_NAMES
        for match in matches
    )
    dropped = _safe_int(capture.get("dropped_event_count"))
    truncated = bool(capture.get("truncated"))
    cleanup_succeeded = capture.get("cleanup_succeeded") is True
    complete = (
        len(target_fingerprints) == 3
        and all(
            int(
                by_fingerprint[fingerprint][
                    "correlated_execution_count"
                ]
            )
            > 0
            for fingerprint in target_fingerprints
        )
        and not unmatched_app
        and not sequence_alignment_failures
        and not timing_inconsistencies
        and not row_count_mismatches
        and duplicate_mappings == 0
        and dropped == 0
        and not truncated
        and cleanup_succeeded
        and prepared_execution_observed
    )
    return {
        "schema": "issue-894-server-client-correlation-v2",
        "label": DIAGNOSTIC_LABEL,
        "target_fingerprints": target_fingerprints,
        "targets": by_fingerprint,
        "correlated_execution_count": len(matches),
        "unmatched_server_events": len(unmatched_server),
        "unmatched_application_fetches": len(unmatched_app),
        "ambiguous_correlations": duplicate_mappings,
        "sequence_alignment_failures": sequence_alignment_failures,
        "timing_inconsistencies": timing_inconsistencies,
        "row_count_mismatches": row_count_mismatches,
        "ignored_non_execution_rpc_events": ignored_rpc_events,
        "dropped_event_count": dropped,
        "capture_truncated": truncated,
        "cleanup_succeeded": cleanup_succeeded,
        "prepared_execution_observed": prepared_execution_observed,
        "clock_uncertainty_ms": XE_CLOCK_UNCERTAINTY_MS,
        "clock_correlation_basis": (
            "physical-connection/SPID execution order is the primary "
            "identity rule; server completion must also fall between the "
            "application statement start and DBAPI fetch completion plus "
            "the bounded UTC clock-uncertainty margin; application "
            "monotonic clocks are never subtracted directly from SQL Server "
            "timestamps"
        ),
        "complete": complete,
    }


def _correlate_report(
    trial_path: Path,
    trace_path: Path,
    server_path: Path,
    output: Path,
) -> dict[str, object]:
    report = json.loads(trial_path.read_text(encoding="utf-8"))
    capture = json.loads(server_path.read_text(encoding="utf-8"))
    correlation = _server_client_correlation(
        _trace_rows(trace_path),
        capture,
    )
    report["server_client_correlation"] = correlation
    _write_json(output, report)
    return report

def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _trace_rows(path: Path | None) -> list[dict[str, object]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _first_trace_stall(path: Path | None) -> dict[str, object] | None:
    for row in _trace_rows(path):
        if row.get("kind") == "first_stall_snapshot":
            return row
    return None


def _operation(counter: int, resource_id: str, admin: Credential):
    if counter % 10 == 0:
        return (
            "save_mutations",
            "PATCH",
            f"/api/v1/resources/{resource_id}",
            _headers(admin, csrf=True),
            {
                "name": (
                    "Ressource synthetique Phase 4 #894 "
                    f"{counter % 1000:03d}"
                )
            },
        )
    if counter % 2 == 0:
        return (
            "planning_reads",
            "GET",
            "/api/v1/planning/snapshot?start=2026-01-05&end=2026-01-09",
            _headers(admin),
            None,
        )
    return (
        "project_reads",
        "GET",
        "/api/v1/projects?active_only=true",
        _headers(admin),
        None,
    )


async def _trial(args: argparse.Namespace) -> dict[str, object]:
    admin, _users = _load_credentials(args.credentials)
    trace_file = args.trace_file
    started = time.perf_counter()
    stop_at = started + args.duration_seconds
    stop_offering = asyncio.Event()
    semaphore = asyncio.Semaphore(args.concurrency)
    client = httpx.AsyncClient(
        base_url=args.base_url,
        cookies=_cookies(admin),
        timeout=10.0,
    )

    scheduled = 0
    started_requests = 0
    started_in_window = 0
    completed = 0
    completed_in_window = 0
    succeeded = 0
    failed = 0
    in_flight = 0
    max_in_flight = 0
    outstanding = 0
    max_outstanding = 0
    first_failure_monotonic: float | None = None
    scheduling_lag_ms: list[float] = []
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    errors: list[dict[str, object]] = []
    request_records: dict[str, dict[str, object]] = {}
    operation_counts = {
        "planning_reads": 0,
        "project_reads": 0,
        "save_mutations": 0,
    }
    tasks: list[asyncio.Task[None]] = []
    first_stall_sql: dict[str, object] | None = None
    first_stall_sql_lock = asyncio.Lock()

    resource_id = await _ensure_synthetic_resource(client, admin)
    reset_response = await client.patch(
        f"/api/v1/resources/{resource_id}",
        headers=_headers(admin, csrf=True),
        json={"name": "Ressource synthetique Phase 4 #894 diagnostic"},
    )
    reset_response.raise_for_status()

    async def capture_sql_stall(trigger: str) -> None:
        nonlocal first_stall_sql
        if first_stall_sql is not None:
            return
        async with first_stall_sql_lock:
            if first_stall_sql is not None:
                return
            captured_at = time.perf_counter()
            try:
                snapshot = await asyncio.to_thread(
                    _sql_wait_snapshot,
                    args.database_url,
                )
                first_stall_sql = {
                    "trigger": trigger,
                    "captured_at_monotonic": captured_at,
                    "snapshot": snapshot,
                }
            except Exception as exc:
                first_stall_sql = {
                    "trigger": trigger,
                    "captured_at_monotonic": captured_at,
                    "snapshot": {
                        "available": False,
                        "exception": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                }

    async def watch_stall() -> None:
        if trace_file is None:
            return
        while not stop_offering.is_set() and time.perf_counter() < stop_at:
            marker = _first_trace_stall(trace_file)
            if marker is not None:
                await capture_sql_stall("instrumented_first_pool_stall")
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_stall())

    async def one(counter: int, scheduled_at: float) -> None:
        nonlocal started_requests, started_in_window
        nonlocal completed, completed_in_window, succeeded, failed
        nonlocal in_flight, max_in_flight, outstanding, first_failure_monotonic

        async with semaphore:
            actual_start = time.perf_counter()
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            started_requests += 1
            if actual_start <= stop_at:
                started_in_window += 1
            scheduling_lag_ms.append(
                max(0.0, (actual_start - scheduled_at) * 1000.0)
            )

            operation, method, path, headers, body = _operation(
                counter,
                resource_id,
                admin,
            )
            operation_counts[operation] += 1
            diagnostic_id = uuid4().hex[:20]
            headers = {
                **headers,
                "X-RP894-Diagnostic-ID": diagnostic_id,
            }
            record = {
                "diagnostic_id": diagnostic_id,
                "counter": counter,
                "operation": operation,
                "method": method,
                "client_start_monotonic": actual_start,
                "client_timeout_monotonic": None,
                "client_complete_monotonic": None,
                "elapsed_ms": None,
                "status": None,
                "error": None,
            }
            request_records[diagnostic_id] = record
            try:
                response_started = time.perf_counter()
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    json=body,
                )
                elapsed = (time.perf_counter() - response_started) * 1000.0
                latencies.append(elapsed)
                status = str(response.status_code)
                statuses[status] = statuses.get(status, 0) + 1
                record["status"] = status
                record["elapsed_ms"] = elapsed
                record["client_complete_monotonic"] = time.perf_counter()
                succeeded += 1
            except httpx.TimeoutException as exc:
                now = time.perf_counter()
                elapsed = max(0.0, (now - actual_start) * 1000.0)
                latencies.append(elapsed)
                category = (
                    "read_timeout"
                    if isinstance(exc, httpx.ReadTimeout)
                    else "timeout"
                )
                record["error"] = category
                record["elapsed_ms"] = elapsed
                record["client_timeout_monotonic"] = now
                failed += 1
                errors.append(
                    {
                        "diagnostic_id": diagnostic_id,
                        "operation": operation,
                        "category": category,
                        "exception": type(exc).__name__,
                        "elapsed_ms": elapsed,
                    }
                )
                if first_failure_monotonic is None:
                    first_failure_monotonic = now
                stop_offering.set()
                await capture_sql_stall("client_request_error")
                await capture_sql_stall("client_timeout")
            except httpx.RequestError as exc:
                now = time.perf_counter()
                elapsed = max(0.0, (now - actual_start) * 1000.0)
                latencies.append(elapsed)
                record["error"] = "request_error"
                record["elapsed_ms"] = elapsed
                record["client_complete_monotonic"] = now
                failed += 1
                errors.append(
                    {
                        "diagnostic_id": diagnostic_id,
                        "operation": operation,
                        "category": "request_error",
                        "exception": type(exc).__name__,
                        "elapsed_ms": elapsed,
                    }
                )
                if first_failure_monotonic is None:
                    first_failure_monotonic = now
                stop_offering.set()
            finally:
                completed += 1
                if time.perf_counter() <= stop_at:
                    completed_in_window += 1
                in_flight -= 1
                outstanding -= 1

    interval = 1.0 / args.offered_rps
    next_release = started
    while next_release < stop_at and not stop_offering.is_set():
        await asyncio.sleep(max(0.0, next_release - time.perf_counter()))
        if stop_offering.is_set():
            break
        scheduled += 1
        outstanding += 1
        max_outstanding = max(max_outstanding, outstanding)
        tasks.append(asyncio.create_task(one(scheduled, next_release)))
        next_release += interval

    measurement_end = min(time.perf_counter(), stop_at)
    measurement_snapshot = {
        "monotonic": measurement_end,
        "scheduled": scheduled,
        "started": started_requests,
        "started_in_measurement_window": started_in_window,
        "completed": completed,
        "completed_in_measurement_window": completed_in_window,
        "outstanding": outstanding,
        "in_flight": in_flight,
        "max_in_flight": max_in_flight,
        "max_outstanding": max_outstanding,
    }

    if tasks:
        await asyncio.gather(*tasks)
    client_drained_at = time.perf_counter()
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    await client.aclose()

    observation_started = time.perf_counter()
    if trace_file is not None and errors:
        timed_out_ids = {
            str(item["diagnostic_id"])
            for item in errors
            if item.get("category") in {"read_timeout", "timeout"}
        }
        observation_deadline = (
            observation_started + args.server_observation_seconds
        )
        while timed_out_ids and time.perf_counter() < observation_deadline:
            rows = _trace_rows(trace_file)
            ended_ids = {
                str(row.get("request_id") or "")
                for row in rows
                if row.get("kind") == "request_end"
            }
            if timed_out_ids <= ended_ids:
                break
            await asyncio.sleep(0.10)

    observation_finished = time.perf_counter()
    trace_rows = _trace_rows(trace_file)
    timeout_correlations = _correlate_timeouts(
        errors,
        request_records,
        trace_rows,
    )

    first_stall = next(
        (row for row in trace_rows if row.get("kind") == "first_stall_snapshot"),
        None,
    )
    pre_stall_end = (
        float(first_stall.get("monotonic") or 0.0)
        if isinstance(first_stall, dict)
        else first_failure_monotonic
    )
    pre_stall_requests = [
        row for row in request_records.values()
        if pre_stall_end is not None
        and float(row["client_start_monotonic"]) < pre_stall_end
    ]
    pre_stall_completed = [
        row
        for row in pre_stall_requests
        if (
            row.get("client_complete_monotonic") is not None
            and float(row["client_complete_monotonic"]) <= float(pre_stall_end)
        )
    ]
    pre_stall_failed = [
        row
        for row in pre_stall_requests
        if row.get("error") is not None
    ]
    pre_stall_latencies = [
        float(row["elapsed_ms"])
        for row in pre_stall_completed
        if row.get("elapsed_ms") is not None
    ]

    configured_window = max(0.001, args.duration_seconds)
    actual_offer_seconds = max(0.001, measurement_end - started)
    drain_seconds = max(0.0, client_drained_at - measurement_end)
    server_observation_seconds = max(
        0.0,
        observation_finished - observation_started,
    )
    achieved_request_start_rps = started_in_window / configured_window
    request_start_rate_ok = achieved_request_start_rps >= args.offered_rps * 0.95
    diagnostic_acceptance_behavior_failed = (
        failed > 0
        or not request_start_rate_ok
    )

    report = {
        "schema": "issue-894-connection-trial-v1",
        "label": DIAGNOSTIC_LABEL,
        "mode": args.mode,
        "certification_pass": False,
        "duration_target_seconds": args.duration_seconds,
        "offered_rps": args.offered_rps,
        "concurrency_bound": args.concurrency,
        "http_timeout_seconds": 10.0,
        "scheduled": scheduled,
        "started": started_requests,
        "started_in_measurement_window": started_in_window,
        "completed": completed,
        "completed_in_measurement_window": completed_in_window,
        "succeeded": succeeded,
        "failed": failed,
        "outstanding_at_window_end": measurement_snapshot["outstanding"],
        "drain_duration_seconds": drain_seconds,
        "server_observation_duration_seconds": server_observation_seconds,
        "achieved_schedule_rps": scheduled / actual_offer_seconds,
        "achieved_request_start_rps": achieved_request_start_rps,
        "request_start_rate_ok": request_start_rate_ok,
        "diagnostic_acceptance_behavior_failed": (
            diagnostic_acceptance_behavior_failed
        ),
        "achieved_completion_rps": completed_in_window / configured_window,
        "max_in_flight": max_in_flight,
        "max_outstanding": max_outstanding,
        "operation_counts": operation_counts,
        "statuses": statuses,
        "latency": _latencies(latencies),
        "scheduling_lag": _latencies(scheduling_lag_ms),
        "errors": errors,
        "measurement_window_snapshot": measurement_snapshot,
        "first_failure_monotonic": first_failure_monotonic,
        "first_stall": first_stall,
        "first_stall_sql": first_stall_sql,
        "pre_stall": {
            "boundary_monotonic": pre_stall_end,
            "started": len(pre_stall_requests),
            "completed": len(pre_stall_completed),
            "failed": len(pre_stall_failed),
            "latency": _latencies(pre_stall_latencies),
        },
        "timeout_correlations": timeout_correlations,
        "trace_summary": _summarize_trace(trace_rows),
    }
    _write_json(args.output, report)
    return report


def _correlate_timeouts(
    errors: list[dict[str, object]],
    request_records: dict[str, dict[str, object]],
    trace_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    trace_by_request: dict[str, list[dict[str, object]]] = {}
    for row in trace_rows:
        request_id = str(row.get("request_id") or "")
        if request_id:
            trace_by_request.setdefault(request_id, []).append(row)

    correlations: list[dict[str, object]] = []
    for error in errors[:50]:
        diagnostic_id = str(error["diagnostic_id"])
        record = request_records[diagnostic_id]
        timeout_at = record.get("client_timeout_monotonic")
        rows = trace_by_request.get(diagnostic_id, [])
        request_end = next(
            (row for row in rows if row.get("kind") == "request_end"),
            None,
        )
        returns = [
            row for row in rows
            if row.get("kind") == "connection_return"
        ]
        later_returns = [
            row for row in returns
            if isinstance(timeout_at, float)
            and float(row.get("monotonic") or 0.0) > timeout_at
        ]
        correlations.append(
            {
                "diagnostic_id": diagnostic_id,
                "operation": error["operation"],
                "client_timeout_monotonic": timeout_at,
                "server_request_end": request_end,
                "connection_returns_after_client_timeout": later_returns,
            }
        )
    return correlations


def _summarize_trace(rows: list[dict[str, object]]) -> dict[str, object]:
    by_kind: dict[str, int] = {}
    executor_queue: list[float] = []
    worker_ms: list[float] = []
    executor_by_phase: dict[str, dict[str, list[float]]] = {}
    pool_wait: list[float] = []
    statement_ms: list[float] = []
    post_statement: list[float] = []
    holding_ms: list[float] = []
    materialization_counts: list[float] = []
    statement_to_materialization: list[float] = []
    materialization_span: list[float] = []
    post_materialization: list[float] = []
    current_fingerprints = _current_result_fingerprints(rows)
    targeted: dict[str, dict[str, object]] = {
        fingerprint: {
            "fetch_rows": [],
            "fetch_ms": [],
            "fetch_cpu_ms": [],
            "fetch_errors": 0,
            "consumer_ms": [],
            "nested_fetch_ms": [],
            "non_nested_ms": [],
            "projection_ms": [],
            "worker_cpu_ms": [],
            "non_nested_worker_cpu_ms": [],
            "call_sites": set(),
            "request_ids": set(),
            "connection_ids": set(),
            "stall_matches": set(),
        }
        for fingerprint in current_fingerprints
    }
    first_stall = next(
        (row for row in rows if row.get("kind") == "first_stall_snapshot"),
        None,
    )
    stall_pairs = {
        (
            str(holder.get("request_id") or ""),
            int(holder.get("connection_id") or 0),
        )
        for holder in (
            first_stall.get("holders", [])
            if isinstance(first_stall, dict)
            else []
        )
        if isinstance(holder, dict)
    }

    for row in rows:
        kind = str(row.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind == "executor_worker_start":
            queue_value = float(row.get("queue_wait_ms") or 0.0)
            executor_queue.append(queue_value)
            phase = str(row.get("phase") or "other")
            executor_by_phase.setdefault(
                phase,
                {"queue_ms": [], "worker_ms": []},
            )["queue_ms"].append(queue_value)
        elif kind == "executor_worker_complete":
            worker_value = float(row.get("worker_ms") or 0.0)
            worker_ms.append(worker_value)
            phase = str(row.get("phase") or "other")
            executor_by_phase.setdefault(
                phase,
                {"queue_ms": [], "worker_ms": []},
            )["worker_ms"].append(worker_value)
        elif kind == "pool_checkout_complete":
            pool_wait.append(float(row.get("wait_ms") or 0.0))
        elif kind == "statement_end":
            statement_ms.append(float(row.get("statement_ms") or 0.0))
        elif kind == "connection_return":
            if row.get("post_statement_connection_residence_ms") is not None:
                post_statement.append(
                    float(row.get("post_statement_connection_residence_ms") or 0.0)
                )
            if row.get("holding_ms") is not None:
                holding_ms.append(float(row.get("holding_ms") or 0.0))
        elif kind == "request_end":
            materialization_counts.append(
                float(row.get("orm_materialization_count") or 0.0)
            )
            if row.get("statement_end_to_first_materialization_ms") is not None:
                statement_to_materialization.append(
                    float(
                        row.get(
                            "statement_end_to_first_materialization_ms"
                        )
                        or 0.0
                    )
                )
            if row.get("materialization_span_ms") is not None:
                materialization_span.append(
                    float(row.get("materialization_span_ms") or 0.0)
                )
            if row.get("last_materialization_to_request_end_ms") is not None:
                post_materialization.append(
                    float(
                        row.get(
                            "last_materialization_to_request_end_ms"
                        )
                        or 0.0
                    )
                )

        fingerprint = str(row.get("statement_fingerprint") or "")
        detail = targeted.get(fingerprint)
        if detail is None:
            continue
        request_id = str(row.get("request_id") or "")
        connection_id = int(row.get("connection_id") or 0)
        if request_id:
            detail["request_ids"].add(request_id)
        if connection_id:
            detail["connection_ids"].add(connection_id)
        if (request_id, connection_id) in stall_pairs:
            detail["stall_matches"].add((request_id, connection_id))
        call_site = str(row.get("call_site") or "")
        if call_site:
            detail["call_sites"].add(call_site)
        if kind == "dbapi_fetch":
            detail["fetch_rows"].append(float(row.get("fetched_row_count") or 0.0))
            detail["fetch_ms"].append(float(row.get("fetch_elapsed_ms") or 0.0))
            detail["fetch_cpu_ms"].append(float(row.get("fetch_cpu_ms") or 0.0))
            if bool(row.get("fetch_error")):
                detail["fetch_errors"] = int(detail["fetch_errors"]) + 1
        elif kind == "result_consumer":
            detail["consumer_ms"].append(
                float(row.get("consumer_elapsed_ms") or 0.0)
            )
            detail["nested_fetch_ms"].append(
                float(row.get("nested_fetch_elapsed_ms") or 0.0)
            )
            detail["non_nested_ms"].append(
                float(row.get("non_nested_consumer_elapsed_ms") or 0.0)
            )
            detail["projection_ms"].append(
                float(row.get("immediate_projection_elapsed_ms") or 0.0)
            )
            detail["worker_cpu_ms"].append(
                float(row.get("worker_cpu_ms") or 0.0)
            )
            detail["non_nested_worker_cpu_ms"].append(
                float(row.get("non_nested_worker_cpu_ms") or 0.0)
            )

    targeted_summary: dict[str, dict[str, object]] = {}
    for fingerprint, detail in targeted.items():
        call_sites = sorted(str(value) for value in detail["call_sites"])
        fetch_count = len(detail["fetch_ms"])
        consumer_count = len(detail["consumer_ms"])
        evidence_complete = bool(
            fetch_count
            and consumer_count
            and call_sites
            and "unmapped" not in call_sites
            and len(detail["fetch_rows"]) == fetch_count
            and len(detail["fetch_cpu_ms"]) == fetch_count
            and int(detail["fetch_errors"]) == 0
            and len(detail["nested_fetch_ms"]) == consumer_count
            and len(detail["non_nested_ms"]) == consumer_count
            and len(detail["projection_ms"]) == consumer_count
            and len(detail["worker_cpu_ms"]) == consumer_count
            and len(detail["non_nested_worker_cpu_ms"]) == consumer_count
        )
        fetch_shape = _latencies(detail["fetch_ms"])
        non_nested_shape = _latencies(detail["non_nested_ms"])
        non_nested_cpu_shape = _latencies(detail["non_nested_worker_cpu_ms"])
        if float(fetch_shape["p50_ms"]) >= float(non_nested_shape["p50_ms"]):
            dominant_stage = "fetching"
        elif (
            float(non_nested_shape["p50_ms"]) > 0.0
            and float(non_nested_cpu_shape["p50_ms"])
            >= float(non_nested_shape["p50_ms"]) * 0.5
        ):
            dominant_stage = "active_processing"
        else:
            dominant_stage = "waiting_elsewhere"

        targeted_summary[fingerprint] = {
            "observed_fetches": fetch_count,
            "observed_consumers": consumer_count,
            "fetch_errors": int(detail["fetch_errors"]),
            "call_sites": call_sites,
            "request_count": len(detail["request_ids"]),
            "connection_count": len(detail["connection_ids"]),
            "first_stall_request_connection_matches": len(
                detail["stall_matches"]
            ),
            "fetched_row_count": _count_distribution(detail["fetch_rows"]),
            "fetch_elapsed_ms": fetch_shape,
            "fetch_cpu_ms": _latencies(detail["fetch_cpu_ms"]),
            "consumer_elapsed_ms": _latencies(detail["consumer_ms"]),
            "nested_fetch_elapsed_ms": _latencies(detail["nested_fetch_ms"]),
            "non_nested_consumer_elapsed_ms": _latencies(
                detail["non_nested_ms"]
            ),
            "immediate_projection_elapsed_ms": _latencies(
                detail["projection_ms"]
            ),
            "worker_cpu_ms": _latencies(detail["worker_cpu_ms"]),
            "non_nested_worker_cpu_ms": _latencies(
                detail["non_nested_worker_cpu_ms"]
            ),
            "derived_dominant_stage": dominant_stage,
            "evidence_complete": evidence_complete,
        }

    return {
        "event_counts": by_kind,
        "executor_queue_ms": _latencies(executor_queue),
        "executor_worker_ms": _latencies(worker_ms),
        "executor_by_phase": {
            phase: {
                "queue_ms": _latencies(values["queue_ms"]),
                "worker_ms": _latencies(values["worker_ms"]),
            }
            for phase, values in sorted(executor_by_phase.items())
        },
        "pool_checkout_wait_ms": _latencies(pool_wait),
        "statement_execution_ms": _latencies(statement_ms),
        "post_statement_connection_residence_ms": _latencies(post_statement),
        "connection_holding_ms": _latencies(holding_ms),
        "orm_materialization_count": _latencies(materialization_counts),
        "statement_end_to_first_materialization_ms": _latencies(
            statement_to_materialization
        ),
        "materialization_span_ms": _latencies(materialization_span),
        "last_materialization_to_request_end_ms": _latencies(
            post_materialization
        ),
        "list_shifts_fingerprint": (
            current_fingerprints[0]
            if len(current_fingerprints) == 3
            else ""
        ),
        "target_fingerprints": current_fingerprints,
        "target_identification_ambiguous": len(current_fingerprints) != 3,
        "targeted_result_timing": targeted_summary,
        "targeted_timing_complete": (
            len(current_fingerprints) == 3
            and all(
                bool(targeted_summary[fingerprint]["evidence_complete"])
                for fingerprint in current_fingerprints
            )
        ),
    }


def _validated_trial_report(
    path: Path,
    *,
    expected_mode: str,
    require_server_correlation: bool = False,
) -> dict[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema") != "issue-894-connection-trial-v1":
        raise ValueError(f"{expected_mode} trial schema is invalid")
    if report.get("label") != DIAGNOSTIC_LABEL:
        raise ValueError(f"{expected_mode} trial label is invalid")
    if report.get("mode") != expected_mode:
        raise ValueError(f"{expected_mode} trial mode is invalid")
    if report.get("certification_pass") is not False:
        raise ValueError(f"{expected_mode} trial must never certify")
    if not isinstance(report.get("diagnostic_acceptance_behavior_failed"), bool):
        raise ValueError(
            f"{expected_mode} diagnostic outcome is missing or malformed"
        )
    for field in (
        "achieved_request_start_rps",
        "completed",
        "failed",
        "drain_duration_seconds",
    ):
        value = report.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"{expected_mode} trial field {field} is missing or malformed"
            )
    latency = report.get("latency")
    if not isinstance(latency, dict):
        raise ValueError(f"{expected_mode} trial latency is missing or malformed")
    p95 = latency.get("p95_ms")
    if not isinstance(p95, (int, float)) or isinstance(p95, bool):
        raise ValueError(
            f"{expected_mode} trial latency p95 is missing or malformed"
        )
    trace_summary = report.get("trace_summary")
    if not isinstance(trace_summary, dict):
        raise ValueError(f"{expected_mode} trace summary is missing or malformed")
    targeted = trace_summary.get("targeted_result_timing")
    if not isinstance(targeted, dict):
        raise ValueError(
            f"{expected_mode} targeted result timing is missing or malformed"
        )
    if expected_mode == "instrumented":
        if trace_summary.get("targeted_timing_complete") is not True:
            raise ValueError("instrumented targeted result timing is incomplete")
        list_shifts_fingerprint = trace_summary.get("list_shifts_fingerprint")
        if not isinstance(list_shifts_fingerprint, str) or not list_shifts_fingerprint:
            raise ValueError("instrumented list_shifts fingerprint is missing")
        fingerprints = trace_summary.get("target_fingerprints")
        if not isinstance(fingerprints, (list, tuple)) or len(fingerprints) != 3:
            raise ValueError("instrumented target fingerprints are malformed")
        expected_fingerprints = (
            list_shifts_fingerprint,
            *UNCHANGED_RESULT_FINGERPRINTS,
        )
        if tuple(str(value) for value in fingerprints) != expected_fingerprints:
            raise ValueError("instrumented target fingerprint recognition is invalid")
        for fingerprint in expected_fingerprints:
            detail = targeted.get(fingerprint)
            if not isinstance(detail, dict) or detail.get("evidence_complete") is not True:
                raise ValueError(
                    f"instrumented timing evidence incomplete for {fingerprint}"
                )
        if require_server_correlation:
            correlation = report.get("server_client_correlation")
            if (
                not isinstance(correlation, dict)
                or correlation.get("complete") is not True
            ):
                raise ValueError(
                    "instrumented SQL Server correlation is incomplete"
                )
    else:
        event_counts = trace_summary.get("event_counts")
        if not isinstance(event_counts, dict):
            raise ValueError("canonical trace event counts are missing or malformed")
        if int(event_counts.get("dbapi_fetch") or 0) != 0:
            raise ValueError("canonical mode unexpectedly contains detailed fetch tracing")
        if int(event_counts.get("result_consumer") or 0) != 0:
            raise ValueError(
                "canonical mode unexpectedly contains detailed consumer tracing"
            )
    return report


def _compare(args: argparse.Namespace) -> dict[str, object]:
    canonical = _validated_trial_report(
        args.canonical,
        expected_mode="canonical",
    )
    instrumented = _validated_trial_report(
        args.instrumented,
        expected_mode="instrumented",
        require_server_correlation=True,
    )

    canonical_rate = float(canonical.get("achieved_request_start_rps") or 0.0)
    instrumented_rate = float(instrumented.get("achieved_request_start_rps") or 0.0)
    canonical_p95 = float((canonical.get("latency") or {}).get("p95_ms") or 0.0)
    instrumented_p95 = float(
        (instrumented.get("latency") or {}).get("p95_ms") or 0.0
    )
    rate_floor = canonical_rate * 0.90
    latency_ceiling = max(canonical_p95 * 1.10, canonical_p95 + 50.0)
    canonical_completed = max(1, int(canonical.get("completed") or 0))
    instrumented_completed = max(1, int(instrumented.get("completed") or 0))
    canonical_failures = int(canonical.get("failed") or 0)
    instrumented_failures = int(instrumented.get("failed") or 0)
    canonical_failure_rate = canonical_failures / canonical_completed
    instrumented_failure_rate = instrumented_failures / instrumented_completed
    failure_rate_ceiling = min(
        1.0,
        canonical_failure_rate + FAILURE_RATE_MATERIAL_DELTA,
    )
    material = (
        instrumented_rate < rate_floor
        or instrumented_p95 > latency_ceiling
        or instrumented_failure_rate > failure_rate_ceiling
    )

    report = {
        "schema": "issue-894-connection-comparison-v1",
        "label": DIAGNOSTIC_LABEL,
        "certification_pass": False,
        "canonical": {
            "achieved_request_start_rps": canonical_rate,
            "latency_p95_ms": canonical_p95,
            "failed": canonical_failures,
            "failure_rate": canonical_failure_rate,
            "drain_duration_seconds": float(
                canonical.get("drain_duration_seconds") or 0.0
            ),
        },
        "instrumented": {
            "achieved_request_start_rps": instrumented_rate,
            "latency_p95_ms": instrumented_p95,
            "failed": instrumented_failures,
            "failure_rate": instrumented_failure_rate,
            "drain_duration_seconds": float(
                instrumented.get("drain_duration_seconds") or 0.0
            ),
        },
        "delta": {
            "request_start_rps": instrumented_rate - canonical_rate,
            "latency_p95_ms": instrumented_p95 - canonical_p95,
            "failure_rate": instrumented_failure_rate - canonical_failure_rate,
        },
        "materiality_thresholds": {
            "request_start_rate_floor": rate_floor,
            "latency_p95_ceiling_ms": latency_ceiling,
            "failure_rate_ceiling": failure_rate_ceiling,
            "failure_rate_max_absolute_delta": FAILURE_RATE_MATERIAL_DELTA,
        },
        "material_instrumentation_deterioration": material,
        "interpretation_allowed": not material,
    }
    _write_json(args.output, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    trial = sub.add_parser("trial")
    trial.add_argument("--base-url", default="http://frontend:8080")
    trial.add_argument("--credentials", type=Path, required=True)
    trial.add_argument("--database-url", required=True)
    trial.add_argument("--output", type=Path, required=True)
    trial.add_argument(
        "--mode",
        choices=("canonical", "instrumented"),
        required=True,
    )
    trial.add_argument(
        "--duration-seconds",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
    )
    trial.add_argument("--offered-rps", type=float, default=ORDINARY_RPS)
    trial.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
    )
    trial.add_argument("--trace-file", type=Path)
    trial.add_argument(
        "--server-observation-seconds",
        type=float,
        default=35.0,
    )

    xe_start = sub.add_parser("xe-start")
    xe_start.add_argument("--database-url", required=True)
    xe_start.add_argument("--output", type=Path, required=True)

    xe_stop = sub.add_parser("xe-stop")
    xe_stop.add_argument("--database-url", required=True)
    xe_stop.add_argument("--output", type=Path, required=True)

    xe_cleanup = sub.add_parser("xe-cleanup")
    xe_cleanup.add_argument("--database-url", required=True)

    correlate = sub.add_parser("correlate")
    correlate.add_argument("--trial", type=Path, required=True)
    correlate.add_argument("--trace", type=Path, required=True)
    correlate.add_argument("--server", type=Path, required=True)
    correlate.add_argument("--output", type=Path, required=True)

    compare = sub.add_parser("compare")
    compare.add_argument("--canonical", type=Path, required=True)
    compare.add_argument("--instrumented", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "trial":
        report = asyncio.run(_trial(args))
        _validated_trial_report(args.output, expected_mode=args.mode)
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == "xe-start":
        report = _xe_start(args.database_url, args.output)
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == "xe-stop":
        report = _xe_stop_collect(args.database_url, args.output)
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == "xe-cleanup":
        _xe_cleanup(args.database_url)
        return 0
    if args.command == "correlate":
        report = _correlate_report(
            args.trial,
            args.trace,
            args.server,
            args.output,
        )
        _validated_trial_report(
            args.output,
            expected_mode="instrumented",
            require_server_correlation=True,
        )
        print(json.dumps(report["server_client_correlation"], sort_keys=True))
        return 0
    if args.command == "compare":
        report = _compare(args)
        print(json.dumps(report, sort_keys=True))
        return (
            0
            if report.get("material_instrumentation_deterioration") is False
            and report.get("interpretation_allowed") is True
            else 1
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
