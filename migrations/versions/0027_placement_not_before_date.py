"""ADR-035 durable placement not-before cutoff.

Revision ID: 0027_placement_not_before_date
Revises: 0026_request_work_queue_restorations
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0027_placement_not_before_date"
down_revision: str | None = "0026_request_work_queue_restorations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "request_requirement_planning_intents",
        sa.Column("placement_not_before_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "0027_placement_not_before_date refuses offline downgrade because "
            "durable ADR-035 placement intent cannot be inspected safely."
        )
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM request_requirement_planning_intents "
            "WHERE placement_not_before_date IS NOT NULL"
        )
    ).scalar_one()
    if int(populated or 0) > 0:
        raise RuntimeError(
            "0027_placement_not_before_date refuses downgrade while durable "
            "placement cutoffs exist."
        )
    op.drop_column(
        "request_requirement_planning_intents",
        "placement_not_before_date",
    )
