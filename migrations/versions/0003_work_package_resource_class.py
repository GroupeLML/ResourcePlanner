"""Add canonical resource class to WorkPackage.

Revision ID: 0003_work_package_resource_class
Revises: 0002_work_package_weekly_loads
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0003_work_package_resource_class"
down_revision: str | None = "0002_work_package_weekly_loads"
branch_labels: str | None = None
depends_on: str | None = None


FK_NAME = "fk_work_packages_resource_class_code_resource_class_configs"
INDEX_NAME = "ix_work_packages_resource_class_code"


def _add_resource_class_column() -> None:
    bind = op.get_bind()
    column = sa.Column("resource_class_code", sa.String(length=64), nullable=True)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.add_column(column)
            batch_op.create_foreign_key(
                FK_NAME,
                "resource_class_configs",
                ["resource_class_code"],
                ["code"],
            )
            batch_op.create_index(INDEX_NAME, ["resource_class_code"], unique=False)
        return

    op.add_column("work_packages", column)
    op.create_foreign_key(
        FK_NAME,
        "work_packages",
        "resource_class_configs",
        ["resource_class_code"],
        ["code"],
    )
    op.create_index(
        INDEX_NAME,
        "work_packages",
        ["resource_class_code"],
        unique=False,
    )


def _backfill_resource_class() -> None:
    # The task classification is copied only when every persisted relationship is
    # demonstrably coherent. Inactive classes are intentionally retained because
    # they may be the only historical qualification available.
    op.execute(
        sa.text(
            """
            UPDATE work_packages
            SET resource_class_code = (
                SELECT task_catalog_items.resource_class_code
                FROM task_catalog_items
                JOIN projects
                  ON projects.id = work_packages.project_id
                 AND projects.number = task_catalog_items.project_number
                JOIN resource_class_configs
                  ON resource_class_configs.code = task_catalog_items.resource_class_code
                WHERE task_catalog_items.id = work_packages.task_catalog_item_id
                  AND task_catalog_items.resource_class_code IS NOT NULL
            )
            WHERE work_packages.task_catalog_item_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM task_catalog_items
                JOIN projects
                  ON projects.id = work_packages.project_id
                 AND projects.number = task_catalog_items.project_number
                JOIN resource_class_configs
                  ON resource_class_configs.code = task_catalog_items.resource_class_code
                WHERE task_catalog_items.id = work_packages.task_catalog_item_id
                  AND task_catalog_items.resource_class_code IS NOT NULL
              )
            """
        )
    )


def upgrade() -> None:
    _add_resource_class_column()
    _backfill_resource_class()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.drop_index(INDEX_NAME)
            batch_op.drop_constraint(FK_NAME, type_="foreignkey")
            batch_op.drop_column("resource_class_code")
        return

    op.drop_index(INDEX_NAME, table_name="work_packages")
    op.drop_constraint(FK_NAME, "work_packages", type_="foreignkey")
    op.drop_column("work_packages", "resource_class_code")
