from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


SqlSessionFactory = sessionmaker[Session]


def create_sql_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create the SQLAlchemy engine used by server/import tooling.

    No application/domain code should depend on the concrete engine. Production
    connection-pool settings can be supplied later by the server composition root.
    """

    connect_args = (
        {"check_same_thread": False}
        if str(database_url or "").strip().casefold().startswith("sqlite")
        else {}
    )
    engine = create_engine(
        database_url,
        echo=echo,
        future=True,
        connect_args=connect_args,
    )
    if engine.dialect.name == "sqlite":
        # SQLite does not enforce foreign keys unless explicitly enabled per
        # connection. Tests/dev must exercise the same integrity assumptions as the
        # production SQL Server/PostgreSQL dialects.
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    elif engine.dialect.name == "mssql":
        # SQL Server requires these SET options for DML against filtered indexes.
        # Keep them connection-scoped so runtime writers use the same contract as
        # the Alembic migration that creates the single-ACTIVE filtered index.
        @event.listens_for(engine, "connect")
        def _enable_mssql_filtered_index_options(
            dbapi_connection,
            _connection_record,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    "SET ANSI_NULLS ON; "
                    "SET ANSI_PADDING ON; "
                    "SET ANSI_WARNINGS ON; "
                    "SET ARITHABORT ON; "
                    "SET CONCAT_NULL_YIELDS_NULL ON; "
                    "SET QUOTED_IDENTIFIER ON; "
                    "SET NUMERIC_ROUNDABORT OFF"
                )
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> SqlSessionFactory:
    """Return a factory with explicit transaction/commit semantics."""

    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@contextmanager
def transactional_session(factory: SqlSessionFactory) -> Iterator[Session]:
    """Open one transaction and roll it back automatically on failure."""

    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
