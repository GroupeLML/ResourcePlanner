from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    AssetRequirement,
    AssetType,
    Base,
    Project,
    ResourceRequirement,
)
from app.infrastructure.sql.asset_query import SqlAssetPlanningQuery


DAY = date(2026, 10, 2)


class AssetRequirementReadCompatibilityTests(unittest.TestCase):
    def test_planning_reader_decodes_all_five_origins_without_fake_context(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            session.add(Project(id="P-1", number="P-1", name="Projet 575"))
            session.add(
                ResourceRequirement(
                    id="RR-SEGMENT",
                    legacy_segment_id="SEG-575",
                    project_id="P-1",
                    workforce_request_id=None,
                    origin="AD_HOC",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                )
            )
            session.add(
                AssetType(
                    id="AT-1",
                    code="VEHICLE",
                    label="Véhicule",
                    category="VEHICLE",
                )
            )
            session.flush()

            session.add_all(
                [
                    AssetRequirement(
                        id="AR-REQUEST",
                        project_id="P-1",
                        origin=AssetRequirementOrigin.REQUEST.value,
                        workforce_request_id="WR-1",
                        source_request_line_id="RL-1",
                        approved_entry_key="ENTRY-1",
                        asset_type_id="AT-1",
                        start_date=DAY,
                        end_date=DAY,
                    ),
                    AssetRequirement(
                        id="AR-SHIFT",
                        project_id="P-1",
                        origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        shift_id="SHIFT-1",
                        asset_type_id="AT-1",
                        start_date=DAY,
                        end_date=DAY,
                    ),
                    AssetRequirement(
                        id="AR-PROJECT",
                        project_id="P-1",
                        origin=AssetRequirementOrigin.PROJECT_DIRECT.value,
                        asset_type_id="AT-1",
                        start_date=DAY,
                        end_date=DAY,
                    ),
                    AssetRequirement(
                        id="AR-SEGMENT",
                        project_id="P-1",
                        origin=AssetRequirementOrigin.SEGMENT.value,
                        resource_requirement_id="RR-SEGMENT",
                        asset_type_id="AT-1",
                        start_date=DAY,
                        end_date=DAY,
                    ),
                    AssetRequirement(
                        id="AR-RESOURCE",
                        project_id=None,
                        origin=AssetRequirementOrigin.RESOURCE_PERIOD.value,
                        context_resource_id="RESOURCE-1",
                        asset_type_id="AT-1",
                        start_date=DAY,
                        end_date=DAY,
                    ),
                ]
            )
            session.commit()

            rows = session.scalars(
                select(AssetRequirement).order_by(AssetRequirement.id)
            ).all()
            models = SqlAssetPlanningQuery(session)._requirement_models(rows)
            by_id = {row.requirement_id: row for row in models}

            self.assertEqual(
                {row.origin for row in models},
                {origin.value for origin in AssetRequirementOrigin},
            )

            request = by_id["AR-REQUEST"]
            self.assertEqual(request.request_id, "WR-1")
            self.assertEqual(request.demand_number, "WR-1")
            self.assertEqual(request.project_id, "P-1")
            self.assertEqual(request.project_number, "P-1")
            self.assertEqual(request.source_request_line_id, "RL-1")
            self.assertIsNone(request.shift_id)
            self.assertIsNone(request.resource_requirement_id)
            self.assertIsNone(request.context_resource_id)

            shift = by_id["AR-SHIFT"]
            self.assertEqual(shift.shift_id, "SHIFT-1")
            self.assertIsNone(shift.request_id)
            self.assertIsNone(shift.demand_number)
            self.assertIsNone(shift.resource_requirement_id)
            self.assertIsNone(shift.context_resource_id)

            project = by_id["AR-PROJECT"]
            self.assertEqual(project.project_id, "P-1")
            self.assertEqual(project.project_number, "P-1")
            self.assertIsNone(project.request_id)
            self.assertIsNone(project.shift_id)
            self.assertIsNone(project.resource_requirement_id)
            self.assertIsNone(project.context_resource_id)

            segment = by_id["AR-SEGMENT"]
            self.assertEqual(segment.resource_requirement_id, "RR-SEGMENT")
            self.assertEqual(segment.segment_reference, "SEG-575")
            self.assertEqual(segment.project_id, "P-1")
            self.assertIsNone(segment.request_id)
            self.assertIsNone(segment.shift_id)
            self.assertIsNone(segment.context_resource_id)

            resource_period = by_id["AR-RESOURCE"]
            self.assertIsNone(resource_period.project_id)
            self.assertIsNone(resource_period.project_number)
            self.assertEqual(resource_period.context_resource_id, "RESOURCE-1")
            self.assertIsNone(resource_period.request_id)
            self.assertIsNone(resource_period.demand_number)
            self.assertIsNone(resource_period.resource_requirement_id)
            self.assertIsNone(resource_period.shift_id)

            window = SqlAssetPlanningQuery(session).planning_window(
                start=DAY,
                end=DAY,
            )
            self.assertEqual(
                {row.requirement_id for row in window.requirements},
                set(by_id),
            )
            approval_diagnostic_ids = {
                row.requirement_id
                for row in window.diagnostics
                if row.code.startswith("ASSET_APPROVAL_")
            }
            self.assertNotIn("AR-SHIFT", approval_diagnostic_ids)
            self.assertNotIn("AR-PROJECT", approval_diagnostic_ids)
            self.assertNotIn("AR-SEGMENT", approval_diagnostic_ids)
            self.assertNotIn("AR-RESOURCE", approval_diagnostic_ids)

        engine.dispose()


if __name__ == "__main__":
    unittest.main()
