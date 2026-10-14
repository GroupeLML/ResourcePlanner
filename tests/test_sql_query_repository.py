from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
import unittest

from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlDemandRepository,
    SqlPlannerQueryRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


D1 = date(2026, 8, 24)
D2 = date(2026, 8, 25)


class SqlPlannerQueryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    Project(id="P1", number="P-1", name="Actif", status="Actif"),
                    Project(id="P2", number="P-2", name="Fermé", status="Terminé"),
                    Resource(
                        id="R1",
                        name="Alice",
                        resource_class="Programmation",
                        competencies="PLC; SCADA",
                        note="Lead",
                        active=True,
                        sort_order=20,
                    ),
                    Resource(id="R2", name="Bob", active=False, sort_order=10),
                ]
            )
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id="SCH-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    start_date=D1,
                    end_date=date(2026, 12, 31),
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    active=True,
                )
            )
            demand_number = SqlDemandRepository(session, actor_name="Jean").create(
                {
                    "NumeroProjet": "P-1",
                    "DateDebutSouhaitee": D1,
                    "DateFinSouhaitee": D2,
                    "Description": "Programmation",
                    "NombreRessources": 1,
                }
            )
            request = SqlDemandRepository(session).get(demand_number)
            assert request is not None
            from app.infrastructure.sql.models import WorkforceRequest
            request_row = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=demand_number
            ).one()
            requirement = ResourceRequirement(
                id="REQ1",
                legacy_segment_id="SEG-1",
                project_id="P1",
                workforce_request_id=request_row.id,
                assigned_resource_id="R1",
                start_date=D1,
                end_date=D2,
                planned_hours=Decimal("8"),
                status="Planifié",
                planning_type="Flexible",
                priority="Normale",
                origin="REQUEST",
            )
            session.add(requirement)
            session.flush()
            session.add_all(
                [
                    Shift(
                        id="S1",
                        legacy_allocation_id="MAN-1",
                        resource_requirement_id="REQ1",
                        resource_id="R1",
                        work_date=D1,
                        hours=Decimal("4"),
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S2",
                        legacy_allocation_id="AUTO-1",
                        resource_requirement_id="REQ1",
                        resource_id="R1",
                        work_date=D2,
                        hours=Decimal("4"),
                        source="AUTO",
                        locked=False,
                    ),
                ]
            )
            self.demand_number = demand_number

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_projects_and_resources_apply_active_filters(self) -> None:
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            self.assertEqual(
                [row.number for row in queries.list_projects(active_only=True)],
                ["P-1"],
            )
            self.assertEqual(
                [row.name for row in queries.list_resources(active_only=True)],
                ["Alice"],
            )
            alice = queries.list_resources(active_only=True)[0]
            self.assertEqual(alice.resource_class, "Programmation")
            self.assertEqual(alice.competencies, "PLC; SCADA")
            self.assertEqual(alice.note, "Lead")

    def test_schedulable_resources_follow_standard_schedule_window(self) -> None:
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    Resource(id="R3", name="Ancien", active=True, sort_order=30),
                    Resource(id="R4", name="Futur", active=True, sort_order=40),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceAvailabilityRule(
                        id="SCH-R3",
                        resource_id="R3",
                        availability_type="Horaire standard",
                        start_date=date(2026, 1, 1),
                        end_date=date(2026, 8, 23),
                        active=True,
                    ),
                    ResourceAvailabilityRule(
                        id="SCH-R4",
                        resource_id="R4",
                        availability_type="Horaire standard",
                        start_date=date(2026, 8, 31),
                        end_date=None,
                        active=True,
                    ),
                ]
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            active_master = {row.name for row in queries.list_resources(active_only=True)}
            displayed = queries.list_schedulable_resources(start=D1, end=D2)
            historic = queries.list_schedulable_resources(
                start=date(2026, 8, 17),
                end=date(2026, 8, 23),
            )

        self.assertEqual(active_master, {"Alice", "Ancien", "Futur"})
        self.assertEqual([row.name for row in displayed], ["Alice"])
        self.assertEqual([row.name for row in historic], ["Ancien"])

    @staticmethod
    def _schedule_753(resource_id: str, **overrides) -> ResourceAvailabilityRule:
        values = {
            "id": f"STD-{resource_id}",
            "resource_id": resource_id,
            "availability_type": "Horaire standard",
            "start_date": D1,
            "end_date": D2,
            "weekdays": "Lun,Mar,Mer,Jeu,Ven",
            "start_time": time(8, 0),
            "end_time": time(16, 0),
            "active": True,
        }
        values.update(overrides)
        return ResourceAvailabilityRule(**values)

    def test_753_global_planning_requires_schedule_before_class_membership(self) -> None:
        classes = {
            "R-753-E1": "INSTALL_ELEC",
            "R-753-E2": "INSTALL_ELEC",
            "R-753-E3": "INSTALL_ELEC",
            "R-753-X1": "INSTALL_EXC",
            "R-753-X2": "INSTALL_EXC",
        }
        with transactional_session(self.factory) as session:
            session.add_all(
                Resource(
                    id=identifier, name=identifier, resource_class=code,
                    active=True, erp_active=True,
                )
                for identifier, code in classes.items()
            )
            session.flush()
            session.add_all(self._schedule_753(identifier) for identifier in (
                "R-753-E1", "R-753-E2",
            ))

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            master = {row.id for row in queries.list_resources()}
            snapshot = queries.planning_snapshot(start=D1, end=D2)
            grid = queries.planning_capacity_grid(start=D1, end=D2)
        self.assertTrue(set(classes).issubset(master))
        expected = {"R1", "R-753-E1", "R-753-E2"}
        self.assertEqual({row.id for row in snapshot.resources}, expected)
        self.assertEqual({row.resource_id for row in grid.resources}, expected)
        self.assertNotIn("INSTALL_EXC", {row.resource_class for row in snapshot.resources})

        # Only configure schedules: the same IDs/classes need no Shift, login,
        # ResourceProfile or master-class edit to become globally schedulable.
        with transactional_session(self.factory) as session:
            session.add_all(self._schedule_753(identifier) for identifier in (
                "R-753-E3", "R-753-X1", "R-753-X2",
            ))
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            snapshot = queries.planning_snapshot(start=D1, end=D2)
            grid = queries.planning_capacity_grid(start=D1, end=D2)
            schedulable = queries.list_schedulable_resources(start=D1, end=D2)
        expected = {"R1", *classes}
        self.assertEqual({row.id for row in snapshot.resources}, expected)
        self.assertEqual(len(snapshot.resources), len(expected))
        self.assertEqual({row.id for row in schedulable}, expected)
        self.assertEqual({row.resource_id for row in grid.resources}, expected)
        self.assertEqual(len(grid.resources), len(expected))
        self.assertEqual(
            {row.id: row.resource_class for row in snapshot.resources if row.id in classes},
            classes,
        )
        self.assertEqual(
            len([row for row in snapshot.resources if row.resource_class == "INSTALL_ELEC"]),
            3,
        )
        for row in grid.resources:
            if row.resource_id in classes:
                self.assertEqual(row.capacity_hours, 16.0)
        self.assertEqual({row.resource_id for row in snapshot.shifts}, {"R1"})

    def test_753_global_planning_excludes_non_applicable_schedules(self) -> None:
        cases = {
            "missing": None,
            "expired": {"start_date": D1 - timedelta(days=10), "end_date": D1 - timedelta(days=1)},
            "future": {"start_date": D2 + timedelta(days=1), "end_date": None},
            "disabled": {"active": False},
            "vacation_only": {"availability_type": "Vacances"},
            "inactive_resource": {},
            "erp_inactive_resource": {},
        }
        with transactional_session(self.factory) as session:
            session.add_all(
                Resource(
                    id=f"R-753-{case}", name=f"Resource {case}",
                    resource_class="INSTALL_EXC",
                    active=case != "inactive_resource",
                    erp_active=case != "erp_inactive_resource",
                )
                for case in cases
            )
            session.flush()
            session.add_all(
                self._schedule_753(f"R-753-{case}", **overrides)
                for case, overrides in cases.items() if overrides is not None
            )
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            snapshot = queries.planning_snapshot(start=D1, end=D2)
            grid = queries.planning_capacity_grid(start=D1, end=D2)
        self.assertEqual([row.id for row in snapshot.resources], ["R1"])
        self.assertEqual([row.resource_id for row in grid.resources], ["R1"])

    def test_753_schedule_envelope_is_not_a_positive_daily_capacity_filter(self) -> None:
        # Preserve the established envelope contract: vacation/weekend capacity
        # is not a new reason to remove a resource that has an applicable schedule.
        with transactional_session(self.factory) as session:
            session.add(Resource(
                id="R-753-X", name="Scheduled excavation", resource_class="INSTALL_EXC",
                active=True, erp_active=True,
            ))
            session.flush()
            session.add(self._schedule_753("R-753-X"))
            session.add(self._schedule_753(
                "R-753-X", id="VAC-753-X", availability_type="Vacances",
            ))
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            snapshot = queries.planning_snapshot(start=D1, end=D2)
            grid = queries.planning_capacity_grid(start=D1, end=D2)
        self.assertIn("R-753-X", {row.id for row in snapshot.resources})
        capacity = next(row for row in grid.resources if row.resource_id == "R-753-X")
        self.assertEqual(capacity.capacity_hours, 0.0)
        self.assertTrue(all(day.reason == "Vacances" for day in capacity.days))

    def test_753_empty_authorized_scope_does_not_fall_back_to_global(self) -> None:
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            self.assertTrue(queries.list_schedulable_resources(start=D1, end=D2))
            snapshot = queries.planning_snapshot(start=D1, end=D2, project_ids=())
            grid = queries.planning_capacity_grid(start=D1, end=D2, project_ids=())
        self.assertEqual(snapshot.resources, ())
        self.assertEqual(snapshot.shifts, ())
        self.assertEqual(grid.resources, ())

    def test_demands_and_segments_use_canonical_read_models(self) -> None:
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            demands = queries.list_demands()
            self.assertEqual(len(demands), 1)
            self.assertEqual(demands[0].number, self.demand_number)
            self.assertEqual(demands[0].project_number, "P-1")

            segments = queries.list_segments(start=D2, end=D2)
            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0].segment_id, "SEG-1")
            self.assertEqual(segments[0].resource_name, "Alice")

            self.assertEqual(
                queries.list_segments(start=date(2026, 9, 1), end=date(2026, 9, 2)),
                (),
            )

    def test_shifts_filter_by_window_and_resource(self) -> None:
        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            rows = queries.list_shifts(start=D1, end=D1, resource_name="Alice")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].allocation_id, "MAN-1")
            self.assertEqual(rows[0].segment_id, "SEG-1")
            self.assertEqual(rows[0].requirement_id, "REQ1")
            self.assertEqual(rows[0].demand_number, self.demand_number)
            self.assertEqual(rows[0].resource_name, "Alice")
            self.assertTrue(rows[0].locked)
            self.assertEqual(rows[0].hours, 4.0)

            self.assertEqual(queries.list_shifts(resource_name="Unknown"), ())

    def test_shift_manager_marker_tracks_stable_erp_identity_not_display_name(self) -> None:
        with transactional_session(self.factory) as session:
            project = session.get(Project, "P1")
            assert project is not None
            project.project_manager_external_id = "EMP-709A"
            project.project_manager_name = "Nom historique"

        with self.factory() as session:
            shifts = SqlPlannerQueryRepository(session).list_shifts(start=D1, end=D2, include_manager_colors=True)
        self.assertEqual(len(shifts), 2)
        marker = shifts[0].project_manager_color_id
        self.assertTrue(marker)
        self.assertEqual(shifts[1].project_manager_color_id, marker)
        self.assertEqual(shifts[0].project_manager_color_label, "Nom historique")

        with transactional_session(self.factory) as session:
            project = session.get(Project, "P1")
            assert project is not None
            project.project_manager_name = "Nom renommé"
        with self.factory() as session:
            renamed = SqlPlannerQueryRepository(session).list_shifts(start=D1, end=D1, include_manager_colors=True)[0]
        self.assertEqual(renamed.project_manager_color_id, marker)
        self.assertEqual(renamed.project_manager_color_label, "Nom renommé")

    def test_shift_manager_marker_falls_back_to_canonical_co_manager(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(BusinessContact(id="BC-709A", display_name="Co chargé RP", active=True))
            session.flush()
            session.add(
                AppUser(
                    id="U-709A",
                    display_name="Co chargé RP",
                    email=None,
                    employee_external_id="EMP-CO-709A",
                    business_contact_id="BC-709A",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                )
            )
            session.flush()
            session.add(
                ProjectCoManager(
                    project_id="P1",
                    business_contact_id="BC-709A",
                    created_by_user_id="U-709A",
                )
            )
        with self.factory() as session:
            shifts = SqlPlannerQueryRepository(session).list_shifts(start=D1, end=D2, include_manager_colors=True)
        self.assertEqual(len(shifts), 2)
        self.assertTrue(shifts[0].project_manager_color_id)
        self.assertEqual(shifts[0].project_manager_color_label, "Co chargé RP")
        self.assertEqual(
            shifts[0].project_manager_color_id, shifts[1].project_manager_color_id
        )

    def test_shift_without_effective_manager_has_neutral_marker(self) -> None:
        with self.factory() as session:
            shift = SqlPlannerQueryRepository(session).list_shifts(start=D1, end=D1, include_manager_colors=True)[0]
        self.assertIsNone(shift.project_manager_color_id)
        self.assertIsNone(shift.project_manager_color_label)

    def test_planning_snapshot_is_canonical_and_window_scoped(self) -> None:
        with self.factory() as session:
            snapshot = SqlPlannerQueryRepository(session).planning_snapshot(
                start=D1,
                end=D1,
            )

            self.assertEqual(snapshot.start, D1)
            self.assertEqual(snapshot.end, D1)
            self.assertEqual([row.name for row in snapshot.resources], ["Alice"])
            self.assertEqual([row.number for row in snapshot.demands], [self.demand_number])
            self.assertEqual([row.segment_id for row in snapshot.segments], ["SEG-1"])
            self.assertEqual([row.allocation_id for row in snapshot.shifts], ["MAN-1"])


if __name__ == "__main__":
    unittest.main()
