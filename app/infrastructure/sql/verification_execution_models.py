from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from .verification_models import ID_LENGTH


class VerificationExecutorAssignmentRow(TimestampMixin, Base):
    __tablename__ = "verification_executor_assignments"
    __table_args__ = (
        Index(
            "ix_verification_executor_assignments_requirement_executor_active",
            "requirement_id",
            "executor_user_id",
            "active",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("verification_requirements.id"), nullable=False
    )
    executor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    assigned_by_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    ended_by_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VerificationTestExecutionRow(Base):
    __tablename__ = "verification_test_executions"
    __table_args__ = (
        CheckConstraint(
            "execution_sequence >= 1",
            name="verification_test_execution_sequence_positive",
        ),
        CheckConstraint(
            "result IN ('PASS','FAIL','BLOCKED')",
            name="verification_test_execution_result",
        ),
        UniqueConstraint(
            "requirement_id",
            "execution_sequence",
            name="uq_verification_test_execution_requirement_sequence",
        ),
        Index(
            "ix_verification_test_executions_requirement_revision_sequence",
            "requirement_id",
            "revision_id",
            "execution_sequence",
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
    sequence: Mapped[int] = mapped_column("execution_sequence", Integer, nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    executor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    measurements_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'{}'")
    )
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerificationEvidenceLinkRow(Base):
    __tablename__ = "verification_evidence_links"
    __table_args__ = (
        Index(
            "ix_verification_evidence_links_execution_created",
            "execution_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("verification_test_executions.id"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'external_https'")
    )
    added_by_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
