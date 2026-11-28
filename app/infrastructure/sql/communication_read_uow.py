"""Inactive transaction boundary for coherent communication source reads."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.sql.session import SqlSessionFactory


class CommunicationReadError(RuntimeError):
    """The dedicated read transaction cannot be established or retained."""


_USED = "communication_read_uow_used"
_ACTIVE = ContextVar("communication_read_uow_active", default=False)


def _reject_mutation(*_args: object) -> None:
    raise CommunicationReadError("Communication read scope cannot mutate or commit")


def _select_only(_conn, _cursor, _statement, _parameters, context, executemany):
    # Deliberately support compiled SELECTs, not arbitrary textual/driver SQL.
    # This is an application contract, not a database permission boundary.
    if executemany or context.compiled is None or not context.compiled.statement.is_select:
        _reject_mutation()


def _no_nested(_session, transaction):
    if transaction.parent is not None:
        raise CommunicationReadError("Communication read scope cannot nest transactions")


def _enroll(connection: Connection, dialect: str):
    if connection.in_transaction() or connection.invalidated:
        raise CommunicationReadError("Connection already enrolled or invalidated")
    if dialect == "mssql":
        # SQLAlchemy's dialect may execute DBAPI setup/commit for SNAPSHOT.
        # This must happen before the owned transaction or Session enrollment.
        connection.execution_options(isolation_level="SNAPSHOT")
    elif dialect == "sqlite":
        driver = connection.connection.driver_connection
        if driver.in_transaction or getattr(driver, "autocommit", None) is True:
            raise CommunicationReadError("Unsupported SQLite transaction state")
    else:
        raise CommunicationReadError("Unsupported communication read dialect")
    transaction = connection.begin()
    if dialect == "sqlite":
        # SQLAlchemy's logical begin alone does not start a sqlite3 read transaction.
        connection.exec_driver_sql("BEGIN")
    else:
        state = connection.exec_driver_sql(
            "SELECT snapshot_isolation_state FROM sys.databases WHERE database_id = DB_ID()"
        ).scalar_one()
        if state != 1:
            raise CommunicationReadError("SQL Server SNAPSHOT prerequisite is not ON")
    return transaction


def _discard_uncertain_connection(connection, cause, errors):
    # A failed SQLAlchemy isolation-reset finalizer can occur after the pool
    # record has begun check-in but before the slot has been returned. A plain
    # invalidate at that point can strand the slot. Detach the uncertain DBAPI
    # connection using the public API, close it physically, then invalidate the
    # logical Connection. Also use this path if initial invalidation raises;
    # ordinary close must not check an uncertain driver back into the pool.
    if connection.closed:
        return
    if not connection.invalidated:
        proxy = driver = None
        try:
            proxy = connection.connection
            driver = proxy.driver_connection
            connection.detach()
        except BaseException as exc:
            errors.append(exc)
            # Detach may have failed before severing the pool record. Mark
            # the existing proxy for replacement even if hard invalidation
            # fails again (including eventual pool finalization).
            if proxy is not None:
                try:
                    proxy.invalidate(cause, soft=True)
                except BaseException as quarantine_error:
                    errors.append(quarantine_error)
        if driver is not None:
            try:
                driver.close()
            except BaseException as exc:
                errors.append(exc)
        try:
            connection.invalidate(cause)
        except BaseException as exc:
            errors.append(exc)
            # Do not attempt ordinary closure after failed discard. In this
            # case even pool-slot recovery cannot be promised.
            return
    try:
        connection.close()
    except BaseException as exc:
        errors.append(exc)


@contextmanager
def communication_read_uow(factory: SqlSessionFactory) -> Iterator[Session]:
    """Yield one fresh Session bound to one explicitly owned read transaction.

    Only the existing standard sessionmaker is supported; arbitrary factories
    returning cached Sessions cannot prove freshness. Consumers must materialize
    immutable results inside this scope and must not manage its transaction.
    """
    if _ACTIVE.get():
        raise CommunicationReadError("Communication read scope cannot acquire a nested Session")
    token = _ACTIVE.set(True)
    try:
        with _owned_read_session(factory) as session:
            yield session
    finally:
        _ACTIVE.reset(token)


@contextmanager
def _owned_read_session(factory: SqlSessionFactory) -> Iterator[Session]:
    if (type(factory) is not sessionmaker or factory.class_.__bases__ != (Session,)
            or factory.kw.get("binds") or factory.kw.get("twophase")):
        raise CommunicationReadError("Expected the standard fresh SQL session factory")
    session = factory(autoflush=False, autobegin=False, close_resets_only=False,
                      join_transaction_mode="rollback_only")
    # Do not close, roll back, expunge or sanitize a Session we cannot own.
    if (not isinstance(session, Session) or not session.is_active or session.in_transaction()
            or session.new or session.dirty or session.deleted or session.identity_map
            or session.autoflush or session.autobegin or session.info.get(_USED)
            or not isinstance(session.bind, Engine)):
        raise CommunicationReadError("Communication read scope requires a fresh Session")
    session.info[_USED] = True
    connection = None
    failure = None
    cleanup = []
    try:
        engine = session.bind
        if engine.dialect.name not in {"mssql", "sqlite"}:
            raise CommunicationReadError("Unsupported communication read dialect")
        candidate = engine.connect()
        # A connection already in a logical transaction is not ours to clean up.
        # Never adopt or roll back an unrelated caller's existing transaction.
        if candidate.in_transaction():
            raise CommunicationReadError("Connection already enrolled")
        connection = candidate
        transaction = _enroll(connection, engine.dialect.name)
        session.bind = connection
        session_transaction = session.begin()
        if session.connection() is not connection:
            raise CommunicationReadError("Session did not enroll in owned connection")
        event.listen(session, "before_flush", _reject_mutation)
        event.listen(session, "before_commit", _reject_mutation)
        event.listen(session, "after_transaction_create", _no_nested)
        event.listen(connection, "before_cursor_execute", _select_only)
        event.listen(connection, "commit", _reject_mutation)
        yield session
        if (session.bind is not connection or session.new or session.dirty or session.deleted
                or session.get_transaction() is not session_transaction
                or not session_transaction.is_active or session.in_nested_transaction()
                or connection.get_transaction() is not transaction
                or not transaction.is_active or connection.invalidated):
            raise CommunicationReadError("Communication read scope lost clean transaction ownership")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # rollback_only means closing the Session does not commit, close, or
        # roll back the externally owned root transaction. Keep the connection
        # checked out until Session cleanup succeeds. Connection.close then
        # rolls back that root and restores connection-local isolation via the
        # SQLAlchemy pool's normal check-in machinery.
        try:
            session.close()
        except BaseException as exc:
            cleanup.append(exc)
            try:
                session.invalidate()
            except BaseException as invalidation_error:
                cleanup.append(invalidation_error)
        if connection is not None:
            # Setup failure or a lost/failed transaction makes reuse uncertain.
            try:
                if cleanup or (failure is not None and not connection.in_transaction()):
                    connection.invalidate()
            except BaseException as exc:
                cleanup.append(exc)
                _discard_uncertain_connection(connection, exc, cleanup)
            else:
                try:
                    connection.close()
                except BaseException as exc:
                    cleanup.append(exc)
                    _discard_uncertain_connection(connection, exc, cleanup)
        if cleanup:
            errors = ([failure] if failure is not None else []) + cleanup
            raise BaseExceptionGroup("Communication read transaction cleanup failed", errors)
