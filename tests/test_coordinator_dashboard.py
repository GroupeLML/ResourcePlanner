from __future__ import annotations

from datetime import date, datetime
import unittest

from app.application.approval_progress import ApprovalCycleProgressReadModel
from app.application.operational_contacts import MaterializedContactResolution
from app.application.coordinator_dashboard import (
    ACTION_APPROVAL,
    ACTION_CANCELLATION,
    ACTION_CONFLICT,
    ACTION_PARTIAL_COVERAGE,
    ACTION_WORKFORCE_ASSIGNMENT,
    CoordinatorDashboardService,
)
from app.application.query_models import (
    DemandCancellationMaterializationReadModel,
    PlanningActionReadModel,
)
from app.application.read_models import DemandReadModel, SegmentReadModel
from app.application.security import AuthPrincipal, ROLE_COORDINATOR
from app.application.user_view_context import DemandScopeResolution
from app.domain.operational_contacts import (
    ContactResolution,
    SOURCE_TASK_RESPONSIBLE,
    STATUS_RESOLVED,
)


DAY = date(2026, 9, 25)


def demand(
    number: str,
    *,
    status: str,
    start: date,
    priority: str = "Normale",
    terminal: bool = False,
    cancellation_request_id: str | None = None,
    cancellation_state: str | None = None,
) -> DemandReadModel:
    return DemandReadModel(
        number=number,
        status=status,
        effective_status="Complétée" if terminal else status,
        terminal=terminal,
        project_number="P-278",
        project_name="Projet dashboard",
        priority=priority,
        desired_start=start,
        desired_end=start,
        cancellation_request_id=cancellation_request_id,
        cancellation_state=cancellation_state,
    )


class FakeQueries:
    def __init__(self) -> None:
        self._pairs_by_id = {
            "RID-A-CANCEL": (
                demand(
                    "DMO-A-CANCEL",
                    status="En planification",
                    start=date(2026, 9, 26),
                    cancellation_request_id="CANCEL-A",
                    cancellation_state="PENDING",
                ),
                DemandCancellationMaterializationReadModel(
                    demand_number="DMO-A-CANCEL",
                    human_shift_count=2,
                ),
            ),
            "RID-A-WORK": (
                demand(
                    "DMO-A-WORK",
                    status="En planification",
                    start=date(2026, 9, 24),
                ),
                DemandCancellationMaterializationReadModel(
                    demand_number="DMO-A-WORK",
                ),
            ),
            "RID-A-DONE": (
                demand(
                    "DMO-A-DONE",
                    status="En planification",
                    start=date(2026, 9, 20),
                    terminal=True,
                ),
                DemandCancellationMaterializationReadModel(
                    demand_number="DMO-A-DONE",
                ),
            ),
            "RID-B-WORK": (
                demand(
                    "DMO-B-WORK",
                    status="En planification",
                    start=date(2026, 9, 25),
                ),
                DemandCancellationMaterializationReadModel(
                    demand_number="DMO-B-WORK",
                ),
            ),
            "RID-APPROVAL": (
                demand(
                    "DMO-APPROVAL",
                    status="Soumise",
                    start=date(2026, 9, 27),
                    priority="Urgente",
                ),
                DemandCancellationMaterializationReadModel(
                    demand_number="DMO-APPROVAL",
                ),
            ),
        }
        self._segments = (
            SegmentReadModel(
                segment_id="SEG-A",
                demand_number="DMO-A-WORK",
                project_number="P-278",
                project_name="Projet dashboard",
                resource_name=None,
                requirement_id="REQ-A",
                start_date=date(2026, 9, 24),
                end_date=date(2026, 9, 26),
                planned_hours=16,
                status="Planifié",
                priority="Haute",
                covered_hours=8,
                remaining_hours=8,
                overallocated=True,
                overallocated_hours=2,
            ),
            SegmentReadModel(
                segment_id="SEG-B",
                demand_number="DMO-B-WORK",
                project_number="P-278",
                project_name="Projet dashboard",
                resource_name=None,
                requirement_id="REQ-B",
                start_date=date(2026, 9, 25),
                end_date=date(2026, 9, 25),
                planned_hours=8,
                status="Planifié",
                remaining_hours=8,
            ),
        )
        self._planning_actions = (
            PlanningActionReadModel(
                kind="ASSIGNMENT",
                reference="SEG-A",
                demand_number="DMO-A-WORK",
                segment_id="SEG-A",
                project_number="P-278",
                project_name="Projet dashboard",
                task_code="210",
                task_label="Automatisation",
                start_date=date(2026, 9, 24),
                end_date=date(2026, 9, 26),
                planned_hours=8,
                priority="Haute",
                status="Planifié",
            ),
            PlanningActionReadModel(
                kind="ASSIGNMENT",
                reference="SEG-B",
                demand_number="DMO-B-WORK",
                segment_id="SEG-B",
                project_number="P-278",
                project_name="Projet dashboard",
                task_code="210",
                task_label="Automatisation",
                start_date=date(2026, 9, 25),
                end_date=date(2026, 9, 25),
                planned_hours=8,
                status="Planifié",
            ),
        )

    def list_demands_with_cancellation_materialization(
        self,
        *,
        project_ids=None,
        demand_ids=None,
    ):
        if demand_ids is None:
            return tuple(self._pairs_by_id.values())
        return tuple(
            self._pairs_by_id[identifier]
            for identifier in demand_ids
            if identifier in self._pairs_by_id
        )

    def list_segments(self, **_kwargs):
        return self._segments

    def list_planning_actions(self, **_kwargs):
        return self._planning_actions

    def list_demand_asset_requirements(self, _number):
        return ()


