from __future__ import annotations

from datetime import date, time
import unittest

from app.infrastructure.sql import (
    AvailabilityRuleResourceClass,
    Base,
    Resource,
    ResourceAvailabilityRule,
    ResourceClassConfig,
    SqlPlannerQueryRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 13)
WEDNESDAY = date(2026, 10, 14)


class PlanningHolidayProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed_resource(self, resource_id: str, name: str, resource_class: str) -> None:
        with transactional_session(self.factory) as session:
            if session.get(ResourceClassConfig, resource_class) is None:
                session.add(
                    ResourceClassConfig(
                        code=resource_class,
                        label=resource_class,
                        active=True,
                    )
                )
                session.flush()
            session.add(
                Resource(
                    id=resource_id,
                    name=name,
                    resource_class=resource_class,
                    active=True,
                    erp_active=True,
                )
            )
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id=f"SCH-{resource_id}",
                    resource_id=resource_id,
                    availability_type="Horaire standard",
                    start_date=MONDAY,
                    end_date=WEDNESDAY,
                    weekdays="Lun,Mar,Mer",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )

    def test_capacity_grid_projects_global_class_and_individual_holidays(self) -> None:
        self._seed_resource("R-ELEC", "Électrique", "INSTALL_ELEC")
        self._seed_resource("R-AUTO", "Automation", "AUTOMATION")
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    ResourceAvailabilityRule(
                        id="HOL-GLOBAL",
                        resource_id=None,
                        availability_type="Jour férié",
                        start_date=MONDAY,
                        end_date=MONDAY,
                        note="Action de grâce",
                        active=True,
                    ),
                    ResourceAvailabilityRule(
                        id="HOL-CLASS",
                        resource_id=None,
                        availability_type="Jour férié",
                        start_date=TUESDAY,
                        end_date=TUESDAY,
                        note="Férié installation",
                        active=True,
                    ),
                    ResourceAvailabilityRule(
                        id="HOL-RESOURCE",
                        resource_id="R-AUTO",
                        availability_type="Jour férié",
                        start_date=WEDNESDAY,
                        end_date=WEDNESDAY,
                        note=None,
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                AvailabilityRuleResourceClass(
                    availability_rule_id="HOL-CLASS",
                    resource_class_code="INSTALL_ELEC",
                )
            )

        with self.factory() as session:
            grid = SqlPlannerQueryRepository(session).planning_capacity_grid(
                start=MONDAY,
                end=WEDNESDAY,
            )

        by_resource = {row.resource_id: row for row in grid.resources}
        elec = {row.day: row for row in by_resource["R-ELEC"].days}
        auto = {row.day: row for row in by_resource["R-AUTO"].days}

        self.assertEqual(elec[MONDAY].reason_code, "HOLIDAY")
        self.assertEqual(elec[MONDAY].reason_label, "Action de grâce")
        self.assertEqual(elec[MONDAY].holiday_scope, "GLOBAL")
        self.assertEqual(auto[MONDAY].holiday_scope, "GLOBAL")

        self.assertEqual(elec[TUESDAY].reason_code, "HOLIDAY")
        self.assertEqual(elec[TUESDAY].holiday_scope, "RESOURCE_CLASS")
        self.assertEqual(auto[TUESDAY].capacity_hours, 8.0)
        self.assertIsNone(auto[TUESDAY].reason_code)

        self.assertEqual(auto[WEDNESDAY].reason_code, "HOLIDAY")
        self.assertEqual(auto[WEDNESDAY].reason_label, "Jour férié")
        self.assertEqual(auto[WEDNESDAY].holiday_scope, "RESOURCE")
        self.assertEqual(elec[WEDNESDAY].capacity_hours, 8.0)


if __name__ == "__main__":
    unittest.main()
