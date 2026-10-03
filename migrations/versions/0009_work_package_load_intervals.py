"""Add canonical WorkPackage load intervals and terminal lifecycle fact.

Revision ID: 0009_work_package_load_intervals
Revises: 0008_asset_requirement_contexts
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid

from alembic import context, op
import sqlalchemy as sa


revision: str = "0009_work_package_load_intervals"
down_revision: str | None = "0008_asset_requirement_contexts"
branch_labels: str | None = None
depends_on: str | None = None


TERMINAL_CHECK = "ck_work_packages_work_package_terminal_status"
INTERVAL_WINDOW_CHECK = "ck_work_package_load_intervals_work_package_load_interval_window"
INTERVAL_HOURS_CHECK = "ck_work_package_load_intervals_work_package_load_interval_hours_non_negative"
INTERVAL_ORIGIN_CHECK = "ck_work_package_load_intervals_work_package_load_interval_origin"
INTERVAL_INDEX = "ix_work_package_load_intervals_package_start"

CLOSED_ALIASES = {"closed", "completed", "complete", "termine", "terminé", "ferme", "fermé"}
CANCELLED_ALIASES = {"cancelled", "canceled", "annule", "annulé", "annulee", "annulée"}


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _legacy_rows(bind) -> dict[str, list[dict[str, object]]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                wp.id AS work_package_id,
                wp.start_date,
                wp.end_date,
                wp.planned_hours,
                wp.weekly_load_origin,
                wl.week_start,
                wl.hours
            FROM work_packages AS wp
            JOIN work_package_weekly_loads AS wl
              ON wl.work_package_id = wp.id
            ORDER BY wp.id, wl.week_start
            """
        )
    ).mappings()
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["work_package_id"])].append(dict(row))
    return grouped


def _preflight_legacy_weekly_rows(bind) -> dict[str, list[dict[str, object]]]:
    missing_rows = tuple(
        bind.execute(
            sa.text(
                """
                SELECT wp.id, wp.weekly_load_origin
                FROM work_packages AS wp
                LEFT JOIN work_package_weekly_loads AS wl
                  ON wl.work_package_id = wp.id
                WHERE wp.weekly_load_origin IS NOT NULL
                GROUP BY wp.id, wp.weekly_load_origin
                HAVING COUNT(wl.work_package_id) = 0
                """
            )
        ).mappings()
    )
    if missing_rows:
        identifiers = ", ".join(str(row["id"]) for row in missing_rows)
        raise RuntimeError(
            "Cannot migrate WorkPackage weekly-load origins without persisted rows: "
            + identifiers
        )

    grouped = _legacy_rows(bind)
    for work_package_id, rows in grouped.items():
        first = rows[0]
        if (
            first["start_date"] is None
            or first["end_date"] is None
            or first["planned_hours"] is None
        ):
            raise RuntimeError(
                "Cannot migrate WorkPackage weekly loads with missing dates/planned_hours: "
                + work_package_id
            )
        start = _as_date(first["start_date"])
        end = _as_date(first["end_date"])
        if end < start:
            raise RuntimeError(
                "Cannot migrate WorkPackage weekly loads with invalid date window: "
                + work_package_id
            )
        origin = str(first["weekly_load_origin"] or "").strip().upper()
        if origin not in {"AUTO", "MANUAL"}:
            raise RuntimeError(
                "Cannot migrate WorkPackage weekly loads with invalid origin: "
                + work_package_id
            )
        seen: set[date] = set()
        total = Decimal("0.00")
        for row in rows:
            week_start = _as_date(row["week_start"])
            if week_start.weekday() != 0 or week_start in seen:
                raise RuntimeError(
                    "Cannot migrate WorkPackage weekly loads with invalid/duplicate week: "
                    + work_package_id
                )
            seen.add(week_start)
            if week_start > end or week_start + timedelta(days=6) < start:
                raise RuntimeError(
                    "Cannot migrate WorkPackage weekly loads outside package period: "
                    + work_package_id
                )
            hours = _as_decimal(row["hours"])
            if hours < 0:
                raise RuntimeError(
                    "Cannot migrate WorkPackage weekly loads with negative hours: "
                    + work_package_id
                )
            total += hours
        if total != _as_decimal(first["planned_hours"]):
            raise RuntimeError(
                "Cannot migrate WorkPackage weekly loads whose total differs from planned_hours: "
                + work_package_id
            )
    return grouped


