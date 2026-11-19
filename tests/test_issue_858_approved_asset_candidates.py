from __future__ import annotations

from datetime import date, timedelta
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.domain.reservable_assets import AssetRequirementOrigin, ResourcePeriodMode
from app.infrastructure.sql import Asset, AssetAllocation, AssetRequirement, AssetType, Base
from app.infrastructure.sql.asset_query import SqlAssetPlanningQuery


DAY = date(2026, 11, 19)


class ApprovedAssetCandidateEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([
                AssetType(id="TYPE-VEHICLE", code="VEH", label="Véhicule", category="VEHICLE"),
                AssetType(id="TYPE-TOOL", code="TOOL", label="Outil", category="TOOL"),
            ])
            for asset_id, type_id in [
                ("FREE", "TYPE-VEHICLE"), ("ACTIVE", "TYPE-VEHICLE"),
                ("ENDED", "TYPE-VEHICLE"), ("FUTURE", "TYPE-VEHICLE"),
                ("REVOKE", "TYPE-VEHICLE"), ("TRANSFER", "TYPE-VEHICLE"),
                ("BOUNDED", "TYPE-VEHICLE"), ("PROJECT", "TYPE-VEHICLE"),
                ("SEGMENT", "TYPE-VEHICLE"), ("CURRENT", "TYPE-VEHICLE"),
                ("TOOL-FREE", "TYPE-TOOL"),
            ]:
                session.add(Asset(id=asset_id, code=asset_id, label=asset_id, asset_type_id=type_id))
            session.flush()

            session.add_all([
                self._request("REQ", "TYPE-VEHICLE"),
                self._request("REQ-CURRENT", "TYPE-VEHICLE"),
                self._request("REQ-TOOL", "TYPE-TOOL"),
                self._occupied_requirement("CONT-ACTIVE", AssetRequirementOrigin.RESOURCE_PERIOD, DAY - timedelta(days=10), None, mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-ACTIVE"),
                self._occupied_requirement("CONT-ENDED", AssetRequirementOrigin.RESOURCE_PERIOD, DAY - timedelta(days=10), DAY - timedelta(days=1), mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-ENDED"),
                self._occupied_requirement("CONT-FUTURE", AssetRequirementOrigin.RESOURCE_PERIOD, DAY + timedelta(days=5), None, mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-FUTURE"),
                self._occupied_requirement("CONT-REVOKE", AssetRequirementOrigin.RESOURCE_PERIOD, DAY - timedelta(days=10), DAY + timedelta(days=2), mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-REVOKE"),
                self._occupied_requirement("CONT-TRANSFER-OLD", AssetRequirementOrigin.RESOURCE_PERIOD, DAY - timedelta(days=10), DAY + timedelta(days=4), mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-R1"),
                self._occupied_requirement("CONT-TRANSFER-NEW", AssetRequirementOrigin.RESOURCE_PERIOD, DAY + timedelta(days=5), None, mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-R2"),
                self._occupied_requirement("BOUND-REQ", AssetRequirementOrigin.RESOURCE_PERIOD, DAY, DAY, mode=ResourcePeriodMode.BOUNDED.value, resource="RESOURCE-BOUND"),
                self._occupied_requirement("PROJECT-REQ", AssetRequirementOrigin.PROJECT_DIRECT, DAY, DAY, project="P-HIDDEN"),
                self._occupied_requirement("SEGMENT-REQ", AssetRequirementOrigin.SEGMENT, DAY, DAY, project="P-HIDDEN", resource_requirement="RR-HIDDEN"),
            ])
            session.flush()

            allocations = [
                ("CONT-ACTIVE", "ACTIVE", DAY - timedelta(days=10), None, "PRIVATE-ACTIVE"),
                ("CONT-ENDED", "ENDED", DAY - timedelta(days=10), DAY - timedelta(days=1), "PRIVATE-ENDED"),
                ("CONT-FUTURE", "FUTURE", DAY + timedelta(days=5), None, "PRIVATE-FUTURE"),
                ("CONT-REVOKE", "REVOKE", DAY - timedelta(days=10), DAY + timedelta(days=2), "PRIVATE-REVOKE"),
                ("CONT-TRANSFER-OLD", "TRANSFER", DAY - timedelta(days=10), DAY + timedelta(days=4), "PRIVATE-R1"),
                ("CONT-TRANSFER-NEW", "TRANSFER", DAY + timedelta(days=5), None, "PRIVATE-R2"),
                ("BOUND-REQ", "BOUNDED", DAY, DAY, "RESOURCE-BOUND"),
                ("PROJECT-REQ", "PROJECT", DAY, DAY, None),
                ("SEGMENT-REQ", "SEGMENT", DAY, DAY, None),
                ("REQ-CURRENT", "CURRENT", DAY, DAY, None),
            ]
            for requirement_id, asset_id, start, end, operator in allocations:
                session.add(AssetAllocation(
                    id=f"ALLOC-{requirement_id}",
                    asset_requirement_id=requirement_id,
                    asset_id=asset_id,
                    operator_resource_id=operator,
                    start_date=start,
                    end_date=end,
                    locked=True,
                    source="MANUAL",
                ))
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _request(identifier: str, asset_type_id: str) -> AssetRequirement:
        return AssetRequirement(
            id=identifier,
            project_id="P-VISIBLE",
            origin=AssetRequirementOrigin.REQUEST.value,
            workforce_request_id="WR-VISIBLE",
            source_request_line_id=f"LINE-{identifier}",
            approved_entry_key=f"ENTRY-{identifier}",
            asset_type_id=asset_type_id,
            start_date=DAY - timedelta(days=20),
            end_date=DAY + timedelta(days=20),
            status="À affecter",
        )

    @staticmethod
    def _occupied_requirement(
        identifier: str,
        origin: AssetRequirementOrigin,
        start: date,
        end: date | None,
        *,
        mode: str | None = None,
        resource: str | None = None,
        project: str | None = None,
        resource_requirement: str | None = None,
    ) -> AssetRequirement:
        return AssetRequirement(
            id=identifier,
            project_id=project,
            origin=origin.value,
            context_resource_id=resource,
            resource_requirement_id=resource_requirement,
            asset_type_id="TYPE-VEHICLE",
            start_date=start,
            end_date=end,
            resource_period_mode=mode,
            status="Planifié",
        )

    def _eligible(self, start: date, end: date, requirement_id: str = "REQ") -> set[str]:
        with Session(self.engine) as session:
            rows = SqlAssetPlanningQuery(session).request_candidate_asset_ids(
                requirement_id=requirement_id,
                start_date=start,
                end_date=end,
            )
            self.assertIsNotNone(rows)
            return set(rows or ())

    def test_temporal_overlap_rules_and_ordinary_occupations(self) -> None:
        today = self._eligible(DAY, DAY)
        self.assertIn("FREE", today)
        self.assertIn("ENDED", today)
        self.assertIn("FUTURE", today)
        for blocked in ("ACTIVE", "REVOKE", "TRANSFER", "BOUNDED", "PROJECT", "SEGMENT"):
            self.assertNotIn(blocked, today)

        before_future = self._eligible(DAY + timedelta(days=3), DAY + timedelta(days=4))
        self.assertIn("FUTURE", before_future)
        self.assertIn("REVOKE", before_future)

        after_future = self._eligible(DAY + timedelta(days=6), DAY + timedelta(days=6))
        self.assertNotIn("FUTURE", after_future)
        self.assertIn("REVOKE", after_future)

        across_transfer = self._eligible(DAY + timedelta(days=4), DAY + timedelta(days=5))
        self.assertNotIn("TRANSFER", across_transfer)

    def test_current_allocation_and_non_vehicle_remain_supported(self) -> None:
        self.assertIn("CURRENT", self._eligible(DAY, DAY, "REQ-CURRENT"))
        self.assertEqual(self._eligible(DAY, DAY, "REQ-TOOL"), {"TOOL-FREE"})

    def test_snapshot_keeps_request_visible_and_hides_protected_occupation_details(self) -> None:
        with Session(self.engine) as session:
            window = SqlAssetPlanningQuery(session).planning_window(
                start=DAY,
                end=DAY,
                project_ids=("P-VISIBLE",),
                demand_ids=("WR-VISIBLE",),
                context_resource_ids=(),
            )
        by_id = {row.requirement_id: row for row in window.requirements}
        self.assertIn("REQ", by_id)
        self.assertNotIn("CONT-ACTIVE", by_id)
        self.assertNotIn("ACTIVE", by_id["REQ"].eligible_asset_ids)
        self.assertIn("FREE", by_id["REQ"].eligible_asset_ids)
        self.assertFalse(hasattr(by_id["REQ"], "conflicting_allocation_ids"))
        self.assertFalse(hasattr(by_id["REQ"], "continuous_beneficiary_id"))

    def test_zero_candidates_do_not_remove_approved_requirement(self) -> None:
        with Session(self.engine) as session:
            session.add(AssetType(id="TYPE-ZERO", code="ZERO", label="Zero", category="VEHICLE"))
            session.add(Asset(id="ZERO-ASSET", code="ZERO-ASSET", label="Zero", asset_type_id="TYPE-ZERO"))
            session.add(self._request("REQ-ZERO", "TYPE-ZERO"))
            session.add(self._occupied_requirement(
                "ZERO-BLOCK", AssetRequirementOrigin.RESOURCE_PERIOD, DAY, None,
                mode=ResourcePeriodMode.CONTINUOUS.value, resource="PRIVATE-ZERO",
            ))
            # blocker must use the zero type to match the candidate asset
            blocker = session.get(AssetRequirement, "ZERO-BLOCK")
            session.flush()
            blocker = session.get(AssetRequirement, "ZERO-BLOCK")
            assert blocker is not None
            blocker.asset_type_id = "TYPE-ZERO"
            session.add(AssetAllocation(
                id="ALLOC-ZERO-BLOCK",
                asset_requirement_id="ZERO-BLOCK",
                asset_id="ZERO-ASSET",
                operator_resource_id="PRIVATE-ZERO",
                start_date=DAY,
                end_date=None,
                locked=True,
                source="MANUAL",
            ))
            session.commit()
            window = SqlAssetPlanningQuery(session).planning_window(
                start=DAY,
                end=DAY,
                project_ids=("P-VISIBLE",),
                demand_ids=("WR-VISIBLE",),
                context_resource_ids=(),
            )
        by_id = {row.requirement_id: row for row in window.requirements}
        self.assertIn("REQ-ZERO", by_id)
        self.assertEqual(by_id["REQ-ZERO"].eligible_asset_ids, ())

    def test_eligibility_projection_does_not_add_per_requirement_queries(self) -> None:
        def count_selects() -> int:
            statements: list[str] = []
            def count_statement(_conn, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)
            event.listen(self.engine, "before_cursor_execute", count_statement)
            try:
                with Session(self.engine) as session:
                    SqlAssetPlanningQuery(session).planning_window(
                        start=DAY,
                        end=DAY,
                        project_ids=("P-VISIBLE",),
                        demand_ids=("WR-VISIBLE",),
                        context_resource_ids=(),
                    )
            finally:
                event.remove(self.engine, "before_cursor_execute", count_statement)
            return len(statements)

        baseline = count_selects()
        with Session(self.engine) as session:
            session.add_all([
                AssetRequirement(
                    id=f"REQ-BATCH-{index}",
                    project_id="P-VISIBLE",
                    origin=AssetRequirementOrigin.REQUEST.value,
                    workforce_request_id="WR-VISIBLE",
                    source_request_line_id=f"LINE-BATCH-{index}",
                    approved_entry_key=f"ENTRY-BATCH-{index}",
                    slot_index=index + 10,
                    asset_type_id="TYPE-VEHICLE",
                    start_date=DAY,
                    end_date=DAY,
                    status="À affecter",
                )
                for index in range(20)
            ])
            session.commit()
        self.assertLessEqual(count_selects(), baseline + 1)


if __name__ == "__main__":
    unittest.main()
