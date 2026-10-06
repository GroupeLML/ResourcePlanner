from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event

from app.infrastructure.sql import (
    Base,
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)


class ResourceRecommendationPerformanceTests(unittest.TestCase):
    def _measure_query_count(self, directory: str, resource_count: int) -> tuple[int, int]:
        path = Path(directory) / f"recommend-{resource_count}.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)

        with factory.begin() as session:
            session.add(
                Project(
                    id="P-617B",
                    number="P-617B",
                    name="Projet recommandation",
                    status="active",
                )
            )
            session.add(
                Competency(
                    id="C-PLC",
                    name="PLC",
                    active=True,
                    sort_order=1,
                )
            )
            resources = [
                Resource(
                    id=f"R-{index:04d}",
                    name=f"Technicien {index:04d}",
                    resource_class="Programmation",
                    active=True,
                    erp_active=True,
                    sort_order=index,
                )
                for index in range(resource_count)
            ]
            session.add_all(resources)
            session.flush()
            session.add(
                TaskCatalogEntry(
                    id="T-617B",
                    project_number="P-617B",
                    task_code="310",
                    label="Programmation",
                    active=True,
                    workforce_eligible=True,
                    preferred_resource_id=resources[0].id,
                )
            )
            session.add_all(
                ResourceCompetency(resource_id=resource.id, competency_id="C-PLC")
                for resource in resources
            )
            session.add_all(
                ResourceAvailabilityRule(
                    id=f"SCH-{resource.id}",
                    resource_id=resource.id,
                    availability_type="Horaire standard",
                    start_date=date(2026, 10, 5),
                    end_date=date(2026, 10, 9),
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(7, 0),
                    end_time=time(15, 0),
                    active=True,
                )
                for resource in resources
            )
            request = WorkforceRequest(
                id="WR-617B",
                legacy_demand_number="DMO-617B",
                project_id="P-617B",
                erp_task_code="310",
                erp_task_label="Programmation",
                requester_name="Benchmark",
                priority="Normale",
                confirmation="Confirmée",
                desired_start=date(2026, 10, 5),
                desired_end=date(2026, 10, 9),
                resource_count=1,
                required_competencies="PLC",
                estimated_hours=Decimal("8"),
                status="En planification",
            )
            session.add(request)
            session.flush()
            requirement = ResourceRequirement(
                id="REQ-617B",
                legacy_segment_id="SEG-617B",
                project_id="P-617B",
                workforce_request_id=request.id,
                approved_task_catalog_item_id="T-617B",
                approval_reference_status="CAPTURED",
                start_date=date(2026, 10, 5),
                end_date=date(2026, 10, 9),
                planned_hours=Decimal("8"),
                status="À assigner",
                required_resource_class="Programmation",
                required_competency="PLC",
                required_competency_id="C-PLC",
                priority="Normale",
                confirmation="Confirmée",
                origin="REQUEST",
            )
            session.add(requirement)
            session.flush()
            session.add(
                ResourceRequirementCompetency(
                    resource_requirement_id=requirement.id,
                    competency_id="C-PLC",
                )
            )

        statements = 0

        def count_statement(*_args, **_kwargs) -> None:
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            with factory() as session:
                queries = SqlPlannerQueryRepositoryWithLoadProfiles(session)
                # Warm ORM compilation/caches before measuring the structural query count.
                queries.recommend_resources("SEG-617B")
                statements = 0
                rows = queries.recommend_resources("SEG-617B")
                measured = statements
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
            engine.dispose()

        return measured, len(rows)

    def test_query_count_does_not_scale_with_candidate_count(self) -> None:
        with TemporaryDirectory() as directory:
            small_queries, small_rows = self._measure_query_count(directory, 5)
            large_queries, large_rows = self._measure_query_count(directory, 50)

        self.assertEqual(small_rows, 5)
        self.assertEqual(large_rows, 50)
        self.assertLessEqual(
            large_queries,
            small_queries + 2,
            "Le recommender ne doit pas ajouter de requête SQL par candidat.",
        )


if __name__ == "__main__":
    unittest.main()
