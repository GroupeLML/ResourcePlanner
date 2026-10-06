"""Add Verification assignments, append-only executions and HTTPS evidence links.

Revision ID: 0015_verification_executions
Revises: 0014_verification_persistence
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0015_verification_executions"
down_revision: str | None = "0014_verification_persistence"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "verification_executor_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("executor_user_id", sa.String(length=36), nullable=False),
        sa.Column("assigned_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("ended_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["requirement_id"], ["verification_requirements.id"], name=op.f("fk_verification_executor_assignments_requirement_id_verification_requirements")),
        sa.ForeignKeyConstraint(["executor_user_id"], ["app_users.id"], name=op.f("fk_verification_executor_assignments_executor_user_id_app_users")),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["app_users.id"], name=op.f("fk_verification_executor_assignments_assigned_by_user_id_app_users")),
        sa.ForeignKeyConstraint(["ended_by_user_id"], ["app_users.id"], name=op.f("fk_verification_executor_assignments_ended_by_user_id_app_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_executor_assignments")),
    )
    op.create_index(
        "ix_verification_executor_req_user_active",
        "verification_executor_assignments",
        ["requirement_id", "executor_user_id", "active"],
        unique=False,
    )

    op.create_table(
        "verification_test_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("execution_sequence", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("executor_user_id", sa.String(length=36), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("measurements_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("execution_sequence >= 1", name=op.f("ck_verification_test_executions_verification_test_execution_sequence_positive")),
        sa.CheckConstraint("result IN ('PASS','FAIL','BLOCKED')", name=op.f("ck_verification_test_executions_verification_test_execution_result")),
        sa.ForeignKeyConstraint(["requirement_id"], ["verification_requirements.id"], name=op.f("fk_verification_test_executions_requirement_id_verification_requirements")),
        sa.ForeignKeyConstraint(["revision_id"], ["verification_requirement_revisions.id"], name=op.f("fk_verification_test_executions_revision_id_verification_requirement_revisions")),
        sa.ForeignKeyConstraint(["executor_user_id"], ["app_users.id"], name=op.f("fk_verification_test_executions_executor_user_id_app_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_test_executions")),
        sa.UniqueConstraint("requirement_id", "execution_sequence", name="uq_verification_test_execution_requirement_sequence"),
    )
    op.create_index(
        "ix_verification_test_executions_requirement_revision_sequence",
        "verification_test_executions",
        ["requirement_id", "revision_id", "execution_sequence"],
        unique=False,
    )

    op.create_table(
        "verification_evidence_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("provenance", sa.String(length=64), server_default=sa.text("'external_https'"), nullable=False),
        sa.Column("added_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["verification_test_executions.id"], name=op.f("fk_verification_evidence_links_execution_id_verification_test_executions")),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["app_users.id"], name=op.f("fk_verification_evidence_links_added_by_user_id_app_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_evidence_links")),
    )
    op.create_index(
        "ix_verification_evidence_links_execution_created",
        "verification_evidence_links",
        ["execution_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("verification_evidence_links")
    op.drop_table("verification_test_executions")
    op.drop_table("verification_executor_assignments")