def _add_terminal_status() -> None:
    bind = op.get_bind()
    column = sa.Column("terminal_status", sa.String(length=16), nullable=True)
    check_sql = "terminal_status IS NULL OR terminal_status IN ('closed','cancelled')"
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.add_column(column)
            batch_op.create_check_constraint(op.f(TERMINAL_CHECK), check_sql)
        return
    op.add_column("work_packages", column)
    op.create_check_constraint(
        op.f(TERMINAL_CHECK),
        "work_packages",
        check_sql,
    )


def upgrade() -> None:
    bind = op.get_bind()
    offline = context.is_offline_mode()
    legacy = {} if offline else _preflight_legacy_weekly_rows(bind)

    _add_terminal_status()
    op.create_table(
        "work_package_load_intervals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_package_id", sa.String(length=36), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("hours", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.CheckConstraint(
            "end_date >= start_date",
            name=op.f(INTERVAL_WINDOW_CHECK),
        ),
        sa.CheckConstraint(
            "hours >= 0",
            name=op.f(INTERVAL_HOURS_CHECK),
        ),
        sa.CheckConstraint(
            "origin IN ('MANUAL','LEGACY_AUTO','LEGACY_MANUAL')",
            name=op.f(INTERVAL_ORIGIN_CHECK),
        ),
        sa.ForeignKeyConstraint(
            ["work_package_id"],
            ["work_packages.id"],
            name="fk_work_package_load_intervals_work_package_id_work_packages",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_package_load_intervals"),
    )
    op.create_index(
        INTERVAL_INDEX,
        "work_package_load_intervals",
        ["work_package_id", "start_date", "end_date"],
        unique=False,
    )

    if offline:
        return

    statuses = bind.execute(
        sa.text("SELECT id, status FROM work_packages")
    ).mappings()
    for row in statuses:
        normalized = str(row["status"] or "").strip().casefold()
        terminal = (
            "closed"
            if normalized in CLOSED_ALIASES
            else "cancelled"
            if normalized in CANCELLED_ALIASES
            else None
        )
        if terminal is not None:
            bind.execute(
                sa.text(
                    "UPDATE work_packages SET terminal_status = :terminal_status "
                    "WHERE id = :work_package_id"
                ),
                {
                    "terminal_status": terminal,
                    "work_package_id": str(row["id"]),
                },
            )

    interval_table = sa.table(
        "work_package_load_intervals",
        sa.column("id", sa.String(length=36)),
        sa.column("work_package_id", sa.String(length=36)),
        sa.column("start_date", sa.Date()),
        sa.column("end_date", sa.Date()),
        sa.column("hours", sa.Numeric(12, 2)),
        sa.column("origin", sa.String(length=24)),
    )
    for work_package_id, rows in legacy.items():
        first = rows[0]
        package_start = _as_date(first["start_date"])
        package_end = _as_date(first["end_date"])
        source_origin = str(first["weekly_load_origin"] or "").strip().upper()
        interval_origin = (
            "LEGACY_AUTO" if source_origin == "AUTO" else "LEGACY_MANUAL"
        )
        values = []
        for row in rows:
            week_start = _as_date(row["week_start"])
            values.append(
                {
                    "id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"resourceplanner:work-package-load:{work_package_id}:{week_start.isoformat()}",
                        )
                    ),
                    "work_package_id": work_package_id,
                    "start_date": max(package_start, week_start),
                    "end_date": min(package_end, week_start + timedelta(days=6)),
                    "hours": _as_decimal(row["hours"]),
                    "origin": interval_origin,
                }
            )
        op.bulk_insert(interval_table, values)


def downgrade() -> None:
    bind = op.get_bind()
    manual_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM work_package_load_intervals "
                "WHERE origin = 'MANUAL'"
            )
        ).scalar_one()
        or 0
    )
    if manual_count:
        raise RuntimeError(
            "Cannot downgrade 0009_work_package_load_intervals while canonical MANUAL intervals exist."
        )

    op.drop_index(INTERVAL_INDEX, table_name="work_package_load_intervals")
    op.drop_table("work_package_load_intervals")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.drop_constraint(op.f(TERMINAL_CHECK), type_="check")
            batch_op.drop_column("terminal_status")
        return
    op.drop_constraint(op.f(TERMINAL_CHECK), "work_packages", type_="check")
    op.drop_column("work_packages", "terminal_status")
