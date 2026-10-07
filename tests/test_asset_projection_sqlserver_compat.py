from __future__ import annotations

import unittest

from sqlalchemy import column, select, table
from sqlalchemy.dialects import mssql

from app.infrastructure.sql.query_repository import _boolean_projection


class AssetProjectionSqlServerCompatibilityTests(unittest.TestCase):
    def test_exists_boolean_projection_compiles_as_case_for_sql_server(self) -> None:
        asset_requirements = table("asset_requirements", column("id"))
        shifts = table(
            "shifts",
            column("id"),
            column("asset_requirement_id"),
        )
        compatible = (
            select(shifts.c.id)
            .where(shifts.c.asset_requirement_id == asset_requirements.c.id)
            .limit(1)
            .correlate(asset_requirements)
            .exists()
        )
        statement = select(
            asset_requirements.c.id,
            _boolean_projection(compatible).label("human_compatible"),
        )

        sql = str(
            statement.compile(
                dialect=mssql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).upper()

        self.assertIn("CASE WHEN (EXISTS", sql)
        self.assertIn("THEN 1 ELSE 0 END AS HUMAN_COMPATIBLE", sql)
        self.assertNotIn(
            "SELECT ASSET_REQUIREMENTS.ID, EXISTS",
            sql,
        )


if __name__ == "__main__":
    unittest.main()
