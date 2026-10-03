from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.errors import ApplicationConflictError
from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
    Base,
    Competency,
    Project,
    Resource,
    ResourceCompetency,
    ResourceRequirement,
    SqlSegmentRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


DAY = date(2026, 10, 13)


class SegmentAssetReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "segment-assets.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        Base.metadata.create_all(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    Project(id="P1", number="P-575D", name="Projet 575D"),
                    Project(id="P2", number="P-OTHER", name="Autre projet"),
                    Resource(id="OP-1", name="Opérateur 1", active=True),
                    Resource(id="OP-2", name="Opérateur 2", active=True),
                    Resource(id="OP-NO", name="Sans permis", active=True),
                    Competency(id="COMP-575D", name="Permis 575D", active=True),
                    AssetType(
                        id="TYPE-575D",
                        code="VEH-575D",
                        label="Véhicule 575D",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceCompetency(
                        resource_id="OP-1",
                        competency_id="COMP-575D",
                    ),
                    ResourceCompetency(
                        resource_id="OP-2",
                        competency_id="COMP-575D",
                    ),
                    AssetTypeCompetency(
                        asset_type_id="TYPE-575D",
                        competency_id="COMP-575D",
                    ),
                    Asset(
                        id="ASSET-A",
                        code="A-575D",
                        label="Actif A",
                        asset_type_id="TYPE-575D",
                    ),
                    Asset(
                        id="ASSET-B",
                        code="B-575D",
                        label="Actif B",
                        asset_type_id="TYPE-575D",
                    ),
                    WorkforceRequest(
                        id="D-575D",
                        legacy_demand_number="DMO-575D",
                        project_id="P1",
                        status="En planification",
                        aggregate_version=1,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ-575D",
                        legacy_segment_id="SEG-575D",
                        project_id="P1",
                        workforce_request_id="D-575D",
                        assigned_resource_id="OP-2",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=2),
                        planned_hours=24,
                        status="Planifié",
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ-575D-STANDALONE",
                        legacy_segment_id="SEG-575D-STANDALONE",
                        project_id="P1",
                        workforce_request_id=None,
                        assigned_resource_id="OP-2",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=2),
                        planned_hours=24,
                        status="Planifié",
                        origin="AD_HOC",
                    ),
                ]
            )
        engine.dispose()

        self.client = TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def _version(self) -> int:
        response = self.client.get("/api/v1/assets/requirements")
        self.assertEqual(response.status_code, 200, response.text)
        return int(response.json()["planning_version"])

    def _create(
        self,
        *,
        asset_id: str = "ASSET-A",
        operator_resource_id: str = "OP-1",
        start_date: date = DAY + timedelta(days=1),
        end_date: date = DAY + timedelta(days=1),
        version: int | None = None,
        key: str = "segment-create-575d",
    ):
        return self.client.post(
            "/api/v1/assets/segment-reservations",
            headers={"Idempotency-Key": key},
            json={
                "segment_id": "SEG-575D",
                "asset_type_id": "TYPE-575D",
                "asset_id": asset_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "operator_resource_id": operator_resource_id,
                "expected_planning_version": version or self._version(),
            },
        )

    def test_segment_reservation_uses_explicit_operator_and_supports_multiple_assets(self) -> None:
        version = self._version()
        first = self._create(version=version)
        self.assertEqual(first.status_code, 201, first.text)
        body = first.json()
        self.assertEqual(body["requirement_origin"], "SEGMENT")
        self.assertEqual(body["resource_requirement_id"], "REQ-575D")
        self.assertEqual(body["project_id"], "P1")
        self.assertEqual(body["operator_resource_id"], "OP-1")
        self.assertEqual(body["qualification_state"], "SATISFIED")
        self.assertEqual(body["planning_version"], version + 1)

        replay = self._create(version=version)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), body)
        self.assertEqual(self._version(), body["planning_version"])

        candidates = self.client.get(
            f"/api/v1/assets/requirements/{body['requirement_id']}/operator-candidates"
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(
            [row["resource_id"] for row in candidates.json()["candidates"]],
            ["OP-1", "OP-2"],
        )

        second = self._create(
            asset_id="ASSET-B",
            operator_resource_id="OP-2",
            version=body["planning_version"],
            key="segment-second-575d",
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(
            second.json()["requirement_id"],
            body["requirement_id"],
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                requirement = session.get(
                    AssetRequirement,
                    body["requirement_id"],
                )
                self.assertIsNotNone(requirement)
                assert requirement is not None
                self.assertEqual(
                    requirement.origin,
                    AssetRequirementOrigin.SEGMENT.value,
                )
                self.assertEqual(
                    requirement.resource_requirement_id,
                    "REQ-575D",
                )
                self.assertIsNone(requirement.workforce_request_id)
                allocation = session.scalar(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id == requirement.id
                    )
                )
                self.assertIsNotNone(allocation)
                assert allocation is not None
                # The segment target is OP-2, but the reservation explicitly
                # keeps OP-1. No operator is inferred from assigned_resource_id.
                self.assertEqual(allocation.operator_resource_id, "OP-1")
        finally:
            engine.dispose()

    def test_segment_reservation_rejects_invalid_operator_and_outside_window(self) -> None:
        bad_operator = self._create(
            operator_resource_id="OP-NO",
            key="segment-bad-operator-575d",
        )
        self.assertEqual(bad_operator.status_code, 422, bad_operator.text)
        self.assertEqual(
            bad_operator.json()["error"]["code"],
            "asset_operator_skill_mismatch",
        )

        outside = self._create(
            start_date=DAY - timedelta(days=1),
            end_date=DAY,
            key="segment-outside-575d",
        )
        self.assertEqual(outside.status_code, 422, outside.text)
        self.assertEqual(
            outside.json()["error"]["code"],
            "asset_outside_segment_window",
        )

    def test_segment_lifecycle_preserves_compatible_changes_and_blocks_shrink_or_project_change(self) -> None:
        created = self._create()
        self.assertEqual(created.status_code, 201, created.text)
        requirement_id = created.json()["requirement_id"]

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                repository = SqlSegmentRepository(session)
                repository.update(
                    "SEG-575D",
                    {
                        "Technicien": "Opérateur 1",
                        "DateDebut": DAY,
                        "DateFin": DAY + timedelta(days=3),
                    },
                )
            with factory() as session:
                segment = session.get(ResourceRequirement, "REQ-575D")
                allocation = session.scalar(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id == requirement_id
                    )
                )
                self.assertIsNotNone(segment)
                self.assertIsNotNone(allocation)
                assert segment is not None and allocation is not None
                self.assertEqual(segment.assigned_resource_id, "OP-1")
                self.assertEqual(allocation.operator_resource_id, "OP-1")
                self.assertEqual(
                    allocation.start_date,
                    DAY + timedelta(days=1),
                )
                self.assertEqual(
                    allocation.end_date,
                    DAY + timedelta(days=1),
                )

            with self.assertRaises(ApplicationConflictError) as shrink:
                with factory.begin() as session:
                    SqlSegmentRepository(session).update(
                        "SEG-575D",
                        {"DateFin": DAY},
                    )
            self.assertEqual(
                shrink.exception.code,
                "segment_asset_reservation_conflict",
            )

            with factory.begin() as session:
                standalone_requirement = AssetRequirement(
                    id="AREQ-575D-STANDALONE",
                    project_id="P1",
                    origin=AssetRequirementOrigin.SEGMENT.value,
                    resource_requirement_id="REQ-575D-STANDALONE",
                    asset_type_id="TYPE-575D",
                    start_date=DAY + timedelta(days=1),
                    end_date=DAY + timedelta(days=1),
                    status="Planifié",
                )
                session.add(standalone_requirement)
                session.flush()
                session.add(
                    AssetAllocation(
                        id="ALLOC-575D-STANDALONE",
                        asset_requirement_id=standalone_requirement.id,
                        asset_id="ASSET-B",
                        operator_resource_id="OP-1",
                        start_date=DAY + timedelta(days=1),
                        end_date=DAY + timedelta(days=1),
                        locked=True,
                        source="MANUAL",
                    )
                )

            with self.assertRaises(ApplicationConflictError) as project:
                with factory.begin() as session:
                    SqlSegmentRepository(session).update(
                        "SEG-575D-STANDALONE",
                        {"NumeroProjet": "P-OTHER"},
                    )
            self.assertEqual(
                project.exception.code,
                "segment_asset_reservation_conflict",
            )
        finally:
            engine.dispose()

    def test_segment_update_release_and_global_double_booking_share_planning_cas(self) -> None:
        created = self._create()
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()

        conflict = self.client.post(
            "/api/v1/assets/project-reservations",
            headers={"Idempotency-Key": "segment-double-book-575d"},
            json={
                "project_id": "P1",
                "asset_type_id": "TYPE-575D",
                "asset_id": "ASSET-A",
                "start_date": (DAY + timedelta(days=1)).isoformat(),
                "end_date": (DAY + timedelta(days=1)).isoformat(),
                "operator_resource_id": "OP-1",
                "expected_planning_version": body["planning_version"],
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "asset_double_booking")
        self.assertEqual(self._version(), body["planning_version"])

        updated = self.client.put(
            f"/api/v1/assets/segment-reservations/{body['requirement_id']}",
            headers={"Idempotency-Key": "segment-update-575d"},
            json={
                "asset_id": "ASSET-A",
                "start_date": DAY.isoformat(),
                "end_date": (DAY + timedelta(days=1)).isoformat(),
                "operator_resource_id": "OP-2",
                "expected_planning_version": body["planning_version"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["operator_resource_id"], "OP-2")

        released = self.client.delete(
            f"/api/v1/assets/segment-reservations/{body['requirement_id']}",
            params={
                "expected_planning_version": updated.json()[
                    "planning_version"
                ]
            },
            headers={"Idempotency-Key": "segment-release-575d"},
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["operation"], "RELEASE")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                self.assertIsNone(
                    session.get(AssetRequirement, body["requirement_id"])
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
