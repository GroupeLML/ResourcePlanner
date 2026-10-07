from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestPeriod,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.period_approved_sync import (
    SqlPeriodAwareApprovedDemandSyncAdapter,
)
from app.infrastructure.sql.plan_delta_query_repository import (
    SqlPlannerQueryRepositoryWithPlanDelta,
)
from app.infrastructure.sql.request_plan_preparation import (
    LOCKED_HOURS_EXCEED_BUDGET,
    LOCKED_REQUIREMENT_REMOVAL,
    LOCKED_SHIFT_OUTSIDE_WINDOW,
    SEGMENT_ASSET_OUTSIDE_WINDOW,
    SEGMENT_ASSET_REQUIREMENT_REMOVAL,
    SqlRequestPlanPreparer,
)


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)


class SharedRequestPlanPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet 13C"))
            session.add(Resource(id="R1", name="Alice", active=True))
            session.add(
                AssetType(
                    id="AT-SEG",
                    code="VEH-SEG",
                    label="Véhicule segment",
                    category="VEHICLE",
                )
            )
            session.flush()
            session.add(
                Asset(
                    id="A-SEG",
                    code="A-SEG",
                    label="Actif segment",
                    asset_type_id="AT-SEG",
                )
            )
            session.add(
                ResourceAvailabilityRule(
                    id="SCH-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    start_date=D1,
                    end_date=D2,
                    weekdays="Lun,Mar",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _locked_shift(
        *,
        shift_id: str,
        requirement_id: str,
        hours: str = "8",
    ) -> Shift:
        return Shift(
            id=shift_id,
            resource_requirement_id=requirement_id,
            resource_id="R1",
            work_date=D1,
            hours=Decimal(hours),
            allocation_type="Flexible",
            source="MANUAL",
            locked=True,
        )

    def _legacy_conflict(self, *, hours: str = "8", start: date = D2) -> str:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-LEG",
                legacy_demand_number="DEM-LEG",
                project_id="P1",
                desired_start=start,
                desired_end=start,
                estimated_hours=Decimal(hours),
                resource_count=1,
                status="Soumise",
            )
            requirement = ResourceRequirement(
                id="REQ-LEG",
                project_id="P1",
                workforce_request_id="D-LEG",
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="Planifié",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add(requirement)
            session.flush()
            session.add(
                self._locked_shift(
                    shift_id="SHIFT-LEG",
                    requirement_id="REQ-LEG",
                )
            )
            session.flush()
            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[requirement])
            conflicts = preparer.locked_conflicts(
                request,
                [requirement],
                plan.specs,
            )
            self.assertEqual(len(conflicts), 1)
            return conflicts[0].code

    def _line_conflict(self, *, hours: str = "8", start: date = D2) -> str:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-LINE",
                legacy_demand_number="DEM-LINE",
                project_id="P1",
                line_mode=True,
                status="Soumise",
            )
            line = RequestLine(
                id="L1",
                workforce_request_id="D-LINE",
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=start,
                desired_end=start,
                estimated_hours=Decimal(hours),
                confirmation="Confirmée",
                active=True,
            )
            requirement = ResourceRequirement(
                id="REQ-LINE",
                project_id="P1",
                workforce_request_id="D-LINE",
                source_request_line_id="L1",
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="Planifié",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add(line)
            session.flush()
            session.add(requirement)
            session.flush()
            session.add(
                self._locked_shift(
                    shift_id="SHIFT-LINE",
                    requirement_id="REQ-LINE",
                )
            )
            session.flush()
            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[requirement])
            conflicts = preparer.locked_conflicts(
                request,
                [requirement],
                plan.specs,
            )
            self.assertEqual(len(conflicts), 1)
            return conflicts[0].code

    def test_legacy_and_line_mode_share_locked_window_decision(self) -> None:
        self.assertEqual(
            self._legacy_conflict(start=D2),
            LOCKED_SHIFT_OUTSIDE_WINDOW,
        )
        self.assertEqual(
            self._line_conflict(start=D2),
            LOCKED_SHIFT_OUTSIDE_WINDOW,
        )

    def test_legacy_and_line_mode_share_locked_budget_decision(self) -> None:
        self.assertEqual(
            self._legacy_conflict(hours="4", start=D1),
            LOCKED_HOURS_EXCEED_BUDGET,
        )
        self.assertEqual(
            self._line_conflict(hours="4", start=D1),
            LOCKED_HOURS_EXCEED_BUDGET,
        )

    def test_removal_of_locked_requirement_is_rejected_for_legacy_slots(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-REMOVE",
                legacy_demand_number="DEM-REMOVE",
                project_id="P1",
                desired_start=D1,
                desired_end=D1,
                estimated_hours=Decimal("8"),
                resource_count=1,
                status="Soumise",
            )
            keep = ResourceRequirement(
                id="REQ-KEEP",
                project_id="P1",
                workforce_request_id="D-REMOVE",
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("4"),
                status="Planifié",
                origin="REQUEST",
            )
            remove = ResourceRequirement(
                id="REQ-REMOVE",
                project_id="P1",
                workforce_request_id="D-REMOVE",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("4"),
                status="À assigner",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add_all([keep, remove])
            session.flush()
            session.add(
                self._locked_shift(
                    shift_id="SHIFT-REMOVE",
                    requirement_id="REQ-REMOVE",
                    hours="4",
                )
            )
            session.flush()
            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[keep, remove])
            conflicts = preparer.locked_conflicts(
                request,
                [keep, remove],
                plan.specs,
            )
            self.assertEqual(
                [row.code for row in conflicts],
                [LOCKED_REQUIREMENT_REMOVAL],
            )

    def test_modern_period_quantity_and_inherited_values_come_from_line_master(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-PERIOD-MASTER",
                legacy_demand_number="DEM-PERIOD-MASTER",
                project_id="P1",
                line_mode=True,
                status="Soumise",
            )
            line = RequestLine(
                id="L-PERIOD-MASTER",
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
                slot_count=3,
                desired_start=D1,
                desired_end=D2,
                estimated_hours=Decimal("12"),
                confirmation="Confirmée",
                proposed_resource_id="R1",
                active=True,
            )
            session.add(request)
            session.flush()
            session.add(line)
            session.flush()
            session.add(
                WorkforceRequestPeriod(
                    period_key="P1",
                    workforce_request_id=request.id,
                    request_line_id=line.id,
                    sequence=1,
                    kind="CUMULATIVE",
                    start_date=D1,
                    end_date=D2,
                    hours=Decimal("12"),
                    inheritance_contract_version=1,
                    confirmation_mode="INHERIT_MASTER",
                    confirmation="Tentative",
                    proposed_resource_mode="INHERIT_MASTER",
                    proposed_resource_id=None,
                    resource_count=99,
                    active=True,
                )
            )
            session.flush()

            plan = SqlRequestPlanPreparer(session).prepare(request)

            self.assertEqual(len(plan.specs), 3)
            self.assertEqual([row.slot_index for row in plan.specs], [0, 1, 2])
            self.assertEqual(
                [row.planned_hours for row in plan.specs],
                [Decimal("4.00"), Decimal("4.00"), Decimal("4.00")],
            )
            self.assertEqual(
                [row.confirmation for row in plan.specs],
                ["Confirmée", "Confirmée", "Confirmée"],
            )
            self.assertEqual(
                [row.proposed_resource_id for row in plan.specs],
                ["R1", None, None],
            )

    def test_line_identity_is_preserved_in_shared_spec_and_matching(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-ID",
                legacy_demand_number="DEM-ID",
                project_id="P1",
                line_mode=True,
                status="Soumise",
            )
            line = RequestLine(
                id="L-ID",
                workforce_request_id="D-ID",
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D1,
                desired_end=D1,
                estimated_hours=Decimal("8"),
                confirmation="Tentative",
                active=True,
            )
            requirement = ResourceRequirement(
                id="REQ-ID",
                project_id="P1",
                workforce_request_id="D-ID",
                source_request_line_id="L-ID",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="À assigner",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add(line)
            session.flush()
            session.add(requirement)
            session.flush()

            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[requirement])
            self.assertEqual(len(plan.specs), 1)
            spec = plan.specs[0]
            self.assertEqual(spec.key, ("LINE", "L-ID"))
            self.assertEqual(spec.source_request_line_id, "L-ID")
            self.assertIn('"L-ID"', spec.approved_entry_key)

            matches, obsolete = preparer.match_current(
                request,
                [requirement],
                plan.specs,
            )
            self.assertEqual(obsolete, ())
            self.assertEqual(len(matches), 1)
            self.assertIs(matches[0].requirement, requirement)

    def test_plan_delta_uses_same_locked_window_conflict(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-PREVIEW",
                legacy_demand_number="DEM-PREVIEW",
                project_id="P1",
                desired_start=D2,
                desired_end=D2,
                estimated_hours=Decimal("8"),
                resource_count=1,
                status="Soumise",
            )
            requirement = ResourceRequirement(
                id="REQ-PREVIEW",
                legacy_segment_id="SEG-PREVIEW",
                project_id="P1",
                workforce_request_id=request.id,
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="Planifié",
                planning_type="Flexible",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add(requirement)
            session.flush()
            session.add(
                self._locked_shift(
                    shift_id="SHIFT-PREVIEW",
                    requirement_id=requirement.id,
                )
            )
            session.flush()

            result = SqlPlannerQueryRepositoryWithPlanDelta(
                session
            ).demand_plan_delta("DEM-PREVIEW")

            self.assertIsNotNone(result)
            assert result is not None
            self.assertFalse(result.available)
            self.assertEqual(result.reason, LOCKED_SHIFT_OUTSIDE_WINDOW)
            self.assertTrue(session.get(Shift, "SHIFT-PREVIEW").locked)
            self.assertEqual(
                session.get(ResourceRequirement, requirement.id).start_date,
                D1,
            )

    def test_materialization_guard_blocks_same_locked_window_change_in_both_modes(self) -> None:
        with transactional_session(self.factory) as session:
            legacy = WorkforceRequest(
                id="D-GUARD-LEG",
                legacy_demand_number="DEM-GUARD-LEG",
                project_id="P1",
                desired_start=D2,
                desired_end=D2,
                estimated_hours=Decimal("8"),
                resource_count=1,
                status="En planification",
            )
            legacy_req = ResourceRequirement(
                id="REQ-GUARD-LEG",
                project_id="P1",
                workforce_request_id=legacy.id,
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="Planifié",
                origin="REQUEST",
            )
            line_request = WorkforceRequest(
                id="D-GUARD-LINE",
                legacy_demand_number="DEM-GUARD-LINE",
                project_id="P1",
                line_mode=True,
                status="En planification",
            )
            line = RequestLine(
                id="L-GUARD",
                workforce_request_id=line_request.id,
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D2,
                desired_end=D2,
                estimated_hours=Decimal("8"),
                confirmation="Confirmée",
                active=True,
            )
            line_req = ResourceRequirement(
                id="REQ-GUARD-LINE",
                project_id="P1",
                workforce_request_id=line_request.id,
                source_request_line_id=line.id,
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D1,
                planned_hours=Decimal("8"),
                status="Planifié",
                origin="REQUEST",
            )
            session.add_all([legacy, line_request])
            session.flush()
            session.add(line)
            session.flush()
            session.add_all([legacy_req, line_req])
            session.flush()
            session.add_all(
                [
                    self._locked_shift(
                        shift_id="SHIFT-GUARD-LEG",
                        requirement_id=legacy_req.id,
                    ),
                    self._locked_shift(
                        shift_id="SHIFT-GUARD-LINE",
                        requirement_id=line_req.id,
                    ),
                ]
            )
            session.flush()

            adapter = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            for number in ("DEM-GUARD-LEG", "DEM-GUARD-LINE"):
                with self.assertRaisesRegex(
                    ValueError,
                    "exclut un quart verrouillé",
                ):
                    adapter.sync_approved(number)

            self.assertEqual(
                session.get(ResourceRequirement, legacy_req.id).start_date,
                D1,
            )
            self.assertEqual(
                session.get(ResourceRequirement, line_req.id).start_date,
                D1,
            )
            self.assertTrue(session.get(Shift, "SHIFT-GUARD-LEG").locked)
            self.assertTrue(session.get(Shift, "SHIFT-GUARD-LINE").locked)


    def test_segment_asset_blocks_reapproval_window_shrink(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-SEG-SHRINK",
                legacy_demand_number="DEM-SEG-SHRINK",
                project_id="P1",
                line_mode=True,
                status="Soumise",
            )
            line = RequestLine(
                id="L-SEG-SHRINK",
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D1,
                desired_end=D1,
                estimated_hours=Decimal("8"),
                confirmation="Confirmée",
                active=True,
            )
            requirement = ResourceRequirement(
                id="REQ-SEG-SHRINK",
                project_id="P1",
                workforce_request_id=request.id,
                source_request_line_id=line.id,
                start_date=D1,
                end_date=D2,
                planned_hours=Decimal("16"),
                status="Planifié",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add(line)
            session.flush()
            session.add(requirement)
            session.flush()
            asset_requirement = AssetRequirement(
                id="AREQ-SEG-SHRINK",
                project_id="P1",
                origin="SEGMENT",
                resource_requirement_id=requirement.id,
                asset_type_id="AT-SEG",
                start_date=D2,
                end_date=D2,
                status="Planifié",
            )
            session.add(asset_requirement)
            session.flush()
            session.add(
                AssetAllocation(
                    id="ALLOC-SEG-SHRINK",
                    asset_requirement_id=asset_requirement.id,
                    asset_id="A-SEG",
                    operator_resource_id="R1",
                    start_date=D2,
                    end_date=D2,
                    locked=True,
                    source="MANUAL",
                )
            )
            session.flush()

            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[requirement])
            conflicts = preparer.locked_conflicts(
                request,
                [requirement],
                plan.specs,
                target_project_id=plan.project_id,
            )

            self.assertEqual(
                [row.code for row in conflicts],
                [SEGMENT_ASSET_OUTSIDE_WINDOW],
            )
            with self.assertRaisesRegex(ValueError, "réservation d'actif"):
                preparer.assert_locked_compatible(
                    request,
                    [requirement],
                    plan.specs,
                    target_project_id=plan.project_id,
                )

    def test_segment_asset_blocks_reapproval_that_replaces_owner_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D-SEG-REMOVE",
                legacy_demand_number="DEM-SEG-REMOVE",
                project_id="P1",
                line_mode=True,
                status="Soumise",
            )
            old_line = RequestLine(
                id="L-SEG-OLD",
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D1,
                desired_end=D2,
                estimated_hours=Decimal("16"),
                confirmation="Confirmée",
                active=False,
            )
            new_line = RequestLine(
                id="L-SEG-NEW",
                workforce_request_id=request.id,
                position=1,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D1,
                desired_end=D2,
                estimated_hours=Decimal("16"),
                confirmation="Confirmée",
                active=True,
            )
            requirement = ResourceRequirement(
                id="REQ-SEG-REMOVE",
                project_id="P1",
                workforce_request_id=request.id,
                source_request_line_id=old_line.id,
                start_date=D1,
                end_date=D2,
                planned_hours=Decimal("16"),
                status="Planifié",
                origin="REQUEST",
            )
            session.add(request)
            session.flush()
            session.add_all([old_line, new_line])
            session.flush()
            session.add(requirement)
            session.flush()
            asset_requirement = AssetRequirement(
                id="AREQ-SEG-REMOVE",
                project_id="P1",
                origin="SEGMENT",
                resource_requirement_id=requirement.id,
                asset_type_id="AT-SEG",
                start_date=D1,
                end_date=D1,
                status="Planifié",
            )
            session.add(asset_requirement)
            session.flush()
            session.add(
                AssetAllocation(
                    id="ALLOC-SEG-REMOVE",
                    asset_requirement_id=asset_requirement.id,
                    asset_id="A-SEG",
                    operator_resource_id="R1",
                    start_date=D1,
                    end_date=D1,
                    locked=True,
                    source="MANUAL",
                )
            )
            session.flush()

            preparer = SqlRequestPlanPreparer(session)
            plan = preparer.prepare(request, current=[requirement])
            conflicts = preparer.locked_conflicts(
                request,
                [requirement],
                plan.specs,
                target_project_id=plan.project_id,
            )
            self.assertIn(
                SEGMENT_ASSET_REQUIREMENT_REMOVAL,
                [row.code for row in conflicts],
            )

if __name__ == "__main__":
    unittest.main()
