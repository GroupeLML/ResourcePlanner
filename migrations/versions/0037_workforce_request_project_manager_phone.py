"""#880 request-owned project-manager contact phone (widening only).

Revision ID: 0037_workforce_request_project_manager_phone
Revises: 0036_communication_message_body_html
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0037_workforce_request_project_manager_phone"
down_revision: str | None = "0036_communication_message_body_html"
branch_labels: str | None = None
depends_on: str | None = None


def _drop_mssql_default_constraint(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mssql":
        return
    constraint_name = bind.execute(
        sa.text(
            """
            SELECT dc.name
            FROM sys.default_constraints AS dc
            JOIN sys.columns AS c
              ON c.object_id = dc.parent_object_id
             AND c.column_id = dc.parent_column_id
            WHERE dc.parent_object_id = OBJECT_ID(:table_name)
              AND c.name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar_one_or_none()
    if not constraint_name:
        return
    preparer = bind.dialect.identifier_preparer
    op.execute(
        sa.text(
            f"ALTER TABLE {preparer.quote(table_name)} "
            f"DROP CONSTRAINT {preparer.quote(str(constraint_name))}"
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("workforce_requests") as batch:
        batch.add_column(
            sa.Column(
                "include_project_manager_contact_phone",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "project_manager_contact_phone",
                sa.String(64),
                nullable=True,
            )
        )


def downgrade() -> None:
    # Operational rollback retains the forward schema. Explicit downgrade is
    # destructive once request-owned contact choices have been entered and must
    # not run automatically during PREV/code rollback.
    _drop_mssql_default_constraint(
        "workforce_requests",
        "include_project_manager_contact_phone",
    )
    with op.batch_alter_table("workforce_requests") as batch:
        batch.drop_column("project_manager_contact_phone")
        batch.drop_column("include_project_manager_contact_phone")
