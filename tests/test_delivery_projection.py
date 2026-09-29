from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.domain.approval_envelope import (
    ApprovalEnvelope,
    ApprovalEnvelopeEntry,
    EnvelopeEntryIdentity,
)
from app.infrastructure.sql import (
    APPROVAL_REFERENCE_CAPTURED,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    DeliveryItemRow,
    DeliveryPlanRow,
    PlanningMutationState,
    Project,
    RequestApprovalReference,
    RequestApprovalRevision,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDeliveryPlanningReadRepository,
    WorkPackage,
    WorkforceRequest,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


D1 = date(2026, 10, 5)
D2 = date(2026, 10, 6)
D3 = date(2026, 10, 7)


def _entry(
    line_id: str,
    *,
    kind: str,
    work_package_id: str,
    start: date,
    end: date,
    hours: Decimal | None,
    asset_type_id: str | None = None,
) -> ApprovalEnvelopeEntry:
    return ApprovalEnvelopeEntry(
        identity=EnvelopeEntryIdentity(line_id=line_id),
        project_id="P-1",
        site_id=None,
        location=None,
        slot_count=1,
        line_kind=kind,
        required_resource_class=None,
        competency_ids=(),
        task_ref=None,
        work_package_ref=work_package_id,
        start_date=start,
        end_date=end,
        hours=hours,
        kind="BASE",
        group=None,
        source_period_id=None,
        confirmation="Confirmée",
        selected=True,
        proposed_resource_id=None,
        desired_active_days=None,
        asset_type_id=asset_type_id,
        occupancy_policy=("EXCLUSIVE_DAY" if asset_type_id else None),
        proposed_asset_id=None,
    )


class DeliveryProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "delivery-projection.db"
        self.app = create_api_app(
            f"sqlite+pysqlite:///{self.database_path.as_posix()}",
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
        )
        self.factory = self.app.state.session_factory
        Base.metadata.create_all(self.factory.kw["bind"])
        self._seed()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _seed(self) -> None:
        human = _entry(
            "LINE-W",
            kind="WORKFORCE",
            work_package_id="WP-1",
            start=D1,
            end=D2,
            hours=Decimal("8"),
        )
        asset = _entry(
            "LINE-A",
            kind="ASSET",
            work_package_id="WP-1",
            start=D2,
            end=D3,
            hours=None,
            asset_type_id="AT-1",
        )
        envelope = ApprovalEnvelope(entries=(human, asset))
        authorization = envelope.to_snapshot_payload()
        revision_payload = {
            "format_version": authorization["format_version"],
            "request": {
                "request_id": "D-1",
                "request_version": 3,
                "project_id": "P-1",
            },
            "authorization": authorization,
        }

        with self.factory.begin() as session:
            # Keep the fixture ordering explicit because these SQL models deliberately
            # avoid ORM relationships. SQLite foreign-key checks therefore need each
            # parent layer persisted before its dependent layer is flushed.
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.flush()

            session.add_all(
                [
                    WorkPackage(
                        id="WP-1",
                        project_id="P-1",
                        code="WP-100",
                        name="WorkPackage 100",
                        planned_hours=Decimal("40"),
                    ),
                    WorkPackage(
                        id="WP-2",
                        project_id="P-1",
                        code="WP-200",
                        name="WorkPackage 200",
                        planned_hours=Decimal("20"),
                    ),
                    Resource(id="R-1", name="Alice", active=True),
                    AssetType(
                        id="AT-1",
                        code="LIFT",
                        label="Nacelle",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()

            session.add(
                WorkforceRequest(
                    id="D-1",
                    legacy_demand_number="DMO-1",
                    project_id="P-1",
                    work_package_id="WP-1",
                    status="En planification",
                    aggregate_version=4,
                )
            )
            session.flush()

            session.add_all(
                [
                    RequestLine(
                        id="LINE-W",
                        workforce_request_id="D-1",
                        position=0,
                        kind="WORKFORCE",
                        work_package_id="WP-1",
                        desired_start=D1,
                        desired_end=D2,
                        estimated_hours=Decimal("8"),
                    ),
                    RequestLine(
                        id="LINE-A",
                        workforce_request_id="D-1",
                        position=1,
                        kind="ASSET",
                        work_package_id="WP-1",
                        desired_start=D2,
                        desired_end=D3,
                    ),
                ]
            )
            session.flush()

            session.add(
                RequestApprovalRevision(
                    id="REV-1",
                    workforce_request_id="D-1",
                    request_version=3,
                    approved_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
                    provenance="APPROVAL",
                    payload_format_version=int(authorization["format_version"]),
                    payload_text=json.dumps(revision_payload),
                    authorization_fingerprint=envelope.authorization_fingerprint,
                )
            )
            session.flush()
            session.add(
                RequestApprovalReference(
                    workforce_request_id="D-1",
                    active_revision_id="REV-1",
                    status=APPROVAL_REFERENCE_CAPTURED,
                )
            )
            session.flush()

            session.add(
                Asset(
                    id="ASSET-1",
                    code="LIFT-01",
                    label="Nacelle 01",
                    asset_type_id="AT-1",
                )
            )
            session.add(
                ResourceRequirement(
                    id="REQ-1",
                    project_id="P-1",
                    workforce_request_id="D-1",
                    source_request_line_id="LINE-W",
                    approval_revision_id="REV-1",
                    approved_entry_key=human.identity.stable_key,
                    approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                    start_date=D1,
                    end_date=D2,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    origin="REQUEST",
                )
            )
            session.add(
                AssetRequirement(
                    id="AREQ-1",
                    project_id="P-1",
                    workforce_request_id="D-1",
                    source_request_line_id="LINE-A",
                    approval_revision_id="REV-1",
                    approved_entry_key=asset.identity.stable_key,
                    asset_type_id="AT-1",
                    start_date=D2,
                    end_date=D3,
                    status="Planifié",
                )
            )
            session.add(
                DeliveryPlanRow(
                    id="DP-1",
                    work_package_id="WP-1",
                    status="ACTIVE",
                    delivery_version=4,
                )
            )
            session.add(PlanningMutationState(id="GLOBAL", version=7))
            session.flush()

            session.add(
                Shift(
                    id="SHIFT-1",
                    resource_requirement_id="REQ-1",
                    resource_id="R-1",
                    work_date=D1,
                    hours=Decimal("6"),
                    source="MANUAL",
                    locked=True,
                )
            )
            session.add(
                AssetAllocation(
                    id="AALLOC-1",
                    asset_requirement_id="AREQ-1",
                    asset_id="ASSET-1",
                    start_date=D2,
                    end_date=D3,
                    locked=True,
                    source="MANUAL",
                )
            )
            session.add_all(
                [
                    DeliveryItemRow(
                        id="STORY-DONE",
                        delivery_plan_id="DP-1",
                        item_type="STORY",
                        title="Terminée",
                        status="DONE",
                        current_estimate_hours=Decimal("8"),
                        reference_estimate_hours=Decimal("8"),
                        remaining_hours=Decimal("0"),
                        position=0,
                    ),
                    DeliveryItemRow(
                        id="STORY-OPEN",
                        delivery_plan_id="DP-1",
                        item_type="STORY",
                        title="En cours",
                        status="IN_PROGRESS",
                        current_estimate_hours=Decimal("4"),
                        reference_estimate_hours=Decimal("4"),
                        remaining_hours=Decimal("3"),
                        position=1,
                    ),
                    DeliveryItemRow(
                        id="STORY-MISSING",
                        delivery_plan_id="DP-1",
                        item_type="STORY",
                        title="Non estimée",
                        status="BACKLOG",
                        position=2,
                    ),
                ]
            )

    def test_active_approved_capacity_uses_immutable_revision_not_candidate(self) -> None:
        with self.factory() as session:
            projection = SqlDeliveryPlanningReadRepository(
                session
            ).get_work_package_planning_capacity("WP-1")
            self.assertIsNotNone(projection)
            assert projection is not None
            self.assertEqual(projection.work_package_reference, "WP-100")
            self.assertEqual(projection.human_reserved_hours, 6)
            self.assertEqual(projection.window_start, D1)
            self.assertEqual(projection.window_end, D3)
            self.assertEqual(projection.observed_planning_version, 7)
            self.assertEqual(len(projection.approved_sources), 1)
            self.assertEqual(
                set(projection.approved_sources[0].approved_entry_keys),
                {
                    EnvelopeEntryIdentity(line_id="LINE-W").stable_key,
                    EnvelopeEntryIdentity(line_id="LINE-A").stable_key,
                },
            )
            self.assertEqual(
                projection.asset_reserved_capacity[0].reserved_days,
                2,
            )

        with self.factory.begin() as session:
            request = session.get(WorkforceRequest, "D-1")
            workforce_line = session.get(RequestLine, "LINE-W")
            asset_line = session.get(RequestLine, "LINE-A")
            assert request is not None
            assert workforce_line is not None
            assert asset_line is not None
            request.work_package_id = "WP-2"
            request.estimated_hours = Decimal("99")
            workforce_line.work_package_id = "WP-2"
            asset_line.work_package_id = "WP-2"

        with self.factory() as session:
            repository = SqlDeliveryPlanningReadRepository(session)
            original = repository.get_work_package_planning_capacity("WP-1")
            candidate = repository.get_work_package_planning_capacity("WP-2")
            self.assertIsNotNone(original)
            self.assertIsNotNone(candidate)
            assert original is not None
            assert candidate is not None
            self.assertEqual(original.human_reserved_hours, 6)
            self.assertEqual(len(original.approved_sources), 1)
            self.assertEqual(candidate.human_reserved_hours, 0)
            self.assertEqual(candidate.approved_sources, ())

    def test_summary_rolls_up_progress_forecast_and_reserved_capacity(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/api/v1/delivery/work-packages/WP-1/summary")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()

        self.assertEqual(payload["work_package"]["reference"], "WP-100")
        self.assertEqual(payload["work_package"]["reference_hours"], 40)
        self.assertEqual(payload["delivery_plan"]["id"], "DP-1")

        self.assertEqual(payload["progress"]["state"], "PARTIAL_COVERAGE")
        self.assertEqual(payload["progress"]["progress_ratio"], 0.6667)
        self.assertEqual(payload["progress"]["coverage_ratio"], 0.6667)
        self.assertEqual(payload["progress"]["completed_reference_hours"], 8)
        self.assertEqual(payload["progress"]["total_reference_hours"], 12)

        self.assertEqual(payload["forecast"]["state"], "PARTIAL_COVERAGE")
        self.assertEqual(payload["forecast"]["total_remaining_hours"], 3)
        self.assertEqual(payload["forecast"]["coverage_ratio"], 0.5)

        capacity = payload["planning_capacity"]
        self.assertEqual(capacity["provenance"], "ACTIVE_APPROVED_PLAN")
        self.assertEqual(capacity["human_reserved_hours"], 6)
        self.assertEqual(capacity["total_reserved_hours"], 6)
        self.assertEqual(capacity["observed_planning_version"], 7)
        self.assertEqual(capacity["asset_reserved_capacity"][0]["reserved_days"], 2)
        self.assertEqual(payload["forecast_capacity_balance_hours"], 3)
        self.assertNotIn("actual_hours", capacity)

        diagnostic_codes = {
            diagnostic["code"] for diagnostic in payload["diagnostics"]
        }
        self.assertEqual(
            diagnostic_codes,
            {
                "DELIVERY_REFERENCE_ESTIMATE_INCOMPLETE",
                "DELIVERY_REMAINING_HOURS_INCOMPLETE",
            },
        )

    def test_shift_change_is_visible_on_next_read_and_is_never_an_actual(self) -> None:
        with self.factory.begin() as session:
            shift = session.get(Shift, "SHIFT-1")
            state = session.get(PlanningMutationState, "GLOBAL")
            assert shift is not None
            assert state is not None
            shift.hours = Decimal("4")
            state.version = 8

        with TestClient(self.app) as client:
            response = client.get("/api/v1/delivery/work-packages/WP-1/summary")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["planning_capacity"]["human_reserved_hours"], 4)
        self.assertEqual(payload["planning_capacity"]["observed_planning_version"], 8)
        self.assertEqual(payload["forecast"]["total_remaining_hours"], 3)
        self.assertNotIn("actual", json.dumps(payload).lower())

    def test_work_package_without_delivery_plan_remains_valid(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/api/v1/delivery/work-packages/WP-2/summary")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsNone(payload["delivery_plan"])
        self.assertEqual(payload["progress"]["state"], "NO_INCLUDED_STORIES")
        self.assertEqual(payload["planning_capacity"]["human_reserved_hours"], 0)
        self.assertEqual(
            {item["code"] for item in payload["diagnostics"]},
            {"DELIVERY_PLAN_MISSING", "APPROVED_PLANNING_CAPACITY_ABSENT"},
        )


if __name__ == "__main__":
    unittest.main()
