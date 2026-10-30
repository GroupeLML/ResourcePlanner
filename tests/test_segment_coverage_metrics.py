from __future__ import annotations

from datetime import date, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    ORIGIN_AD_HOC,
    Base,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.overallocation import _allocation_projection_metrics
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class SegmentCoverageMetricsTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> tuple[str, date]:
        path = Path(directory) / "segment-coverage.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        today = date.today()

        with factory.begin() as session:
            session.add(Project(id="P1", number="P-1", name="Projet couverture"))
            session.add_all(
                (
                    Resource(id="R-A", name="Alice", active=True),
                    Resource(id="R-B", name="Bob", active=True),
                )
            )
            session.flush()

            session.add_all(
                (
                    ResourceRequirement(
                        id="REQ-1",
                        legacy_segment_id="SEG-1",
                        project_id="P1",
                        assigned_resource_id="R-A",
                        start_date=today,
                        end_date=today + timedelta(days=1),
                        planned_hours=40,
                        status="Planifié",
                        origin=ORIGIN_AD_HOC,
                        planning_type="Flexible",
                        confirmation="Confirmée",
                    ),
                    ResourceRequirement(
                        id="REQ-2",
                        legacy_segment_id="SEG-2",
                        project_id="P1",
                        assigned_resource_id="R-A",
                        start_date=today,
                        end_date=today,
                        planned_hours=8,
                        status="Planifié",
                        origin=ORIGIN_AD_HOC,
                        planning_type="Flexible",
                        confirmation="Confirmée",
                    ),
                )
            )
            session.flush()

            session.add_all(
                (
                    Shift(
                        id="S1",
                        resource_requirement_id="REQ-1",
                        resource_id="R-A",
                        work_date=today,
                        hours=8,
                        allocation_type="Flexible",
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S2",
                        resource_requirement_id="REQ-1",
                        resource_id="R-B",
                        work_date=today,
                        hours=8,
                        allocation_type="Flexible",
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S3",
                        resource_requirement_id="REQ-1",
                        resource_id="R-A",
                        work_date=today,
                        hours=8,
                        allocation_type="Flexible",
                        source="AUTO",
                        locked=False,
                    ),
                    Shift(
                        id="S4",
                        resource_requirement_id="REQ-1",
                        resource_id="R-B",
                        work_date=today + timedelta(days=1),
                        hours=16,
                        allocation_type=MISSING_ALLOCATION_TYPE,
                        source="AUTO",
                        locked=False,
                    ),
                    Shift(
                        id="S5",
                        resource_requirement_id="REQ-2",
                        resource_id="R-A",
                        work_date=today,
                        hours=6,
                        allocation_type="Flexible",
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S6",
                        resource_requirement_id="REQ-2",
                        resource_id="R-B",
                        work_date=today,
                        hours=4,
                        allocation_type="Flexible",
                        source="AUTO",
                        locked=False,
                    ),
                )
            )

        engine.dispose()
        return url, today

    def test_segment_projection_separates_target_actual_resources_and_coverage(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app) as client:
                response = client.get("/api/v1/segments/SEG-1")
                self.assertEqual(response.status_code, 200, response.text)
                segment = response.json()

                self.assertEqual(segment["requirement_id"], "REQ-1")
                self.assertEqual(segment["resource_name"], "Alice")
                self.assertEqual(segment["automatic_target_resource_id"], "R-A")
                self.assertEqual(segment["automatic_target_resource_name"], "Alice")

                self.assertEqual(segment["planned_hours"], 40.0)
                self.assertEqual(segment["locked_hours"], 16.0)
                self.assertEqual(segment["replaceable_hours"], 8.0)
                self.assertEqual(segment["covered_hours"], 24.0)
                self.assertEqual(segment["automatic_rebuild_hours"], 24.0)
                self.assertEqual(segment["remaining_hours"], 16.0)
                self.assertEqual(segment["excess_hours"], 0.0)
                self.assertEqual(segment["overallocated_hours"], 0.0)
                self.assertFalse(segment["overallocated"])

                mobilized = {
                    row["resource_id"]: row
                    for row in segment["mobilized_resources"]
                }
                self.assertEqual(set(mobilized), {"R-A", "R-B"})
                self.assertEqual(mobilized["R-A"]["resource_name"], "Alice")
                self.assertEqual(mobilized["R-A"]["allocated_hours"], 16.0)
                self.assertEqual(mobilized["R-A"]["locked_hours"], 8.0)
                self.assertEqual(mobilized["R-A"]["replaceable_hours"], 8.0)
                self.assertEqual(mobilized["R-B"]["resource_name"], "Bob")
                self.assertEqual(mobilized["R-B"]["allocated_hours"], 8.0)
                self.assertEqual(mobilized["R-B"]["locked_hours"], 8.0)
                self.assertEqual(mobilized["R-B"]["replaceable_hours"], 0.0)

                # The persisted "Hors horaire requis" proposal on tomorrow is not
                # counted as coverage or as a planned active day.
                self.assertEqual(segment["planned_active_days"], 1)

    def test_partial_projection_override_falls_back_to_canonical_effective_need(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    requirements = session.query(ResourceRequirement).order_by(
                        ResourceRequirement.id
                    ).all()
                    metrics = _allocation_projection_metrics(
                        session,
                        requirements,
                        planned_hours_by_reference={"SEG-1": 40.0},
                    )
            finally:
                engine.dispose()

            self.assertEqual(metrics["SEG-1"]["planned_hours"], 40.0)
            self.assertEqual(metrics["SEG-2"]["planned_hours"], 8.0)
            self.assertEqual(metrics["SEG-2"]["remaining_hours"], 0.0)

    def test_coverage_excess_is_distinct_from_locked_overallocation(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app) as client:
                response = client.get("/api/v1/segments/SEG-2")
                self.assertEqual(response.status_code, 200, response.text)
                segment = response.json()

                self.assertEqual(segment["planned_hours"], 8.0)
                self.assertEqual(segment["locked_hours"], 6.0)
                self.assertEqual(segment["replaceable_hours"], 4.0)
                self.assertEqual(segment["covered_hours"], 10.0)
                self.assertEqual(segment["automatic_rebuild_hours"], 2.0)
                self.assertEqual(segment["remaining_hours"], 0.0)
                self.assertEqual(segment["excess_hours"], 2.0)

                # Preserve #38 semantics: "overallocated" still means the explicit
                # locked/manual exception, not transient replaceable coverage.
                self.assertEqual(segment["overallocated_hours"], 0.0)
                self.assertFalse(segment["overallocated"])


if __name__ == "__main__":
    unittest.main()
