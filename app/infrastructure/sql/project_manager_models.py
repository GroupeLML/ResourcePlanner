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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id


ID_LENGTH = 36
PROJECT_MANAGER_SOURCE_RP = "RP"


class ProjectCoManager(Base):
    __tablename__ = "project_co_managers"
    __table_args__ = (
        Index(
            "ix_project_co_managers_contact_project",
            "business_contact_id",
            "project_id",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("projects.id"),
        primary_key=True,
    )
    business_contact_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("business_contacts.id"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        nullable=False,
    )


class ProjectManagerAudit(Base):
    __tablename__ = "project_manager_audit"
    __table_args__ = (
        CheckConstraint(
            "resulting_version IS NULL OR resulting_version >= 1",
            name="resulting_version_positive",
        ),
        Index(
            "ix_project_manager_audit_project_created",
            "project_id",
            "created_at",
        ),
        Index("ix_project_manager_audit_actor", "actor_user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
        default=new_id,
    )
    project_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("projects.id"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    resulting_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
