from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.infrastructure.sql import (
    PlanningWindowOverride,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.test_planning_window_override_commands import (
    ADMIN_ID,
    ALLOCATION_ID,
    DAY,
    NEXT_DAY,
    REQUEST_ID,
    REQUIREMENT_ID,
    RESOURCE_B,
    REVISION_ID,
    SEGMENT_ID,
    SHIFT_ID,
    PlanningWindowOverrideCommandTests,
)


AUTH = static_auth_resolver(
    AuthPrincipal.from_roles(
        local_user_id=ADMIN_ID,
        issuer="urn:resourceplanner:test",
        subject="admin-613c",
        display_name="Admin 613D",
        email=None,
        roles=(ROLE_ADMIN,),
        auth_mode="test",
    )
)


class PlanningWindowOverrideApiTests(unittest.TestCase):
    def test_evaluation_and_override_move_are_exposed_without_contaminating_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            url = PlanningWindowOverrideCommandTests._database(directory)
            app = create_api_app(url, auth_resolver=AUTH)
            with TestClient(app, raise_server_exceptions=False) as client:
                evaluated = client.post(
                    f"/api/v1/allocations/{ALLOCATION_ID}/evaluate-drop",
                    json={
                        "resource_id": RESOURCE_B,
                        "day": NEXT_DAY.isoformat(),
                        "outside_standard_hours": False,
                        "include_planning_window_override_options": True,
                    },
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                context = evaluated.json()
                self.assertEqual(
                    context["authorization_decision"],
                    "PLANNING_WINDOW_OVERRIDE_AVAILABLE",
                )
                self.assertEqual(
                    [row["code"] for row in context["actions"]],
                    [
                        "OVERRIDE_WINDOW_AND_MOVE",
                        "PROPOSE_WINDOW_EXTENSION",
                        "CANCEL",
                    ],
                )
                self.assertEqual(
                    context["requested_window"],
                    {"start": DAY.isoformat(), "end": DAY.isoformat()},
                )
                self.assertEqual(
                    context["approved_window"],
                    {"start": DAY.isoformat(), "end": DAY.isoformat()},
                )

                body = {
                    "resource_id": RESOURCE_B,
                    "day": NEXT_DAY.isoformat(),
                    "reason": "Intervention coordonnée 613D",
                    "expected_planning_version": context["planning_version"],
                    "expected_approval_revision_id": context["approval_revision_id"],
                    "expected_operational_version": context["operational_version"],
                    "outside_standard_hours": False,
                    "overallocation_policy": None,
                }
                headers = {"Idempotency-Key": "613d-api-move"}
                first = client.post(
                    f"/api/v1/allocations/{ALLOCATION_ID}/planning-window-override-move",
                    json=body,
                    headers=headers,
                )
                replay = client.post(
                    f"/api/v1/allocations/{ALLOCATION_ID}/planning-window-override-move",
                    json=body,
                    headers=headers,
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(first.json()["operation"], "OVERRIDE_WINDOW_AND_MOVE")
            self.assertEqual(first.json()["effective_window"]["end"], NEXT_DAY.isoformat())

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                request = session.get(WorkforceRequest, REQUEST_ID)
                requirement = session.get(ResourceRequirement, REQUIREMENT_ID)
                shift = session.get(Shift, SHIFT_ID)
                self.assertIsNotNone(request)
                self.assertIsNotNone(requirement)
                self.assertIsNotNone(shift)
                assert request is not None and requirement is not None and shift is not None
                self.assertEqual(request.desired_end, DAY)
                self.assertEqual(request.estimated_hours, 8)
                self.assertEqual(requirement.end_date, NEXT_DAY)
                self.assertEqual(shift.resource_id, RESOURCE_B)
                self.assertEqual(shift.work_date, NEXT_DAY)
                self.assertTrue(shift.locked)
                self.assertEqual(shift.source, "MANUAL")
                self.assertEqual(
                    int(session.scalar(select(func.count()).select_from(PlanningWindowOverride)) or 0),
                    1,
                )
            engine.dispose()

    def test_segment_override_route_replays_and_keeps_approved_revision_reference(self) -> None:
        with TemporaryDirectory() as directory:
            url = PlanningWindowOverrideCommandTests._database(directory)
            app = create_api_app(url, auth_resolver=AUTH)
            body = {
                "start_date": DAY.isoformat(),
                "end_date": NEXT_DAY.isoformat(),
                "reason": "Fenêtre de chantier prolongée 613D",
                "expected_planning_version": 1,
                "expected_approval_revision_id": REVISION_ID,
                "expected_operational_version": None,
            }
            headers = {"Idempotency-Key": "613d-api-segment"}
            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.post(
                    f"/api/v1/segments/{SEGMENT_ID}/planning-window-override",
                    json=body,
                    headers=headers,
                )
                replay = client.post(
                    f"/api/v1/segments/{SEGMENT_ID}/planning-window-override",
                    json=body,
                    headers=headers,
                )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(first.json()["operation"], "OVERRIDE_WINDOW")
            self.assertEqual(first.json()["approval_revision_id"], REVISION_ID)
            self.assertEqual(first.json()["effective_window"]["end"], NEXT_DAY.isoformat())

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                request = session.get(WorkforceRequest, REQUEST_ID)
                requirement = session.get(ResourceRequirement, REQUIREMENT_ID)
                assert request is not None and requirement is not None
                self.assertEqual(request.desired_end, DAY)
                self.assertEqual(requirement.end_date, NEXT_DAY)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
