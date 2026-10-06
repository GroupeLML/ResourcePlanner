from __future__ import annotations

import unittest

from app.application.security import (
    AuthPrincipal,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.application.verification_contracts import VerificationAction
from app.application.verification_security import authorized_verification_actions_for
from app.domain.verification import VerificationScope


def principal(user_id: str, *roles: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name=user_id,
        email=None,
        roles=roles,
        auth_mode="test",
    )


class VerificationSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = VerificationScope(
            id="VS-1",
            work_package_id="WP-1",
            lead_user_id="LEAD",
        )

    def test_lead_management_and_execution_permissions_stay_independent(self) -> None:
        lead = principal("LEAD", ROLE_DELIVERY_CONTRIBUTOR)
        actions = authorized_verification_actions_for(lead, self.scope)
        self.assertIn(VerificationAction.ASSIGN_EXECUTORS, actions)
        self.assertIn(VerificationAction.REQUEST_RETEST, actions)
        self.assertNotIn(VerificationAction.RECORD_RESULT, actions)

    def test_assigned_technician_can_execute_but_cannot_manage(self) -> None:
        tech = principal(
            "TECH", ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR
        )
        actions = authorized_verification_actions_for(
            tech,
            self.scope,
            assigned_executor=True,
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

    def test_project_manager_scope_authority_is_pilot_only(self) -> None:
        project_manager = principal("PM", ROLE_PROJECT_MANAGER)
        actions = authorized_verification_actions_for(
            project_manager,
            self.scope,
            project_scope_authorized=True,
        )
        self.assertEqual(
            actions,
            frozenset(
                {
                    VerificationAction.VIEW_SCOPE,
                    VerificationAction.PILOT_SCOPE,
                }
            ),
        )
        self.assertNotIn(VerificationAction.ASSIGN_EXECUTORS, actions)


if __name__ == "__main__":
    unittest.main()
