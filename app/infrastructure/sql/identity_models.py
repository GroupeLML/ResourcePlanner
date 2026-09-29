from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text, true
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


class AppUser(TimestampMixin, Base):
    __tablename__ = "app_users"
    __table_args__ = (
        CheckConstraint(
            "(issuer IS NULL AND subject IS NULL) OR "
            "(issuer IS NOT NULL AND subject IS NOT NULL "
            "AND issuer <> '' AND subject <> '')",
            name="oidc_identity_complete",
        ),
        Index(
            "ux_app_users_oidc_identity_not_null",
            "issuer",
            "subject",
            unique=True,
            sqlite_where=text("issuer IS NOT NULL AND subject IS NOT NULL"),
            postgresql_where=text("issuer IS NOT NULL AND subject IS NOT NULL"),
            mssql_where=text("issuer IS NOT NULL AND subject IS NOT NULL"),
        ),
        Index(
            "ux_app_users_employee_external_id_not_null",
            "employee_external_id",
            unique=True,
            sqlite_where=text("employee_external_id IS NOT NULL"),
            mssql_where=text("employee_external_id IS NOT NULL"),
        ),
        Index(
            "ux_app_users_erp_user_id_not_null",
            "erp_user_id",
            unique=True,
            sqlite_where=text("erp_user_id IS NOT NULL"),
            postgresql_where=text("erp_user_id IS NOT NULL"),
            mssql_where=text("erp_user_id IS NOT NULL"),
        ),
        Index(
            "ux_app_users_business_contact_id_not_null",
            "business_contact_id",
            unique=True,
            sqlite_where=text("business_contact_id IS NOT NULL"),
            postgresql_where=text("business_contact_id IS NOT NULL"),
            mssql_where=text("business_contact_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    employee_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    erp_user_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("erp_user_directory.user_id"),
        nullable=True,
    )
    business_contact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("business_contacts.id"),
        nullable=True,
    )
    roles_json: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true(), index=True)


class AuthLoginTransaction(TimestampMixin, Base):
    __tablename__ = "auth_login_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(255), nullable=False)
    browser_binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthSession(TimestampMixin, Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("app_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