class FakeOperationalContacts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def resolve_resource_requirements(self, requirement_ids):
        requested = tuple(requirement_ids)
        self.calls.append(requested)
        result = []
        for requirement_id in requested:
            suffix = requirement_id.removeprefix("REQ-")
            responsible = ContactResolution(
                status=STATUS_RESOLVED,
                contact_id=f"C-RESP-{suffix}",
                display_name=f"Responsable {suffix}",
                email=None,
                phone=None,
                source_type=SOURCE_TASK_RESPONSIBLE,
                source_entity_id=f"TASK-{suffix}",
                source_label=f"Tâche {suffix}",
            )
            result.append(
                MaterializedContactResolution(
                    subject_type="RESOURCE_REQUIREMENT",
                    subject_id=requirement_id,
                    requirement_id=requirement_id,
                    shift_id=None,
                    request_line_id=None,
                    demand_number=f"DMO-{suffix}-WORK",
                    project_number="P-278",
                    approved_request_version=1,
                    approved_contact_context_status="CURRENT",
                    task_id=f"TASK-{suffix}",
                    task_code="210",
                    task_label="Automatisation",
                    resource_id=None,
                    resource_name=None,
                    operational_responsible=responsible,
                    coordinator=responsible,
                )
            )
        return tuple(result)


class FakeScopeResolver:
    def resolve_demand_scope(self, principal, requested_scope=None):
        demand_ids = {
            "U-A": ("RID-A-CANCEL", "RID-A-WORK", "RID-A-DONE"),
            "U-B": ("RID-B-WORK",),
        }.get(principal.local_user_id, ())
        return DemandScopeResolution(
            scope=requested_scope or "mine",
            project_ids=(),
            demand_ids=demand_ids,
        )


class FakeApprovalProgress:
    def get(self, demand_number, *, current_user_id, permissions):
        if demand_number != "DMO-APPROVAL" or current_user_id != "U-A":
            return None
        return ApprovalCycleProgressReadModel(
            approval_cycle_id="CYCLE-A",
            state="OPEN",
            submitted_request_version=2,
            submitted_at=datetime(2026, 9, 24, 12, 0),
            invalidated_at=None,
            invalidation_reason=None,
            completed_at=None,
            approval_revision_id=None,
            total_requirements=2,
            satisfied_requirements=1,
            quorum_complete=False,
            actor_approvable_requirement_ids=("AR-2",),
            actor_approvable_request_line_ids=("L-2",),
            requirements=(),
        )


