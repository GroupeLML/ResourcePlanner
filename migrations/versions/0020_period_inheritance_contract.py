"""Add explicit period inheritance contract for 655A.

Revision ID: 0020_period_inheritance_contract
Revises: 0019_planning_resource_user_order
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0020_period_inheritance_contract"
down_revision: str | None = "0019_planning_resource_user_order"
branch_labels: str | None = None
depends_on: str | None = None


def _create_constraints(operations, *, table_name: str | None = None) -> None:
    constraints = (
        (
            "request_period_inheritance_contract_version",
            "inheritance_contract_version IS NULL OR inheritance_contract_version = 1",
        ),
        (
            "request_period_confirmation_mode",
            "confirmation_mode IS NULL OR confirmation_mode IN ('INHERIT_MASTER', 'EXPLICIT')",
        ),
        (
            "request_period_proposed_resource_mode",
            "proposed_resource_mode IS NULL OR proposed_resource_mode IN "
            "('INHERIT_MASTER', 'EXPLICIT', 'SAME_AS_PERIOD')",
        ),
        (
            "request_period_same_as_consistency",
            "(proposed_resource_mode = 'SAME_AS_PERIOD' AND same_as_period_key IS NOT NULL) OR "
            "(proposed_resource_mode IS NULL) OR "
            "(proposed_resource_mode != 'SAME_AS_PERIOD' AND same_as_period_key IS NULL)",
        ),
    )
    for name, condition in constraints:
        if table_name is None:
            operations.create_check_constraint(name, condition)
        else:
            operations.create_check_constraint(name, table_name, condition)


def _drop_constraints(operations, *, table_name: str | None = None) -> None:
    for name in (
        "request_period_same_as_consistency",
        "request_period_proposed_resource_mode",
        "request_period_confirmation_mode",
        "request_period_inheritance_contract_version",
    ):
        if table_name is None:
            operations.drop_constraint(name, type_="check")
        else:
            operations.drop_constraint(name, table_name, type_="check")

def upgrade() -> None:
    for column in (
        sa.Column("inheritance_contract_version", sa.Integer(), nullable=True),
        sa.Column("confirmation_mode", sa.String(length=32), nullable=True),
        sa.Column("proposed_resource_mode", sa.String(length=32), nullable=True),
        sa.Column("same_as_period_key", sa.String(length=128), nullable=True),
    ):
        op.add_column("workforce_request_periods", column)

    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            "workforce_request_periods",
            schema=None,
            recreate="always",
        ) as batch_op:
            _create_constraints(batch_op)
    else:
        _create_constraints(op, table_name="workforce_request_periods")


def downgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            "workforce_request_periods",
            schema=None,
            recreate="always",
        ) as batch_op:
            _drop_constraints(batch_op)
    else:
        _drop_constraints(op, table_name="workforce_request_periods")

    op.drop_column("workforce_request_periods", "same_as_period_key")
    op.drop_column("workforce_request_periods", "proposed_resource_mode")
    op.drop_column("workforce_request_periods", "confirmation_mode")
    op.drop_column("workforce_request_periods", "inheritance_contract_version")
