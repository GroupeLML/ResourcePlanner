"""Add explicit asset reservation contexts for ADR-018.

Revision ID: 0008_asset_requirement_contexts
Revises: 0007_project_co_managers
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0008_asset_requirement_contexts"
down_revision: str | None = "0007_project_co_managers"
branch_labels: str | None = None
depends_on: str | None = None


REQUEST_ORIGIN = "REQUEST"
SHIFT_AD_HOC_ORIGIN = "SHIFT_AD_HOC"
PROJECT_DIRECT_ORIGIN = "PROJECT_DIRECT"
SEGMENT_ORIGIN = "SEGMENT"
RESOURCE_PERIOD_ORIGIN = "RESOURCE_PERIOD"

ORIGIN_VALUES_CHECK = "ck_asset_requirements_asset_requirement_origin_values"
ORIGIN_PROVENANCE_CHECK = "ck_asset_requirements_asset_requirement_origin_provenance"
RESOURCE_REQUIREMENT_FK = (
    "fk_asset_requirements_resource_requirement_id_resource_requirements"
)
CONTEXT_RESOURCE_FK = "fk_asset_requirements_context_resource_id_resources"
RESOURCE_REQUIREMENT_INDEX = "ix_asset_requirements_resource_requirement_id"
CONTEXT_RESOURCE_INDEX = "ix_asset_requirements_context_resource_id"


def _origin_values_sql() -> str:
    return (
        "origin IN ("
        "'REQUEST', 'SHIFT_AD_HOC', 'PROJECT_DIRECT', 'SEGMENT', 'RESOURCE_PERIOD'"
        ")"
    )


def _historical_origin_provenance_sql() -> str:
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


def _origin_provenance_sql() -> str:
    approval_refs_null = (
        "workforce_request_id IS NULL "
        "AND source_request_line_id IS NULL "
        "AND source_period_id IS NULL "
        "AND approval_revision_id IS NULL "
        "AND approved_entry_key IS NULL"
    )
    return (
        "("
        "origin = 'REQUEST' "
        "AND project_id IS NOT NULL "
        "AND shift_id IS NULL "
        "AND resource_requirement_id IS NULL "
        "AND context_resource_id IS NULL "
        "AND workforce_request_id IS NOT NULL "
        "AND source_request_line_id IS NOT NULL "
        "AND approved_entry_key IS NOT NULL"
        ") OR ("
        "origin = 'SHIFT_AD_HOC' "
        "AND project_id IS NOT NULL "
        "AND shift_id IS NOT NULL "
        "AND resource_requirement_id IS NULL "
        "AND context_resource_id IS NULL "
        f"AND {approval_refs_null}"
        ") OR ("
        "origin = 'PROJECT_DIRECT' "
        "AND project_id IS NOT NULL "
        "AND shift_id IS NULL "
        "AND resource_requirement_id IS NULL "
        "AND context_resource_id IS NULL "
        f"AND {approval_refs_null}"
        ") OR ("
        "origin = 'SEGMENT' "
        "AND project_id IS NOT NULL "
        "AND shift_id IS NULL "
        "AND resource_requirement_id IS NOT NULL "
        "AND context_resource_id IS NULL "
        f"AND {approval_refs_null}"
        ") OR ("
        "origin = 'RESOURCE_PERIOD' "
        "AND shift_id IS NULL "
        "AND resource_requirement_id IS NULL "
        "AND context_resource_id IS NOT NULL "
        f"AND {approval_refs_null}"
        ")"
    )


def _create_context_indexes() -> None:
    op.create_index(
        RESOURCE_REQUIREMENT_INDEX,
        "asset_requirements",
        ["resource_requirement_id"],
        unique=False,
    )
    op.create_index(
        CONTEXT_RESOURCE_INDEX,
        "asset_requirements",
        ["context_resource_id"],
        unique=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    resource_requirement_column = sa.Column(
        "resource_requirement_id",
        sa.String(length=36),
        nullable=True,
    )
    context_resource_column = sa.Column(
        "context_resource_id",
        sa.String(length=36),
        nullable=True,
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "asset_requirements",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                op.f(ORIGIN_PROVENANCE_CHECK),
                type_="check",
            )
            batch_op.drop_constraint(
                op.f(ORIGIN_VALUES_CHECK),
                type_="check",
            )
            batch_op.add_column(resource_requirement_column)
            batch_op.add_column(context_resource_column)
            batch_op.alter_column(
                "project_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )
            batch_op.create_foreign_key(
                RESOURCE_REQUIREMENT_FK,
                "resource_requirements",
                ["resource_requirement_id"],
                ["id"],
            )
            batch_op.create_foreign_key(
                CONTEXT_RESOURCE_FK,
                "resources",
                ["context_resource_id"],
                ["id"],
            )
            batch_op.create_check_constraint(
                op.f(ORIGIN_VALUES_CHECK),
                _origin_values_sql(),
            )
            batch_op.create_check_constraint(
                op.f(ORIGIN_PROVENANCE_CHECK),
                _origin_provenance_sql(),
            )
        _create_context_indexes()
        return

    op.drop_constraint(
        op.f(ORIGIN_PROVENANCE_CHECK),
        "asset_requirements",
        type_="check",
    )
    op.drop_constraint(
        op.f(ORIGIN_VALUES_CHECK),
        "asset_requirements",
        type_="check",
    )
    op.add_column("asset_requirements", resource_requirement_column)
    op.add_column("asset_requirements", context_resource_column)
    op.alter_column(
        "asset_requirements",
        "project_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.create_foreign_key(
        RESOURCE_REQUIREMENT_FK,
        "asset_requirements",
        "resource_requirements",
        ["resource_requirement_id"],
        ["id"],
    )
    op.create_foreign_key(
        CONTEXT_RESOURCE_FK,
        "asset_requirements",
        "resources",
        ["context_resource_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f(ORIGIN_VALUES_CHECK),
        "asset_requirements",
        _origin_values_sql(),
    )
    op.create_check_constraint(
        op.f(ORIGIN_PROVENANCE_CHECK),
        "asset_requirements",
        _origin_provenance_sql(),
    )
    _create_context_indexes()


def downgrade() -> None:
    bind = op.get_bind()
    incompatible = tuple(
        bind.execute(
            sa.text(
                "SELECT DISTINCT origin FROM asset_requirements "
                "WHERE origin IN (:project_direct, :segment, :resource_period)"
            ),
            {
                "project_direct": PROJECT_DIRECT_ORIGIN,
                "segment": SEGMENT_ORIGIN,
                "resource_period": RESOURCE_PERIOD_ORIGIN,
            },
        ).scalars()
    )
    if incompatible:
        raise RuntimeError(
            "Cannot downgrade 0008_asset_requirement_contexts while "
            "new asset requirement origins exist: "
            + ", ".join(sorted(incompatible))
        )

    op.drop_index(
        CONTEXT_RESOURCE_INDEX,
        table_name="asset_requirements",
    )
    op.drop_index(
        RESOURCE_REQUIREMENT_INDEX,
        table_name="asset_requirements",
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "asset_requirements",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                op.f(ORIGIN_PROVENANCE_CHECK),
                type_="check",
            )
            batch_op.drop_constraint(
                op.f(ORIGIN_VALUES_CHECK),
                type_="check",
            )
            batch_op.drop_constraint(
                RESOURCE_REQUIREMENT_FK,
                type_="foreignkey",
            )
            batch_op.drop_constraint(
                CONTEXT_RESOURCE_FK,
                type_="foreignkey",
            )
            batch_op.alter_column(
                "project_id",
                existing_type=sa.String(length=36),
                nullable=False,
            )
            batch_op.drop_column("resource_requirement_id")
            batch_op.drop_column("context_resource_id")
            batch_op.create_check_constraint(
                op.f(ORIGIN_VALUES_CHECK),
                "origin IN ('REQUEST', 'SHIFT_AD_HOC')",
            )
            batch_op.create_check_constraint(
                op.f(ORIGIN_PROVENANCE_CHECK),
                _historical_origin_provenance_sql(),
            )
        return

    op.drop_constraint(
        op.f(ORIGIN_PROVENANCE_CHECK),
        "asset_requirements",
        type_="check",
    )
    op.drop_constraint(
        op.f(ORIGIN_VALUES_CHECK),
        "asset_requirements",
        type_="check",
    )
    op.drop_constraint(
        RESOURCE_REQUIREMENT_FK,
        "asset_requirements",
        type_="foreignkey",
    )
    op.drop_constraint(
        CONTEXT_RESOURCE_FK,
        "asset_requirements",
        type_="foreignkey",
    )
    op.alter_column(
        "asset_requirements",
        "project_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.drop_column("asset_requirements", "resource_requirement_id")
    op.drop_column("asset_requirements", "context_resource_id")
    op.create_check_constraint(
        op.f(ORIGIN_VALUES_CHECK),
        "asset_requirements",
        "origin IN ('REQUEST', 'SHIFT_AD_HOC')",
    )
    op.create_check_constraint(
        op.f(ORIGIN_PROVENANCE_CHECK),
        "asset_requirements",
        _historical_origin_provenance_sql(),
    )
