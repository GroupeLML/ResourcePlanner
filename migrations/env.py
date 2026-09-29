from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from alembic.ddl.mssql import MSSQLImpl
from sqlalchemy import String, engine_from_config, pool

from app.infrastructure.sql import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

runtime_url = os.getenv("RESOURCEPLANNER_DATABASE_URL", "").strip()
if runtime_url:
    # Alembic stores values in ConfigParser, where '%' has interpolation semantics.
    # Escaping is required for encoded ODBC URLs such as mssql+pyodbc odbc_connect.
    config.set_main_option("sqlalchemy.url", runtime_url.replace("%", "%%"))

target_metadata = Base.metadata

ALEMBIC_VERSION_NUM_LENGTH = 128


class ResourcePlannerMSSQLImpl(MSSQLImpl):
    """Keep descriptive RessourcePlanner revision IDs valid on SQL Server."""

    __dialect__ = "mssql"

    def version_table_impl(
        self,
        *,
        version_table: str,
        version_table_schema: str | None,
        version_table_pk: bool,
        **kw,
    ):
        table = super().version_table_impl(
            version_table=version_table,
            version_table_schema=version_table_schema,
            version_table_pk=version_table_pk,
            **kw,
        )
        table.c.version_num.type = String(ALEMBIC_VERSION_NUM_LENGTH)
        return table


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
