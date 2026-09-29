"""Add ResourceClass to ApprovalScope routing mappings.

Revision ID: 0045_approval_scope_resource_classes
Revises: 0044_project_task_odata
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0045_approval_scope_resource_classes"
down_revision: Union[str, Sequence[str], None] = "0044_project_task_odata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_class_approval_scope_mappings",
        sa.Column(
            "resource_class_code",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "approval_scope_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["resource_class_code"],
            ["resource_class_configs.code"],
            name="fk_resource_class_scope_mapping_class",
        ),
        sa.ForeignKeyConstraint(
            ["approval_scope_id"],
            ["approval_scopes.id"],
            name="fk_resource_class_scope_mapping_scope",
        ),
        sa.PrimaryKeyConstraint(
            "resource_class_code",
            "approval_scope_id",
            name="pk_resource_class_approval_scope_mappings",
        ),
    )
    op.create_index(
        "ix_resource_class_scope_mapping_scope",
        "resource_class_approval_scope_mappings",
        ["approval_scope_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resource_class_scope_mapping_scope",
        table_name="resource_class_approval_scope_mappings",
    )
    op.drop_table("resource_class_approval_scope_mappings")
