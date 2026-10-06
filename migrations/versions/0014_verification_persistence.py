"""Add Verification persistence, CAS root, audit, and retest intent.

Revision ID: 0014_verification_persistence
Revises: 0013_planning_window_overrides
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0014_verification_persistence"
down_revision: str | None = "0013_planning_window_overrides"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "verification_scopes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_package_id", sa.String(length=36), nullable=False),
        sa.Column("lead_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "verification_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "verification_version >= 1",
            name=op.f("ck_verification_scopes_verification_scope_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["work_package_id"],
            ["work_packages.id"],
            name=op.f("fk_verification_scopes_work_package_id_work_packages"),
        ),
        sa.ForeignKeyConstraint(
            ["lead_user_id"],
            ["app_users.id"],
            name=op.f("fk_verification_scopes_lead_user_id_app_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_scopes")),
        sa.UniqueConstraint(
            "work_package_id",
            name="uq_verification_scopes_work_package",
        ),
    )
    op.create_index(
        "ix_verification_scopes_lead_user",
        "verification_scopes",
        ["lead_user_id"],
        unique=False,
    )

    op.create_table(
        "verification_requirements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("verification_scope_id", sa.String(length=36), nullable=False),
        sa.Column("story_id", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("withdrawal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "phase IN ('FAT','SAT','COMMISSIONING')",
            name=op.f("ck_verification_requirements_verification_requirement_phase"),
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','WITHDRAWN')",
            name=op.f("ck_verification_requirements_verification_requirement_state"),
        ),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND withdrawal_reason IS NULL) OR "
            "(state = 'WITHDRAWN' AND withdrawal_reason IS NOT NULL)",
            name=op.f("ck_verification_requirements_verification_requirement_withdrawal_state"),
        ),
        sa.ForeignKeyConstraint(
            ["verification_scope_id"],
            ["verification_scopes.id"],
            name=op.f("fk_verification_requirements_verification_scope_id_verification_scopes"),
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["delivery_items.id"],
            name=op.f("fk_verification_requirements_story_id_delivery_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_requirements")),
    )
    op.create_index(
        "ix_verification_requirements_scope_story_state",
        "verification_requirements",
        ["verification_scope_id", "story_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_verification_requirements_current_revision",
        "verification_requirements",
        ["current_revision_id"],
        unique=False,
    )

    op.create_table(
        "verification_requirement_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("expected_result", sa.Text(), nullable=False),
        sa.Column("prerequisites_json", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("criticality", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "revision_number >= 1",
            name=op.f("ck_verification_requirement_revisions_verification_requirement_revision_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["verification_requirements.id"],
            name=op.f("fk_verification_requirement_revisions_requirement_id_verification_requirements"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_requirement_revisions")),
        sa.UniqueConstraint(
            "requirement_id",
            "revision_number",
            name="uq_verification_requirement_revision_number",
        ),
    )
    op.create_index(
        "ix_verification_requirement_revisions_requirement_created",
        "verification_requirement_revisions",
        ["requirement_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "story_verification_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("verification_scope_id", sa.String(length=36), nullable=False),
        sa.Column("story_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('NO_TEST_REQUIRED','TESTS_DEFINED')",
            name=op.f("ck_story_verification_decisions_story_verification_decision_kind"),
        ),
        sa.CheckConstraint(
            "kind <> 'NO_TEST_REQUIRED' OR justification IS NOT NULL",
            name=op.f("ck_story_verification_decisions_story_verification_decision_no_test_justification"),
        ),
        sa.ForeignKeyConstraint(
            ["verification_scope_id"],
            ["verification_scopes.id"],
            name=op.f("fk_story_verification_decisions_verification_scope_id_verification_scopes"),
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["delivery_items.id"],
            name=op.f("fk_story_verification_decisions_story_id_delivery_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_verification_decisions")),
    )
    op.create_index(
        "ix_story_verification_decisions_scope_story_created",
        "story_verification_decisions",
        ["verification_scope_id", "story_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "story_verification_decision_requirements",
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["story_verification_decisions.id"],
            name=op.f("fk_story_verification_decision_requirements_decision_id_story_verification_decisions"),
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["verification_requirements.id"],
            name=op.f("fk_story_verification_decision_requirements_requirement_id_verification_requirements"),
        ),
        sa.PrimaryKeyConstraint(
            "decision_id",
            "requirement_id",
            name=op.f("pk_story_verification_decision_requirements"),
        ),
    )

    op.create_table(
        "verification_retest_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("after_execution_sequence", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "after_execution_sequence >= 0",
            name=op.f("ck_verification_retest_requests_verification_retest_sequence_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["verification_requirements.id"],
            name=op.f("fk_verification_retest_requests_requirement_id_verification_requirements"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["verification_requirement_revisions.id"],
            name=op.f("fk_verification_retest_requests_revision_id_verification_requirement_revisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_retest_requests")),
    )
    op.create_index(
        "ix_verification_retest_requests_requirement_created",
        "verification_retest_requests",
        ["requirement_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "verification_change_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("verification_scope_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("story_id", sa.String(length=36), nullable=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("verification_version", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "verification_version >= 1",
            name=op.f("ck_verification_change_history_verification_history_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["verification_scope_id"],
            ["verification_scopes.id"],
            name=op.f("fk_verification_change_history_verification_scope_id_verification_scopes"),
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["delivery_items.id"],
            name=op.f("fk_verification_change_history_story_id_delivery_items"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.id"],
            name=op.f("fk_verification_change_history_actor_user_id_app_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_change_history")),
    )
    op.create_index(
        "ix_verification_change_history_scope_occurred",
        "verification_change_history",
        ["verification_scope_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_verification_change_history_actor",
        "verification_change_history",
        ["actor_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("verification_change_history")
    op.drop_table("verification_retest_requests")
    op.drop_table("story_verification_decision_requirements")
    op.drop_table("story_verification_decisions")
    op.drop_table("verification_requirement_revisions")
    op.drop_table("verification_requirements")
    op.drop_table("verification_scopes")
