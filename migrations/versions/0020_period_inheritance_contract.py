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


def upgrade() -> None:
    with op.batch_alter_table("workforce_request_periods", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("inheritance_contract_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("confirmation_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("proposed_resource_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("same_as_period_key", sa.String(length=128), nullable=True)
        )
        batch_op.create_check_constraint(
            "request_period_inheritance_contract_version",
            "inheritance_contract_version IS NULL OR inheritance_contract_version = 1",
        )
        batch_op.create_check_constraint(
            "request_period_confirmation_mode",
            "confirmation_mode IS NULL OR confirmation_mode IN ('INHERIT_MASTER', 'EXPLICIT')",
        )
        batch_op.create_check_constraint(
            "request_period_proposed_resource_mode",
            "proposed_resource_mode IS NULL OR proposed_resource_mode IN "
            "('INHERIT_MASTER', 'EXPLICIT', 'SAME_AS_PERIOD')",
        )
        batch_op.create_check_constraint(
            "request_period_same_as_consistency",
            "(proposed_resource_mode = 'SAME_AS_PERIOD' AND same_as_period_key IS NOT NULL) OR "
            "(proposed_resource_mode IS NULL) OR "
            "(proposed_resource_mode != 'SAME_AS_PERIOD' AND same_as_period_key IS NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("workforce_request_periods", schema=None) as batch_op:
        batch_op.drop_constraint(
            "request_period_same_as_consistency", type_="check"
        )
        batch_op.drop_constraint(
            "request_period_proposed_resource_mode", type_="check"
        )
        batch_op.drop_constraint(
            "request_period_confirmation_mode", type_="check"
        )
        batch_op.drop_constraint(
            "request_period_inheritance_contract_version", type_="check"
        )
        batch_op.drop_column("same_as_period_key")
        batch_op.drop_column("proposed_resource_mode")
        batch_op.drop_column("confirmation_mode")
        batch_op.drop_column("inheritance_contract_version")
