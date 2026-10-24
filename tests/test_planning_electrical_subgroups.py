from __future__ import annotations

from datetime import date
import unittest

from app.domain.planning_resource_subgroups import (
    APPRENTICE,
    COMPANION,
    OTHER,
    classify_planning_resource_subgroup,
)
from app.infrastructure.sql import (
    Base,
    Resource,
    ResourceAvailabilityRule,
    SqlPlannerQueryRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


D1 = date(2026, 10, 19)
D2 = date(2026, 10, 25)


class PlanningElectricalSubgroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_authoritative_employee_class_mapping_and_parent_guard(self) -> None:
        self.assertEqual(
            classify_planning_resource_subgroup(
                resource_class="INSTALL_ELEC",
                erp_employee_class="ELCOMP",
            ).code,
            COMPANION,
        )
        for employee_class in ("ELAPP1", "ELAPP2", "ELAPP3", "ELAPP4"):
            with self.subTest(employee_class=employee_class):
                result = classify_planning_resource_subgroup(
                    resource_class="INSTALL_ELEC",
                    erp_employee_class=employee_class,
                )
                assert result is not None
                self.assertEqual(result.code, APPRENTICE)

        for employee_class in (None, "", "GENERAL", "UNKNOWN_CODE"):
            with self.subTest(employee_class=employee_class):
                result = classify_planning_resource_subgroup(
                    resource_class="INSTALL_ELEC",
                    erp_employee_class=employee_class,
                )
                assert result is not None
                self.assertEqual(result.code, OTHER)

        self.assertIsNone(
            classify_planning_resource_subgroup(
                resource_class="INSTALL_EXC",
                erp_employee_class="ELCOMP",
            )
        )

    def test_schedulable_projection_exposes_subgroup_without_broadening_visibility(self) -> None:
        with transactional_session(self.factory) as session:
            resources = [
                Resource(
                    id="R-COMP",
                    name="Compagnon",
                    resource_class="INSTALL_ELEC",
                    erp_employee_class="ELCOMP",
                    active=True,
                    erp_active=True,
                    sort_order=10,
                ),
                Resource(
                    id="R-APP1",
                    name="Apprenti 1",
                    resource_class="INSTALL_ELEC",
                    erp_employee_class="ELAPP1",
                    active=True,
                    erp_active=True,
                    sort_order=20,
                ),
                Resource(
                    id="R-APP4",
                    name="Apprenti 4",
                    resource_class="INSTALL_ELEC",
                    erp_employee_class="ELAPP4",
                    active=True,
                    erp_active=True,
                    sort_order=30,
                ),
                Resource(
                    id="R-OTHER",
                    name="Autre",
                    resource_class="INSTALL_ELEC",
                    erp_employee_class="GENERAL",
                    active=True,
                    erp_active=True,
                    sort_order=40,
                ),
                Resource(
                    id="R-EXC",
                    name="Excavation compagnon ERP",
                    resource_class="INSTALL_EXC",
                    erp_employee_class="ELCOMP",
                    active=True,
                    erp_active=True,
                    sort_order=50,
                ),
                Resource(
                    id="R-NOSCHEDULE",
                    name="Sans horaire",
                    resource_class="INSTALL_ELEC",
                    erp_employee_class="ELCOMP",
                    active=True,
                    erp_active=True,
                    sort_order=60,
                ),
            ]
            session.add_all(resources)
            session.flush()
            session.add_all(
                [
                    ResourceAvailabilityRule(
                        id=f"SCH-{resource.id}",
                        resource_id=resource.id,
                        availability_type="Horaire standard",
                        start_date=D1,
                        end_date=D2,
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        active=True,
                    )
                    for resource in resources
                    if resource.id != "R-NOSCHEDULE"
                ]
            )

        with self.factory() as session:
            rows = SqlPlannerQueryRepository(session).list_schedulable_resources(
                start=D1,
                end=D2,
            )

        by_id = {row.id: row for row in rows}
        self.assertNotIn("R-NOSCHEDULE", by_id)
        self.assertEqual(by_id["R-COMP"].planning_subgroup_code, COMPANION)
        self.assertEqual(
            by_id["R-COMP"].planning_subgroup_label,
            "Compagnons électriciens",
        )
        self.assertEqual(by_id["R-APP1"].planning_subgroup_code, APPRENTICE)
        self.assertEqual(by_id["R-APP4"].planning_subgroup_code, APPRENTICE)
        self.assertEqual(by_id["R-OTHER"].planning_subgroup_code, OTHER)
        self.assertEqual(
            by_id["R-OTHER"].planning_subgroup_label,
            "Autres / Non classé",
        )
        self.assertIsNone(by_id["R-EXC"].planning_subgroup_code)
        self.assertIsNone(by_id["R-EXC"].planning_subgroup_label)

    def test_erp_progression_changes_projection_without_changing_parent_class(self) -> None:
        with transactional_session(self.factory) as session:
            resource = Resource(
                id="R-PROGRESS",
                name="Progression",
                resource_class="INSTALL_ELEC",
                erp_employee_class="ELAPP4",
                active=True,
                erp_active=True,
            )
            session.add(resource)

        with self.factory() as session:
            before = SqlPlannerQueryRepository(session).list_resources()
            self.assertEqual(before[0].planning_subgroup_code, APPRENTICE)

        with transactional_session(self.factory) as session:
            resource = session.get(Resource, "R-PROGRESS")
            assert resource is not None
            resource.erp_employee_class = "ELCOMP"

        with self.factory() as session:
            after = SqlPlannerQueryRepository(session).list_resources()
            self.assertEqual(after[0].resource_class, "INSTALL_ELEC")
            self.assertEqual(after[0].planning_subgroup_code, COMPANION)


if __name__ == "__main__":
    unittest.main()
