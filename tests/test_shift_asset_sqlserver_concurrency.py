from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    ORIGIN_AD_HOC,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    PlanningChangeHistory,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.asset_service import SqlAssetService
from app.infrastructure.sql.idempotency import CommandIdempotencyReceipt
from app.infrastructure.sql.planning_version import (
    SqlPlanningMutationVersionRepository,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 10, 5)


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 560B réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class ShiftAssetSqlServerConcurrencyTests(unittest.TestCase):
    def test_same_planning_version_allows_only_one_shift_asset_mutation(self) -> None:
        marker = uuid4().hex[:12]
        project_id = f"P560B-{marker}"
        resource_id = f"R560B-{marker}"
        human_requirement_id = f"HR560B-{marker}"
        shift_id = f"S560B-{marker}"
        asset_type_id = f"AT560B-{marker}"
        asset_a_id = f"AA560B-{marker}"
        asset_b_id = f"AB560B-{marker}"
        actor = f"560b-sql-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        requirement_ids: tuple[str, ...] = ()
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_id,
                            number=f"P-{marker}",
                            name="560B SQL Server",
                        ),
                        Resource(
                            id=resource_id,
                            name=f"Ressource 560B {marker}",
                            active=True,
                        ),
                        AssetType(
                            id=asset_type_id,
                            code=f"T-{marker}",
                            label="Type 560B",
                            category="EQUIPMENT",
                        ),
                    ]
                )
                session.flush()
                session.add_all(
                    [
                        Asset(
                            id=asset_a_id,
                            code=f"A-{marker}",
                            label="Actif A",
                            asset_type_id=asset_type_id,
                        ),
                        Asset(
                            id=asset_b_id,
                            code=f"B-{marker}",
                            label="Actif B",
                            asset_type_id=asset_type_id,
                        ),
                        ResourceRequirement(
                            id=human_requirement_id,
                            project_id=project_id,
                            workforce_request_id=None,
                            origin=ORIGIN_AD_HOC,
                            start_date=DAY,
                            end_date=DAY,
                            planned_hours=Decimal("8"),
                            status="Planifié",
                        ),
                    ]
                )
                session.flush()
                session.add(
                    Shift(
                        id=shift_id,
                        resource_requirement_id=human_requirement_id,
                        resource_id=resource_id,
                        work_date=DAY,
                        hours=Decimal("8"),
                        source="AUTO",
                        locked=False,
                    )
                )

            with factory() as session:
                expected_version = SqlPlanningMutationVersionRepository(
                    session
                ).current_version()

            def mutate(asset_id: str, key: str) -> tuple[str, str | None]:
                with factory.begin() as session:
                    try:
                        result = SqlAssetService(
                            session,
                            actor=actor,
                        ).set_shift_asset(
                            shift_id=shift_id,
                            asset_id=asset_id,
                            requirement_id=None,
                            start_date=None,
                            end_date=None,
                            expected_version=expected_version,
                            idempotency_key=key,
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["asset_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(mutate, asset_a_id, f"a-{marker}")
                second = executor.submit(mutate, asset_b_id, f"b-{marker}")
                outcomes = (first.result(timeout=30), second.result(timeout=30))

            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            conflict = next(row for row in outcomes if row[0] == "conflict")
            self.assertEqual(conflict[1], "planning_version_conflict")

            with factory() as session:
                requirements = tuple(
                    session.scalars(
                        select(AssetRequirement).where(
                            AssetRequirement.origin
                            == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                            AssetRequirement.shift_id == shift_id,
                        )
                    ).all()
                )
                self.assertEqual(len(requirements), 1)
                requirement_ids = tuple(row.id for row in requirements)
                allocations = tuple(
                    session.scalars(
                        select(AssetAllocation).where(
                            AssetAllocation.asset_requirement_id.in_(
                                requirement_ids
                            )
                        )
                    ).all()
                )
                self.assertEqual(len(allocations), 1)
                self.assertIn(
                    allocations[0].asset_id,
                    {asset_a_id, asset_b_id},
                )
                self.assertEqual(
                    allocations[0].operator_resource_id,
                    resource_id,
                )
                shift = session.get(Shift, shift_id)
                self.assertIsNotNone(shift)
                self.assertEqual(shift.source, "MANUAL")
                self.assertTrue(shift.locked)
        finally:
            with factory.begin() as session:
                persisted_requirement_ids = tuple(
                    session.scalars(
                        select(AssetRequirement.id).where(
                            AssetRequirement.origin
                            == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                            AssetRequirement.shift_id == shift_id,
                        )
                    ).all()
                )
                if persisted_requirement_ids:
                    session.execute(
                        delete(AssetAllocation).where(
                            AssetAllocation.asset_requirement_id.in_(
                                persisted_requirement_ids
                            )
                        )
                    )
                    session.execute(
                        delete(AssetRequirement).where(
                            AssetRequirement.id.in_(
                                persisted_requirement_ids
                            )
                        )
                    )
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.actor_name == actor
                    )
                )
                session.execute(
                    delete(CommandIdempotencyReceipt).where(
                        CommandIdempotencyReceipt.actor_name == actor
                    )
                )
                session.execute(delete(Shift).where(Shift.id == shift_id))
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id == human_requirement_id
                    )
                )
                session.execute(
                    delete(Asset).where(
                        Asset.id.in_((asset_a_id, asset_b_id))
                    )
                )
                session.execute(
                    delete(AssetType).where(AssetType.id == asset_type_id)
                )
                session.execute(
                    delete(Resource).where(Resource.id == resource_id)
                )
                session.execute(
                    delete(Project).where(Project.id == project_id)
                )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
