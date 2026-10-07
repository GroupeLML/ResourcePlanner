"""Add competency class grouping, CAS and audit.

Revision ID: 0018_competency_resource_class
Revises: 0017_task_preferred_resource
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0018_competency_resource_class"
down_revision: str | None = "0017_task_preferred_resource"
branch_labels: str | None = None
depends_on: str | None = None


def _upgrade_competencies() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.add_column(
            "competencies",
            sa.Column("resource_class_code", sa.String(length=64), nullable=True),
        )
        op.add_column(
            "competencies",
            sa.Column(
                "resource_class_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        with op.batch_alter_table("competencies", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                op.f("fk_competencies_resource_class_code_resource_class_configs"),
                "resource_class_configs",
                ["resource_class_code"],
                ["code"],
            )
            batch_op.create_check_constraint(
                op.f("ck_competencies_resource_class_version_positive"),
                "resource_class_version >= 1",
            )
            batch_op.create_index(
                op.f("ix_competencies_resource_class_code"),
                ["resource_class_code"],
                unique=False,
            )
        return

    op.add_column(
        "competencies",
        sa.Column("resource_class_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "competencies",
        sa.Column(
            "resource_class_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_foreign_key(
        op.f("fk_competencies_resource_class_code_resource_class_configs"),
        "competencies",
        "resource_class_configs",
        ["resource_class_code"],
        ["code"],
    )
    op.create_check_constraint(
        op.f("ck_competencies_resource_class_version_positive"),
        "competencies",
        "resource_class_version >= 1",
    )
    op.create_index(
        op.f("ix_competencies_resource_class_code"),
        "competencies",
        ["resource_class_code"],
        unique=False,
    )


def upgrade() -> None:
    _upgrade_competencies()
    op.create_table(
        "competency_resource_class_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("competency_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("old_resource_class_code", sa.String(length=64), nullable=True),
        sa.Column("new_resource_class_code", sa.String(length=64), nullable=True),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resulting_version >= 1",
            name=op.f("ck_competency_resource_class_audit_resulting_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["competency_id"],
            ["competencies.id"],
            name=op.f("fk_competency_resource_class_audit_competency_id_competencies"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_competency_resource_class_audit"),
        ),
    )
    op.create_index(
        "ix_competency_resource_class_audit_competency_created",
        "competency_resource_class_audit",
        ["competency_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_competency_resource_class_audit_actor",
        "competency_resource_class_audit",
        ["actor_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_competency_resource_class_audit_actor",
        table_name="competency_resource_class_audit",
    )
    op.drop_index(
        "ix_competency_resource_class_audit_competency_created",
        table_name="competency_resource_class_audit",
    )
    op.drop_table("competency_resource_class_audit")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("competencies", recreate="always") as batch_op:
            batch_op.drop_index(op.f("ix_competencies_resource_class_code"))
            batch_op.drop_constraint(
                op.f("ck_competencies_resource_class_version_positive"),
                type_="check",
            )
            batch_op.drop_constraint(
                op.f("fk_competencies_resource_class_code_resource_class_configs"),
                type_="foreignkey",
            )
            batch_op.drop_column("resource_class_version")
            batch_op.drop_column("resource_class_code")
        return

    op.drop_index(
        op.f("ix_competencies_resource_class_code"),
        table_name="competencies",
    )
    op.drop_constraint(
        op.f("ck_competencies_resource_class_version_positive"),
        "competencies",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_competencies_resource_class_code_resource_class_configs"),
        "competencies",
        type_="foreignkey",
    )
    op.drop_column("competencies", "resource_class_version")
    op.drop_column("competencies", "resource_class_code")
