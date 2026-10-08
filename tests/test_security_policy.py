from __future__ import annotations

import unittest

from app.application.security import (
    PERMISSION_ADMIN_SETTINGS,
    PERMISSION_ADMIN_USERS,
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_EXECUTE_VERIFICATION,
    PERMISSION_MANAGE_VERIFICATION,
    PERMISSION_MANAGE_DELIVERY,
    PERMISSION_MANAGE_COMMUNICATIONS,
    PERMISSION_MANAGE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_OVERRIDE_PLANNING_WINDOW,
    PERMISSION_MANAGE_RESOURCES,
    PERMISSION_MANAGE_WORK_PACKAGES,
    PERMISSION_READ,
    PERMISSION_READ_DEMANDS,
    PERMISSION_READ_PROJECTS,
    PERMISSION_READ_WORK_PACKAGES,
    PERMISSION_SYNC_PROJECTS,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
    permissions_for_roles,
)
from app.server.security import required_module_read_permissions, required_permission


class SecurityPolicyTests(unittest.TestCase):
    def test_admin_has_every_declared_permission(self) -> None:
        permissions = set(permissions_for_roles((ROLE_ADMIN,)))
        self.assertEqual(
            permissions,
            {
                PERMISSION_READ,
                PERMISSION_READ_DEMANDS,
                PERMISSION_READ_PROJECTS,
                PERMISSION_READ_WORK_PACKAGES,
                PERMISSION_MANAGE_DEMANDS,
                PERMISSION_APPROVE_DEMANDS,
                PERMISSION_MANAGE_PLANNING,
                PERMISSION_OVERRIDE_PLANNING_WINDOW,
                PERMISSION_MANAGE_WORK_PACKAGES,
                PERMISSION_MANAGE_DELIVERY,
                PERMISSION_CONTRIBUTE_DELIVERY,
                PERMISSION_MANAGE_VERIFICATION,
                PERMISSION_EXECUTE_VERIFICATION,
                PERMISSION_MANAGE_RESOURCES,
                PERMISSION_MANAGE_COMMUNICATIONS,
                PERMISSION_SYNC_PROJECTS,
                PERMISSION_ADMIN_USERS,
                PERMISSION_ADMIN_SETTINGS,
            },
        )

    def test_role_boundaries_are_explicit(self) -> None:
        technician = set(permissions_for_roles((ROLE_TECHNICIAN,)))
        manager = set(permissions_for_roles((ROLE_MANAGER,)))
        project_manager = set(permissions_for_roles((ROLE_PROJECT_MANAGER,)))
        coordinator = set(permissions_for_roles((ROLE_COORDINATOR,)))

        self.assertEqual(technician, {PERMISSION_READ})
        for cap in (PERMISSION_READ_DEMANDS, PERMISSION_READ_PROJECTS, PERMISSION_READ_WORK_PACKAGES):
            self.assertNotIn(cap, technician)
            for allowed in (manager, coordinator, project_manager):
                self.assertIn(cap, allowed)
        self.assertIn(PERMISSION_APPROVE_DEMANDS, manager)
        self.assertNotIn(PERMISSION_MANAGE_DEMANDS, manager)
        self.assertIn(PERMISSION_MANAGE_DEMANDS, project_manager)
        self.assertIn(PERMISSION_MANAGE_WORK_PACKAGES, project_manager)
        self.assertIn(PERMISSION_MANAGE_DELIVERY, project_manager)
        self.assertIn(PERMISSION_CONTRIBUTE_DELIVERY, project_manager)
        self.assertNotIn(PERMISSION_MANAGE_PLANNING, project_manager)
        self.assertNotIn(PERMISSION_OVERRIDE_PLANNING_WINDOW, project_manager)
        self.assertNotIn(PERMISSION_MANAGE_COMMUNICATIONS, project_manager)
        self.assertIn(PERMISSION_MANAGE_PLANNING, coordinator)
        self.assertIn(PERMISSION_OVERRIDE_PLANNING_WINDOW, coordinator)
        self.assertIn(PERMISSION_MANAGE_RESOURCES, coordinator)
        self.assertIn(PERMISSION_MANAGE_COMMUNICATIONS, coordinator)
        self.assertNotIn(PERMISSION_SYNC_PROJECTS, coordinator)
        self.assertNotIn(PERMISSION_ADMIN_SETTINGS, coordinator)
        self.assertNotIn(PERMISSION_CONTRIBUTE_DELIVERY, manager)
        self.assertNotIn(PERMISSION_MANAGE_DELIVERY, manager)
        self.assertNotIn(PERMISSION_OVERRIDE_PLANNING_WINDOW, manager)
        delivery_contributor = set(
            permissions_for_roles((ROLE_DELIVERY_CONTRIBUTOR,))
        )
        self.assertEqual(
            delivery_contributor,
            {
                PERMISSION_READ,
                PERMISSION_READ_PROJECTS,
                PERMISSION_READ_WORK_PACKAGES,
                PERMISSION_CONTRIBUTE_DELIVERY,
                PERMISSION_MANAGE_VERIFICATION,
                PERMISSION_EXECUTE_VERIFICATION,
            },
        )

    def test_delivery_multirole_preserves_read_without_demands(self) -> None:
        permissions = set(permissions_for_roles((ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR)))
        self.assertIn(PERMISSION_READ_PROJECTS, permissions)
        self.assertIn(PERMISSION_READ_WORK_PACKAGES, permissions)
        self.assertNotIn(PERMISSION_READ_DEMANDS, permissions)

    def test_full_module_routes_require_explicit_capabilities(self) -> None:
        paths = {
            "/api/v1/projects": (PERMISSION_READ_PROJECTS,),
            "/api/v1/projects/P/managers": (PERMISSION_READ_PROJECTS,),
            "/api/v1/task-catalog": (PERMISSION_READ_PROJECTS,),
            "/api/v1/demands/D/detail": (PERMISSION_READ_DEMANDS,),
            "/api/v1/demands/D/workflow-actions": (PERMISSION_READ_DEMANDS,),
            "/api/v1/work-packages/WP/weekly-loads": (PERMISSION_READ_WORK_PACKAGES,),
            "/api/v1/medium-term/budget": (PERMISSION_READ_PROJECTS, PERMISSION_READ_WORK_PACKAGES, PERMISSION_READ_DEMANDS),
            "/api/v1/delivery/plans/PLAN": (PERMISSION_READ_PROJECTS, PERMISSION_READ_WORK_PACKAGES),
            "/api/v1/verification/work-packages/WP/documents/traceability.csv": (PERMISSION_READ_PROJECTS, PERMISSION_READ_WORK_PACKAGES),
            "/api/v1/assets/requirements": (PERMISSION_READ_PROJECTS, PERMISSION_READ_DEMANDS),
            "/api/v1/segments/S/history": (PERMISSION_READ_DEMANDS,),
            "/api/v1/shifts/S/history": (PERMISSION_READ_DEMANDS,),
            "/api/v1/allocations/S/operational-responsibility": (PERMISSION_READ_DEMANDS,),
        }
        for path, expected in paths.items():
            with self.subTest(path=path):
                self.assertEqual(required_module_read_permissions("GET", path), expected)
                self.assertEqual(required_module_read_permissions("POST", path), ())
        for path in ("/api/v1/me/schedule", "/api/v1/planning/snapshot", "/api/v1/shifts"):
            self.assertEqual(required_module_read_permissions("GET", path), ())

    def test_invalid_role_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AuthPrincipal.from_roles(
                local_user_id=None,
                issuer="issuer",
                subject="subject",
                display_name="Test",
                email=None,
                roles=("SUPERUSER",),
                auth_mode="test",
            )

    def test_http_permission_mapping_is_fail_closed_for_mutations(self) -> None:
        self.assertEqual(required_permission("GET", "/api/v1/projects"), PERMISSION_READ)
        self.assertEqual(
            required_permission("GET", "/api/v1/demand-requesters"),
            PERMISSION_MANAGE_DEMANDS,
        )
        self.assertEqual(
            required_permission("GET", "/api/v1/communications/contacts"),
            PERMISSION_MANAGE_COMMUNICATIONS,
        )
        self.assertEqual(
            required_permission("GET", "/api/v1/admin/settings/smtp"),
            PERMISSION_ADMIN_SETTINGS,
        )
        self.assertEqual(
            required_permission("PUT", "/api/v1/admin/settings/smtp"),
            PERMISSION_ADMIN_SETTINGS,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/demands/DMO-1/approve"),
            PERMISSION_APPROVE_DEMANDS,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/demands/DMO-1/approval-votes"),
            PERMISSION_APPROVE_DEMANDS,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/demands/DMO-1/request-cancellation",
            ),
            PERMISSION_MANAGE_DEMANDS,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/demands/DMO-1/reject-cancellation",
            ),
            PERMISSION_APPROVE_DEMANDS,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/integrations/acumatica/projects/sync"),
            PERMISSION_SYNC_PROJECTS,
        )
        self.assertEqual(
            required_permission("PATCH", "/api/v1/resources/r-1"),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/business-contacts"),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PUT",
                "/api/v1/projects/P-1/co-managers/C-1",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "DELETE",
                "/api/v1/projects/P-1/co-managers/C-1",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PUT",
                "/api/v1/assets/requirements/AR-1/operator",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "PUT",
                "/api/v1/assets/A-1/approvers/U-1",
            ),
            PERMISSION_ADMIN_SETTINGS,
        )
        self.assertEqual(
            required_permission(
                "DELETE",
                "/api/v1/assets/A-1/approvers/U-1",
            ),
            PERMISSION_ADMIN_SETTINGS,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/assets/project-reservations",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "DELETE",
                "/api/v1/assets/resource-period-reservations/AR-2",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "PUT",
                "/api/v1/assets/segment-reservations/AR-SEG",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "PUT",
                "/api/v1/assets/types/AT-1/qualification",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/task-catalog/T1/business-contacts",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/projects/P-1/project-manager-contact",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/projects/P-1/operational-responsible",
            ),
            PERMISSION_MANAGE_RESOURCES,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/segments/SEG-1/operational-responsible",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/planning/resources/R-1/reorder",
            ),
            PERMISSION_READ,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/allocations/SHIFT-1/operational-responsible",
            ),
            PERMISSION_MANAGE_PLANNING,
        )
        self.assertEqual(
            required_permission(
                "PATCH",
                "/api/v1/demands/DMO-1/operational-responsible",
            ),
            PERMISSION_MANAGE_DEMANDS,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/delivery/plans"),
            PERMISSION_CONTRIBUTE_DELIVERY,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/verification/requirements/REQ-1/assignments",
            ),
            PERMISSION_MANAGE_VERIFICATION,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/verification/requirements/REQ-1/executions",
            ),
            PERMISSION_EXECUTE_VERIFICATION,
        )
        self.assertEqual(
            required_permission(
                "POST",
                "/api/v1/verification/executions/EX-1/evidence-links",
            ),
            PERMISSION_EXECUTE_VERIFICATION,
        )
        self.assertEqual(
            required_permission("POST", "/api/v1/future-command"),
            "__unassigned_mutation__",
        )
        self.assertIsNone(required_permission("GET", "/api/v1/auth/me"))
        self.assertIsNone(required_permission("GET", "/health"))


if __name__ == "__main__":
    unittest.main()
