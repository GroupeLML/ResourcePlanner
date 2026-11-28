from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Unicode, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id


BROADCAST_ACTIVE = "ACTIVE"
BROADCAST_ENDED = "ENDED"


class EmergencyBroadcast(Base):
    __tablename__ = "emergency_broadcasts"
    __table_args__ = (
        CheckConstraint("version = 1", name="version_one"),
        CheckConstraint("state IN ('ACTIVE', 'ENDED')", name="state_valid"),
        CheckConstraint(
            "(state = 'ACTIVE' AND ended_at IS NULL AND ended_by_user_id IS NULL) "
            "OR (state = 'ENDED' AND ended_at IS NOT NULL AND ended_by_user_id IS NOT NULL)",
            name="end_fields_match_state",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= activated_at",
            name="ended_after_activation",
        ),
        Index(
            "ux_emergency_broadcasts_creator_command",
            "created_by_user_id",
            "creation_command_id",
            unique=True,
        ),
        Index(
            "ux_emergency_broadcasts_single_active",
            "state",
            unique=True,
            sqlite_where=text("state = 'ACTIVE'"),
            mssql_where=text("state = 'ACTIVE'"),
        ),
        Index(
            "ix_emergency_broadcasts_history",
            "state",
            "ended_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    title: Mapped[str] = mapped_column(Unicode(160), nullable=False)
    message: Mapped[str] = mapped_column(Unicode(4000), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("app_users.id"), nullable=False
    )
    creation_command_id: Mapped[str] = mapped_column(String(36), nullable=False)
    creation_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=BROADCAST_ACTIVE)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("app_users.id"), nullable=True
    )


class EmergencyBroadcastAcknowledgment(Base):
    __tablename__ = "emergency_broadcast_acknowledgments"

    broadcast_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("emergency_broadcasts.id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("app_users.id"), primary_key=True
    )
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