def principal(user_id: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:resourceplanner:local",
        subject=user_id.casefold(),
        display_name=f"Coordonnateur {user_id}",
        email=None,
        roles=(ROLE_COORDINATOR,),
        auth_mode="local",
    )


class CoordinatorDashboardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operational_contacts = FakeOperationalContacts()
        self.service = CoordinatorDashboardService(
            FakeQueries(),
            FakeScopeResolver(),
            FakeApprovalProgress(),
            self.operational_contacts,
        )

    def test_dashboard_composes_existing_policies_for_current_coordinator(self) -> None:
        result = self.service.read(principal("U-A"), today=DAY)

        self.assertEqual(
            {row.demand_number for row in result.personal_demands},
            {"DMO-A-CANCEL", "DMO-A-WORK"},
        )
        self.assertNotIn(
            "DMO-A-DONE",
            {row.demand_number for row in result.personal_demands},
        )

        kinds = {row.kind for row in result.actions}
        self.assertEqual(
            kinds,
            {
                ACTION_WORKFORCE_ASSIGNMENT,
                ACTION_CANCELLATION,
                ACTION_APPROVAL,
                ACTION_PARTIAL_COVERAGE,
                ACTION_CONFLICT,
            },
        )
        self.assertNotIn(
            "DMO-B-WORK",
            {row.demand_number for row in result.actions},
        )
        approval = next(
            row for row in result.actions if row.kind == ACTION_APPROVAL
        )
        self.assertEqual(approval.source_id, "CYCLE-A")
        self.assertEqual(approval.related_ids, ("AR-2",))

        self.assertEqual(result.kpis.personal_demands, 2)
        self.assertEqual(result.kpis.assignments, 1)
        self.assertEqual(result.kpis.cancellations, 1)
        self.assertEqual(result.kpis.approvals, 1)
        self.assertEqual(result.kpis.partial_coverages, 1)
        self.assertEqual(result.kpis.conflicts, 1)

    def test_responsibility_batch_is_limited_to_existing_personal_scope(self) -> None:
        result = self.service.read(principal("U-A"), today=DAY)

        self.assertEqual(self.operational_contacts.calls, [("REQ-A",)])
        assignment = next(
            row for row in result.actions
            if row.kind == ACTION_WORKFORCE_ASSIGNMENT
        )
        self.assertEqual(
            assignment.operational_responsible_display_name,
            "Responsable A",
        )
        self.assertEqual(
            assignment.operational_responsible_source_type,
            SOURCE_TASK_RESPONSIBLE,
        )
        coverage = [
            row for row in result.actions
            if row.kind in {ACTION_PARTIAL_COVERAGE, ACTION_CONFLICT}
        ]
        self.assertTrue(coverage)
        self.assertTrue(
            all(
                row.operational_responsible_contact_id == "C-RESP-A"
                for row in coverage
            )
        )

    def test_two_coordinators_are_isolated_by_stable_demand_scope_ids(self) -> None:
        alpha = self.service.read(principal("U-A"), today=DAY)
        beta = self.service.read(principal("U-B"), today=DAY)

        self.assertEqual(
            {row.demand_number for row in beta.personal_demands},
            {"DMO-B-WORK"},
        )
        self.assertEqual(
            {row.demand_number for row in beta.actions},
            {"DMO-B-WORK"},
        )
        self.assertNotEqual(
            {row.demand_number for row in alpha.personal_demands},
            {row.demand_number for row in beta.personal_demands},
        )

    def test_terminal_demands_never_reenter_actions(self) -> None:
        result = self.service.read(principal("U-A"), today=DAY)

        all_numbers = {
            row.demand_number for row in (*result.personal_demands, *result.actions)
        }
        self.assertNotIn("DMO-A-DONE", all_numbers)


if __name__ == "__main__":
    unittest.main()
