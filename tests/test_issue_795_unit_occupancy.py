from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.infrastructure.sql import Base, Resource, create_session_factory, create_sql_engine
from app.infrastructure.sql.asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetUnavailability,
)
from app.infrastructure.sql.asset_query import SqlAssetPlanningQuery


WEEK_START = date(2026, 11, 9)
WEEK_END = date(2026, 11, 15)


class Issue795UnitOccupancyProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "issue-795-unit-occupancy.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        self.factory = create_session_factory(engine)
        with self.factory.begin() as session:
            session.add(
                Resource(
                    id="RESOURCE-795",
                    name="Ressource 795",
                    active=True,
                    sort_order=1,
                )
            )
            session.add(
                AssetType(
                    id="TYPE-795",
                    code="VEH-795",
                    label="Véhicule 795",
                    category="VEHICLE",
                    active=True,
                )
            )
            for asset_id in (
                "ASSET-ORDINARY",
                "ASSET-BOUNDED",
                "ASSET-ACTIVE",
                "ASSET-SCHEDULED",
                "ASSET-ENDED",
                "ASSET-REVOKED",
                "ASSET-TRANSFER",
                "ASSET-MAINTENANCE",
            ):
                session.add(
                    Asset(
                        id=asset_id,
                        code=asset_id,
                        label=asset_id,
                        asset_type_id="TYPE-795",
                        active=True,
                    )
                )

            self._add_period(
                session,
                suffix="BOUNDED",
                asset_id="ASSET-BOUNDED",
                start=date(2026, 11, 10),
                end=date(2026, 11, 12),
                mode="BOUNDED",
            )
            self._add_period(
                session,
                suffix="ACTIVE",
                asset_id="ASSET-ACTIVE",
                start=date(2026, 11, 1),
                end=None,
            )
            self._add_period(
                session,
                suffix="SCHEDULED",
                asset_id="ASSET-SCHEDULED",
                start=date(2026, 11, 16),
                end=None,
            )
            self._add_period(
                session,
                suffix="ENDED",
                asset_id="ASSET-ENDED",
                start=date(2026, 10, 1),
                end=date(2026, 11, 8),
            )
            self._add_period(
                session,
                suffix="REVOKED",
                asset_id="ASSET-REVOKED",
                start=date(2026, 10, 1),
                end=date(2026, 11, 11),
            )
            self._add_period(
                session,
                suffix="TRANSFER-OLD",
                asset_id="ASSET-TRANSFER",
                start=date(2026, 10, 1),
                end=date(2026, 11, 11),
            )
            self._add_period(
                session,
                suffix="TRANSFER-NEW",
                asset_id="ASSET-TRANSFER",
                start=date(2026, 11, 12),
                end=None,
            )
            self._add_period(
                session,
                suffix="MAINTENANCE",
                asset_id="ASSET-MAINTENANCE",
                start=date(2026, 10, 1),
                end=None,
            )
            session.add(
                AssetUnavailability(
                    id="UNAVAILABLE-795",
                    asset_id="ASSET-MAINTENANCE",
                    start_date=date(2026, 11, 10),
                    end_date=date(2026, 11, 11),
                    reason="Réparation 795",
                )
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _add_period(
        session,
        *,
        suffix: str,
        asset_id: str,
        start: date,
        end: date | None,
        mode: str = "CONTINUOUS",
    ) -> None:
        requirement_id = f"REQ-{suffix}"
        session.add(
            AssetRequirement(
                id=requirement_id,
                project_id=None,
                origin="RESOURCE_PERIOD",
                context_resource_id="RESOURCE-795",
                asset_type_id="TYPE-795",
                start_date=start,
                end_date=end,
                resource_period_mode=mode,
                status="Réservé",
            )
        )
        session.add(
            AssetAllocation(
                id=f"ALLOC-{suffix}",
                asset_requirement_id=requirement_id,
                asset_id=asset_id,
                operator_resource_id="RESOURCE-795",
                start_date=start,
                end_date=end,
                locked=True,
                source="MANUAL",
            )
        )

    def _window(self, start: date, end: date):
        with self.factory() as session:
            return SqlAssetPlanningQuery(session).planning_window(
                start=start,
                end=end,
                project_ids=(),
                demand_ids=(),
                context_resource_ids=(),
            )

    @staticmethod
    def _visibility(window) -> dict[str, bool]:
        return {
            asset.id: asset.visible_in_unit_occupancy
            for asset in window.assets
        }

    def test_week_visibility_uses_canonical_continuous_allocation_overlap(self) -> None:
        window = self._window(WEEK_START, WEEK_END)
        visibility = self._visibility(window)

        self.assertTrue(visibility["ASSET-ORDINARY"])
        self.assertTrue(visibility["ASSET-BOUNDED"])
        self.assertFalse(visibility["ASSET-ACTIVE"])
        self.assertTrue(visibility["ASSET-SCHEDULED"])
        self.assertTrue(visibility["ASSET-ENDED"])
        self.assertFalse(visibility["ASSET-REVOKED"])
        self.assertFalse(visibility["ASSET-TRANSFER"])
        self.assertFalse(visibility["ASSET-MAINTENANCE"])

    def test_scheduled_continuous_unit_hides_only_when_viewed_window_reaches_start(self) -> None:
        before = self._visibility(self._window(WEEK_START, WEEK_END))
        effective = self._visibility(
            self._window(date(2026, 11, 16), date(2026, 11, 22))
        )

        self.assertTrue(before["ASSET-SCHEDULED"])
        self.assertFalse(effective["ASSET-SCHEDULED"])

    def test_ended_and_effectively_revoked_units_reappear_after_last_applicable_day(self) -> None:
        current = self._visibility(self._window(WEEK_START, WEEK_END))
        after_revocation = self._visibility(
            self._window(date(2026, 11, 12), date(2026, 11, 18))
        )

        self.assertTrue(current["ASSET-ENDED"])
        self.assertFalse(current["ASSET-REVOKED"])
        self.assertTrue(after_revocation["ASSET-REVOKED"])

    def test_scheduled_transfer_has_no_visibility_gap_at_effective_boundary(self) -> None:
        before_boundary = self._visibility(
            self._window(date(2026, 11, 9), date(2026, 11, 11))
        )
        after_boundary = self._visibility(
            self._window(date(2026, 11, 12), date(2026, 11, 15))
        )
        spanning_boundary = self._visibility(
            self._window(date(2026, 11, 9), date(2026, 11, 15))
        )

        self.assertFalse(before_boundary["ASSET-TRANSFER"])
        self.assertFalse(after_boundary["ASSET-TRANSFER"])
        self.assertFalse(spanning_boundary["ASSET-TRANSFER"])

    def test_maintenance_does_not_make_continuously_assigned_unit_visible(self) -> None:
        window = self._window(WEEK_START, WEEK_END)
        visibility = self._visibility(window)
        maintenance_cells = [
            row for row in window.capacity
            if row.asset_id == "ASSET-MAINTENANCE"
            and row.day in {date(2026, 11, 10), date(2026, 11, 11)}
        ]

        self.assertFalse(visibility["ASSET-MAINTENANCE"])
        self.assertTrue(maintenance_cells)
        self.assertTrue(all(row.unavailable for row in maintenance_cells))

    def test_scope_redaction_stays_separate_from_global_continuous_visibility(self) -> None:
        window = self._window(WEEK_START, WEEK_END)

        self.assertEqual(window.requirements, ())
        self.assertEqual(window.allocations, ())
        self.assertFalse(
            self._visibility(window)["ASSET-ACTIVE"]
        )


if __name__ == "__main__":
    unittest.main()
