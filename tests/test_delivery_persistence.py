from __future__ import annotations

import json
import unittest

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domain.delivery import DeliveryItem, DeliveryItemType, DeliveryPlan, DeliveryPlanStatus
from app.infrastructure.sql import (
    AppUser,
    Base,
    DeliveryChangeHistory,
    Project,
    SqlDeliveryRepository,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.delivery_repository import DeliveryVersionConflict


class DeliveryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.add(WorkPackage(id="WP-1", project_id="P-1", name="WP"))
            session.add(
                AppUser(
                    id="USER-LEAD",
                    issuer="test",
                    subject="lead",
                    display_name="Lead",
                    roles_json='["TECHNICIAN"]',
                )
            )
            session.add(
                AppUser(
                    id="USER-TECH",
                    issuer="test",
                    subject="tech",
                    display_name="Technicien",
                    roles_json='["TECHNICIAN"]',
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_plan_and_items_round_trip_with_stable_ids(self) -> None:
        with self.factory.begin() as session:
            repository = SqlDeliveryRepository(session)
            repository.add_plan(
                DeliveryPlan(id="DP-1", work_package_id="WP-1", lead_user_id="USER-LEAD")
            )
            repository.add_item(
                DeliveryItem(
                    id="EPIC-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.EPIC,
                    title="Epic",
                )
            )
            repository.add_item(
                DeliveryItem(
                    id="STORY-1",
                    delivery_plan_id="DP-1",
                    parent_id="EPIC-1",
                    item_type=DeliveryItemType.STORY,
                    title="Story",
                    assignee_user_id="USER-TECH",
                    current_estimate_hours=8,
                    remaining_hours=6,
                    position=1,
                )
            )

        with self.factory() as session:
            repository = SqlDeliveryRepository(session)
            plan = repository.get_non_archived_plan_for_work_package("WP-1")
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(plan.id, "DP-1")
            self.assertEqual(plan.lead_user_id, "USER-LEAD")
            items = repository.list_items("DP-1")
            self.assertEqual([item.id for item in items], ["EPIC-1", "STORY-1"])
            self.assertEqual(items[1].assignee_user_id, "USER-TECH")
            self.assertEqual(items[1].remaining_hours, 6)

    def test_only_one_non_archived_plan_is_enforced_by_database(self) -> None:
        with self.factory() as session:
            repository = SqlDeliveryRepository(session)
            repository.add_plan(DeliveryPlan(id="DP-1", work_package_id="WP-1"))
            with self.assertRaises(IntegrityError):
                repository.add_plan(DeliveryPlan(id="DP-2", work_package_id="WP-1"))
            session.rollback()

        with self.factory.begin() as session:
            repository = SqlDeliveryRepository(session)
            repository.add_plan(
                DeliveryPlan(
                    id="DP-OLD",
                    work_package_id="WP-1",
                    status=DeliveryPlanStatus.ARCHIVED,
                )
            )
            repository.add_plan(DeliveryPlan(id="DP-NEW", work_package_id="WP-1"))

    def test_delivery_version_has_independent_compare_and_swap(self) -> None:
        with self.factory.begin() as session:
            SqlDeliveryRepository(session).add_plan(
                DeliveryPlan(id="DP-1", work_package_id="WP-1")
            )

        with self.factory.begin() as session:
            repository = SqlDeliveryRepository(session)
            self.assertEqual(
                repository.compare_and_increment_version(
                    "DP-1", expected_delivery_version=1
                ),
                2,
            )
            with self.assertRaises(DeliveryVersionConflict):
                repository.compare_and_increment_version(
                    "DP-1", expected_delivery_version=1
                )

        with self.factory() as session:
            plan = SqlDeliveryRepository(session).get_plan("DP-1")
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(plan.delivery_version, 2)

    def test_audit_history_persists_actor_item_version_and_details(self) -> None:
        with self.factory.begin() as session:
            repository = SqlDeliveryRepository(session)
            repository.add_plan(DeliveryPlan(id="DP-1", work_package_id="WP-1"))
            repository.add_item(
                DeliveryItem(
                    id="STORY-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.STORY,
                    title="Story",
                )
            )
            history_id = repository.append_history(
                plan_id="DP-1",
                item_id="STORY-1",
                actor_user_id="USER-TECH",
                action="STORY_UPDATED",
                delivery_version=2,
                details={"remaining_hours": 4},
            )

        with self.factory() as session:
            row = session.scalar(
                select(DeliveryChangeHistory).where(
                    DeliveryChangeHistory.id == history_id
                )
            )
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.delivery_plan_id, "DP-1")
            self.assertEqual(row.delivery_item_id, "STORY-1")
            self.assertEqual(row.actor_user_id, "USER-TECH")
            self.assertEqual(row.delivery_version, 2)
            self.assertEqual(json.loads(row.details_json), {"remaining_hours": 4})

    def test_delivery_links_require_existing_work_package_and_app_user(self) -> None:
        with self.factory() as session:
            repository = SqlDeliveryRepository(session)
            with self.assertRaises(IntegrityError):
                repository.add_plan(
                    DeliveryPlan(
                        id="DP-MISSING",
                        work_package_id="WP-404",
                        lead_user_id="USER-404",
                    )
                )
            session.rollback()


if __name__ == "__main__":
    unittest.main()
