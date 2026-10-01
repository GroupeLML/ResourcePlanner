from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ID_LENGTH = 36


class ApprovalScope(TimestampMixin, Base):
    __tablename__ = "approval_scopes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_approval_scopes_code"),
        CheckConstraint("version >= 1", name="approval_scope_version_positive"),
    )

    id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
        default=new_id,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=true(),
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )


class ApprovalScopeApprover(Base):
    __tablename__ = "approval_scope_approvers"

    approval_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_scopes.id"),
        primary_key=True,
    )
    app_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        primary_key=True,
        index=True,
    )


class ResourceClassApprovalScopeMapping(Base):
    __tablename__ = "resource_class_approval_scope_mappings"

    resource_class_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("resource_class_configs.code"),
        primary_key=True,
    )
    approval_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_scopes.id"),
        primary_key=True,
        index=True,
    )


class AssetTypeApprovalScopeMapping(Base):
    __tablename__ = "asset_type_approval_scope_mappings"

    asset_type_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("asset_types.id"),
        primary_key=True,
    )
    approval_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_scopes.id"),
        primary_key=True,
        index=True,
    )


class TaskApprovalScopeMapping(Base):
    __tablename__ = "task_approval_scope_mappings"

    task_catalog_item_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("task_catalog_items.id"),
        primary_key=True,
    )
    approval_scope_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_scopes.id"),
        primary_key=True,
        index=True,
    )
