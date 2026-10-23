"""Support continuous vehicle RESOURCE_PERIOD reservations for #744.

Revision ID: 0022_continuous_vehicle_resource_period
Revises: 0021_quick_shift_planning_exclusions
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0022_continuous_vehicle_resource_period"
down_revision: str | None = "0021_quick_shift_planning_exclusions"
branch_labels: str | None = None
depends_on: str | None = None


def _create_requirement_constraints(operations, *, table_name: str | None = None) -> None:
    args = (
        ("asset_requirement_window", "end_date IS NULL OR end_date >= start_date"),
        ("asset_requirement_open_end_origin", "end_date IS NOT NULL OR origin = 'RESOURCE_PERIOD'"),
        (
            "asset_requirement_resource_period_mode_values",
            "resource_period_mode IS NULL OR resource_period_mode IN ('BOUNDED', 'CONTINUOUS')",
        ),
        (
            "asset_requirement_resource_period_mode_origin",
            "(origin = 'RESOURCE_PERIOD' AND resource_period_mode IS NOT NULL) OR "
            "(origin != 'RESOURCE_PERIOD' AND resource_period_mode IS NULL)",
        ),
    )
    for name, condition in args:
        if table_name is None:
            operations.create_check_constraint(name, condition)
        else:
            operations.create_check_constraint(name, table_name, condition)


def upgrade() -> None:
    op.add_column(
        "asset_requirements",
        sa.Column("resource_period_mode", sa.String(length=16), nullable=True),
    )
    op.execute(
        "UPDATE asset_requirements SET resource_period_mode = 'BOUNDED' "
        "WHERE origin = 'RESOURCE_PERIOD'"
    )

    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("asset_requirements", recreate="always") as batch:
            batch.drop_constraint("asset_requirement_window", type_="check")
            batch.alter_column("end_date", existing_type=sa.Date(), nullable=True)
            _create_requirement_constraints(batch)
        with op.batch_alter_table("asset_allocations", recreate="always") as batch:
            batch.drop_constraint("asset_allocation_window", type_="check")
            batch.alter_column("end_date", existing_type=sa.Date(), nullable=True)
            batch.create_check_constraint(
                "asset_allocation_window",
                "end_date IS NULL OR end_date >= start_date",
            )
    else:
        op.drop_constraint(
            "asset_requirement_window",
            "asset_requirements",
            type_="check",
        )
        op.alter_column(
            "asset_requirements",
            "end_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        _create_requirement_constraints(op, table_name="asset_requirements")

        op.drop_constraint(
            "asset_allocation_window",
            "asset_allocations",
            type_="check",
        )
        op.alter_column(
            "asset_allocations",
            "end_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        op.create_check_constraint(
            "asset_allocation_window",
            "asset_allocations",
            "end_date IS NULL OR end_date >= start_date",
        )


def downgrade() -> None:
    connection = op.get_bind()
    open_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM asset_requirements ar "
            "JOIN asset_allocations aa ON aa.asset_requirement_id = ar.id "
            "WHERE ar.origin = 'RESOURCE_PERIOD' "
            "AND (ar.end_date IS NULL OR aa.end_date IS NULL)"
        )
    ).scalar_one()
    if int(open_count or 0) > 0:
        raise RuntimeError(
            "Cannot downgrade continuous vehicle support while open RESOURCE_PERIOD allocations exist."
        )

    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("asset_requirements", recreate="always") as batch:
            for name in (
                "asset_requirement_resource_period_mode_origin",
                "asset_requirement_resource_period_mode_values",
                "asset_requirement_open_end_origin",
                "asset_requirement_window",
            ):
                batch.drop_constraint(name, type_="check")
            batch.alter_column("end_date", existing_type=sa.Date(), nullable=False)
            batch.create_check_constraint(
                "asset_requirement_window",
                "end_date >= start_date",
            )
        with op.batch_alter_table("asset_allocations", recreate="always") as batch:
            batch.drop_constraint("asset_allocation_window", type_="check")
            batch.alter_column("end_date", existing_type=sa.Date(), nullable=False)
            batch.create_check_constraint(
                "asset_allocation_window",
                "end_date >= start_date",
            )
    else:
        for name in (
            "asset_requirement_resource_period_mode_origin",
            "asset_requirement_resource_period_mode_values",
            "asset_requirement_open_end_origin",
            "asset_requirement_window",
        ):
            op.drop_constraint(name, "asset_requirements", type_="check")
        op.alter_column(
            "asset_requirements",
            "end_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        op.create_check_constraint(
            "asset_requirement_window",
            "asset_requirements",
            "end_date >= start_date",
        )

        op.drop_constraint(
            "asset_allocation_window",
            "asset_allocations",
            type_="check",
        )
        op.alter_column(
            "asset_allocations",
            "end_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        op.create_check_constraint(
            "asset_allocation_window",
            "asset_allocations",
            "end_date >= start_date",
        )

    op.drop_column("asset_requirements", "resource_period_mode")
