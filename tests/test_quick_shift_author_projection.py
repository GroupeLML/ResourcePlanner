from __future__ import annotations

from datetime import date, time
import unittest

from sqlalchemy import select

from app.application.commands import QuickShiftCreateCommand
from app.infrastructure.migration import (
    CutoverDataset,
    CutoverExtractionReport,
    extract_requirement_creator_names,
    import_cutover_dataset,
)
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    SqlPlannerQueryRepository,
    SqlSegmentRepository,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server.composition import build_sql_facade


DAY = date(2026, 9, 10)


class _Reader:
    def records(self, sheet: str, expected_header: str):
        if sheet != "SegmentsMO":
            return ()
        return (
            {"IDSegment": "SEG-QS", "CreePar": "  Coordonnateur  "},
            {"IDSegment": "SEG-NO-AUTHOR", "CreePar": ""},
        )


class QuickShiftAuthorProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed(self, session) -> None:
        session.add(
            Project(
                id="P1",
                number="P-1",
                name="Projet A",
                project_manager_name="Responsable A",
                status="Actif",
            )
        )
        session.add(Resource(id="R1", name="Alice", active=True))
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="STD-R1",
                resource_id="R1",
                availability_type="Horaire standard",
                weekdays="Lun,Mar,Mer,Jeu,Ven",
                start_time=time(8, 0),
                end_time=time(16, 0),
                active=True,
            )
        )
        session.flush()

    def test_sql_quick_shift_captures_server_actor_and_projects_it_to_reads(self) -> None:
        with transactional_session(self.factory) as session:
            self._seed(session)
            facade = build_sql_facade(session, actor_name="Coordonnateur")
            result = facade.create_quick_shift(
                QuickShiftCreateCommand(
                    project_number="P-1",
                    technician="Alice",
                    day=DAY,
                    hours=2,
                    outside_standard_hours=True,
                    description="Intervention urgente",
                )
            )

            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == result.segment_id
                )
            )
            assert requirement is not None
            self.assertIsNone(requirement.workforce_request_id)
            self.assertEqual(requirement.origin, "QUICK_SHIFT")
            self.assertEqual(requirement.created_by_name, "Coordonnateur")

            segment = SqlSegmentRepository(session).get(result.segment_id)
            assert segment is not None
            self.assertEqual(segment.project_manager, "Responsable A")
            self.assertEqual(segment.requester, "Coordonnateur")

            shifts = SqlPlannerQueryRepository(session).list_shifts(
                start=DAY,
                end=DAY,
            )
            self.assertEqual(len(shifts), 1)
            self.assertEqual(shifts[0].project_manager, "Responsable A")
            self.assertEqual(shifts[0].requester, "Coordonnateur")
            self.assertIsNone(shifts[0].demand_number)

    def test_legacy_segment_creator_is_preserved_through_cutover_side_channel(self) -> None:
        creators = extract_requirement_creator_names(_Reader())
        self.assertEqual(creators, {"SEG-QS": "Coordonnateur"})

        source = CutoverExtractionReport(
            dataset=CutoverDataset(
                projects=(
                    {
                        "number": "P-1",
                        "name": "Projet A",
                        "client": None,
                        "project_manager": "Responsable A",
                        "status": "Actif",
                    },
                ),
                work_packages=(),
                resources=(
                    {
                        "name": "Alice",
                        "resource_class": "Programmation",
                        "competencies": None,
                        "note": None,
                        "active": True,
                        "sort_order": 0,
                    },
                ),
                availability=(),
                demands=(),
                history=(),
                requirements=(
                    {
                        "segment_id": "SEG-QS",
                        "demand_number": None,
                        "project_number": "P-1",
                        "resource_name": "Alice",
                        "start_date": DAY,
                        "end_date": DAY,
                        "planned_hours": 2,
                        "status": "Planifié",
                        "description": "Quart rapide historique",
                        "source_effort_id": None,
                        "required_competency": None,
                        "planning_type": "Fixe",
                        "priority": "Normale",
                        "outside_standard_hours_allowed": True,
                        "origin": "QUICK_SHIFT",
                        "created_at": None,
                        "updated_at": None,
                    },
                ),
                shifts=(),
            ),
            diagnostics=(),
            counts={"projects": 1, "resources": 1, "requirements": 1},
            hours={"requirement_planned": 2.0},
        )

        with transactional_session(self.factory) as session:
            imported = import_cutover_dataset(
                session,
                source,
                requirement_creator_names=creators,
            )
            self.assertTrue(imported.ok)
            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == "SEG-QS"
                )
            )
            assert requirement is not None
            self.assertEqual(requirement.created_by_name, "Coordonnateur")


if __name__ == "__main__":
    unittest.main()
