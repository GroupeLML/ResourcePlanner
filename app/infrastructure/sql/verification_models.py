from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ID_LENGTH = 36


class VerificationScopeRow(TimestampMixin, Base):
    __tablename__ = "verification_scopes"
    __table_args__ = (
        CheckConstraint(
            "verification_version >= 1",
            name="verification_scope_version_positive",
        ),
        UniqueConstraint(
            "work_package_id",
            name="uq_verification_scopes_work_package",
        ),
        Index("ix_verification_scopes_lead_user", "lead_user_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    work_package_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("work_packages.id"), nullable=False
    )
    lead_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )
    verification_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )


class VerificationRequirementRow(TimestampMixin, Base):
    __tablename__ = "verification_requirements"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('FAT','SAT','COMMISSIONING')",
            name="verification_requirement_phase",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','WITHDRAWN')",
            name="verification_requirement_state",
        ),
        CheckConstraint(
            "(state = 'ACTIVE' AND withdrawal_reason IS NULL) OR "
            "(state = 'WITHDRAWN' AND withdrawal_reason IS NOT NULL)",
            name="verification_requirement_withdrawal_state",
        ),
        Index(
            "ix_verification_requirements_scope_story_state",
            "verification_scope_id",
            "story_id",
            "state",
        ),
        Index(
            "ix_verification_requirements_current_revision",
            "current_revision_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verification_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_scopes.id"), nullable=False
    )
    story_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_items.id"), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    current_revision_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'ACTIVE'")
    )
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class VerificationRequirementRevisionRow(Base):
    __tablename__ = "verification_requirement_revisions"
    __table_args__ = (
        CheckConstraint(
            "revision_number >= 1",
            name="verification_requirement_revision_number_positive",
        ),
        UniqueConstraint(
            "requirement_id",
            "revision_number",
            name="uq_verification_requirement_revision_number",
        ),
        Index(
            "ix_verification_requirement_revisions_requirement_created",
            "requirement_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_requirements.id"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False)
    prerequisites_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    criticality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StoryVerificationDecisionRow(Base):
    __tablename__ = "story_verification_decisions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('NO_TEST_REQUIRED','TESTS_DEFINED')",
            name="story_verification_decision_kind",
        ),
        CheckConstraint(
            "kind <> 'NO_TEST_REQUIRED' OR justification IS NOT NULL",
            name="story_verification_decision_no_test_justification",
        ),
        Index(
            "ix_story_verification_decisions_scope_story_created",
            "verification_scope_id",
            "story_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verification_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_scopes.id"), nullable=False
    )
    story_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_items.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StoryVerificationDecisionRequirementRow(Base):
    __tablename__ = "story_verification_decision_requirements"

    decision_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("story_verification_decisions.id"),
        primary_key=True,
    )
    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("verification_requirements.id"),
        primary_key=True,
    )


class VerificationRetestRequestRow(Base):
    __tablename__ = "verification_retest_requests"
    __table_args__ = (
        CheckConstraint(
            "after_execution_sequence >= 0",
            name="verification_retest_sequence_non_negative",
        ),
        Index(
            "ix_verification_retest_requests_requirement_created",
            "requirement_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_requirements.id"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("verification_requirement_revisions.id"),
        nullable=False,
    )
    after_execution_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerificationChangeHistory(Base):
    __tablename__ = "verification_change_history"
    __table_args__ = (
        CheckConstraint(
            "verification_version >= 1",
            name="verification_history_version_positive",
        ),
        Index(
            "ix_verification_change_history_scope_occurred",
            "verification_scope_id",
            "occurred_at",
        ),
        Index("ix_verification_change_history_actor", "actor_user_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verification_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_scopes.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    story_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_items.id"), nullable=True
    )
    actor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    details_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
