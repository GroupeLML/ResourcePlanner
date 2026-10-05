"""Persist plural To recipients for project communications.

Revision ID: 0011_communication_to_recipients
Revises: 0010_operational_responsibility_context
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0011_communication_to_recipients"
down_revision: str | None = "0010_operational_responsibility_context"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "communication_messages",
        sa.Column("to_recipients_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("communication_messages", "to_recipients_json")
