"""Add ADR-033 643B encrypted ERP evidence, baseline and recovery state.

Revision ID: 0031_erp_sync_evidence_checkpoint
Revises: 0030_erp_sync_retry_identity
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0031_erp_sync_evidence_checkpoint"
down_revision: str | None = "0030_erp_sync_retry_identity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "erp_sync_evidence_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("proof_type", sa.String(length=24), nullable=False),
        sa.Column("profile_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plaintext_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ciphertext_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("protocol_version >= 1", name="erp_sync_evidence_protocol_positive"),
        sa.CheckConstraint(
            "source_type IN ('PROJECTS','TASKS','CHECKPOINT')",
            name="erp_sync_evidence_source_type_values",
        ),
        sa.CheckConstraint(
            "proof_type IN ('SOURCE','CHECKPOINT','COMPENSATION')",
            name="erp_sync_evidence_proof_type_values",
        ),
        sa.CheckConstraint(
            "state IN ('WRITING','SEALED','READY','EXPIRED','PURGED')",
            name="erp_sync_evidence_state_values",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id",
            "source_type",
            "proof_type",
            name="uq_erp_sync_evidence_attempt_source_proof",
        ),
    )
    op.create_index(
        "ix_erp_sync_evidence_attempt",
        "erp_sync_evidence_manifests",
        ["attempt_id"],
    )
    op.create_index(
        "ix_erp_sync_evidence_state_created",
        "erp_sync_evidence_manifests",
        ["state", "created_at"],
    )

    op.create_table(
        "erp_sync_evidence_chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("manifest_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("plaintext_sha256", sa.String(length=64), nullable=False),
        sa.Column("plaintext_bytes", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["erp_sync_evidence_manifests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manifest_id", "ordinal", name="uq_erp_sync_evidence_chunk_ordinal"
        ),
    )
    op.create_index(
        "ix_erp_sync_evidence_chunk_manifest",
        "erp_sync_evidence_chunks",
        ["manifest_id"],
    )

    op.create_table(
        "erp_sync_evidence_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_plaintext_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_plaintext_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND used_bytes >= 0 "
            "AND reserved_plaintext_bytes >= 0 AND used_plaintext_bytes >= 0",
            name="erp_sync_evidence_reservation_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", name="uq_erp_sync_evidence_reservation_attempt"
        ),
    )
    op.create_index(
        "ix_erp_sync_evidence_reservation_released",
        "erp_sync_evidence_reservations",
        ["released_at"],
    )

    op.create_table(
        "erp_sync_baselines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("profile_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("encrypted_project_counts", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approval_reason", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('PROJECTS','TASKS')",
            name="erp_sync_baseline_source_type_values",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type",
            "profile_fingerprint",
            name="uq_erp_sync_baseline_source_profile",
        ),
    )
    op.create_index(
        "ix_erp_sync_baseline_approved",
        "erp_sync_baselines",
        ["approved_at"],
    )

    op.create_table(
        "erp_sync_plausibility_exceptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("threshold_scope", sa.String(length=255), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id",
            "source_type",
            "source_fingerprint",
            "threshold_scope",
            name="uq_erp_sync_plausibility_exception",
        ),
    )
    op.create_index(
        "ix_erp_sync_plausibility_exception_attempt",
        "erp_sync_plausibility_exceptions",
        ["attempt_id"],
    )

    op.create_table(
        "erp_sync_mutation_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("unit_type", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_key", sa.String(length=160), nullable=False),
        sa.Column("before_present", sa.Boolean(), nullable=False),
        sa.Column("encrypted_before", sa.Text(), nullable=True),
        sa.Column("before_sha256", sa.String(length=64), nullable=False),
        sa.Column("encrypted_after", sa.Text(), nullable=True),
        sa.Column("after_sha256", sa.String(length=64), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id",
            "unit_type",
            "entity_type",
            "entity_key",
            name="uq_erp_sync_mutation_evidence_entity",
        ),
    )
    op.create_index(
        "ix_erp_sync_mutation_evidence_attempt",
        "erp_sync_mutation_evidence",
        ["attempt_id"],
    )

    op.create_table(
        "erp_sync_project_quality",
        sa.Column("project_number", sa.String(length=64), nullable=False),
        sa.Column("producer_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("quality_state", sa.String(length=16), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quality_state IN ('VALID','STALE','UNKNOWN','QUARANTINED')",
            name="erp_sync_project_quality_values",
        ),
        sa.ForeignKeyConstraint(
            ["producer_attempt_id"],
            ["erp_sync_attempts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("project_number"),
    )
    op.create_index(
        "ix_erp_sync_project_quality_attempt",
        "erp_sync_project_quality",
        ["producer_attempt_id"],
    )
    op.create_index(
        "ix_erp_sync_project_quality_state",
        "erp_sync_project_quality",
        ["quality_state"],
    )

    op.create_table(
        "erp_sync_recovery_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('NONE','AVAILABLE','REQUIRED','RESOLVED','EXPIRED')",
            name="erp_sync_recovery_state_values",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_erp_sync_recovery_attempt"),
    )
    op.create_index(
        "ix_erp_sync_recovery_state",
        "erp_sync_recovery_state",
        ["state"],
    )

    op.create_table(
        "erp_sync_compensation_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("command_identity", sa.String(length=160), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("affected_records", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_identity", name="uq_erp_sync_compensation_command"
        ),
    )
    op.create_index(
        "ix_erp_sync_compensation_attempt",
        "erp_sync_compensation_receipts",
        ["attempt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_erp_sync_compensation_attempt",
        table_name="erp_sync_compensation_receipts",
    )
    op.drop_table("erp_sync_compensation_receipts")
    op.drop_index("ix_erp_sync_recovery_state", table_name="erp_sync_recovery_state")
    op.drop_table("erp_sync_recovery_state")
    op.drop_index(
        "ix_erp_sync_project_quality_state",
        table_name="erp_sync_project_quality",
    )
    op.drop_index(
        "ix_erp_sync_project_quality_attempt",
        table_name="erp_sync_project_quality",
    )
    op.drop_table("erp_sync_project_quality")
    op.drop_index(
        "ix_erp_sync_mutation_evidence_attempt",
        table_name="erp_sync_mutation_evidence",
    )
    op.drop_table("erp_sync_mutation_evidence")
    op.drop_index(
        "ix_erp_sync_plausibility_exception_attempt",
        table_name="erp_sync_plausibility_exceptions",
    )
    op.drop_table("erp_sync_plausibility_exceptions")
    op.drop_index("ix_erp_sync_baseline_approved", table_name="erp_sync_baselines")
    op.drop_table("erp_sync_baselines")
    op.drop_index(
        "ix_erp_sync_evidence_reservation_released",
        table_name="erp_sync_evidence_reservations",
    )
    op.drop_table("erp_sync_evidence_reservations")
    op.drop_index(
        "ix_erp_sync_evidence_chunk_manifest",
        table_name="erp_sync_evidence_chunks",
    )
    op.drop_table("erp_sync_evidence_chunks")
    op.drop_index(
        "ix_erp_sync_evidence_state_created",
        table_name="erp_sync_evidence_manifests",
    )
    op.drop_index(
        "ix_erp_sync_evidence_attempt",
        table_name="erp_sync_evidence_manifests",
    )
    op.drop_table("erp_sync_evidence_manifests")
