from __future__ import annotations

import unittest

from app.application.delivery_contracts import DeliveryAction
from app.application.delivery_security import authorized_delivery_actions_for
from app.application.security import (
    AuthPrincipal,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_MANAGE_DELIVERY,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_READ,
    ROLE_COORDINATOR,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    permissions_for_roles,
)
from app.domain.delivery import DeliveryItem, DeliveryItemType, DeliveryPlan
from app.server.security import required_permission


def principal(user_id: str | None, *roles: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="test",
        subject=user_id or "anonymous",
        display_name=user_id or "Anonymous",
        email=None,
        roles=roles,
        auth_mode="test",
    )


class DeliverySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = DeliveryPlan(
            id="DP-1", work_package_id="WP-1", lead_user_id="USER-LEAD"
        )
        self.story = DeliveryItem(
            id="STORY-1",
            delivery_plan_id="DP-1",
            item_type=DeliveryItemType.STORY,
            title="Story",
            assignee_user_id="USER-TECH",
        )

    def test_delivery_permissions_are_independent_from_planning(self) -> None:
        project_manager = set(permissions_for_roles((ROLE_PROJECT_MANAGER,)))
        technician = set(permissions_for_roles((ROLE_TECHNICIAN,)))
        contributor = set(permissions_for_roles((ROLE_DELIVERY_CONTRIBUTOR,)))
        coordinator = set(permissions_for_roles((ROLE_COORDINATOR,)))
        manager = set(permissions_for_roles((ROLE_MANAGER,)))

        self.assertIn(PERMISSION_MANAGE_DELIVERY, project_manager)
        self.assertIn(PERMISSION_CONTRIBUTE_DELIVERY, project_manager)
        self.assertNotIn(PERMISSION_MANAGE_PLANNING, project_manager)

        self.assertEqual(technician, {PERMISSION_READ})
        self.assertIn(PERMISSION_CONTRIBUTE_DELIVERY, contributor)
        for permissions in (technician, coordinator, manager):
            self.assertNotIn(PERMISSION_MANAGE_DELIVERY, permissions)
        self.assertNotIn(PERMISSION_CONTRIBUTE_DELIVERY, coordinator)
        self.assertNotIn(PERMISSION_CONTRIBUTE_DELIVERY, manager)
        self.assertNotIn(PERMISSION_MANAGE_PLANNING, technician)

    def test_project_manager_gets_plan_actions_without_board_lead_actions(self) -> None:
        actions = authorized_delivery_actions_for(
            principal("USER-PM", ROLE_PROJECT_MANAGER), self.plan
        )
        self.assertIn(DeliveryAction.MANAGE_PLAN_LIFECYCLE, actions)
        self.assertIn(DeliveryAction.ASSIGN_TEAM_LEAD, actions)
        self.assertNotIn(DeliveryAction.MANAGE_STRUCTURE, actions)

    def test_team_lead_gets_contextual_board_actions_without_planning_permission(self) -> None:
        actor = principal(
            "USER-LEAD", ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR
        )
        actions = authorized_delivery_actions_for(actor, self.plan)
        self.assertIn(DeliveryAction.MANAGE_STRUCTURE, actions)
        self.assertIn(DeliveryAction.ESTIMATE_STORIES, actions)
        self.assertFalse(actor.has_permission(PERMISSION_MANAGE_PLANNING))

    def test_technician_actions_are_limited_to_owned_story(self) -> None:
        actor = principal(
            "USER-TECH", ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR
        )
        own = authorized_delivery_actions_for(actor, self.plan, self.story)
        self.assertEqual(
            own,
            frozenset(
                {
                    DeliveryAction.UPDATE_OWN_STORY_STATUS,
                    DeliveryAction.UPDATE_OWN_REMAINING_HOURS,
                    DeliveryAction.DOCUMENT_OWN_BLOCKAGE,
                }
            ),
        )
        other = DeliveryItem(
            id="STORY-2",
            delivery_plan_id="DP-1",
            item_type=DeliveryItemType.STORY,
            title="Other",
            assignee_user_id="OTHER",
        )
        self.assertEqual(
            authorized_delivery_actions_for(actor, self.plan, other), frozenset()
        )

    def test_unresolved_local_identity_has_no_contextual_delivery_actions(self) -> None:
        self.assertEqual(
            authorized_delivery_actions_for(
                principal(None, ROLE_PROJECT_MANAGER), self.plan
            ),
            frozenset(),
        )

    def test_future_delivery_mutations_are_fail_closed_to_contribute_permission(self) -> None:
        self.assertEqual(
            required_permission("POST", "/api/v1/delivery/plans"),
            PERMISSION_CONTRIBUTE_DELIVERY,
        )
        self.assertEqual(
            required_permission("PATCH", "/api/v1/delivery/items/STORY-1"),
            PERMISSION_CONTRIBUTE_DELIVERY,
        )
        self.assertEqual(
            required_permission("GET", "/api/v1/delivery/plans/DP-1"),
            PERMISSION_READ,
        )


if __name__ == "__main__":
    unittest.main()
