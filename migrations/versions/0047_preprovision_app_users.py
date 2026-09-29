"""Allow AppUser pre-provisioning before OIDC identity binding.

Revision ID: 0047_preprovision_app_users
Revises: 0046_delivery_persistence
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0047_preprovision_app_users"
down_revision: str | None = "0046_delivery_persistence"
branch_labels: str | None = None
depends_on: str | None = None


OIDC_UNIQUE_INDEX = "ux_app_users_oidc_identity_not_null"
ERP_USER_UNIQUE_INDEX = "ux_app_users_erp_user_id_not_null"
OIDC_COMPLETE_CHECK = "ck_app_users_oidc_identity_complete"
ERP_USER_FK = "fk_app_users_erp_user_id_erp_user_directory"
LEGACY_OIDC_UNIQUE = "uq_app_users_issuer_subject"


def _filtered_index_kwargs(predicate: str) -> dict[str, object]:
    clause = sa.text(predicate)
    return {
        "sqlite_where": clause,
        "postgresql_where": clause,
        "mssql_where": clause,
    }


def _alter_app_users_for_preprovisioning() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("app_users", recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column("erp_user_id", sa.String(length=128), nullable=True)
            )
            batch_op.alter_column(
                "issuer",
                existing_type=sa.String(length=512),
                nullable=True,
            )
            batch_op.alter_column(
                "subject",
                existing_type=sa.String(length=255),
                nullable=True,
            )
            batch_op.drop_constraint(LEGACY_OIDC_UNIQUE, type_="unique")
            batch_op.create_check_constraint(
                OIDC_COMPLETE_CHECK,
                "(issuer IS NULL AND subject IS NULL) OR "
                "(issuer IS NOT NULL AND subject IS NOT NULL)",
            )
            batch_op.create_foreign_key(
                ERP_USER_FK,
                "erp_user_directory",
                ["erp_user_id"],
                ["user_id"],
            )
        return

    op.add_column(
        "app_users",
        sa.Column("erp_user_id", sa.String(length=128), nullable=True),
    )
    op.alter_column(
        "app_users",
        "issuer",
        existing_type=sa.String(length=512),
        nullable=True,
    )
    op.alter_column(
        "app_users",
        "subject",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.drop_constraint(LEGACY_OIDC_UNIQUE, "app_users", type_="unique")
    op.create_check_constraint(
        OIDC_COMPLETE_CHECK,
        "app_users",
        "(issuer IS NULL AND subject IS NULL) OR "
        "(issuer IS NOT NULL AND subject IS NOT NULL)",
    )
    op.create_foreign_key(
        ERP_USER_FK,
        "app_users",
        "erp_user_directory",
        ["erp_user_id"],
        ["user_id"],
    )


def _backfill_unambiguous_erp_user_ids() -> None:
    # Strictly join through EmployeID. A unique RP_Users.UserID is selected only
    # when exactly one directory row references the AppUser's EmployeID.
    # Ambiguous employees intentionally remain NULL for later administrative repair.
    op.execute(
        sa.text(
            """
            UPDATE app_users
            SET erp_user_id = (
                SELECT MIN(erp_user_directory.user_id)
                FROM erp_user_directory
                WHERE erp_user_directory.employee_external_id =
                      app_users.employee_external_id
            )
            WHERE app_users.erp_user_id IS NULL
              AND app_users.employee_external_id IS NOT NULL
              AND (
                  SELECT COUNT(*)
                  FROM erp_user_directory
                  WHERE erp_user_directory.employee_external_id =
                        app_users.employee_external_id
              ) = 1
            """
        )
    )


def upgrade() -> None:
    _alter_app_users_for_preprovisioning()
    _backfill_unambiguous_erp_user_ids()

    op.create_index(
        OIDC_UNIQUE_INDEX,
        "app_users",
        ["issuer", "subject"],
        unique=True,
        **_filtered_index_kwargs("issuer IS NOT NULL AND subject IS NOT NULL"),
    )
    op.create_index(
        ERP_USER_UNIQUE_INDEX,
        "app_users",
        ["erp_user_id"],
        unique=True,
        **_filtered_index_kwargs("erp_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Downgrade deliberately does not invent OIDC identities for pre-provisioned
    # accounts. If NULL identities exist, restoring NOT NULL will fail rather than
    # silently corrupting identity data.
    op.drop_index(ERP_USER_UNIQUE_INDEX, table_name="app_users")
    op.drop_index(OIDC_UNIQUE_INDEX, table_name="app_users")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("app_users", recreate="always") as batch_op:
            batch_op.drop_constraint(ERP_USER_FK, type_="foreignkey")
            batch_op.drop_constraint(OIDC_COMPLETE_CHECK, type_="check")
            batch_op.alter_column(
                "issuer",
                existing_type=sa.String(length=512),
                nullable=False,
            )
            batch_op.alter_column(
                "subject",
                existing_type=sa.String(length=255),
                nullable=False,
            )
            batch_op.create_unique_constraint(
                LEGACY_OIDC_UNIQUE,
                ["issuer", "subject"],
            )
            batch_op.drop_column("erp_user_id")
        return

    op.drop_constraint(ERP_USER_FK, "app_users", type_="foreignkey")
    op.drop_constraint(OIDC_COMPLETE_CHECK, "app_users", type_="check")
    op.alter_column(
        "app_users",
        "issuer",
        existing_type=sa.String(length=512),
        nullable=False,
    )
    op.alter_column(
        "app_users",
        "subject",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_unique_constraint(
        LEGACY_OIDC_UNIQUE,
        "app_users",
        ["issuer", "subject"],
    )
    op.drop_column("app_users", "erp_user_id")
