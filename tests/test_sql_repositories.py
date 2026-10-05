from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
import unittest

from sqlalchemy import func, select

from app.application.demand_service import DemandService
from app.application.segment_service import SegmentService
from app.infrastructure.sql import (
    Base,
    ORIGIN_QUICK_SHIFT,
    Project,
    RequestLine,
    Resource,
    ResourceClassConfig,
    ResourceRequirement,
    SqlDemandRepository,
    SqlSegmentRepository,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


ROOT = Path(__file__).resolve().parents[1]
SQL_INFRA = ROOT / "app" / "infrastructure" / "sql"


class _Planning:
    def __init__(self) -> None:
        self.calls = 0

    def rebuild(self):
        self.calls += 1
        return {"allocated_hours": 8.0, "unallocated_hours": 0.0}


class _ApprovedSync:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sync_approved(self, number: str) -> None:
        self.calls.append(number)


class SqlRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    Project(id="P1", number="P-1", name="Projet 1", client="Client 1"),
                    Project(id="P2", number="P-2", name="Projet 2", client="Client 2"),
                    Resource(id="R1", name="Alice", resource_class="Programmation"),
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        active=True,
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_transactional_session_rolls_back_the_whole_unit_of_work(self) -> None:
        with self.assertRaises(RuntimeError):
            with transactional_session(self.factory) as session:
                session.add(Project(id="ROLLBACK", number="P-X", name="Rollback"))
                session.flush()
                raise RuntimeError("force rollback")

        with self.factory() as session:
            self.assertIsNone(session.get(Project, "ROLLBACK"))

    def test_demand_service_runs_against_sql_repository(self) -> None:
        planning = _Planning()
        sync = _ApprovedSync()
        year = date.today().year

        with transactional_session(self.factory) as session:
            repository = SqlDemandRepository(session, actor_name="Jean")
            service = DemandService(
                repository,
                planning,
                sync,
                current_user="Jean",
            )

            number = service.create(
                {
                    "NumeroProjet": "P-1",
                    "DateDebutSouhaitee": date(2026, 8, 26),
                    "DateFinSouhaitee": date(2026, 8, 28),
                    "Description": "Installation",
                    "NombreRessources": 1,
                    "TempsEstimeHeures": 8,
                    "TechnicienPropose": "Alice",
                    "RequiredResourceClass": "PROGRAMMEUR",
                },
                submit=True,
            )
            self.assertEqual(number, f"DMO-{year}-0001")

            created = repository.get(number)
            self.assertIsNotNone(created)
            assert created is not None
            self.assertEqual(created.status, "Soumise")
            self.assertEqual(created.project_number, "P-1")
            self.assertEqual(created.project_name, "Projet 1")
            self.assertEqual(created.client, "Client 1")
            self.assertEqual(created.requester, "Jean")
            self.assertEqual(created.lines[0].required_resource_class, "PROGRAMMEUR")
            self.assertEqual(
                created.lines[0].required_resource_class_label,
                "Programmeur",
            )

            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            assert request is not None
            line = session.get(RequestLine, request.id)
            self.assertIsNotNone(line)
            assert line is not None
            self.assertEqual(line.workforce_request_id, request.id)
            self.assertEqual(line.position, 0)
            self.assertEqual(line.kind, "WORKFORCE")
            self.assertEqual(line.slot_count, 1)
            self.assertEqual(line.desired_start, date(2026, 8, 26))
            self.assertEqual(line.desired_end, date(2026, 8, 28))
            self.assertEqual(line.proposed_resource_id, "R1")
            self.assertEqual(line.description, "Installation")

            summary = service.approve(number, "OK")
            self.assertEqual(summary["allocated_hours"], 8.0)
            self.assertEqual(sync.calls, [number])
            self.assertEqual(planning.calls, 1)

            reapproval = service.modify(number, {"Description": "Installation modifiée"})
            self.assertTrue(reapproval)
            updated = repository.get(number)
            assert updated is not None
            self.assertEqual(updated.status, "Soumise")
            self.assertEqual(updated.description, "Installation modifiée")
            assert line is not None
            self.assertEqual(line.description, "Installation modifiée")

            history_count = session.scalar(
                select(func.count()).select_from(WorkforceRequestHistory)
            )
            self.assertGreaterEqual(int(history_count or 0), 3)
            latest_history = session.scalars(
                select(WorkforceRequestHistory)
                .where(WorkforceRequestHistory.workforce_request_id == request.id)
                .order_by(
                    WorkforceRequestHistory.occurred_at.desc(),
                    WorkforceRequestHistory.id.desc(),
                )
            ).first()
            assert latest_history is not None
            self.assertEqual(latest_history.action, "Modification")
            self.assertEqual(latest_history.previous_status, "En planification")
            self.assertEqual(latest_history.status, "Soumise")
            audit = json.loads(latest_history.details or "{}")
            self.assertEqual(audit["aggregate_version"], 3)
            self.assertFalse(audit["line_mode"])
            self.assertIn("Description", audit["changed_fields"])
            self.assertIn("Statut", audit["changed_fields"])

        with self.factory() as session:
            persisted = SqlDemandRepository(session).get(f"DMO-{year}-0001")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "Soumise")

    def test_segment_service_links_request_project_and_resource(self) -> None:
        planning = _Planning()
        with transactional_session(self.factory) as session:
            demand_repository = SqlDemandRepository(session, actor_name="Jean")
            demand_number = demand_repository.create(
                {
                    "NumeroProjet": "P-1",
                    "DateDebutSouhaitee": date(2026, 8, 26),
                    "NombreRessources": 1,
                },
                submit=True,
            )

            segment_repository = SqlSegmentRepository(session)
            service = SegmentService(segment_repository, planning)
            segment_id, summary = service.create(
                {
                    "NoDemande": demand_number,
                    "NumeroProjet": "P-1",
                    "Technicien": "Alice",
                    "DateDebut": date(2026, 8, 26),
                    "DateFin": date(2026, 8, 27),
                    "HeuresPrevues": 8,
                    "CompetenceRequise": "Programmation",
                }
            )

            self.assertTrue(segment_id.startswith(f"SEG-{date.today().year}-"))
            self.assertEqual(summary["allocated_hours"], 8.0)
            segment = segment_repository.get(segment_id)
            self.assertIsNotNone(segment)
            assert segment is not None
            self.assertEqual(segment.demand_number, demand_number)
            self.assertEqual(segment.project_number, "P-1")
            self.assertEqual(segment.resource_name, "Alice")
            self.assertEqual(segment.required_competency, "Programmation")

            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == segment_id
                )
            )
            assert requirement is not None
            request = session.get(WorkforceRequest, requirement.workforce_request_id)
            assert request is not None
            self.assertEqual(requirement.project_id, request.project_id)
            self.assertEqual(requirement.assigned_resource_id, "R1")
            self.assertEqual(requirement.source_request_line_id, request.id)

            service.cancel(segment_id)
            self.assertEqual(segment_repository.list(include_cancelled=False), ())
            self.assertEqual(planning.calls, 2)

    def test_quick_shift_requirement_does_not_create_fake_request(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlSegmentRepository(session)
            segment_id = repository.create(
                {
                    "NoDemande": None,
                    "NumeroProjet": "P-1",
                    "Technicien": "Alice",
                    "DateDebut": date(2026, 8, 26),
                    "DateFin": date(2026, 8, 26),
                    "HeuresPrevues": 4,
                    "Statut": "Planifié",
                    "TypePlanification": "Fixe",
                    "OrigineSegment": ORIGIN_QUICK_SHIFT,
                }
            )

            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == segment_id
                )
            )
            assert requirement is not None
            self.assertIsNone(requirement.workforce_request_id)
            self.assertIsNone(requirement.source_request_line_id)
            self.assertEqual(requirement.origin, ORIGIN_QUICK_SHIFT)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(WorkforceRequest)),
                0,
            )

    def test_project_mismatch_is_rejected_before_segment_flush(self) -> None:
        with transactional_session(self.factory) as session:
            demands = SqlDemandRepository(session)
            number = demands.create(
                {
                    "NumeroProjet": "P-1",
                    "DateDebutSouhaitee": date(2026, 8, 26),
                    "NombreRessources": 1,
                }
            )
            segments = SqlSegmentRepository(session)
            with self.assertRaises(ValueError):
                segments.create(
                    {
                        "NoDemande": number,
                        "NumeroProjet": "P-2",
                        "DateDebut": date(2026, 8, 26),
                        "DateFin": date(2026, 8, 26),
                        "HeuresPrevues": 2,
                    }
                )

    def test_sql_repositories_do_not_commit_or_import_legacy_runtime(self) -> None:
        for filename in ("demand_repository.py", "segment_repository.py"):
            source = (SQL_INFRA / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")

            self.assertNotIn(".commit(", source)
            self.assertNotIn(".rollback(", source)
            for forbidden in (
                "xlwings",
                "nicegui",
                "excel_repository",
                "app.v13",
                "app.v14",
                "app.v15",
                "app.v16",
                "app.v17",
                "app.v18",
            ):
                self.assertFalse(
                    any(forbidden in module for module in imports),
                    f"{filename} leaks legacy dependency: {forbidden}",
                )


if __name__ == "__main__":
    unittest.main()
