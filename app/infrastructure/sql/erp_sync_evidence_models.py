from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ERP_EVIDENCE_PROTOCOL_VERSION = 1

EVIDENCE_SOURCE_PROJECTS = "PROJECTS"
EVIDENCE_SOURCE_TASKS = "TASKS"
EVIDENCE_SOURCE_CHECKPOINT = "CHECKPOINT"

EVIDENCE_PROOF_SOURCE = "SOURCE"
EVIDENCE_PROOF_CHECKPOINT = "CHECKPOINT"
EVIDENCE_PROOF_COMPENSATION = "COMPENSATION"

EVIDENCE_STATE_WRITING = "WRITING"
EVIDENCE_STATE_SEALED = "SEALED"
EVIDENCE_STATE_READY = "READY"
EVIDENCE_STATE_EXPIRED = "EXPIRED"
EVIDENCE_STATE_PURGED = "PURGED"

RECOVERY_NONE = "NONE"
RECOVERY_AVAILABLE = "AVAILABLE"
RECOVERY_REQUIRED = "REQUIRED"
RECOVERY_RESOLVED = "RESOLVED"
RECOVERY_EXPIRED = "EXPIRED"


class ErpSyncEvidenceManifest(TimestampMixin, Base):
    __tablename__ = "erp_sync_evidence_manifests"
    __table_args__ = (
        CheckConstraint(
            "protocol_version = 1",
            name="erp_sync_evidence_protocol_supported",
        ),
        CheckConstraint(
            "source_type IN ('PROJECTS','TASKS','CHECKPOINT')",
            name="erp_sync_evidence_source_type_values",
        ),
        CheckConstraint(
            "proof_type IN ('SOURCE','CHECKPOINT','COMPENSATION')",
            name="erp_sync_evidence_proof_type_values",
        ),
        CheckConstraint(
            "state IN ('WRITING','SEALED','READY','EXPIRED','PURGED')",
            name="erp_sync_evidence_state_values",
        ),
        UniqueConstraint(
            "attempt_id",
            "source_type",
            "proof_type",
            name="uq_erp_sync_evidence_attempt_source_proof",
        ),
        Index("ix_erp_sync_evidence_attempt", "attempt_id"),
        Index("ix_erp_sync_evidence_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    protocol_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=ERP_EVIDENCE_PROTOCOL_VERSION
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    proof_type: Mapped[str] = mapped_column(String(24), nullable=False)
    profile_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ciphertext_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=EVIDENCE_STATE_WRITING)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)


class ErpSyncEvidenceChunk(TimestampMixin, Base):
    __tablename__ = "erp_sync_evidence_chunks"
    __table_args__ = (
        UniqueConstraint(
            "manifest_id",
            "ordinal",
            name="uq_erp_sync_evidence_chunk_ordinal",
        ),
        Index("ix_erp_sync_evidence_chunk_manifest", "manifest_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manifest_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_evidence_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    plaintext_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciphertext_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ErpSyncEvidenceReservation(TimestampMixin, Base):
    __tablename__ = "erp_sync_evidence_reservations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_erp_sync_evidence_reservation_attempt"),
        CheckConstraint(
            "reserved_bytes >= 0 AND used_bytes >= 0",
            name="erp_sync_evidence_reservation_non_negative",
        ),
        Index("ix_erp_sync_evidence_reservation_released", "released_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ErpSyncBaseline(TimestampMixin, Base):
    __tablename__ = "erp_sync_baselines"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "profile_fingerprint",
            name="uq_erp_sync_baseline_source_profile",
        ),
        CheckConstraint(
            "source_type IN ('PROJECTS','TASKS')",
            name="erp_sync_baseline_source_type_values",
        ),
        Index("ix_erp_sync_baseline_approved", "approved_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    profile_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encrypted_project_counts: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ErpSyncPlausibilityException(TimestampMixin, Base):
    __tablename__ = "erp_sync_plausibility_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "source_type",
            "source_fingerprint",
            "threshold_scope",
            name="uq_erp_sync_plausibility_exception",
        ),
        Index("ix_erp_sync_plausibility_exception_attempt", "attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ErpSyncMutationEvidence(TimestampMixin, Base):
    __tablename__ = "erp_sync_mutation_evidence"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "unit_type",
            "entity_type",
            "entity_key",
            name="uq_erp_sync_mutation_evidence_entity",
        ),
        Index("ix_erp_sync_mutation_evidence_attempt", "attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(160), nullable=False)
    before_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    encrypted_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ErpSyncRecoveryState(TimestampMixin, Base):
    __tablename__ = "erp_sync_recovery_state"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_erp_sync_recovery_attempt"),
        CheckConstraint(
            "state IN ('NONE','AVAILABLE','REQUIRED','RESOLVED','EXPIRED')",
            name="erp_sync_recovery_state_values",
        ),
        Index("ix_erp_sync_recovery_state", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=RECOVERY_NONE)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ErpSyncCompensationReceipt(TimestampMixin, Base):
    __tablename__ = "erp_sync_compensation_receipts"
    __table_args__ = (
        UniqueConstraint("command_identity", name="uq_erp_sync_compensation_command"),
        Index("ix_erp_sync_compensation_attempt", "attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    affected_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
