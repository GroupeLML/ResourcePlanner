"""Persist Quick Shift automatic-planning date exclusions for #755.

Revision ID: 0021_quick_shift_planning_exclusions
Revises: 0020_period_inheritance_contract
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0021_quick_shift_planning_exclusions"
down_revision: str | None = "0020_period_inheritance_contract"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "quick_shift_planning_exclusions",
        sa.Column("resource_requirement_id", sa.String(length=36), nullable=False),
        sa.Column("excluded_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_requirement_id"],
            ["resource_requirements.id"],
        ),
        sa.PrimaryKeyConstraint(
            "resource_requirement_id",
            "excluded_date",
            name=op.f("pk_quick_shift_planning_exclusions"),
        ),
    )
    op.create_index(
        "ix_quick_shift_planning_exclusions_date",
        "quick_shift_planning_exclusions",
        ["excluded_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quick_shift_planning_exclusions_date",
        table_name="quick_shift_planning_exclusions",
    )
    op.drop_table("quick_shift_planning_exclusions")
