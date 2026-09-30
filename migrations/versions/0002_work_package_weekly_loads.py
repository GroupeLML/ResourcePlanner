"""Add persistent WorkPackage weekly loads and distribution origin.

Revision ID: 0002_work_package_weekly_loads
Revises: v2_production_baseline
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0002_work_package_weekly_loads"
down_revision: str | None = "v2_production_baseline"
branch_labels: str | None = None
depends_on: str | None = None


ORIGIN_CHECK = "ck_work_packages_work_package_weekly_load_origin"
HOURS_CHECK = "ck_work_package_weekly_loads_weekly_load_hours_non_negative"


def _add_origin() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("weekly_load_origin", sa.String(length=16), nullable=True))
            batch_op.create_check_constraint(
                ORIGIN_CHECK,
                "weekly_load_origin IS NULL OR weekly_load_origin IN ('AUTO','MANUAL')",
            )
        return

    op.add_column(
        "work_packages",
        sa.Column("weekly_load_origin", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        ORIGIN_CHECK,
        "work_packages",
        "weekly_load_origin IS NULL OR weekly_load_origin IN ('AUTO','MANUAL')",
    )


def upgrade() -> None:
    _add_origin()
    op.create_table(
        "work_package_weekly_loads",
        sa.Column("work_package_id", sa.String(length=36), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("hours", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint("hours >= 0", name=HOURS_CHECK),
        sa.ForeignKeyConstraint(
            ["work_package_id"],
            ["work_packages.id"],
            name="fk_work_package_weekly_loads_work_package_id_work_packages",
        ),
        sa.PrimaryKeyConstraint(
            "work_package_id",
            "week_start",
            name="pk_work_package_weekly_loads",
        ),
    )


def downgrade() -> None:
    op.drop_table("work_package_weekly_loads")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.drop_constraint(ORIGIN_CHECK, type_="check")
            batch_op.drop_column("weekly_load_origin")
        return
    op.drop_constraint(ORIGIN_CHECK, "work_packages", type_="check")
    op.drop_column("work_packages", "weekly_load_origin")
