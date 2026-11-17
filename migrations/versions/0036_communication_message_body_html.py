"""#855 durable nullable HTML communication body (widening only).

Revision ID: 0036_communication_message_body_html
Revises: 0035_workforce_request_site_contact
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0036_communication_message_body_html"
down_revision: str | None = "0035_workforce_request_site_contact"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("communication_messages") as batch:
        batch.add_column(sa.Column("body_html", sa.Text(), nullable=True))


def downgrade() -> None:
    # Operational rollback retains the forward schema. Explicit downgrade is
    # destructive once HTML artifacts have been prepared and must not run
    # automatically during PREV/code rollback.
    with op.batch_alter_table("communication_messages") as batch:
        batch.drop_column("body_html")
