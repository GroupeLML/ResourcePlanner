from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.security import (
    AuthPrincipal,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    DeliveryChangeHistory,
    DeliveryItemRow,
    DeliveryPlanRow,
    PlanningMutationState,
    Project,
    Shift,
    WorkPackage,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


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


class DeliveryBoardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "delivery-board.db"
        bootstrap = self._app(
            principal("PM", ROLE_PROJECT_MANAGER)
        )
        factory = bootstrap.state.session_factory
        Base.metadata.create_all(factory.kw["bind"])
        with factory() as session, session.begin():
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.add(WorkPackage(id="WP-1", project_id="P-1", name="WP"))
            session.add(PlanningMutationState(id="GLOBAL", version=37))
            session.add_all(
                [
                    AppUser(
                        id="PM",
                        issuer="urn:test",
                        subject="subject-PM",
                        display_name="PM",
                        roles_json=json.dumps([ROLE_PROJECT_MANAGER]),
                        active=True,
                    ),
                    AppUser(
                        id="LEAD",
                        issuer="urn:test",
                        subject="subject-LEAD",
                        display_name="Lead",
                        roles_json=json.dumps(
                            [ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR]
                        ),
                        active=True,
                    ),
                    AppUser(
                        id="TECH",
                        issuer="urn:test",
                        subject="subject-TECH",
                        display_name="Technicien",
                        roles_json=json.dumps(
                            [ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR]
                        ),
                        active=True,
                    ),
                ]
            )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, actor: AuthPrincipal):
        return create_api_app(
            f"sqlite+pysqlite:///{self.database_path.as_posix()}",
            auth_resolver=static_auth_resolver(actor),
        )

    def test_pm_lead_technician_flow_uses_delivery_version_and_contextual_actions(
        self,
    ) -> None:
        pm_app = self._app(principal("PM", ROLE_PROJECT_MANAGER))
        with TestClient(pm_app) as pm:
            created = pm.post(
                "/api/v1/delivery/plans",
                json={"work_package_id": "WP-1", "lead_user_id": "LEAD"},
            )
            self.assertEqual(created.status_code, 201, created.text)
            plan_id = created.json()["plan"]["id"]
            self.assertEqual(created.json()["plan"]["delivery_version"], 1)
            self.assertIn("MANAGE_PLAN_LIFECYCLE", created.json()["actions"])
            self.assertNotIn("MANAGE_STRUCTURE", created.json()["actions"])

            activated = pm.post(
                f"/api/v1/delivery/plans/{plan_id}/activate",
                json={"expected_delivery_version": 1},
            )
            self.assertEqual(activated.status_code, 200, activated.text)
            self.assertEqual(activated.json()["plan"]["delivery_version"], 2)
            self.assertEqual(activated.json()["plan"]["status"], "ACTIVE")

        lead_app = self._app(
            principal(
                "LEAD",
                ROLE_TECHNICIAN,
                ROLE_DELIVERY_CONTRIBUTOR,
            )
        )
        with TestClient(lead_app) as lead:
            board = lead.get(f"/api/v1/delivery/plans/{plan_id}")
            self.assertEqual(board.status_code, 200, board.text)
            self.assertIn("MANAGE_STRUCTURE", board.json()["actions"])
            self.assertNotIn("MANAGE_PLAN_LIFECYCLE", board.json()["actions"])

            epic = lead.post(
                f"/api/v1/delivery/plans/{plan_id}/items",
                json={
                    "expected_delivery_version": 2,
                    "item_type": "EPIC",
                    "title": "Epic 1",
                },
            )
            self.assertEqual(epic.status_code, 201, epic.text)
            self.assertEqual(epic.json()["plan"]["delivery_version"], 3)
            epic_id = next(
                item["id"]
                for item in epic.json()["items"]
                if item["item_type"] == "EPIC"
            )

            story = lead.post(
                f"/api/v1/delivery/plans/{plan_id}/items",
                json={
                    "expected_delivery_version": 3,
                    "item_type": "STORY",
                    "parent_id": epic_id,
                    "title": "Story 1",
                    "status": "TODO",
                    "assignee_user_id": "TECH",
                    "current_estimate_hours": 8,
                    "remaining_hours": 8,
                },
            )
            self.assertEqual(story.status_code, 201, story.text)
            self.assertEqual(story.json()["plan"]["delivery_version"], 4)
            story_row = next(
                item
                for item in story.json()["items"]
                if item["item_type"] == "STORY"
            )
            story_id = story_row["id"]
            self.assertEqual(story_row["reference_estimate_hours"], 8)
            self.assertEqual(story_row["assignee_user_id"], "TECH")

        tech_app = self._app(
            principal(
                "TECH",
                ROLE_TECHNICIAN,
                ROLE_DELIVERY_CONTRIBUTOR,
            )
        )
        with TestClient(tech_app) as tech:
            board = tech.get(f"/api/v1/delivery/plans/{plan_id}")
            own_story = next(
                item for item in board.json()["items"] if item["id"] == story_id
            )
            self.assertIn("UPDATE_OWN_STORY_STATUS", own_story["actions"])
            self.assertIn("UPDATE_OWN_REMAINING_HOURS", own_story["actions"])
            self.assertNotIn("ASSIGN_STORIES", own_story["actions"])

            updated = tech.patch(
                f"/api/v1/delivery/items/{story_id}",
                json={
                    "expected_delivery_version": 4,
                    "status": "IN_PROGRESS",
                    "remaining_hours": 5,
                },
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["plan"]["delivery_version"], 5)
            updated_story = next(
                item
                for item in updated.json()["items"]
                if item["id"] == story_id
            )
            self.assertEqual(updated_story["status"], "IN_PROGRESS")
            self.assertEqual(updated_story["remaining_hours"], 5)
            self.assertEqual(updated_story["reference_estimate_hours"], 8)

            blockage = tech.post(
                f"/api/v1/delivery/items/{story_id}/blockage-note",
                json={
                    "expected_delivery_version": 5,
                    "note": "Dépendance externe.",
                },
            )
            self.assertEqual(blockage.status_code, 200, blockage.text)
            self.assertEqual(blockage.json()["plan"]["delivery_version"], 6)

            forbidden_assignment = tech.patch(
                f"/api/v1/delivery/items/{story_id}",
                json={
                    "expected_delivery_version": 6,
                    "assignee_user_id": "LEAD",
                },
            )
            self.assertEqual(forbidden_assignment.status_code, 403)
            self.assertEqual(
                forbidden_assignment.json()["error"]["code"],
                "delivery_action_denied",
            )

            planning = tech.post("/api/v1/planning/rebuild")
            self.assertEqual(planning.status_code, 403)
            self.assertEqual(
                planning.json()["error"]["context"]["required_permission"],
                "manage_planning",
            )

        with TestClient(lead_app) as lead:
            stale = lead.patch(
                f"/api/v1/delivery/items/{story_id}",
                json={
                    "expected_delivery_version": 4,
                    "remaining_hours": 3,
                },
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                stale.json()["error"]["code"],
                "delivery_version_conflict",
            )

        with TestClient(pm_app) as pm:
            archived = pm.post(
                f"/api/v1/delivery/plans/{plan_id}/archive",
                json={"expected_delivery_version": 6},
            )
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertEqual(archived.json()["plan"]["status"], "ARCHIVED")
            self.assertEqual(archived.json()["plan"]["delivery_version"], 7)

        with TestClient(lead_app) as lead:
            archived_edit = lead.patch(
                f"/api/v1/delivery/items/{story_id}",
                json={
                    "expected_delivery_version": 7,
                    "remaining_hours": 2,
                },
            )
            self.assertEqual(archived_edit.status_code, 409)
            self.assertEqual(
                archived_edit.json()["error"]["code"],
                "delivery_plan_archived",
            )

        with pm_app.state.session_factory() as session:
            planning_state = session.get(PlanningMutationState, "GLOBAL")
            self.assertIsNotNone(planning_state)
            assert planning_state is not None
            self.assertEqual(planning_state.version, 37)
            self.assertEqual(len(session.scalars(select(Shift)).all()), 0)

            plan_row = session.get(DeliveryPlanRow, plan_id)
            story_db = session.get(DeliveryItemRow, story_id)
            self.assertIsNotNone(plan_row)
            self.assertIsNotNone(story_db)
            assert plan_row is not None
            assert story_db is not None
            self.assertEqual(plan_row.id, plan_id)
            self.assertEqual(plan_row.status, "ARCHIVED")
            self.assertIsNotNone(plan_row.archived_at)
            self.assertEqual(story_db.id, story_id)
            self.assertEqual(story_db.delivery_plan_id, plan_id)
            self.assertEqual(story_db.status, "IN_PROGRESS")
            self.assertEqual(float(story_db.reference_estimate_hours or 0), 8)
            self.assertEqual(float(story_db.remaining_hours or 0), 5)

            history = session.scalars(
                select(DeliveryChangeHistory)
                .where(DeliveryChangeHistory.delivery_plan_id == plan_id)
                .order_by(
                    DeliveryChangeHistory.delivery_version,
                    DeliveryChangeHistory.occurred_at,
                )
            ).all()
            self.assertEqual(
                [row.action for row in history],
                [
                    "PLAN_CREATED",
                    "PLAN_ACTIVE",
                    "ITEM_CREATED",
                    "ITEM_CREATED",
                    "ITEM_UPDATED",
                    "BLOCKAGE_DOCUMENTED",
                    "PLAN_ARCHIVED",
                ],
            )
            self.assertEqual(
                [row.delivery_version for row in history],
                [1, 2, 3, 4, 5, 6, 7],
            )
            self.assertEqual(
                [row.actor_user_id for row in history],
                ["PM", "PM", "LEAD", "LEAD", "TECH", "TECH", "PM"],
            )
            self.assertEqual(
                [row.delivery_item_id for row in history],
                [None, None, epic_id, story_id, story_id, story_id, None],
            )

    def test_plan_uniqueness_and_missing_links_are_structured(self) -> None:
        app = self._app(principal("PM", ROLE_PROJECT_MANAGER))
        with TestClient(app) as client:
            missing_wp = client.post(
                "/api/v1/delivery/plans",
                json={"work_package_id": "WP-404"},
            )
            self.assertEqual(missing_wp.status_code, 422)
            self.assertEqual(
                missing_wp.json()["error"]["code"],
                "delivery_work_package_not_found",
            )

            missing_lead = client.post(
                "/api/v1/delivery/plans",
                json={
                    "work_package_id": "WP-1",
                    "lead_user_id": "USER-404",
                },
            )
            self.assertEqual(missing_lead.status_code, 422)
            self.assertEqual(
                missing_lead.json()["error"]["code"],
                "delivery_user_not_found",
            )

            first = client.post(
                "/api/v1/delivery/plans",
                json={"work_package_id": "WP-1"},
            )
            self.assertEqual(first.status_code, 201, first.text)
            duplicate = client.post(
                "/api/v1/delivery/plans",
                json={"work_package_id": "WP-1"},
            )
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(
                duplicate.json()["error"]["code"],
                "delivery_plan_exists",
            )


if __name__ == "__main__":
    unittest.main()
