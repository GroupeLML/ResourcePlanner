from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.application.approval_cycles import (
    ApprovalApproverSnapshot,
    ApprovalCycleRecord,
    ApprovalCycleRequestRecord,
    ApprovalRequirementRecord,
)
from app.application.approval_progress import ApprovalProgressService, ApprovalUserSummaryRecord
from app.application.approval_voting import ApprovalDecisionRecord
from app.application.security import PERMISSION_APPROVE_DEMANDS
from app.domain.approval_cycles import (
    APPROVAL_CYCLE_INIT_SUBMISSION,
    APPROVAL_CYCLE_STATE_COMPLETED,
    APPROVAL_CYCLE_STATE_OPEN,
)

NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)


class FakeCycles:
    def __init__(self, cycle: ApprovalCycleRecord) -> None:
        self.cycle = cycle
        self.request = ApprovalCycleRequestRecord(
            id="REQ",
            number="DMO-276",
            status="Soumise" if cycle.state == APPROVAL_CYCLE_STATE_OPEN else "En planification",
            aggregate_version=4,
            project_id="P1",
            priority="Normale",
            site_client=None,
            location=None,
            line_mode=True,
            approved_at=None,
        )

    def get_request(self, _request_id: str):
        return self.request

    def get_active_cycle(self, _request_id: str):
        return self.cycle if self.cycle.state == APPROVAL_CYCLE_STATE_OPEN else None

    def get_latest_cycle(self, _request_id: str):
        return self.cycle


class FakeRepository:
    def __init__(self) -> None:
        self.decisions = (
            ApprovalDecisionRecord(
                requirement_id="AR1",
                app_user_id="U2",
                decision="APPROVE",
                action_id="ACTION-1",
                decided_at=NOW,
                comment="Automatisation approuvée",
            ),
        )
        self.users = (
            ApprovalUserSummaryRecord("U1", "Gestionnaire électricité", True),
            ApprovalUserSummaryRecord("U2", "Coordonnateur automatisation", True),
            ApprovalUserSummaryRecord("U3", "Administrateur", True),
        )

    def list_decisions(self, _cycle_id: str):
        return self.decisions

    def list_approval_users(self, user_ids):
        wanted = set(user_ids)
        return tuple(row for row in self.users if row.app_user_id in wanted)

    def list_actor_approvable_demand_numbers(self, demand_numbers, *, current_user_id):
        if current_user_id != "U1":
            return ()
        return tuple(number for number in demand_numbers if number == "DMO-276")


def make_cycle(state: str = APPROVAL_CYCLE_STATE_OPEN) -> ApprovalCycleRecord:
    return ApprovalCycleRecord(
        id="CYCLE-1",
        workforce_request_id="REQ",
        submitted_request_version=3,
        state=state,
        subject_fingerprint="abc",
        initialization_reason=APPROVAL_CYCLE_INIT_SUBMISSION,
        submitted_at=NOW,
        invalidated_at=None,
        invalidation_reason=None,
        completed_at=NOW if state == APPROVAL_CYCLE_STATE_COMPLETED else None,
        approved_revision_id="REV-1" if state == APPROVAL_CYCLE_STATE_COMPLETED else None,
        requirements=(
            ApprovalRequirementRecord(
                id="AR1",
                request_line_id="L-AUTO",
                task_catalog_item_id="TASK-210",
                approval_scope_id="SCOPE-AUTO",
                proposed_resource_id=None,
                routing_sources=("SCOPE",),
                approvers=(ApprovalApproverSnapshot("U2", ("SCOPE:SCOPE-AUTO",)),),
            ),
            ApprovalRequirementRecord(
                id="AR2",
                request_line_id="L-ELEC",
                task_catalog_item_id="TASK-110",
                approval_scope_id="SCOPE-ELEC",
                proposed_resource_id=None,
                routing_sources=("SCOPE",),
                approvers=(ApprovalApproverSnapshot("U1", ("SCOPE:SCOPE-ELEC",)),),
            ),
        ),
    )


class ApprovalProgressTests(unittest.TestCase):
    def test_batch_actor_projection_requires_permission_and_preserves_repository_result(self) -> None:
        service = ApprovalProgressService(FakeCycles(make_cycle()), FakeRepository())

        self.assertEqual(
            service.actor_approvable_demand_numbers(
                ("DMO-276", "DMO-OTHER"),
                current_user_id="U1",
                permissions=(PERMISSION_APPROVE_DEMANDS,),
            ),
            ("DMO-276",),
        )
        self.assertEqual(
            service.actor_approvable_demand_numbers(
                ("DMO-276",),
                current_user_id="U1",
                permissions=(),
            ),
            (),
        )

    def test_projection_exposes_quorum_decisions_and_my_remaining_lines(self) -> None:
        row = ApprovalProgressService(
            FakeCycles(make_cycle()),
            FakeRepository(),
        ).get(
            "DMO-276",
            current_user_id="U1",
            permissions=(PERMISSION_APPROVE_DEMANDS,),
        )

        assert row is not None
        self.assertEqual((row.satisfied_requirements, row.total_requirements), (1, 2))
        self.assertFalse(row.quorum_complete)
        self.assertEqual(row.actor_approvable_requirement_ids, ("AR2",))
        self.assertEqual(row.actor_approvable_request_line_ids, ("L-ELEC",))
        self.assertTrue(row.requirements[0].satisfied)
        self.assertEqual(row.requirements[0].approvers[0].display_name, "Coordonnateur automatisation")
        self.assertEqual(row.requirements[0].decisions[0].comment, "Automatisation approuvée")
        self.assertTrue(row.requirements[1].actor_can_approve)

    def test_approve_permission_does_not_create_snapshot_eligibility(self) -> None:
        row = ApprovalProgressService(
            FakeCycles(make_cycle()),
            FakeRepository(),
        ).get(
            "DMO-276",
            current_user_id="U3",
            permissions=(PERMISSION_APPROVE_DEMANDS,),
        )

        assert row is not None
        self.assertEqual(row.actor_approvable_requirement_ids, ())

    def test_completed_cycle_remains_visible_without_actor_action(self) -> None:
        repo = FakeRepository()
        repo.decisions += (
            ApprovalDecisionRecord(
                requirement_id="AR2",
                app_user_id="U1",
                decision="APPROVE",
                action_id="ACTION-2",
                decided_at=NOW,
                comment="Électricité approuvée",
            ),
        )
        row = ApprovalProgressService(
            FakeCycles(make_cycle(APPROVAL_CYCLE_STATE_COMPLETED)),
            repo,
        ).get(
            "DMO-276",
            current_user_id="U1",
            permissions=(PERMISSION_APPROVE_DEMANDS,),
        )

        assert row is not None
        self.assertTrue(row.quorum_complete)
        self.assertEqual(row.satisfied_requirements, 2)
        self.assertEqual(row.actor_approvable_requirement_ids, ())
        self.assertEqual(row.approval_revision_id, "REV-1")


if __name__ == "__main__":
    unittest.main()
