"""Add explicit REQUEST / SHIFT_AD_HOC ownership to asset requirements.

Revision ID: 0006_asset_requirement_origins
Revises: 0005_task_sync_runs
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0006_asset_requirement_origins"
down_revision: str | None = "0005_task_sync_runs"
branch_labels: str | None = None
depends_on: str | None = None


REQUEST_ORIGIN = "REQUEST"
SHIFT_AD_HOC_ORIGIN = "SHIFT_AD_HOC"

REQUEST_UNIQUE_INDEX = "ux_asset_requirements_request_entry_slot"
SHIFT_AD_HOC_UNIQUE_INDEX = "ux_asset_requirements_shift_ad_hoc"
ORIGIN_VALUES_CHECK = "ck_asset_requirements_asset_requirement_origin_values"
ORIGIN_PROVENANCE_CHECK = "ck_asset_requirements_asset_requirement_origin_provenance"
SHIFT_FK = "fk_asset_requirements_shift_id_shifts"


def _create_filtered_indexes() -> None:
    op.create_index(
        REQUEST_UNIQUE_INDEX,
        "asset_requirements",
        ["workforce_request_id", "approved_entry_key", "slot_index"],
        unique=True,
        sqlite_where=sa.text("origin = 'REQUEST'"),
        mssql_where=sa.text("origin = 'REQUEST'"),
    )
    op.create_index(
        SHIFT_AD_HOC_UNIQUE_INDEX,
        "asset_requirements",
        ["shift_id"],
        unique=True,
        sqlite_where=sa.text("origin = 'SHIFT_AD_HOC'"),
        mssql_where=sa.text("origin = 'SHIFT_AD_HOC'"),
    )


def _origin_provenance_sql() -> str:
    return (
        "("
        "origin = 'REQUEST' "
        "AND shift_id IS NULL "
        "AND workforce_request_id IS NOT NULL "
        "AND source_request_line_id IS NOT NULL "
        "AND approved_entry_key IS NOT NULL"
        ") OR ("
        "origin = 'SHIFT_AD_HOC' "
        "AND shift_id IS NOT NULL "
        "AND workforce_request_id IS NULL "
        "AND source_request_line_id IS NULL "
        "AND source_period_id IS NULL "
        "AND approval_revision_id IS NULL "
        "AND approved_entry_key IS NULL"
        ")"
    )


def upgrade() -> None:
    bind = op.get_bind()
    origin_column = sa.Column(
        "origin",
        sa.String(length=32),
        nullable=False,
        server_default=sa.text("'REQUEST'"),
    )
    shift_column = sa.Column("shift_id", sa.String(length=36), nullable=True)

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("asset_requirements", recreate="always") as batch_op:
            batch_op.add_column(origin_column)
            batch_op.add_column(shift_column)
            batch_op.drop_constraint(
                "uq_asset_requirement_entry_slot",
                type_="unique",
            )
            batch_op.alter_column(
                "workforce_request_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )
            batch_op.alter_column(
                "source_request_line_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )
            batch_op.alter_column(
                "approved_entry_key",
                existing_type=sa.String(length=512),
                nullable=True,
            )
            batch_op.create_foreign_key(
                SHIFT_FK,
                "shifts",
                ["shift_id"],
                ["id"],
            )
            batch_op.create_check_constraint(
                ORIGIN_VALUES_CHECK,
                "origin IN ('REQUEST', 'SHIFT_AD_HOC')",
            )
            batch_op.create_check_constraint(
                ORIGIN_PROVENANCE_CHECK,
                _origin_provenance_sql(),
            )
        _create_filtered_indexes()
        return

    op.add_column("asset_requirements", origin_column)
    op.add_column("asset_requirements", shift_column)
    op.drop_constraint(
        "uq_asset_requirement_entry_slot",
        "asset_requirements",
        type_="unique",
    )
    op.alter_column(
        "asset_requirements",
        "workforce_request_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        "asset_requirements",
        "source_request_line_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        "asset_requirements",
        "approved_entry_key",
        existing_type=sa.String(length=512),
        nullable=True,
    )
    op.create_foreign_key(
        SHIFT_FK,
        "asset_requirements",
        "shifts",
        ["shift_id"],
        ["id"],
    )
    op.create_check_constraint(
        ORIGIN_VALUES_CHECK,
        "asset_requirements",
        "origin IN ('REQUEST', 'SHIFT_AD_HOC')",
    )
    op.create_check_constraint(
        ORIGIN_PROVENANCE_CHECK,
        "asset_requirements",
        _origin_provenance_sql(),
    )
    _create_filtered_indexes()


def downgrade() -> None:
    bind = op.get_bind()
    ad_hoc_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM asset_requirements "
            "WHERE origin = :origin"
        ),
        {"origin": SHIFT_AD_HOC_ORIGIN},
    ).scalar_one()
    if ad_hoc_count:
        raise RuntimeError(
            "Cannot downgrade 0006_asset_requirement_origins while "
            "SHIFT_AD_HOC asset requirements exist."
        )

    op.drop_index(
        SHIFT_AD_HOC_UNIQUE_INDEX,
        table_name="asset_requirements",
    )
    op.drop_index(
        REQUEST_UNIQUE_INDEX,
        table_name="asset_requirements",
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("asset_requirements", recreate="always") as batch_op:
            batch_op.drop_constraint(ORIGIN_PROVENANCE_CHECK, type_="check")
            batch_op.drop_constraint(ORIGIN_VALUES_CHECK, type_="check")
            batch_op.drop_constraint(SHIFT_FK, type_="foreignkey")
            batch_op.alter_column(
                "workforce_request_id",
                existing_type=sa.String(length=36),
                nullable=False,
            )
            batch_op.alter_column(
                "source_request_line_id",
                existing_type=sa.String(length=36),
                nullable=False,
            )
            batch_op.alter_column(
                "approved_entry_key",
                existing_type=sa.String(length=512),
                nullable=False,
            )
            batch_op.drop_column("shift_id")
            batch_op.drop_column("origin")
            batch_op.create_unique_constraint(
                "uq_asset_requirement_entry_slot",
                ["workforce_request_id", "approved_entry_key", "slot_index"],
            )
        return

    op.drop_constraint(
        ORIGIN_PROVENANCE_CHECK,
        "asset_requirements",
        type_="check",
    )
    op.drop_constraint(
        ORIGIN_VALUES_CHECK,
        "asset_requirements",
        type_="check",
    )
    op.drop_constraint(
        SHIFT_FK,
        "asset_requirements",
        type_="foreignkey",
    )
    op.alter_column(
        "asset_requirements",
        "workforce_request_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "asset_requirements",
        "source_request_line_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "asset_requirements",
        "approved_entry_key",
        existing_type=sa.String(length=512),
        nullable=False,
    )
    op.drop_column("asset_requirements", "shift_id")
    op.drop_column("asset_requirements", "origin")
    op.create_unique_constraint(
        "uq_asset_requirement_entry_slot",
        "asset_requirements",
        ["workforce_request_id", "approved_entry_key", "slot_index"],
    )
