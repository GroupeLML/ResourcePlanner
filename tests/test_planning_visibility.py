from __future__ import annotations

from datetime import date
import unittest

from app.application.errors import ApplicationAuthorizationError
from app.application.planning_visibility import PlanningVisibilityService
from app.application.query_models import ResourceReadModel, ShiftReadModel
from app.application.security import (
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)


class _Repository:
    def __init__(self) -> None:
        self.resource = ResourceReadModel(
            id="RES-ME",
            name="Moi",
            external_id="EMP-1",
        )
        self.managed = ("PROJ-MANAGED",)
        self.participating = ("PROJ-PARTICIPATING",)
        self.coordinated_demands = ("REQ-COORD",)
        self.direct_resources = ("RES-DIRECT",)
        self.approval_demands = ("REQ-APPROVAL",)
        self.project_days = (("PROJ-SHARED", date(2026, 10, 6)),)

    def get_resource_by_external_id(
        self,
        employee_external_id: str,
    ) -> ResourceReadModel | None:
        return self.resource if employee_external_id == "EMP-1" else None

    def list_managed_project_ids(
        self,
        local_user_id: str | None,
        employee_external_id: str | None,
    ) -> tuple[str, ...]:
        return self.managed

    def list_participating_project_ids(
        self,
        resource_id: str,
    ) -> tuple[str, ...]:
        return self.participating

    def list_coordinated_demand_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]:
        return self.coordinated_demands

    def list_directly_coordinated_resource_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]:
        return self.direct_resources

    def list_current_approval_demand_ids(
        self,
        local_user_id: str,
    ) -> tuple[str, ...]:
        return self.approval_demands

    def list_shift_project_days(
        self,
        resource_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[tuple[str, date], ...]:
        return self.project_days


def _principal(*roles: str, employee_external_id: str | None = "EMP-1") -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id="USER-1",
        issuer="urn:test",
        subject="user-1",
        display_name="Utilisateur",
        email=None,
        roles=roles,
        auth_mode="test",
        employee_external_id=employee_external_id,
    )


class PlanningVisibilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = _Repository()
        self.service = PlanningVisibilityService(self.repository)

    def test_project_manager_cannot_request_global_scope(self) -> None:
        principal = _principal(ROLE_PROJECT_MANAGER)

        self.assertEqual(
            PlanningVisibilityService.available_scopes(principal),
            ("mine",),
        )
        self.assertEqual(
            PlanningVisibilityService.default_scope(principal),
            "mine",
        )
        with self.assertRaises(ApplicationAuthorizationError) as caught:
            self.service.resolve(principal, "global")

        self.assertEqual(caught.exception.code, "planning_scope_not_authorized")

    def test_frontend_policy_projection_uses_same_authorized_scopes(self) -> None:
        project_manager_policy = PlanningVisibilityService.view_policy(
            _principal(ROLE_PROJECT_MANAGER)
        )
        coordinator_policy = PlanningVisibilityService.view_policy(
            _principal(ROLE_COORDINATOR)
        )

        self.assertEqual(project_manager_policy.available_scopes, ("mine",))
        self.assertEqual(project_manager_policy.default_scope, "mine")
        self.assertEqual(coordinator_policy.available_scopes, ("mine", "global"))
        self.assertEqual(coordinator_policy.default_scope, "global")

    def test_manager_defaults_to_global_scope(self) -> None:
        resolution = self.service.resolve(_principal(ROLE_MANAGER), None)

        self.assertEqual(resolution.scope, "global")
        self.assertIsNone(resolution.project_ids)

    def test_coordinator_mine_unions_managed_direct_and_current_demands(self) -> None:
        resolution = self.service.resolve(
            _principal(ROLE_COORDINATOR),
            "mine",
        )

        self.assertEqual(resolution.project_ids, ("PROJ-MANAGED",))
        self.assertEqual(
            resolution.include_resource_ids,
            ("RES-DIRECT", "RES-ME"),
        )
        self.assertEqual(
            {
                row.demand_id: row.sources
                for row in resolution.visible_demands
            },
            {
                "REQ-APPROVAL": ("CURRENT_APPROVER",),
                "REQ-COORD": ("COORDINATOR",),
            },
        )

    def test_technician_scope_uses_real_shift_project_day_pairs_only(self) -> None:
        resolution = self.service.resolve(
            _principal(ROLE_TECHNICIAN),
            None,
            start=date(2026, 10, 5),
            end=date(2026, 10, 11),
        )

        self.assertEqual(resolution.scope, "mine")
        self.assertEqual(resolution.personal_resource_id, "RES-ME")
        self.assertEqual(
            resolution.project_day_keys,
            (("PROJ-SHARED", date(2026, 10, 6)),),
        )
        self.assertEqual(resolution.segment_resource_ids, ())

    def test_technician_neighbor_shift_is_minimal_and_non_navigable(self) -> None:
        resolution = self.service.resolve(
            _principal(ROLE_TECHNICIAN),
            "mine",
        )
        neighbor = ShiftReadModel(
            allocation_id="SHIFT-SECRET",
            segment_id="SEG-SECRET",
            requirement_id="REQMT-SECRET",
            resource_id="RES-COLLEAGUE",
            resource_name="Collègue",
            work_date=date(2026, 10, 6),
            hours=8.0,
            demand_id="REQ-SECRET",
            demand_number="DMO-SECRET",
            project_id="PROJ-SHARED",
            project_number="P-100",
            project_name="Projet partagé",
            confirmation="Confirmée",
            note="Note privée",
            requester="Demandeur privé",
            project_manager_color_id="private-stable-token",
            project_manager_color_label="Chargé privé",
        )

        projected = self.service.project_shift(neighbor, resolution)

        self.assertEqual(projected.resource_name, "Collègue")
        self.assertEqual(projected.work_date, date(2026, 10, 6))
        self.assertEqual(projected.hours, 8.0)
        self.assertEqual(projected.project_number, "P-100")
        self.assertEqual(projected.source, "SCOPE_NEIGHBOR")
        self.assertNotEqual(projected.allocation_id, "SHIFT-SECRET")
        self.assertIsNone(projected.project_id)
        self.assertEqual(projected.segment_id, "")
        self.assertIsNone(projected.requirement_id)
        self.assertIsNone(projected.demand_id)
        self.assertIsNone(projected.demand_number)
        self.assertIsNone(projected.note)
        self.assertIsNone(projected.requester)
        self.assertIsNone(projected.project_manager_color_id)
        self.assertIsNone(projected.project_manager_color_label)
        self.assertFalse(
            self.service.can_read_shift_details(neighbor, resolution)
        )

    def test_technician_own_shift_keeps_full_context(self) -> None:
        resolution = self.service.resolve(
            _principal(ROLE_TECHNICIAN),
            "mine",
        )
        own = ShiftReadModel(
            allocation_id="SHIFT-OWN",
            segment_id="SEG-OWN",
            resource_id="RES-ME",
            resource_name="Moi",
            work_date=date(2026, 10, 6),
            hours=8.0,
            demand_id="REQ-OWN",
            demand_number="DMO-OWN",
            project_id="PROJ-SHARED",
            project_number="P-100",
            note="Ma note",
        )

        self.assertIs(self.service.project_shift(own, resolution), own)
        self.assertTrue(self.service.can_read_shift_details(own, resolution))


if __name__ == "__main__":
    unittest.main()
