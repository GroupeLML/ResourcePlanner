"""Add optional canonical residence city to resources.

Revision ID: 0028_resource_residence_city
Revises: 0027_placement_not_before_date
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0028_resource_residence_city"
down_revision: str | None = "0027_placement_not_before_date"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "resources",
        sa.Column("residence_city", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resources", "residence_city")
