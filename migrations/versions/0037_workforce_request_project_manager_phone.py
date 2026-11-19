"""#880 request-owned project-manager contact phone (widening only).

Revision ID: 0037_workforce_request_project_manager_phone
Revises: 0036_communication_message_body_html
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0037_workforce_request_project_manager_phone"
down_revision: str | None = "0036_communication_message_body_html"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("workforce_requests") as batch:
        batch.add_column(
            sa.Column(
                "include_project_manager_contact_phone",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "project_manager_contact_phone",
                sa.String(64),
                nullable=True,
            )
        )


def downgrade() -> None:
    # Operational rollback retains the forward schema. Explicit downgrade is
    # destructive once request-owned contact choices have been entered and must
    # not run automatically during PREV/code rollback.
    with op.batch_alter_table("workforce_requests") as batch:
        batch.drop_column("project_manager_contact_phone")
        batch.drop_column("include_project_manager_contact_phone")
