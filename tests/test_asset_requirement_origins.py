from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mssql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex

from app.domain.approval_envelope import ApprovalEnvelope
from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    ORIGIN_AD_HOC,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
)
from app.infrastructure.sql.asset_plan import SqlAssetPlanSynchronizer


DAY = date(2026, 10, 1)


class AssetRequirementOriginTests(unittest.TestCase):
    def _engine(self, *, foreign_keys: bool = False):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        if foreign_keys:
            @event.listens_for(engine, "connect")
            def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
                dbapi_connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
        return engine

    @staticmethod
    def _requirement_values(identifier: str, **overrides):
        values = {
            "id": identifier,
            "project_id": "P-1",
            "origin": AssetRequirementOrigin.REQUEST.value,
            "shift_id": None,
            "workforce_request_id": "WR-1",
            "source_request_line_id": "RL-1",
            "source_period_id": None,
            "approval_revision_id": None,
            "approved_entry_key": "ENTRY-1",
            "slot_index": 0,
            "asset_type_id": "AT-1",
            "start_date": DAY,
            "end_date": DAY,
            "status": "À affecter",
        }
        values.update(overrides)
        return values

    def test_canonical_origin_values_are_shared(self) -> None:
        self.assertEqual(AssetRequirementOrigin.REQUEST.value, "REQUEST")
        self.assertEqual(AssetRequirementOrigin.SHIFT_AD_HOC.value, "SHIFT_AD_HOC")

    def test_request_and_shift_ad_hoc_shapes_are_mutually_exclusive(self) -> None:
        engine = self._engine()
        requirements = Base.metadata.tables["asset_requirements"]

        with engine.begin() as connection:
            connection.execute(
                requirements.insert().values(**self._requirement_values("AR-REQUEST"))
            )
            connection.execute(
                requirements.insert().values(
                    **self._requirement_values(
                        "AR-AD-HOC",
                        origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        shift_id="SHIFT-1",
                        workforce_request_id=None,
                        source_request_line_id=None,
                        approved_entry_key=None,
                    )
                )
            )

        invalid_rows = (
            self._requirement_values("AR-REQUEST-WITH-SHIFT", shift_id="SHIFT-2"),
            self._requirement_values(
                "AR-REQUEST-MISSING-LINE",
                source_request_line_id=None,
                approved_entry_key="ENTRY-2",
            ),
            self._requirement_values(
                "AR-AD-HOC-MISSING-SHIFT",
                origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                workforce_request_id=None,
                source_request_line_id=None,
                approved_entry_key=None,
            ),
            self._requirement_values(
                "AR-AD-HOC-WITH-APPROVAL",
                origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                shift_id="SHIFT-3",
                workforce_request_id=None,
                source_request_line_id=None,
                source_period_id="PERIOD-1",
                approval_revision_id="REV-1",
                approved_entry_key=None,
            ),
        )
        for values in invalid_rows:
            with self.subTest(identifier=values["id"]):
                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(requirements.insert().values(**values))

        engine.dispose()

    def test_request_and_shift_ad_hoc_uniqueness_are_independent(self) -> None:
        engine = self._engine()
        requirements = Base.metadata.tables["asset_requirements"]

        with engine.begin() as connection:
            connection.execute(
                requirements.insert().values(**self._requirement_values("AR-REQ-0"))
            )
            connection.execute(
                requirements.insert().values(
                    **self._requirement_values("AR-REQ-1", slot_index=1)
                )
            )
            connection.execute(
                requirements.insert().values(
                    **self._requirement_values(
                        "AR-SHIFT-1",
                        origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        shift_id="SHIFT-1",
                        workforce_request_id=None,
                        source_request_line_id=None,
                        approved_entry_key=None,
                    )
                )
            )
            connection.execute(
                requirements.insert().values(
                    **self._requirement_values(
                        "AR-SHIFT-2",
                        origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        shift_id="SHIFT-2",
                        workforce_request_id=None,
                        source_request_line_id=None,
                        approved_entry_key=None,
                    )
                )
            )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    requirements.insert().values(
                        **self._requirement_values("AR-REQ-DUPLICATE")
                    )
                )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    requirements.insert().values(
                        **self._requirement_values(
                            "AR-SHIFT-DUPLICATE",
                            origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                            shift_id="SHIFT-1",
                            workforce_request_id=None,
                            source_request_line_id=None,
                            approved_entry_key=None,
                        )
                    )
                )

        engine.dispose()

    def test_filtered_uniqueness_compiles_for_sqlite_and_sql_server(self) -> None:
        indexes = {
            index.name: index
            for index in Base.metadata.tables["asset_requirements"].indexes
        }
        request_index = indexes["ux_asset_requirements_request_entry_slot"]
        shift_index = indexes["ux_asset_requirements_shift_ad_hoc"]

        for dialect in (sqlite.dialect(), mssql.dialect()):
            request_ddl = str(CreateIndex(request_index).compile(dialect=dialect)).upper()
            shift_ddl = str(CreateIndex(shift_index).compile(dialect=dialect)).upper()
            self.assertIn("WHERE ORIGIN = 'REQUEST'", request_ddl)
            self.assertIn("WHERE ORIGIN = 'SHIFT_AD_HOC'", shift_ddl)

    def test_shift_fk_is_restrictive_and_allocation_still_requires_requirement(self) -> None:
        engine = self._engine(foreign_keys=True)

        with Session(engine) as session:
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.add(Resource(id="R-1", name="Technicien"))
            session.add(
                ResourceRequirement(
                    id="RR-1",
                    project_id="P-1",
                    workforce_request_id=None,
                    origin=ORIGIN_AD_HOC,
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                )
            )
            session.add(
                Shift(
                    id="SHIFT-1",
                    resource_requirement_id="RR-1",
                    resource_id="R-1",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                )
            )
            session.add(
                AssetType(
                    id="AT-1",
                    code="VEHICLE",
                    label="Véhicule",
                    category="VEHICLE",
                )
            )
            session.flush()
            session.add(
                Asset(
                    id="ASSET-1",
                    code="V-1",
                    label="Véhicule 1",
                    asset_type_id="AT-1",
                )
            )
            session.commit()

            session.add(
                AssetRequirement(
                    id="AR-BAD-SHIFT",
                    project_id="P-1",
                    origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                    shift_id="SHIFT-MISSING",
                    asset_type_id="AT-1",
                    start_date=DAY,
                    end_date=DAY,
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

            requirement = AssetRequirement(
                id="AR-1",
                project_id="P-1",
                origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                shift_id="SHIFT-1",
                asset_type_id="AT-1",
                start_date=DAY,
                end_date=DAY,
                status="Planifié",
            )
            session.add(requirement)
            session.commit()

            persisted = session.get(AssetRequirement, "AR-1")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.shift.id, "SHIFT-1")

            shift = session.get(Shift, "SHIFT-1")
            self.assertIsNotNone(shift)
            assert shift is not None
            self.assertEqual([row.id for row in shift.asset_requirements], ["AR-1"])

            session.add(
                AssetAllocation(
                    id="ALLOC-1",
                    asset_requirement_id="AR-1",
                    asset_id="ASSET-1",
                    start_date=DAY,
                    end_date=DAY,
                    source="MANUAL",
                )
            )
            session.commit()

            session.add(
                AssetAllocation(
                    id="ALLOC-NO-REQ",
                    asset_requirement_id=None,
                    asset_id="ASSET-1",
                    start_date=DAY,
                    end_date=DAY,
                    source="MANUAL",
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

            shift = session.get(Shift, "SHIFT-1")
            assert shift is not None
            session.delete(shift)
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

        allocation_fk_targets = {
            foreign_key.column.table.name
            for foreign_key in Base.metadata.tables["asset_allocations"].foreign_keys
        }
        self.assertNotIn("shifts", allocation_fk_targets)

        shift_fk = next(
            foreign_key
            for foreign_key in Base.metadata.tables["asset_requirements"].foreign_keys
            if foreign_key.parent.name == "shift_id"
        )
        self.assertEqual(shift_fk.column.table.name, "shifts")
        self.assertIsNone(shift_fk.ondelete)
        engine.dispose()

    def test_request_synchronizer_ignores_shift_ad_hoc_requirement(self) -> None:
        engine = self._engine()
        requirements = Base.metadata.tables["asset_requirements"]
        with engine.begin() as connection:
            connection.execute(
                requirements.insert().values(
                    **self._requirement_values(
                        "AR-AD-HOC",
                        origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        shift_id="SHIFT-1",
                        workforce_request_id=None,
                        source_request_line_id=None,
                        approved_entry_key=None,
                        status="Planifié",
                    )
                )
            )

        with Session(engine) as session:
            synchronizer = SqlAssetPlanSynchronizer(session)
            synchronizer.sync(
                SimpleNamespace(id="WR-OTHER"),
                ApprovalEnvelope(entries=()),
                SimpleNamespace(id="REV-OTHER"),
                selections={},
            )
            persisted = session.get(AssetRequirement, "AR-AD-HOC")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(
                persisted.origin,
                AssetRequirementOrigin.SHIFT_AD_HOC.value,
            )
            self.assertEqual(persisted.shift_id, "SHIFT-1")
            self.assertEqual(persisted.status, "Planifié")

        engine.dispose()


if __name__ == "__main__":
    unittest.main()
