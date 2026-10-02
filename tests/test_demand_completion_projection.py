from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)


TODAY = date.today()
PAST = TODAY - timedelta(days=2)
FUTURE = TODAY + timedelta(days=2)


class DemandCompletionProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "completion.db"
        self.url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    Project(id="P1", number="P-1", name="Projet"),
                    Resource(id="R1", name="Alice", active=True),
                    AssetType(
                        id="AT1",
                        code="VEH",
                        label="Véhicule",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add(
                Asset(
                    id="A1",
                    code="TRUCK-1",
                    label="Camion",
                    asset_type_id="AT1",
                )
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _created(offset: int) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=offset)

    def _request(
        self,
        session,
        key: str,
        *,
        status: str = "En planification",
        desired_end: date = PAST,
        cancellation_state: str | None = None,
        created_offset: int = 0,
    ) -> WorkforceRequest:
        request = WorkforceRequest(
            id=f"D-{key}",
            legacy_demand_number=f"DMO-{key}",
            project_id="P1",
            status=status,
            aggregate_version=2,
            line_mode=True,
            desired_start=desired_end,
            desired_end=desired_end,
            cancellation_state=cancellation_state,
            created_at=self._created(created_offset),
        )
        session.add(request)
        session.flush()
        return request

    def _human(
        self,
        session,
        request: WorkforceRequest,
        key: str,
        *,
        planned_hours: float = 8,
        shift_day: date | None = PAST,
        shift_hours: float | None = 8,
    ) -> None:
        line_id = f"L-{key}"
        requirement_id = f"REQ-{key}"
        session.add(
            RequestLine(
                id=line_id,
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
                desired_start=PAST,
                desired_end=FUTURE if shift_day == FUTURE else PAST,
                estimated_hours=planned_hours,
            )
        )
        session.flush()
        session.add(
            ResourceRequirement(
                id=requirement_id,
                legacy_segment_id=f"SEG-{key}",
                project_id="P1",
                workforce_request_id=request.id,
                source_request_line_id=line_id,
                start_date=PAST,
                end_date=FUTURE if shift_day == FUTURE else PAST,
                planned_hours=planned_hours,
                status="Planifié",
                origin="REQUEST",
            )
        )
        session.flush()
        if shift_day is not None and shift_hours is not None:
            session.add(
                Shift(
                    id=f"SHIFT-{key}",
                    resource_requirement_id=requirement_id,
                    resource_id="R1",
                    work_date=shift_day,
                    hours=shift_hours,
                    source="MANUAL",
                    locked=True,
                )
            )
            session.flush()

    def _asset(
        self,
        session,
        request: WorkforceRequest,
        key: str,
        *,
        start_date: date = PAST,
        end_date: date = PAST,
        allocated: bool = True,
        allocation_start_date: date | None = None,
        allocation_end_date: date | None = None,
        usage_hours: float | None = None,
    ) -> None:
        line_id = f"L-{key}"
        requirement_id = f"AREQ-{key}"
        session.add(
            RequestLine(
                id=line_id,
                workforce_request_id=request.id,
                position=1,
                kind="ASSET",
                desired_start=start_date,
                desired_end=end_date,
                asset_type_id="AT1",
            )
        )
        session.flush()
        session.add(
            AssetRequirement(
                id=requirement_id,
                project_id="P1",
                workforce_request_id=request.id,
                source_request_line_id=line_id,
                approved_entry_key=f"LINE:{line_id}",
                slot_index=0,
                asset_type_id="AT1",
                start_date=start_date,
                end_date=end_date,
                usage_hours=usage_hours,
                status="Planifié" if allocated else "À affecter",
            )
        )
        session.flush()
        if allocated:
            session.add(
                AssetAllocation(
                    id=f"ALLOC-{key}",
                    asset_requirement_id=requirement_id,
                    asset_id="A1",
                    start_date=allocation_start_date or start_date,
                    end_date=allocation_end_date or end_date,
                    locked=True,
                    source="MANUAL",
                )
            )
            session.flush()

    def _rows(self) -> dict[str, object]:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                return {
                    row.number: row
                    for row in SqlPlannerQueryRepository(session).list_demands()
                }
        finally:
            engine.dispose()

    def test_effective_completion_uses_materialized_plan_not_desired_date(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            completed = self._request(session, "COMPLETE", created_offset=1)
            self._human(session, completed, "COMPLETE")

            future = self._request(
                session,
                "FUTURE",
                desired_end=PAST,
                created_offset=2,
            )
            self._human(
                session,
                future,
                "FUTURE",
                shift_day=FUTURE,
                shift_hours=8,
            )

            remainder = self._request(session, "REMAINDER", created_offset=3)
            self._human(
                session,
                remainder,
                "REMAINDER",
                shift_day=PAST,
                shift_hours=4,
            )

            no_plan = self._request(session, "NO-PLAN", created_offset=4)
            session.add(
                RequestLine(
                    id="L-NO-PLAN",
                    workforce_request_id=no_plan.id,
                    position=0,
                    kind="WORKFORCE",
                    desired_start=PAST,
                    desired_end=PAST,
                    estimated_hours=8,
                )
            )
        engine.dispose()

        rows = self._rows()
        self.assertEqual(rows["DMO-COMPLETE"].status, "En planification")
        self.assertEqual(rows["DMO-COMPLETE"].effective_status, "Complétée")
        self.assertTrue(rows["DMO-COMPLETE"].terminal)
        self.assertIsNotNone(rows["DMO-COMPLETE"].created_at)
        self.assertEqual(
            rows["DMO-COMPLETE"].created_at.replace(tzinfo=timezone.utc),
            self._created(1),
        )

        self.assertEqual(rows["DMO-FUTURE"].effective_status, "En planification")
        self.assertFalse(rows["DMO-FUTURE"].terminal)
        self.assertEqual(rows["DMO-REMAINDER"].effective_status, "En planification")
        self.assertFalse(rows["DMO-REMAINDER"].terminal)
        self.assertEqual(rows["DMO-NO-PLAN"].effective_status, "En planification")
        self.assertFalse(rows["DMO-NO-PLAN"].terminal)

    def test_asset_subwindow_allocation_covers_request_requirement(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            request = self._request(session, "ASSET-SUBWINDOW")
            self._asset(
                session,
                request,
                "ASSET-SUBWINDOW",
                start_date=PAST,
                end_date=FUTURE,
                allocation_start_date=PAST,
                allocation_end_date=PAST,
                usage_hours=8,
            )
        engine.dispose()

        row = self._rows()["DMO-ASSET-SUBWINDOW"]
        self.assertEqual(row.effective_status, "Complétée")
        self.assertTrue(row.terminal)

    def test_assets_mixed_cancellation_and_reapproval_remain_distinct(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            asset_done = self._request(session, "ASSET-DONE")
            self._asset(session, asset_done, "ASSET-DONE")

            asset_future = self._request(session, "ASSET-FUTURE")
            self._asset(
                session,
                asset_future,
                "ASSET-FUTURE",
                start_date=FUTURE,
                end_date=FUTURE,
            )

            asset_remainder = self._request(session, "ASSET-REMAINDER")
            self._asset(
                session,
                asset_remainder,
                "ASSET-REMAINDER",
                allocated=False,
            )

            mixed = self._request(session, "MIXED")
            self._human(session, mixed, "MIXED-HUMAN")
            self._asset(session, mixed, "MIXED-ASSET")

            cancelled = self._request(session, "CANCELLED", status="Annulée")
            self._human(session, cancelled, "CANCELLED")

            candidate = self._request(session, "REAPPROVAL", status="Soumise")
            self._human(session, candidate, "REAPPROVAL")

            cancellation_pending = self._request(
                session,
                "CANCELLATION-PENDING",
                cancellation_state="PENDING",
            )
            self._human(session, cancellation_pending, "CANCELLATION-PENDING")
        engine.dispose()

        rows = self._rows()
        self.assertEqual(rows["DMO-ASSET-DONE"].effective_status, "Complétée")
        self.assertTrue(rows["DMO-ASSET-DONE"].terminal)
        self.assertEqual(
            rows["DMO-ASSET-FUTURE"].effective_status,
            "En planification",
        )
        self.assertFalse(rows["DMO-ASSET-FUTURE"].terminal)
        self.assertEqual(
            rows["DMO-ASSET-REMAINDER"].effective_status,
            "En planification",
        )
        self.assertFalse(rows["DMO-ASSET-REMAINDER"].terminal)
        self.assertEqual(rows["DMO-MIXED"].effective_status, "Complétée")
        self.assertTrue(rows["DMO-MIXED"].terminal)

        self.assertEqual(rows["DMO-CANCELLED"].effective_status, "Annulée")
        self.assertTrue(rows["DMO-CANCELLED"].terminal)
        self.assertEqual(rows["DMO-REAPPROVAL"].effective_status, "Soumise")
        self.assertFalse(rows["DMO-REAPPROVAL"].terminal)
        self.assertEqual(
            rows["DMO-CANCELLATION-PENDING"].effective_status,
            "En planification",
        )
        self.assertFalse(rows["DMO-CANCELLATION-PENDING"].terminal)


if __name__ == "__main__":
    unittest.main()
