from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.application.errors import ApplicationConflictError
from sqlalchemy import select
from sqlalchemy.dialects import mssql

from app.domain.demand_periods import (
    DemandPeriodDefinition,
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    Project,
    RequestApprovalReference,
    RequestCompletionReduction,
    RequestLine,
    RequestRequirementPlanningIntent,
    RequestShiftSurvivorProtection,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDemandPeriodRepository,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    WorkforceRequest,
    WorkforceRequestPeriodRequirement,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.same_resource_periods import SqlSameResourcePeriodCoordinator
from tools.repair_legacy_simple_resource_class import (
    _unclassified_request_ids_statement,
    repair_all_request_classes,
    repair_request_class,
)


D1 = date(2026, 9, 7)
D2 = date(2026, 9, 8)
D3 = date(2026, 9, 9)


class SqlPeriodApprovalSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet périodes"))
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True),
                    Resource(id="R2", name="Bob", active=True),
                ]
            )
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DEM-1",
                    project_id="P1",
                    desired_start=D1,
                    desired_end=D3,
                    estimated_hours=Decimal("8"),
                    resource_count=1,
                    required_competencies="Programmation",
                    priority="Normale",
                    description="Support projet",
                    status="En planification",
                    approved_by_name="coord-test-user",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _alternatives() -> tuple[DemandPeriodDefinition, ...]:
        return (
            DemandPeriodDefinition(
                period_id="OPT-A",
                start_date=D1,
                end_date=D1,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="VISITE",
                proposed_resource="Alice",
            ),
            DemandPeriodDefinition(
                period_id="OPT-B",
                start_date=D2,
                end_date=D2,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="VISITE",
                proposed_resource="Bob",
            ),
        )

    def _active_requirements(self, session) -> list[ResourceRequirement]:
        return list(
            session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == "D1",
                    ResourceRequirement.status != "Annulé",
                )
            ).all()
        )

    def _protect_requirement(
        self,
        session,
        requirement: ResourceRequirement,
        *,
        reduction_state: str = "ABSORBED",
    ) -> tuple[RequestRequirementPlanningIntent, Shift, RequestShiftSurvivorProtection]:
        if session.get(AppUser, "U-877-PERIOD") is None:
            session.add(
                AppUser(
                    id="U-877-PERIOD",
                    issuer="urn:resourceplanner:test",
                    subject="u-877-period",
                    display_name="877 Period",
                    roles_json='["ADMIN"]',
                    active=True,
                )
            )
            session.flush()
        intent = session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        )
        assert intent is not None
        assert intent.approval_revision_id is not None
        shift = Shift(
            id=f"S-877-{requirement.id}",
            resource_requirement_id=requirement.id,
            resource_id="R1",
            work_date=requirement.start_date,
            hours=Decimal("8.00"),
            source="MANUAL",
            locked=False,
        )
        session.add(shift)
        session.flush()
        reduction = RequestCompletionReduction(
            id=f"RED-877-{requirement.id}",
            planning_intent_id=intent.id,
            workforce_request_id="D1",
            historical_approval_revision_id=intent.approval_revision_id,
            source_request_line_id=intent.source_request_line_id,
            approved_entry_key=intent.approved_entry_key,
            original_shift_id=f"REMOVED-877-{requirement.id}",
            shift_snapshot_text="{}",
            reduction_hours=Decimal("8.00"),
            effective_need_before=Decimal("16.00"),
            effective_need_after=Decimal("8.00"),
            actor_user_id="U-877-PERIOD",
            command_id=f"CMD-877-{requirement.id}",
            reconciliation_state=reduction_state,
        )
        session.add(reduction)
        session.flush()
        protection = RequestShiftSurvivorProtection(
            id=f"SP-877-{requirement.id}",
            planning_intent_id=intent.id,
            shift_id=shift.id,
            original_shift_id=shift.id,
            originating_reduction_id=reduction.id,
            active=True,
        )
        session.add(protection)
        session.flush()
        return intent, shift, protection

    def test_unresolved_alternative_group_materializes_no_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", self._alternatives())

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            self.assertEqual(self._active_requirements(session), [])

    def test_only_selected_alternative_materializes_and_switch_replaces_it(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", self._alternatives())
            periods.select_alternative("DEM-1", "VISITE", "OPT-A")
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)

            sync.sync_approved("DEM-1")
            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            first = active[0]
            self.assertEqual(first.start_date, D1)
            self.assertEqual(first.end_date, D1)
            self.assertEqual(first.planned_hours, Decimal("8.00"))
            self.assertIsNone(first.assigned_resource_id)
            first_link = session.get(WorkforceRequestPeriodRequirement, first.id)
            self.assertIsNotNone(first_link)

            periods.select_alternative("DEM-1", "VISITE", "OPT-B")
            sync.sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            second = active[0]
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(second.start_date, D2)
            self.assertIsNone(second.assigned_resource_id)
            self.assertEqual(session.get(ResourceRequirement, first.id).status, "Annulé")

    def test_legacy_period_switch_rejects_protected_survivor_without_terminalizing(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", self._alternatives())
            periods.select_alternative("DEM-1", "VISITE", "OPT-A")
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            sync.sync_approved("DEM-1")
            requirement = self._active_requirements(session)[0]
            intent, shift, protection = self._protect_requirement(session, requirement)
            previous_revision_id = session.get(RequestApprovalReference, "D1").active_revision_id

            periods.select_alternative("DEM-1", "VISITE", "OPT-B")
            with self.assertRaises(ApplicationConflictError) as raised:
                sync.sync_approved("DEM-1")
            self.assertEqual(raised.exception.code, "reduction_rebase_required")

            self.assertEqual(requirement.status, "À assigner")
            self.assertEqual(intent.current_resource_requirement_id, requirement.id)
            self.assertEqual(shift.resource_requirement_id, requirement.id)
            self.assertTrue(protection.active)
            self.assertEqual(protection.shift_id, shift.id)
            self.assertEqual(
                session.get(RequestApprovalReference, "D1").active_revision_id,
                previous_revision_id,
            )

    def test_modern_same_slot_preserves_absorbed_reduction_survivor_and_removal_refuses(self) -> None:
        with transactional_session(self.factory) as session:
            request = session.get(WorkforceRequest, "D1")
            assert request is not None
            request.line_mode = True
            line = RequestLine(
                id="L-877-MODERN",
                workforce_request_id="D1",
                position=0,
                kind="WORKFORCE",
                slot_count=1,
                desired_start=D1,
                desired_end=D1,
                estimated_hours=Decimal("8.00"),
                confirmation="Confirmée",
                active=True,
            )
            session.add(line)
            session.flush()
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            sync.sync_approved("DEM-1")
            requirement = self._active_requirements(session)[0]
            intent, shift, protection = self._protect_requirement(session, requirement)
            first_requirement_id = requirement.id
            first_shift_id = shift.id
            first_protection_id = protection.id

            sync.sync_approved("DEM-1")
            current = self._active_requirements(session)
            self.assertEqual([row.id for row in current], [first_requirement_id])
            self.assertEqual(session.get(Shift, first_shift_id).resource_requirement_id, first_requirement_id)
            preserved = session.get(RequestShiftSurvivorProtection, first_protection_id)
            assert preserved is not None
            self.assertTrue(preserved.active)
            self.assertEqual(preserved.shift_id, first_shift_id)

            previous_revision_id = session.get(RequestApprovalReference, "D1").active_revision_id
            line.desired_end = D2
            session.flush()
            with self.assertRaises(ApplicationConflictError) as raised:
                sync.sync_approved("DEM-1")
            self.assertEqual(raised.exception.code, "reduction_rebase_required")
            self.assertEqual(requirement.status, "À assigner")
            self.assertIsNotNone(session.get(Shift, first_shift_id))
            preserved = session.get(RequestShiftSurvivorProtection, first_protection_id)
            assert preserved is not None
            self.assertTrue(preserved.active)
            self.assertEqual(preserved.shift_id, first_shift_id)
            self.assertEqual(
                session.get(RequestApprovalReference, "D1").active_revision_id,
                previous_revision_id,
            )

    def test_modern_obsolete_protected_line_removal_refuses_and_rolls_back(self) -> None:
        line_a_id = "L-877-OBSOLETE-A"
        line_b_id = "L-877-OBSOLETE-B"

        with self.factory.begin() as session:
            request = session.get(WorkforceRequest, "D1")
            assert request is not None
            request.line_mode = True
            session.add_all(
                [
                    RequestLine(
                        id=line_a_id,
                        workforce_request_id="D1",
                        position=0,
                        kind="WORKFORCE",
                        slot_count=1,
                        desired_start=D1,
                        desired_end=D1,
                        estimated_hours=Decimal("8.00"),
                        confirmation="Confirmée",
                        active=True,
                    ),
                    RequestLine(
                        id=line_b_id,
                        workforce_request_id="D1",
                        position=1,
                        kind="WORKFORCE",
                        slot_count=1,
                        desired_start=D2,
                        desired_end=D2,
                        estimated_hours=Decimal("8.00"),
                        confirmation="Confirmée",
                        active=True,
                    ),
                ]
            )
            session.flush()

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")
            current = {
                row.source_request_line_id: row
                for row in self._active_requirements(session)
            }
            self.assertEqual(set(current), {line_a_id, line_b_id})
            protected_requirement = current[line_a_id]
            sibling_requirement = current[line_b_id]
            protected_intent, protected_shift, protection = self._protect_requirement(
                session,
                protected_requirement,
                reduction_state="ABSORBED",
            )
            sibling_intent = session.scalar(
                select(RequestRequirementPlanningIntent).where(
                    RequestRequirementPlanningIntent.current_resource_requirement_id
                    == sibling_requirement.id
                )
            )
            assert sibling_intent is not None
            approval_reference = session.get(RequestApprovalReference, "D1")
            assert approval_reference is not None
            previous_revision_id = approval_reference.active_revision_id
            protected_requirement_id = protected_requirement.id
            protected_requirement_status = protected_requirement.status
            protected_intent_id = protected_intent.id
            protected_shift_id = protected_shift.id
            protection_id = protection.id
            sibling_requirement_id = sibling_requirement.id
            sibling_requirement_status = sibling_requirement.status
            sibling_intent_id = sibling_intent.id

        with self.assertRaises(ApplicationConflictError) as raised:
            with self.factory.begin() as session:
                protected_line = session.get(RequestLine, line_a_id)
                sibling_line = session.get(RequestLine, line_b_id)
                assert protected_line is not None
                assert sibling_line is not None
                protected_line.active = False
                self.assertTrue(sibling_line.active)
                session.flush()
                SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                    "DEM-1"
                )

        self.assertEqual(raised.exception.code, "reduction_rebase_required")

        with self.factory() as session:
            approval_reference = session.get(RequestApprovalReference, "D1")
            protected_line = session.get(RequestLine, line_a_id)
            sibling_line = session.get(RequestLine, line_b_id)
            protected_requirement = session.get(
                ResourceRequirement,
                protected_requirement_id,
            )
            sibling_requirement = session.get(
                ResourceRequirement,
                sibling_requirement_id,
            )
            protected_intent = session.get(
                RequestRequirementPlanningIntent,
                protected_intent_id,
            )
            sibling_intent = session.get(
                RequestRequirementPlanningIntent,
                sibling_intent_id,
            )
            protected_shift = session.get(Shift, protected_shift_id)
            protection = session.get(
                RequestShiftSurvivorProtection,
                protection_id,
            )

            assert approval_reference is not None
            assert protected_line is not None
            assert sibling_line is not None
            assert protected_requirement is not None
            assert sibling_requirement is not None
            assert protected_intent is not None
            assert sibling_intent is not None
            assert protected_shift is not None
            assert protection is not None

            self.assertEqual(
                approval_reference.active_revision_id,
                previous_revision_id,
            )
            self.assertTrue(protected_line.active)
            self.assertTrue(sibling_line.active)
            self.assertEqual(
                protected_requirement.status,
                protected_requirement_status,
            )
            self.assertNotEqual(protected_requirement.status, "Annulé")
            self.assertEqual(
                protected_shift.resource_requirement_id,
                protected_requirement_id,
            )
            self.assertTrue(protection.active)
            self.assertEqual(protection.shift_id, protected_shift_id)
            self.assertEqual(
                protected_intent.current_resource_requirement_id,
                protected_requirement_id,
            )
            self.assertEqual(
                sibling_requirement.status,
                sibling_requirement_status,
            )
            self.assertEqual(
                sibling_intent.current_resource_requirement_id,
                sibling_requirement_id,
            )
            active_ids = {
                row.id for row in self._active_requirements(session)
            }
            self.assertEqual(
                active_ids,
                {protected_requirement_id, sibling_requirement_id},
            )

    def test_cumulative_period_and_selected_alternative_are_both_effective(self) -> None:
        with transactional_session(self.factory) as session:
            definitions = (
                DemandPeriodDefinition(
                    period_id="PREP",
                    start_date=D1,
                    end_date=D1,
                    hours=4,
                    kind=PERIOD_KIND_CUMULATIVE,
                    proposed_resource="Alice",
                ),
                *self._alternatives(),
            )
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", definitions)
            periods.select_alternative("DEM-1", "VISITE", "OPT-B")

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            active = sorted(self._active_requirements(session), key=lambda row: row.start_date)
            self.assertEqual(len(active), 2)
            self.assertEqual([(row.start_date, float(row.planned_hours)) for row in active], [(D1, 4.0), (D2, 8.0)])
            self.assertEqual(sum(float(row.planned_hours) for row in active), 12.0)

    def test_same_as_period_materialization_can_be_resolved_collectively(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand(
                "DEM-1",
                (
                    DemandPeriodDefinition(
                        period_id="ROOT",
                        start_date=D1,
                        end_date=D1,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                        proposed_resource="Alice",
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_EXPLICIT,
                    ),
                    DemandPeriodDefinition(
                        period_id="FOLLOW",
                        start_date=D2,
                        end_date=D2,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
                        same_as_period_id="ROOT",
                    ),
                ),
            )

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")
            active = sorted(self._active_requirements(session), key=lambda row: row.start_date)
            self.assertEqual(len(active), 2)
            self.assertTrue(all(row.assigned_resource_id is None for row in active))

            summary = SqlSameResourcePeriodCoordinator(session).enforce_targets()

            self.assertEqual(summary.group_count, 1)
            self.assertEqual(summary.unresolved_group_count, 1)
            self.assertTrue(all(row.assigned_resource_id is None for row in active))

    def _set_legacy_resource_class(self, session, class_code: str) -> None:
        """Mirror a simple request created via the React class selector (#535)."""
        session.add(
            RequestLine(
                id="D1",
                workforce_request_id="D1",
                position=0,
                kind="WORKFORCE",
                required_resource_class=class_code,
                desired_start=D1,
                desired_end=D3,
                estimated_hours=Decimal("8"),
                confirmation="Confirmée",
                active=True,
            )
        )
        session.flush()

    def test_legacy_simple_class_reaches_unplanned_planning_action(self) -> None:
        with transactional_session(self.factory) as session:
            self._set_legacy_resource_class(session, "INSTALL_ELEC")
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            sync.sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            requirement = active[0]
            self.assertIsNone(requirement.assigned_resource_id)
            self.assertEqual(requirement.required_resource_class, "INSTALL_ELEC")

            actions = SqlPlannerQueryRepositoryWithLoadProfiles(session).list_planning_actions(
                start=D1, end=D3,
            )
            unplanned = [action for action in actions if action.kind == "ASSIGNMENT"]
            self.assertEqual(len(unplanned), 1)
            self.assertEqual(unplanned[0].required_resource_class, "INSTALL_ELEC")
            self.assertEqual(unplanned[0].planned_hours, 8.0)

            # The request can change after approval: repair must use the immutable
            # active authorization, never the current editable RequestLine class.
            requirement.required_resource_class = None
            session.get(RequestLine, "D1").required_resource_class = "PROGRAMMEUR"
            session.flush()
            preview = repair_request_class(session, "DEM-1")
            self.assertEqual(preview["repaired_count"], 1)
            self.assertEqual(preview["changes"][0]["class_code"], "INSTALL_ELEC")
            self.assertIsNone(requirement.required_resource_class)
            applied = repair_request_class(session, "DEM-1", apply=True)
            self.assertEqual(applied["repaired_count"], 1)
            self.assertEqual(requirement.required_resource_class, "INSTALL_ELEC")

            # A subsequent operational rebuild also derives class from approval.
            requirement.required_resource_class = None
            session.flush()
            sync.sync_operational_choices("DEM-1")
            repaired = self._active_requirements(session)
            self.assertEqual(len(repaired), 1)
            self.assertEqual(repaired[0].id, requirement.id)
            self.assertEqual(repaired[0].required_resource_class, "INSTALL_ELEC")
            self.assertIsNone(repaired[0].assigned_resource_id)

    def test_legacy_periods_keep_class_on_each_materialized_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            self._set_legacy_resource_class(session, "INSTALL_ELEC")
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand(
                "DEM-1",
                (
                    DemandPeriodDefinition(
                        period_id="PREP",
                        start_date=D1,
                        end_date=D1,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                    ),
                    DemandPeriodDefinition(
                        period_id="FIELD",
                        start_date=D2,
                        end_date=D2,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                    ),
                ),
            )
            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")
            requirements = self._active_requirements(session)
            self.assertEqual(len(requirements), 2)
            self.assertEqual(
                {row.required_resource_class for row in requirements},
                {"INSTALL_ELEC"},
            )
            self.assertTrue(all(row.assigned_resource_id is None for row in requirements))
            self.assertEqual(sum(float(row.planned_hours) for row in requirements), 8.0)

    def test_bulk_repair_candidate_query_uses_sql_server_compatible_boolean(self) -> None:
        sql = str(
            _unclassified_request_ids_statement().compile(
                dialect=mssql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("workforce_requests.line_mode = 0", sql)
        self.assertNotIn("line_mode IS 0", sql)

    def test_bulk_repair_only_updates_active_approval_proven_missing_classes(self) -> None:
        with transactional_session(self.factory) as session:
            self._set_legacy_resource_class(session, "INSTALL_ELEC")
            second = WorkforceRequest(
                id="D2",
                legacy_demand_number="DEM-2",
                project_id="P1",
                desired_start=D1,
                desired_end=D3,
                estimated_hours=Decimal("8"),
                resource_count=1,
                status="En planification",
                approved_by_name="coord-test-user",
            )
            third = WorkforceRequest(
                id="D3",
                legacy_demand_number="DEM-3",
                project_id="P1",
                desired_start=D1,
                desired_end=D3,
                estimated_hours=Decimal("8"),
                resource_count=1,
                status="En planification",
            )
            session.add_all((second, third))
            session.flush()
            session.add(
                RequestLine(
                    id="D2",
                    workforce_request_id="D2",
                    position=0,
                    kind="WORKFORCE",
                    required_resource_class="PROGRAMMEUR",
                    desired_start=D1,
                    desired_end=D3,
                    estimated_hours=Decimal("8"),
                    active=True,
                )
            )
            # Historical requirement without trustworthy active approval is
            # always reported for manual review rather than guessed.
            session.add(
                ResourceRequirement(
                    id="REQ-NO-AUTH",
                    project_id="P1",
                    workforce_request_id="D3",
                    start_date=D1,
                    end_date=D3,
                    planned_hours=Decimal("8"),
                    status="À assigner",
                    origin="REQUEST",
                )
            )
            session.flush()
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            sync.sync_approved("DEM-1")
            sync.sync_approved("DEM-2")
            requirements = {
                request_id: self._active_requirements(session)[0]
                if request_id == "D1"
                else session.scalar(
                    select(ResourceRequirement).where(
                        ResourceRequirement.workforce_request_id == request_id,
                        ResourceRequirement.status != "Annulé",
                    )
                )
                for request_id in ("D1", "D2")
            }
            ids_before = {key: req.id for key, req in requirements.items()}
            revisions_before = {
                key: req.approval_revision_id for key, req in requirements.items()
            }
            for req in requirements.values():
                req.required_resource_class = None
            session.flush()

            preview = repair_all_request_classes(session)
            self.assertEqual(preview["requests_scanned"], 3)
            self.assertEqual(preview["requests_corrected"], 2)
            self.assertEqual(preview["requirements_corrected"], 2)
            self.assertEqual(preview["requests_needing_review"], 1)
            self.assertEqual(
                {row["demand_number"] for row in preview["changed_requests"]},
                {"DEM-1", "DEM-2"},
            )
            self.assertTrue(all(req.required_resource_class is None for req in requirements.values()))

            applied = repair_all_request_classes(session, apply=True)
            self.assertEqual(applied["requests_corrected"], 2)
            self.assertEqual(applied["requirements_corrected"], 2)
            self.assertEqual(requirements["D1"].required_resource_class, "INSTALL_ELEC")
            self.assertEqual(requirements["D2"].required_resource_class, "PROGRAMMEUR")
            self.assertEqual(
                {key: req.id for key, req in requirements.items()},
                ids_before,
            )
            self.assertEqual(
                {key: req.approval_revision_id for key, req in requirements.items()},
                revisions_before,
            )
            self.assertIsNone(session.get(ResourceRequirement, "REQ-NO-AUTH").required_resource_class)

            again = repair_all_request_classes(session)
            self.assertEqual(again["requests_corrected"], 0)
            self.assertEqual(again["requests_needing_review"], 1)

    def test_request_without_detailed_periods_preserves_legacy_sync(self) -> None:
        with transactional_session(self.factory) as session:
            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].start_date, D1)
            self.assertEqual(active[0].end_date, D3)
            self.assertEqual(active[0].planned_hours, Decimal("8.00"))
            self.assertIsNone(
                session.get(WorkforceRequestPeriodRequirement, active[0].id)
            )


if __name__ == "__main__":
    unittest.main()
