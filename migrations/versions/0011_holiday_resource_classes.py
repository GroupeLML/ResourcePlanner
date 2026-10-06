"""Add class scopes to holiday availability rules.

Revision ID: 0011_holiday_resource_classes
Revises: 0010_operational_responsibility_context
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0011_holiday_resource_classes"
down_revision: str | None = "0010_operational_responsibility_context"
branch_labels: str | None = None
depends_on: str | None = None


TABLE = "availability_rule_resource_classes"
RULE_FK = "fk_availability_rule_resource_classes_rule"
CLASS_FK = "fk_availability_rule_resource_classes_class"
CLASS_INDEX = "ix_availability_rule_resource_classes_resource_class_code"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("availability_rule_id", sa.String(length=36), nullable=False),
        sa.Column("resource_class_code", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["availability_rule_id"],
            ["resource_availability_rules.id"],
            name=RULE_FK,
        ),
        sa.ForeignKeyConstraint(
            ["resource_class_code"],
            ["resource_class_configs.code"],
            name=CLASS_FK,
        ),
        sa.PrimaryKeyConstraint(
            "availability_rule_id",
            "resource_class_code",
            name="pk_availability_rule_resource_classes",
        ),
    )
    op.create_index(
        CLASS_INDEX,
        TABLE,
        ["resource_class_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(CLASS_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
