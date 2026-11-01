from __future__ import annotations

import unittest

from sqlalchemy import select

from app.application.employee_sync import EmployeeSyncService, ExternalEmployeeRecord
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    Resource,
    SqlEmployeeSyncRepository,
    SqlPlannerQueryRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


class StubEmployeeSource:
    def __init__(self, rows: list[ExternalEmployeeRecord]) -> None:
        self.rows = rows

    def list_employees(self) -> tuple[ExternalEmployeeRecord, ...]:
        return tuple(self.rows)


def employee(
    external_id: str,
    display_name: str,
    *,
    email: str | None = None,
    status: str = "Actif",
    branch: str | None = "210",
    employee_class: str | None = "GENERAL",
) -> ExternalEmployeeRecord:
    return ExternalEmployeeRecord(
        external_id=external_id,
        display_name=display_name,
        email=email,
        erp_status=status,
        erp_active=status.casefold() == "actif",
        department_description="Automatisation",
        department_code="AUTO",
        employee_class=employee_class,
        supervisor_external_id="EMP-BOSS",
        telephone="555-0100",
        branch_code=branch,
        contact_id=1001,
    )


class EmployeeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync(self, source: StubEmployeeSource):
        with transactional_session(self.factory) as session:
            return EmployeeSyncService(
                source,
                SqlEmployeeSyncRepository(session),
            ).synchronize()

    def test_sync_is_idempotent_defaults_local_activation_off_and_preserves_planning_fields(self) -> None:
        first_email = "employee1" + chr(64) + "example.invalid"
        second_email = "employee2" + chr(64) + "example.invalid"
        source = StubEmployeeSource(
            [employee("EMP-1", "Employé initial", email=first_email)]
        )

        first = self._sync(source)
        second = self._sync(source)
        self.assertEqual(
            (first.created, first.updated, first.unchanged, first.errors),
            (1, 0, 0, 0),
        )
        self.assertEqual(
            (second.created, second.updated, second.unchanged, second.errors),
            (0, 0, 1, 0),
        )

        with transactional_session(self.factory) as session:
            row = session.scalar(select(Resource).where(Resource.external_id == "EMP-1"))
            assert row is not None
            self.assertFalse(row.active)
            self.assertTrue(row.erp_active)
            row.active = True
            row.resource_class = "Programmation"
            row.residence_city = "Chambly"
            row.competencies = "PLC;SCADA"
            row.note = "Attribut local"
            row.sort_order = 7

        source.rows[0] = employee(
            "EMP-1",
            "Employé renommé",
            email=second_email,
            status="Inactif",
            branch="110",
        )
        result = self._sync(source)
        self.assertEqual(
            (result.created, result.updated, result.unchanged, result.errors),
            (0, 1, 0, 0),
        )

        with self.factory() as session:
            row = session.scalar(select(Resource).where(Resource.external_id == "EMP-1"))
            assert row is not None
            self.assertEqual(row.name, "Employé renommé")
            self.assertEqual(row.email, second_email)
            self.assertTrue(row.active, "ERP sync must preserve local activation")
            self.assertFalse(row.erp_active)
            self.assertEqual(row.erp_status, "Inactif")
            self.assertEqual(row.erp_branch_code, "110")
            self.assertEqual(row.resource_class, "Programmation")
            self.assertEqual(row.residence_city, "Chambly")
            self.assertEqual(row.competencies, "PLC;SCADA")
            self.assertEqual(row.note, "Attribut local")
            self.assertEqual(row.sort_order, 7)

            queries = SqlPlannerQueryRepository(session)
            self.assertEqual(queries.list_resources(), ())

    def test_later_erp_sync_never_auto_activates_resource_locally(self) -> None:
        source = StubEmployeeSource(
            [employee("EMP-2", "Ressource inactive ERP", status="Inactif")]
        )
        self._sync(source)
        source.rows[0] = employee("EMP-2", "Ressource redevenue ERP active", status="Actif")
        self._sync(source)

        with self.factory() as session:
            row = session.scalar(select(Resource).where(Resource.external_id == "EMP-2"))
            assert row is not None
            self.assertTrue(row.erp_active)
            self.assertFalse(row.active)
            self.assertEqual(SqlPlannerQueryRepository(session).list_resources(), ())

    def test_admin_local_activation_plus_erp_active_makes_resource_effectively_planifiable(self) -> None:
        source = StubEmployeeSource([employee("EMP-3", "Planifiable")])
        self._sync(source)
        with transactional_session(self.factory) as session:
            row = session.scalar(select(Resource).where(Resource.external_id == "EMP-3"))
            assert row is not None
            row.active = True

        with self.factory() as session:
            resources = SqlPlannerQueryRepository(session).list_resources()
            self.assertEqual([row.external_id for row in resources], ["EMP-3"])
            self.assertTrue(resources[0].effective_active)

    def test_erp_employee_class_progression_updates_planning_subgroup_without_local_class_change(self) -> None:
        source = StubEmployeeSource(
            [employee("EMP-ELEC", "Électricien", employee_class="ELAPP4")]
        )
        self._sync(source)
        with transactional_session(self.factory) as session:
            row = session.scalar(
                select(Resource).where(Resource.external_id == "EMP-ELEC")
            )
            assert row is not None
            row.active = True
            row.resource_class = "INSTALL_ELEC"

        with self.factory() as session:
            before = SqlPlannerQueryRepository(session).list_resources()
            self.assertEqual(before[0].resource_class, "INSTALL_ELEC")
            self.assertEqual(before[0].planning_subgroup_code, "APPRENTICE")

        source.rows[0] = employee(
            "EMP-ELEC",
            "Électricien",
            employee_class="ELCOMP",
        )
        result = self._sync(source)
        self.assertEqual(result.updated, 1)

        with self.factory() as session:
            row = session.scalar(
                select(Resource).where(Resource.external_id == "EMP-ELEC")
            )
            assert row is not None
            self.assertEqual(row.resource_class, "INSTALL_ELEC")
            self.assertEqual(row.erp_employee_class, "ELCOMP")
            projected = SqlPlannerQueryRepository(session).list_resources()
            self.assertEqual(projected[0].planning_subgroup_code, "COMPANION")

    def test_partial_snapshot_preserves_missing_employee_and_local_activation(self) -> None:
        source = StubEmployeeSource(
            [
                employee("EMP-A", "Ressource A"),
                employee("EMP-B", "Ressource B"),
            ]
        )
        self._sync(source)
        with transactional_session(self.factory) as session:
            row_b = session.scalar(select(Resource).where(Resource.external_id == "EMP-B"))
            assert row_b is not None
            row_b.active = True

        source.rows = [employee("EMP-A", "Ressource A")]
        self._sync(source)

        with self.factory() as session:
            rows = {
                row.external_id: row
                for row in session.scalars(select(Resource)).all()
            }
            self.assertEqual(set(rows), {"EMP-A", "EMP-B"})
            self.assertTrue(rows["EMP-B"].active)
            self.assertTrue(rows["EMP-B"].erp_active)

    def test_sync_preserves_local_resource_coordinator(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                BusinessContact(
                    id="BC-COORD",
                    display_name="Coordonnateur local",
                    phone="555-0400",
                )
            )
            session.flush()
            session.add(
                Resource(
                    id="R-LOCAL",
                    external_id="EMP-COORD",
                    name="Ressource existante",
                    coordinator_contact_id="BC-COORD",
                    active=True,
                )
            )

        self._sync(
            StubEmployeeSource([employee("EMP-COORD", "Ressource renommée")])
        )

        with self.factory() as session:
            row = session.get(Resource, "R-LOCAL")
            assert row is not None
            self.assertEqual(row.name, "Ressource renommée")
            self.assertEqual(row.coordinator_contact_id, "BC-COORD")
            self.assertTrue(row.active)

    def test_invalid_individual_row_is_counted_without_name_based_adoption(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(Resource(id="LOCAL-1", name="Même nom", active=True))

        source = StubEmployeeSource(
            [
                employee("EMP-NEW", "Même nom"),
                employee("EMP-OK", "Autre nom"),
            ]
        )
        result = self._sync(source)

        self.assertEqual(result.received, 2)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.errors, 1)
        with self.factory() as session:
            linked = session.scalar(
                select(Resource).where(Resource.external_id == "EMP-OK")
            )
            self.assertIsNotNone(linked)
            manual = session.get(Resource, "LOCAL-1")
            assert manual is not None
            self.assertIsNone(manual.external_id)

    def test_two_distinct_external_ids_may_have_same_name_once_linked(self) -> None:
        result = self._sync(
            StubEmployeeSource(
                [
                    employee("EMP-10", "Nom partagé"),
                    employee("EMP-11", "Nom partagé"),
                ]
            )
        )
        self.assertEqual(result.created, 2)
        self.assertEqual(result.errors, 0)


if __name__ == "__main__":
    unittest.main()
