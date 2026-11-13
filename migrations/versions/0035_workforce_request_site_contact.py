"""#830 request-owned chantier contact (widening only).

Revision ID: 0035_workforce_request_site_contact
Revises: 0034_request_shift_survivor_protections
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0035_workforce_request_site_contact"
down_revision: str | None = "0034_request_shift_survivor_protections"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("workforce_requests") as batch:
        batch.add_column(sa.Column("site_contact_name", sa.String(255), nullable=True))
        batch.add_column(sa.Column("site_contact_phone", sa.String(64), nullable=True))


def downgrade() -> None:
    # Operational rollback retains the forward schema. Explicit downgrade is
    # destructive and must not be used once contact data has been entered.
    with op.batch_alter_table("workforce_requests") as batch:
        batch.drop_column("site_contact_phone")
        batch.drop_column("site_contact_name")
