from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.application.demand_planning_progress import project_demand_planning_progress
from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDemandRepository,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


DAY = date(2027, 1, 4)


class DemandPlanningProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    Project(id="P861", number="P-861", name="Projet 861", status="Actif"),
                    Resource(id="R861", name="Alice 861", active=True, erp_active=True),
                    AssetType(
                        id="AT861",
                        code="VEH-861",
                        label="Véhicule 861",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add(
                Asset(
                    id="A861",
                    code="MAT861",
                    label="Unité 861",
                    asset_type_id="AT861",
                )
            )
            self.number = SqlDemandRepository(session, actor_name="Test").create(
                {
                    "NumeroProjet": "P-861",
                    "DateDebutSouhaitee": DAY,
                    "DateFinSouhaitee": DAY,
                    "Description": "Progression 861",
                    "NombreRessources": 1,
                }
            )
            request = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=self.number
            ).one()
            request.status = "En planification"
            session.add(
                ResourceRequirement(
                    id="RR861-A",
                    project_id="P861",
                    workforce_request_id=request.id,
                    assigned_resource_id="R861",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    planning_type="Flexible",
                    priority="Normale",
                    origin="REQUEST",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def progress(self) -> str | None:
        with self.factory() as session:
            row = SqlPlannerQueryRepository(session).get_demand(self.number)
            assert row is not None
            return row.planning_progress

    def test_pure_projector_preserves_independent_component_semantics(self) -> None:
        self.assertIsNone(project_demand_planning_progress(()))
        self.assertEqual(
            project_demand_planning_progress(("uncovered", "uncovered")),
            "À planifier",
        )
        self.assertEqual(
            project_demand_planning_progress(("uncovered", "full")),
            "En planification",
        )
        self.assertEqual(
            project_demand_planning_progress(("partial", "full")),
            "En planification",
        )
        self.assertEqual(
            project_demand_planning_progress(("full", "full")),
            "Planifié",
        )

    def test_workforce_progress_uses_real_counted_shift_coverage(self) -> None:
        self.assertEqual(self.progress(), "À planifier")
        with transactional_session(self.factory) as session:
            session.add(
                Shift(
                    id="S861-4",
                    resource_requirement_id="RR861-A",
                    resource_id="R861",
                    work_date=DAY,
                    hours=Decimal("4"),
                    source="MANUAL",
                )
            )
        self.assertEqual(self.progress(), "En planification")
        with transactional_session(self.factory) as session:
            session.add(
                Shift(
                    id="S861-8",
                    resource_requirement_id="RR861-A",
                    resource_id="R861",
                    work_date=DAY,
                    hours=Decimal("4"),
                    source="MANUAL",
                )
            )
        self.assertEqual(self.progress(), "Planifié")

    def test_overallocation_cannot_compensate_for_uncovered_sibling(self) -> None:
        with transactional_session(self.factory) as session:
            request = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=self.number
            ).one()
            session.add_all(
                [
                    Shift(
                        id="S861-OVER",
                        resource_requirement_id="RR861-A",
                        resource_id="R861",
                        work_date=DAY,
                        hours=Decimal("16"),
                        source="MANUAL",
                    ),
                    ResourceRequirement(
                        id="RR861-B",
                        project_id="P861",
                        workforce_request_id=request.id,
                        assigned_resource_id="R861",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        planning_type="Flexible",
                        priority="Normale",
                        origin="REQUEST",
                    ),
                ]
            )
        self.assertEqual(self.progress(), "En planification")

    def test_confirmation_is_independent_from_planning_progress(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                Shift(
                    id="S861-FULL",
                    resource_requirement_id="RR861-A",
                    resource_id="R861",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                )
            )
            request = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=self.number
            ).one()
            request.confirmation = "Tentative"
        tentative = self.progress()
        with transactional_session(self.factory) as session:
            request = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=self.number
            ).one()
            request.confirmation = "Confirmée"
        self.assertEqual(tentative, "Planifié")
        self.assertEqual(self.progress(), tentative)

    def test_request_owned_asset_is_an_independent_component(self) -> None:
        with transactional_session(self.factory) as session:
            request = session.query(WorkforceRequest).filter_by(
                legacy_demand_number=self.number
            ).one()
            session.add_all(
                [
                    Shift(
                        id="S861-HUMAN",
                        resource_requirement_id="RR861-A",
                        resource_id="R861",
                        work_date=DAY,
                        hours=Decimal("8"),
                        source="MANUAL",
                    ),
                    RequestLine(
                        id="L861-ASSET",
                        workforce_request_id=request.id,
                        position=1,
                        kind="ASSET",
                        asset_type_id="AT861",
                        desired_start=DAY,
                        desired_end=DAY,
                    ),
                ]
            )
            session.flush()
            session.add(
                AssetRequirement(
                    id="AR861",
                    project_id="P861",
                    origin="REQUEST",
                    workforce_request_id=request.id,
                    source_request_line_id="L861-ASSET",
                    approved_entry_key="asset:861",
                    asset_type_id="AT861",
                    start_date=DAY,
                    end_date=DAY,
                    status="À affecter",
                )
            )
        self.assertEqual(self.progress(), "En planification")
        with transactional_session(self.factory) as session:
            session.add(
                AssetAllocation(
                    id="AA861",
                    asset_requirement_id="AR861",
                    asset_id="A861",
                    start_date=DAY,
                    end_date=DAY,
                )
            )
        self.assertEqual(self.progress(), "Planifié")

    def test_demand_without_planifiable_components_projects_null(self) -> None:
        with transactional_session(self.factory) as session:
            number = SqlDemandRepository(session, actor_name="Test").create(
                {
                    "NumeroProjet": "P-861",
                    "DateDebutSouhaitee": DAY,
                    "DateFinSouhaitee": DAY,
                    "Description": "Sans composante",
                    "NombreRessources": 1,
                }
            )
        with self.factory() as session:
            row = SqlPlannerQueryRepository(session).get_demand(number)
            assert row is not None
            self.assertIsNone(row.planning_progress)


if __name__ == "__main__":
    unittest.main()
