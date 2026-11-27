from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, insert, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import InvalidRequestError

from app.infrastructure.sql.communication_read_uow import (
    CommunicationReadError,
    communication_read_uow,
)
from app.infrastructure.sql.session import create_session_factory, create_sql_engine


class Base(DeclarativeBase):
    pass


class Fact(Base):
    __tablename__ = "read_uow_fact"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[int]


class CommunicationReadUowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_sql_engine(f"sqlite:///{Path(directory.name) / 'test.db'}")
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            connection.execute(insert(Fact).values(id=1, value=10))
        self.trace = []
        event.listen(self.engine.pool, "checkout", self._checkout)
        event.listen(self.engine.pool, "checkin", self._checkin)
        event.listen(self.engine.pool, "invalidate", self._invalidate)
        event.listen(self.engine.pool, "detach", lambda dbapi, _record: self.trace.append(("detach", dbapi)))
        event.listen(self.engine, "begin", lambda _conn: self.trace.append("begin"))
        event.listen(self.engine, "rollback", lambda _conn: self.trace.append("rollback"))
        event.listen(self.engine, "commit", lambda _conn: self.trace.append("commit"))

    def _checkout(self, dbapi, _record, _proxy):
        self.trace.append(("checkout", dbapi))

    def _checkin(self, dbapi, _record):
        self.trace.append(("checkin", dbapi))

    def _invalidate(self, dbapi, _record, _exception):
        self.trace.append(("invalidate", dbapi))

    def _read(self, session):
        return session.execute(select(Fact.value)).scalar_one()

    def _assert_closed(self, session):
        self.assertFalse(session.in_transaction())
        self.assertFalse(session.identity_map)
        with self.assertRaises(InvalidRequestError) as caught:
            session.begin()
        self.assertIn("closed", str(caught.exception).lower())

    def _assert_one_lifetime(self):
        self.assertEqual([x[0] if isinstance(x, tuple) else x for x in self.trace],
                         ["checkout", "begin", "rollback", "checkin"])
        self.assertIs(self.trace[0][1], self.trace[-1][1])

    def test_fresh_factory_session_one_connection_and_transaction(self):
        original = sessionmaker.__call__
        calls = []

        def make(factory, **options):
            session = original(factory, **options)
            calls.append((factory, options, session))
            return session

        pool = self.engine.pool
        with patch.object(sessionmaker, "__call__", make):
            with communication_read_uow(self.factory) as session:
                self.assertIs(session, calls[0][2])
                self.assertIs(calls[0][0], self.factory)
                self.assertFalse(session.autoflush)
                self.assertFalse(session.autobegin)
                connection = session.connection()
                self.assertIs(connection.engine, self.engine)
                self.assertIs(connection.engine.pool, pool)
                root = connection.get_transaction()
                self.assertEqual(self._read(session), 10)
                self.assertIs(session.connection(), connection)
                self.assertIs(connection.get_transaction(), root)
                self.assertEqual(self._read(session), 10)
        self.assertEqual(len(calls), 1)
        self._assert_one_lifetime()
        self._assert_closed(session)
        self.assertTrue(connection.closed)

    def test_explicit_sqlite_begin_and_repeatable_reads(self):
        with communication_read_uow(self.factory) as session:
            connection = session.connection()
            self.assertTrue(connection.connection.driver_connection.in_transaction)
            self.assertEqual(self._read(session), 10)
            with self.engine.begin() as writer:
                writer.execute(update(Fact).values(value=20))
            self.assertEqual(self._read(session), 10)
        with communication_read_uow(self.factory) as fresh:
            self.assertEqual(self._read(fresh), 20)

    def test_success_rollback_closure_and_pooled_reuse(self):
        with communication_read_uow(self.factory) as session:
            self.assertEqual(self._read(session), 10)
            dbapi = session.connection().connection.driver_connection
        self._assert_one_lifetime()
        self._assert_closed(session)
        self.assertFalse(dbapi.in_transaction)
        with self.factory() as ordinary:
            connection = ordinary.connection()
            self.assertIs(connection.connection.driver_connection, dbapi)
            self.assertFalse(dbapi.in_transaction)
            self.assertEqual(self._read(ordinary), 10)
            ordinary.add(Fact(id=2, value=30))
            ordinary.commit()
        with self.factory() as ordinary:
            self.assertEqual(ordinary.execute(select(Fact.value).order_by(Fact.id)).scalars().all(), [10, 30])

    def test_read_exception_rolls_back_and_preserves_origin(self):
        origin = LookupError("read failed")
        with self.assertRaises(LookupError) as caught:
            with communication_read_uow(self.factory) as session:
                self.assertEqual(self._read(session), 10)
                raise origin
        self.assertIs(caught.exception, origin)
        self._assert_one_lifetime()
        self._assert_closed(session)

    def test_nonstandard_cached_factory_rejected_without_touching_session(self):
        with self.factory() as unrelated:
            unrelated.add(Fact(id=2, value=20))
            with self.assertRaisesRegex(CommunicationReadError, "standard fresh"):
                with communication_read_uow(lambda: unrelated):
                    self.fail("reused Session was yielded")
            self.assertTrue(unrelated.in_transaction())
            self.assertEqual(len(unrelated.new), 1)
            self.assertFalse(self.trace)

    def test_injected_preloaded_dirty_or_pending_session_rejected(self):
        for state in ("preloaded", "dirty", "pending"):
            with self.subTest(state=state), self.factory() as unrelated:
                if state == "pending":
                    unrelated.add(Fact(id=2, value=20))
                else:
                    fact = unrelated.get(Fact, 1)
                    if state == "dirty":
                        fact.value = 99
                transaction = unrelated.get_transaction()
                before = list(self.trace)
                with patch.object(sessionmaker, "__call__", return_value=unrelated):
                    with self.assertRaisesRegex(CommunicationReadError, "fresh Session"):
                        with communication_read_uow(self.factory):
                            self.fail("unrelated Session was yielded")
                self.assertIs(unrelated.get_transaction(), transaction)
                self.assertTrue(transaction.is_active)
                self.assertEqual(self.trace, before)
                if state == "preloaded":
                    self.assertIs(unrelated.get(Fact, 1), fact)
                elif state == "dirty":
                    self.assertIn(fact, unrelated.dirty)
                else:
                    self.assertEqual(len(unrelated.new), 1)

    def test_previously_used_and_closed_owned_session_rejected(self):
        with communication_read_uow(self.factory) as used:
            self.assertEqual(self._read(used), 10)
        before = list(self.trace)
        with patch.object(sessionmaker, "__call__", return_value=used):
            with self.assertRaisesRegex(CommunicationReadError, "fresh Session"):
                with communication_read_uow(self.factory):
                    self.fail("closed Session was yielded")
        self.assertEqual(self.trace, before)
        with self.factory() as ordinary:
            self.assertEqual(self._read(ordinary), 10)
            ordinary.rollback()
            with patch.object(sessionmaker, "__call__", return_value=ordinary):
                with self.assertRaisesRegex(CommunicationReadError, "fresh Session"):
                    with communication_read_uow(self.factory):
                        self.fail("previously used ordinary Session was yielded")

    def test_external_transaction_not_enrolled_or_rolled_back(self):
        with self.engine.begin() as external:
            external.execute(insert(Fact).values(id=2, value=20))
            with self.factory(bind=external) as unrelated:
                unrelated.execute(select(Fact.value)).all()
                before = list(self.trace)
                with patch.object(sessionmaker, "__call__", return_value=unrelated):
                    with self.assertRaisesRegex(CommunicationReadError, "fresh Session"):
                        with communication_read_uow(self.factory):
                            self.fail("external transaction was yielded")
                self.assertEqual(self.trace, before)
                self.assertTrue(external.in_transaction())
                self.assertEqual(external.execute(select(Fact.value).order_by(Fact.id)).scalars().all(), [10, 20])
        with self.factory() as ordinary:
            self.assertEqual(ordinary.execute(select(Fact.value).order_by(Fact.id)).scalars().all(), [10, 20])

    def test_mutation_no_autoflush_or_persistence(self):
        with self.assertRaisesRegex(CommunicationReadError, "lost clean"):
            with communication_read_uow(self.factory) as session:
                fact = session.get(Fact, 1)
                fact.value = 99
                session.add(Fact(id=2, value=20))
                self.assertEqual(self._read(session), 10)
                with self.assertRaisesRegex(CommunicationReadError, "cannot mutate"):
                    session.flush()
        with self.factory() as ordinary:
            self.assertEqual(ordinary.execute(select(Fact.value)).scalars().all(), [10])

    def test_explicit_dml_raw_sql_and_commit_rejected(self):
        with communication_read_uow(self.factory) as session:
            for operation in (lambda: session.execute(update(Fact).values(value=99)),
                              lambda: session.connection().exec_driver_sql("UPDATE read_uow_fact SET value=99"),
                              session.commit):
                with self.assertRaisesRegex(CommunicationReadError, "cannot mutate"):
                    operation()
            self.assertEqual(self._read(session), 10)
        self._assert_one_lifetime()

    def test_premature_transaction_release_rejected(self):
        with self.assertRaisesRegex(CommunicationReadError, "lost clean"):
            with communication_read_uow(self.factory) as session:
                self.assertEqual(self._read(session), 10)
                session.rollback()
        self.assertTrue(any(isinstance(x, tuple) and x[0] == "invalidate" for x in self.trace))

    def test_unsupported_dialect_rejected_before_checkout(self):
        with patch.object(self.engine.dialect, "name", "postgresql"):
            with self.assertRaisesRegex(CommunicationReadError, "Unsupported"):
                with communication_read_uow(self.factory):
                    self.fail("unsupported dialect was yielded")
        self.assertFalse(self.trace)

    def test_sqlite_already_enrolled_connection_rejected(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            root = connection.get_transaction()
            before = list(self.trace)
            with patch.object(self.engine, "connect", return_value=connection):
                with self.assertRaisesRegex(CommunicationReadError, "already enrolled"):
                    with communication_read_uow(self.factory):
                        self.fail("already enrolled connection was yielded")
            self.assertFalse(connection.closed)
            self.assertIs(connection.get_transaction(), root)
            self.assertTrue(root.is_active)
            self.assertEqual(self.trace, before)

    def test_nested_acquisition_rejected_before_factory_or_checkout(self):
        with communication_read_uow(self.factory) as session:
            before = list(self.trace)
            with patch.object(sessionmaker, "__call__", side_effect=AssertionError("extra Session")):
                with self.assertRaisesRegex(CommunicationReadError, "nested Session"):
                    with communication_read_uow(self.factory):
                        self.fail("nested acquisition was yielded")
            self.assertEqual(self.trace, before)
            self.assertEqual(self._read(session), 10)
        self._assert_one_lifetime()
        with communication_read_uow(self.factory) as fresh:
            self.assertIsNot(fresh, session)
            self.assertEqual(self._read(fresh), 10)

    def test_rollback_failure_invalidates_and_preserves_read_failure(self):
        origin = LookupError("read failed")
        cleanup = RuntimeError("rollback failed")
        with self.assertRaises(BaseExceptionGroup) as caught:
            with communication_read_uow(self.factory) as session:
                self.assertEqual(self._read(session), 10)
                connection = session.connection()
                dbapi = connection.connection.driver_connection
                patcher = patch.object(self.engine.dialect, "do_rollback", side_effect=cleanup)
                rollback = patcher.start()
                self.addCleanup(patcher.stop)
                raise origin
        patcher.stop()
        self.assertGreaterEqual(rollback.call_count, 1)
        self.assertIn(origin, caught.exception.exceptions)
        self.assertIn(cleanup, caught.exception.exceptions)
        self.assertIn(("detach", dbapi), self.trace)
        self.assertTrue(connection.closed)
        self._assert_closed(session)
        self.assertEqual(self.engine.pool.checkedout(), 0)
        with self.engine.connect() as replacement:
            self.assertIsNot(replacement.connection.driver_connection, dbapi)

    def test_session_close_failure_invalidates(self):
        cleanup = RuntimeError("Session close failed")
        with self.assertRaises(BaseExceptionGroup) as caught:
            with patch.object(Session, "close", autospec=True, side_effect=cleanup):
                with communication_read_uow(self.factory) as session:
                    self.assertEqual(self._read(session), 10)
                    dbapi = session.connection().connection.driver_connection
        self.assertIn(cleanup, caught.exception.exceptions)
        self.assertIn(("invalidate", dbapi), self.trace)
        self._assert_closed(session)
        self.assertEqual(self.engine.pool.checkedout(), 0)
        with self.engine.connect() as replacement:
            self.assertIsNot(replacement.connection.driver_connection, dbapi)

    @contextmanager
    def _sql_server_contract(self, *, state=1, setup_error=None, restore_error=None):
        """Real SQLAlchemy checkout/begin/close, simulated MSSQL dialect/metadata."""
        levels = []
        default = self.engine.dialect.default_isolation_level

        def set_level(dbapi, level):
            levels.append((dbapi, level))
            self.trace.append(("isolation", level))
            self.assertFalse(dbapi.in_transaction)
            if level == "SNAPSHOT" and setup_error is not None:
                raise setup_error
            if level == default and restore_error is not None:
                raise restore_error

        def metadata(_conn, _cursor, statement, parameters, _context, _many):
            if "snapshot_isolation_state" in statement:
                self.trace.append("prerequisite")
                return f"SELECT {state}", parameters
            self.trace.append("read")
            return statement, parameters

        event.listen(self.engine, "before_cursor_execute", metadata, retval=True)
        try:
            with patch.object(self.engine.dialect, "name", "mssql"), \
                    patch.object(self.engine.dialect, "get_isolation_level_values", return_value=[default, "SNAPSHOT"]), \
                    patch.object(self.engine.dialect, "set_isolation_level", side_effect=set_level):
                yield levels, default
        finally:
            event.remove(self.engine, "before_cursor_execute", metadata)

    def test_sql_server_contract_snapshot_before_begin_read_and_pool_restore(self):
        with self._sql_server_contract() as (levels, default):
            options = self.engine.get_execution_options()
            with communication_read_uow(self.factory) as session:
                self.assertEqual(session.connection().get_execution_options()["isolation_level"], "SNAPSHOT")
                self.assertEqual(self._read(session), 10)
            self.assertEqual(self.engine.get_execution_options(), options)
            self.assertEqual([level for _, level in levels], ["SNAPSHOT", default])
            self.assertIs(levels[0][0], levels[1][0])
            self.assertEqual([x[0] if isinstance(x, tuple) else x for x in self.trace],
                             ["checkout", "isolation", "begin", "prerequisite", "read", "rollback", "isolation", "checkin"])
            with self.engine.connect() as ordinary:
                self.assertIs(ordinary.connection.driver_connection, levels[0][0])
                self.assertNotIn("isolation_level", ordinary.get_execution_options())

    def test_sql_server_contract_setup_failure_no_fallback_or_retry(self):
        origin = RuntimeError("isolation unavailable")
        with self._sql_server_contract(setup_error=origin) as (levels, _default):
            with self.assertRaises(RuntimeError) as caught:
                with communication_read_uow(self.factory):
                    self.fail("failed isolation was yielded")
            self.assertIs(caught.exception, origin)
            self.assertEqual([level for _, level in levels], ["SNAPSHOT"])
            self.assertEqual([x[0] if isinstance(x, tuple) else x for x in self.trace],
                             ["checkout", "isolation", "invalidate", "checkin"])

    def test_sql_server_contract_prerequisite_off_or_transition_rejected(self):
        for state in (0, 2, 3):
            with self.subTest(state=state), self._sql_server_contract(state=state) as (levels, default):
                self.trace.clear()
                with self.assertRaisesRegex(CommunicationReadError, "prerequisite"):
                    with communication_read_uow(self.factory):
                        self.fail("unavailable prerequisite was yielded")
                self.assertEqual([level for _, level in levels], ["SNAPSHOT", default])
                self.assertEqual([x[0] if isinstance(x, tuple) else x for x in self.trace],
                                 ["checkout", "isolation", "begin", "prerequisite", "rollback", "isolation", "checkin"])


    def test_sql_server_contract_restore_failure_discards_and_returns_pool_slot(self):
        origin = RuntimeError("isolation restoration failed")
        with self._sql_server_contract(restore_error=origin) as (levels, default):
            with self.assertRaises(BaseExceptionGroup) as caught:
                with communication_read_uow(self.factory) as session:
                    self.assertEqual(self._read(session), 10)
                    connection = session.connection()
                    dbapi = connection.connection.driver_connection
            self.assertIn(origin, caught.exception.exceptions)
            self.assertEqual([level for _, level in levels], ["SNAPSHOT", default])
            self.assertIn(("detach", dbapi), self.trace)
            self._assert_closed(session)
            self.assertTrue(connection.closed)
            self.assertEqual(self.engine.pool.checkedout(), 0)
        with self.engine.connect() as replacement:
            self.assertIsNot(replacement.connection.driver_connection, dbapi)


    def _f01_compound_cleanup(self, *, origin=None, configure=None):
        from contextlib import ExitStack

        session_error = RuntimeError("F01 Session cleanup failed")
        first_invalidation = RuntimeError("F01 initial invalidation failed")
        later_invalidation = RuntimeError("F01 later invalidation failed")
        invalidations = []

        def fail_session(_session, _transaction):
            raise session_error

        def fail_invalidation(_driver, _record, _cause):
            error = first_invalidation if not invalidations else later_invalidation
            invalidations.append(error)
            raise error

        with ExitStack() as injections:
            event.listen(self.engine.pool, "invalidate", fail_invalidation)
            injections.callback(event.remove, self.engine.pool, "invalidate", fail_invalidation)
            with self.assertRaises(BaseExceptionGroup) as caught:
                with communication_read_uow(self.factory) as session:
                    self.assertEqual(self._read(session), 10)
                    connection = session.connection()
                    driver = connection.connection.driver_connection
                    event.listen(session, "after_transaction_end", fail_session)
                    if configure is not None:
                        configure(injections, connection, driver)
                    if origin is not None:
                        raise origin
        errors = caught.exception.exceptions
        self.assertIn(session_error, errors)
        self.assertIn(first_invalidation, errors)
        if origin is not None:
            self.assertIs(errors[0], origin)
        self.assertEqual([x for x in self.trace if isinstance(x, tuple) and x[0] == "checkout"],
                         [("checkout", driver)])
        self.assertNotIn(("checkin", driver), self.trace)
        self._assert_closed(session)
        return connection, driver, errors, invalidations

    def _assert_f01_discard(self, connection, driver, *, physical_closed=True):
        import sqlite3

        self.assertIn(("detach", driver), self.trace)
        if physical_closed:
            with self.assertRaises(sqlite3.ProgrammingError):
                driver.execute("SELECT 1")
        self.assertEqual(self.engine.pool.checkedout(), 0)
        with self.engine.connect() as replacement:
            self.assertIsNot(replacement, connection)
            self.assertIsNot(replacement.connection.driver_connection, driver)
            self.assertEqual(replacement.execute(select(Fact.value)).scalar_one(), 10)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_f01_successful_read_compound_cleanup_discards_driver(self):
        connection, driver, errors, invalidations = self._f01_compound_cleanup()
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(invalidations), 1)
        self.assertTrue(connection.closed)
        self._assert_f01_discard(connection, driver)

    def test_f01_read_failure_compound_cleanup_preserves_origin_and_discards(self):
        origin = LookupError("F01 originating read failed")
        connection, driver, errors, invalidations = self._f01_compound_cleanup(origin=origin)
        self.assertEqual(len(errors), 3)
        self.assertEqual(len(invalidations), 1)
        self.assertTrue(connection.closed)
        self._assert_f01_discard(connection, driver)

    def test_f01_detach_event_failure_preserved_after_pool_separation(self):
        detach_error = RuntimeError("F01 detach event failed")

        def configure(injections, _connection, _driver):
            def fail_detach(_driver, _record):
                raise detach_error
            event.listen(self.engine.pool, "detach", fail_detach)
            injections.callback(event.remove, self.engine.pool, "detach", fail_detach)

        connection, driver, errors, _ = self._f01_compound_cleanup(configure=configure)
        self.assertIn(detach_error, errors)
        self.assertTrue(connection.closed)
        self._assert_f01_discard(connection, driver)

    def test_f01_detach_and_later_invalidation_fail_without_ordinary_close(self):
        detach_error = RuntimeError("F01 detach failed before separation")
        quarantine = []
        close_calls = []

        def configure(injections, connection, _driver):
            def soft_invalidate(driver, _record, _cause):
                quarantine.append(driver)
            event.listen(self.engine.pool, "soft_invalidate", soft_invalidate)
            injections.callback(event.remove, self.engine.pool, "soft_invalidate", soft_invalidate)
            injections.enter_context(patch.object(connection, "detach", side_effect=detach_error))
            close_calls.append(injections.enter_context(patch.object(connection, "close", wraps=connection.close)))

        connection, driver, errors, invalidations = self._f01_compound_cleanup(configure=configure)
        self.addCleanup(lambda: connection.close() if not connection.closed else None)
        self.addCleanup(lambda: connection.invalidate() if not connection.closed else None)
        self.assertIn(detach_error, errors)
        self.assertEqual(len(invalidations), 2)
        self.assertIn(invalidations[1], errors)
        self.assertEqual(quarantine, [driver])
        close_calls[0].assert_not_called()
        self.assertFalse(connection.closed)
        self.assertFalse(connection.invalidated)
        self.assertEqual(self.engine.pool.checkedout(), 1)
        # Test-owned teardown after removing injected failures; not UOW recovery.
        connection.invalidate()
        connection.close()
        with self.engine.connect() as replacement:
            self.assertIsNot(replacement.connection.driver_connection, driver)

    def test_f01_failed_soft_quarantine_preserves_all_errors_without_close(self):
        detach_error = RuntimeError("F01 detach failed")
        quarantine_error = RuntimeError("F01 soft quarantine failed")
        close_calls = []

        def configure(injections, connection, _driver):
            def fail_quarantine(_driver, _record, _cause):
                raise quarantine_error
            event.listen(self.engine.pool, "soft_invalidate", fail_quarantine)
            injections.callback(event.remove, self.engine.pool, "soft_invalidate", fail_quarantine)
            injections.enter_context(patch.object(connection, "detach", side_effect=detach_error))
            close_calls.append(injections.enter_context(patch.object(connection, "close", wraps=connection.close)))

        connection, _driver, errors, invalidations = self._f01_compound_cleanup(configure=configure)
        self.addCleanup(lambda: connection.close() if not connection.closed else None)
        self.addCleanup(lambda: connection.invalidate() if not connection.closed else None)
        self.assertIn(detach_error, errors)
        self.assertIn(quarantine_error, errors)
        self.assertEqual(len(invalidations), 2)
        self.assertIn(invalidations[1], errors)
        close_calls[0].assert_not_called()
        self.assertFalse(connection.closed)
        self.assertEqual(self.engine.pool.checkedout(), 1)
        # No successful pool recovery is asserted under these compound faults.
        connection.invalidate()
        connection.close()

    def test_f01_physical_close_failure_preserved_after_detach(self):
        import sqlite3
        from sqlalchemy import create_engine

        physical_error = RuntimeError("F01 physical close failed")

        class FailingClose(sqlite3.Connection):
            fail_close = False

            def close(self):
                if self.fail_close:
                    raise physical_error
                return super().close()

        # Real SQLite driver subclass, separate test engine over the existing fixture.
        self.engine = create_engine(self.engine.url, connect_args={"factory": FailingClose})
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)
        event.listen(self.engine.pool, "checkout", self._checkout)
        event.listen(self.engine.pool, "checkin", self._checkin)
        event.listen(self.engine.pool, "invalidate", self._invalidate)
        event.listen(self.engine.pool, "detach", lambda dbapi, _record: self.trace.append(("detach", dbapi)))

        def configure(_injections, _connection, driver):
            driver.fail_close = True

            def clean_driver():
                driver.fail_close = False
                driver.close()
            self.addCleanup(clean_driver)

        connection, driver, errors, _ = self._f01_compound_cleanup(
            origin=LookupError("F01 read failed before physical close failure"), configure=configure)
        self.assertIn(physical_error, errors)
        self.assertTrue(connection.closed)
        self._assert_f01_discard(connection, driver, physical_closed=False)

    def test_f01_later_logical_invalidation_failure_does_not_fall_through(self):
        later_error = RuntimeError("F01 logical invalidation failed after detach")
        close_calls = []

        def configure(injections, connection, _driver):
            original = connection.invalidate
            calls = []

            def fail_later(*args, **kwargs):
                calls.append(None)
                if len(calls) == 1:
                    return original(*args, **kwargs)
                raise later_error
            injections.enter_context(patch.object(connection, "invalidate", side_effect=fail_later))
            close_calls.append(injections.enter_context(patch.object(connection, "close", wraps=connection.close)))

        connection, driver, errors, _ = self._f01_compound_cleanup(configure=configure)
        self.addCleanup(lambda: connection.close() if not connection.closed else None)
        self.addCleanup(lambda: connection.invalidate() if not connection.closed else None)
        self.assertIn(later_error, errors)
        close_calls[0].assert_not_called()
        self.assertFalse(connection.closed)
        self._assert_f01_discard(connection, driver)
        connection.invalidate()
        connection.close()

    def test_f01_logical_close_failure_after_discard_is_observable(self):
        close_error = RuntimeError("F01 logical close failed after discard")

        def configure(injections, connection, _driver):
            injections.enter_context(patch.object(connection, "close", side_effect=close_error))

        connection, driver, errors, _ = self._f01_compound_cleanup(configure=configure)
        self.addCleanup(connection.close)
        self.assertIn(close_error, errors)
        self.assertTrue(connection.invalidated)
        self.assertFalse(connection.closed)
        self._assert_f01_discard(connection, driver)
        connection.close()


if __name__ == "__main__":
    unittest.main()
