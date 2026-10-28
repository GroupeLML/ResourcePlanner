"""ADR-034 unique command identities for release/reduction events.

Revision ID: 0024_request_work_queue_command_identity
Revises: 0023_request_work_queue_intent
"""
from __future__ import annotations

from alembic import op


revision: str = "0024_request_work_queue_command_identity"
down_revision: str | None = "0023_request_work_queue_intent"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("planning_work_queue_releases") as batch:
        batch.create_unique_constraint(
            "uq_planning_work_queue_release_command",
            ["command_id"],
        )
    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.create_unique_constraint(
            "uq_request_completion_reduction_command",
            ["command_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.drop_constraint(
            "uq_request_completion_reduction_command",
            type_="unique",
        )
    with op.batch_alter_table("planning_work_queue_releases") as batch:
        batch.drop_constraint(
            "uq_planning_work_queue_release_command",
            type_="unique",
        )
