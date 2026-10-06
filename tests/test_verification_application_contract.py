from __future__ import annotations

import unittest

from app.application.verification_contracts import (
    VerificationAction,
    VerificationActorContext,
    VerificationAuthorityContext,
    VerificationStorySourceReadModel,
    validate_story_source_for_scope,
    verification_actions_for,
)
from app.domain.delivery import DeliveryItemType
from app.domain.verification import VerificationScope


class VerificationApplicationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = VerificationScope(
            id="VS-1",
            work_package_id="WP-1",
            lead_user_id="USER-LEAD",
        )

    def test_story_source_must_be_story_from_same_work_package(self) -> None:
        source = VerificationStorySourceReadModel(
            story_id="STORY-1",
            delivery_plan_id="DP-1",
            work_package_id="WP-1",
            item_type=DeliveryItemType.STORY,
            delivery_status="IN_PROGRESS",
        )
        validate_story_source_for_scope(self.scope, source)

        with self.assertRaisesRegex(ValueError, "Delivery STORY"):
            validate_story_source_for_scope(
                self.scope,
                VerificationStorySourceReadModel(
                    story_id="EPIC-1",
                    delivery_plan_id="DP-1",
                    work_package_id="WP-1",
                    item_type=DeliveryItemType.EPIC,
                    delivery_status="IN_PROGRESS",
                ),
            )

        with self.assertRaisesRegex(ValueError, "same WorkPackage"):
            validate_story_source_for_scope(
                self.scope,
                VerificationStorySourceReadModel(
                    story_id="STORY-2",
                    delivery_plan_id="DP-2",
                    work_package_id="WP-2",
                    item_type=DeliveryItemType.STORY,
                    delivery_status="IN_PROGRESS",
                ),
            )

    def test_authorized_story_closer_can_record_decision_and_initial_requirements_only(
        self,
    ) -> None:
        actions = verification_actions_for(
            VerificationActorContext("USER-TECH", ("TECHNICIAN",)),
            self.scope,
            VerificationAuthorityContext(story_closure_authorized=True),
        )
        self.assertIn(
            VerificationAction.RECORD_STORY_DECISION,
            actions,
        )
        self.assertIn(
            VerificationAction.DEFINE_STORY_REQUIREMENTS,
            actions,
        )
        self.assertNotIn(
            VerificationAction.REVISE_REQUIREMENTS,
            actions,
        )
        self.assertNotIn(
            VerificationAction.RECORD_RESULT,
            actions,
        )

    def test_verification_lead_can_manage_requirements_without_implicit_execution_right(
        self,
    ) -> None:
        actions = verification_actions_for(
            VerificationActorContext("USER-LEAD", ("TECHNICIAN",)),
            self.scope,
        )
        expected = {
            VerificationAction.VIEW_SCOPE,
            VerificationAction.DEFINE_STORY_REQUIREMENTS,
            VerificationAction.REVISE_REQUIREMENTS,
            VerificationAction.ASSIGN_EXECUTORS,
            VerificationAction.WITHDRAW_REQUIREMENTS,
            VerificationAction.REQUEST_RETEST,
        }
        self.assertEqual(actions, frozenset(expected))
        self.assertNotIn(
            VerificationAction.RECORD_RESULT,
            actions,
        )

    def test_assignment_alone_never_grants_execution_permission(self) -> None:
        actions = verification_actions_for(
            VerificationActorContext("USER-EXEC", ("TECHNICIAN",)),
            self.scope,
            VerificationAuthorityContext(assigned_executor=True),
        )
        self.assertEqual(actions, frozenset())

    def test_explicitly_assigned_and_authorized_executor_can_record_result_and_evidence(
        self,
    ) -> None:
        actions = verification_actions_for(
            VerificationActorContext("USER-EXEC", ("TECHNICIAN",)),
            self.scope,
            VerificationAuthorityContext(
                assigned_executor=True,
                execution_authorized=True,
            ),
        )
        self.assertEqual(
            actions,
            frozenset(
                {
                    VerificationAction.VIEW_SCOPE,
                    VerificationAction.RECORD_RESULT,
                    VerificationAction.ADD_EVIDENCE,
                }
            ),
        )

    def test_project_role_requires_resolved_project_scope_authority(self) -> None:
        actor = VerificationActorContext(
            "PM-1",
            ("PROJECT_MANAGER",),
        )
        self.assertEqual(
            verification_actions_for(actor, self.scope),
            frozenset(),
        )
        scoped = verification_actions_for(
            actor,
            self.scope,
            VerificationAuthorityContext(
                project_scope_authorized=True
            ),
        )
        self.assertEqual(
            scoped,
            frozenset(
                {
                    VerificationAction.VIEW_SCOPE,
                    VerificationAction.PILOT_SCOPE,
                }
            ),
        )

    def test_admin_actions_are_explicitly_verification_scoped(self) -> None:
        actions = verification_actions_for(
            VerificationActorContext("ADMIN-1", ("ADMIN",)),
            self.scope,
        )
        self.assertEqual(
            actions,
            frozenset(VerificationAction),
        )
        self.assertFalse(
            any(
                "PLANNING" in action.value
                or "DELIVERY" in action.value
                for action in actions
            )
        )


if __name__ == "__main__":
    unittest.main()
