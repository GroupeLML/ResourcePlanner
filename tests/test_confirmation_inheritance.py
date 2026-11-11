from __future__ import annotations

from functools import partial

from datetime import date, time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain.confirmation import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_TENTATIVE,
    effective_confirmation,
    normalize_confirmation,
)
from app.infrastructure.sql import (
    Base,
    Project,
    RequestApprovalRevision,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.operational_choice_models import RequestOperationalState
from app.server import create_api_app


DAY = date(2026, 8, 24)  # lundi


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ConfirmationPolicyTests(unittest.TestCase):
    def test_policy_normalizes_common_confirmed_spellings_and_inherits(self) -> None:
        self.assertEqual(normalize_confirmation("Confirmé"), CONFIRMATION_CONFIRMED)
        self.assertEqual(normalize_confirmation("confirmee"), CONFIRMATION_CONFIRMED)
        self.assertEqual(normalize_confirmation("Tentative"), CONFIRMATION_TENTATIVE)
        self.assertEqual(
            effective_confirmation(None, "Tentative"),
            CONFIRMATION_TENTATIVE,
        )
        self.assertEqual(
            effective_confirmation("Confirmé", "Tentative"),
            CONFIRMATION_CONFIRMED,
        )

        with self.assertRaises(ValueError):
            normalize_confirmation("peut-être")


class ConfirmationInheritanceApiTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet confirmation"))
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
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="confirmation.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _create_and_approve(
        client: TestClient,
        *,
        confirmation: str,
    ) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "desired_start": DAY.isoformat(),
                "desired_end": DAY.isoformat(),
                "estimated_hours": 4,
                "proposed_technician": "Alice",
                "confirmation": confirmation,
            }),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        submitted = client.post(f"/api/v1/demands/{number}/submit")
        assert submitted.status_code == 200, submitted.text
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "ok"},
        )
        assert approved.status_code == 200, approved.text
        actions = client.get(
            "/api/v1/planning/actions",
            params={"start": DAY.isoformat(), "end": DAY.isoformat(), "scope": "global"},
        )
        assert actions.status_code == 200, actions.text
        assignment = next(
            row for row in actions.json()
            if row["kind"] == "ASSIGNMENT" and row["demand_number"] == number
        )
        assigned = client.post(
            f"/api/v1/segments/{assignment['segment_id']}/assign",
            json={"resource_id": "R1"},
        )
        assert assigned.status_code == 200, assigned.text
        return number

    @staticmethod
    def _create_and_approve_tentative(client: TestClient) -> str:
        return ConfirmationInheritanceApiTests._create_and_approve(
            client,
            confirmation="Tentative",
        )

    def test_request_confirmation_flows_to_requirement_and_auto_shift(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_and_approve_tentative(client)

                segments = client.get("/api/v1/segments").json()
                segment = next(row for row in segments if row["demand_number"] == number)
                self.assertEqual(segment["confirmation"], "Tentative")
                self.assertFalse(segment["confirmation_overridden"])

                shifts = client.get("/api/v1/shifts").json()
                shift = next(row for row in shifts if row["segment_id"] == segment["segment_id"])
                self.assertEqual(shift["confirmation"], "Tentative")
                self.assertIsNone(shift["confirmation_override"])

    def test_839_post_approval_demand_edit_converges_confirmation_without_reapproval_or_replacement(self) -> None:
        for initial, target in (
            ("Tentative", "Confirmée"),
            ("Confirmée", "Tentative"),
        ):
            with self.subTest(initial=initial, target=target):
                with TemporaryDirectory() as directory:
                    database_url = self._database(directory)
                    app = create_api_app(database_url, actor_name="Jean")
                    with TestClient(app, raise_server_exceptions=False) as client:
                        number = self._create_and_approve(
                            client,
                            confirmation=initial,
                        )
                        detail = client.get(f"/api/v1/demands/{number}/detail").json()
                        before_segment = next(
                            row for row in client.get("/api/v1/segments").json()
                            if row["demand_number"] == number
                        )
                        before_shift = next(
                            row for row in client.get("/api/v1/shifts").json()
                            if row["segment_id"] == before_segment["segment_id"]
                        )
                        before_identity = (
                            before_segment["segment_id"],
                            before_shift["allocation_id"],
                            before_shift["resource_id"],
                            before_shift["hours"],
                        )
                        before_window = (
                            before_segment["start_date"],
                            before_segment["end_date"],
                            before_segment["planned_hours"],
                        )

                        engine = create_sql_engine(database_url)
                        factory = create_session_factory(engine)
                        try:
                            with factory() as session:
                                request = session.scalar(
                                    select(WorkforceRequest).where(
                                        WorkforceRequest.legacy_demand_number == number
                                    )
                                )
                                assert request is not None
                                revision_count_before = session.scalar(
                                    select(func.count(RequestApprovalRevision.id)).where(
                                        RequestApprovalRevision.workforce_request_id == request.id
                                    )
                                )
                        finally:
                            engine.dispose()

                        edited = client.patch(
                            f"/api/v1/demands/{number}",
                            json={
                                "confirmation": target,
                                "expected_version": detail["version"],
                                "expected_operational_version": detail["policy"][
                                    "expected_operational_version"
                                ],
                                "comment": "Régression #839",
                            },
                        )
                        self.assertEqual(edited.status_code, 200, edited.text)
                        self.assertFalse(edited.json()["reapproval_required"])

                        after_detail = client.get(f"/api/v1/demands/{number}/detail").json()
                        self.assertEqual(after_detail["demand"]["confirmation"], target)
                        self.assertEqual(
                            after_detail["demand"]["status"],
                            "En planification",
                        )
                        self.assertEqual(
                            after_detail["approval_state"]["active_revision_id"],
                            detail["approval_state"]["active_revision_id"],
                        )
                        self.assertEqual(
                            after_detail["approval_state"]["operational_version"],
                            detail["approval_state"]["operational_version"] + 1,
                        )

                        after_segment = next(
                            row for row in client.get("/api/v1/segments").json()
                            if row["demand_number"] == number
                        )
                        after_shift = next(
                            row for row in client.get("/api/v1/shifts").json()
                            if row["segment_id"] == after_segment["segment_id"]
                        )
                        self.assertEqual(after_segment["confirmation"], target)
                        self.assertEqual(after_shift["confirmation"], target)
                        self.assertEqual(
                            (
                                after_segment["segment_id"],
                                after_shift["allocation_id"],
                                after_shift["resource_id"],
                                after_shift["hours"],
                            ),
                            before_identity,
                        )
                        self.assertEqual(
                            (
                                after_segment["start_date"],
                                after_segment["end_date"],
                                after_segment["planned_hours"],
                            ),
                            before_window,
                        )

                    engine = create_sql_engine(database_url)
                    factory = create_session_factory(engine)
                    try:
                        with factory() as session:
                            request = session.scalar(
                                select(WorkforceRequest).where(
                                    WorkforceRequest.legacy_demand_number == number
                                )
                            )
                            assert request is not None
                            line = session.scalar(
                                select(RequestLine).where(
                                    RequestLine.workforce_request_id == request.id,
                                    RequestLine.active.is_(True),
                                )
                            )
                            requirement = session.scalar(
                                select(ResourceRequirement).where(
                                    ResourceRequirement.workforce_request_id == request.id,
                                    ResourceRequirement.status != "Annulé",
                                )
                            )
                            operational = session.get(RequestOperationalState, request.id)
                            assert line is not None and requirement is not None and operational is not None
                            self.assertEqual(request.confirmation, target)
                            self.assertEqual(line.confirmation, target)
                            self.assertEqual(requirement.confirmation, target)
                            self.assertFalse(requirement.confirmation_overridden)
                            self.assertEqual(
                                next(iter(json.loads(operational.confirmations_text).values())),
                                target,
                            )
                            revision_count_after = session.scalar(
                                select(func.count(RequestApprovalRevision.id)).where(
                                    RequestApprovalRevision.workforce_request_id == request.id
                                )
                            )
                            self.assertEqual(revision_count_after, revision_count_before)
                    finally:
                        engine.dispose()

    def test_839_stale_operational_version_rolls_back_candidate_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client, confirmation="Tentative")
                detail = client.get(f"/api/v1/demands/{number}/detail").json()
                stale = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "confirmation": "Confirmée",
                        "expected_version": detail["version"],
                        "expected_operational_version":
                            detail["policy"]["expected_operational_version"] + 99,
                        "comment": "Conflit CAS #839",
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                reread = client.get(f"/api/v1/demands/{number}").json()
                self.assertEqual(reread["confirmation"], "Tentative")
                segment = next(
                    row for row in client.get("/api/v1/segments").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(segment["confirmation"], "Tentative")

    def test_839_multiline_edit_updates_only_changed_confirmation_entry(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "priority": "Normale",
                        "description": "Multi-ligne #839",
                        "lines": [
                            {
                                "kind": "WORKFORCE",
                                "desired_start": DAY.isoformat(),
                                "desired_end": DAY.isoformat(),
                                "estimated_hours": 4,
                                "confirmation": "Tentative",
                            },
                            {
                                "kind": "WORKFORCE",
                                "desired_start": DAY.isoformat(),
                                "desired_end": DAY.isoformat(),
                                "estimated_hours": 4,
                                "confirmation": "Confirmée",
                            },
                        ],
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                self.assertEqual(client.post(f"/api/v1/demands/{number}/submit").status_code, 200)
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "ok"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                detail = client.get(f"/api/v1/demands/{number}/detail").json()
                lines = [row for row in detail["demand"]["lines"] if row["active"]]
                self.assertEqual([row["confirmation"] for row in lines], ["Tentative", "Confirmée"])

                successor = [
                    {
                        "id": row["line_id"],
                        "position": row["position"],
                        "kind": row["kind"],
                        "required_resource_class": row["required_resource_class"],
                        "required_competency_ids": row["required_competency_ids"],
                        "desired_start": row["desired_start"],
                        "desired_end": row["desired_end"],
                        "desired_active_days": row["desired_active_days"],
                        "estimated_hours": row["estimated_hours"],
                        "work_package_ref": row["work_package_ref"],
                        "task_code": row["task_code"],
                        "proposed_resource_id": row["proposed_resource_id"],
                        "asset_type_id": row["asset_type_id"],
                        "proposed_asset_id": row["proposed_asset_id"],
                        "confirmation": row["confirmation"],
                        "description": row["description"],
                    }
                    for row in lines
                ]
                successor[0]["confirmation"] = "Confirmée"
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "expected_version": detail["version"],
                        "expected_operational_version":
                            detail["policy"]["expected_operational_version"],
                        "lines": successor,
                        "comment": "Une seule ligne change #839",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)

                after = client.get(f"/api/v1/demands/{number}/detail").json()
                after_lines = [row for row in after["demand"]["lines"] if row["active"]]
                self.assertEqual(
                    [row["confirmation"] for row in after_lines],
                    ["Confirmée", "Confirmée"],
                )
                confirmations = [
                    row["confirmation"]
                    for row in client.get("/api/v1/segments").json()
                    if row["demand_number"] == number
                ]
                self.assertEqual(confirmations.count("Confirmée"), 2)

    def test_segment_override_can_be_set_and_cleared_without_changing_request(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_and_approve_tentative(client)
                segment = next(
                    row
                    for row in client.get("/api/v1/segments").json()
                    if row["demand_number"] == number
                )
                segment_id = segment["segment_id"]

                overridden = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"confirmation": "Confirmé"},
                )
                self.assertEqual(overridden.status_code, 200, overridden.text)
                after_override = client.get(f"/api/v1/segments/{segment_id}").json()
                self.assertEqual(after_override["confirmation"], "Confirmée")
                self.assertTrue(after_override["confirmation_overridden"])
                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["segment_id"] == segment_id
                )
                self.assertEqual(shift["confirmation"], "Confirmée")
                self.assertIsNone(shift["confirmation_override"])

                inherited = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"confirmation": None},
                )
                self.assertEqual(inherited.status_code, 200, inherited.text)
                after_clear = client.get(f"/api/v1/segments/{segment_id}").json()
                self.assertEqual(after_clear["confirmation"], "Tentative")
                self.assertFalse(after_clear["confirmation_overridden"])

    def test_manual_shift_can_override_segment_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_and_approve_tentative(client)
                segment = next(
                    row
                    for row in client.get("/api/v1/segments").json()
                    if row["demand_number"] == number
                )
                created = client.post(
                    f"/api/v1/segments/{segment['segment_id']}/allocations",
                    json={
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 4,
                        "confirmation": "Confirmée",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                allocation_id = created.json()["allocation_id"]

                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["allocation_id"] == allocation_id
                )
                self.assertEqual(shift["confirmation"], "Confirmée")
                self.assertEqual(shift["confirmation_override"], "Confirmée")

    def test_quick_shift_confirmation_is_owned_by_adhoc_requirement_and_inherited_by_shift(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/quick-shifts",
                    json={
                        "project_number": "P-1",
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 4,
                        "confirmation": "Tentative",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                result = created.json()

                segment = client.get(
                    f"/api/v1/segments/{result['segment_id']}"
                ).json()
                self.assertEqual(segment["confirmation"], "Tentative")
                self.assertFalse(segment["confirmation_overridden"])

                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["allocation_id"] == result["allocation_id"]
                )
                self.assertEqual(shift["confirmation"], "Tentative")
                self.assertIsNone(shift["confirmation_override"])

    def test_period_confirmation_overrides_request_snapshot_at_approval(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Jean")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": DAY.isoformat(),
                        "desired_end": DAY.isoformat(),
                        "confirmation": "Confirmée",
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                request_version = client.get(
                    f"/api/v1/demands/{number}"
                ).json()["version"]
                replaced = client.put(
                    f"/api/v1/demands/{number}/periods",
                    json={
                        "expected_request_version": request_version,
                        "periods": [
                            {
                                "period_id": "PER-1",
                                "start_date": DAY.isoformat(),
                                "end_date": DAY.isoformat(),
                                "hours": 4,
                                "confirmation": "Tentative",
                                "proposed_resource": "Alice",
                            }
                        ]
                    },
                )
                self.assertEqual(replaced.status_code, 200, replaced.text)
                self.assertEqual(
                    client.post(f"/api/v1/demands/{number}/submit").status_code,
                    200,
                )
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "ok"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                segment = next(
                    row
                    for row in client.get("/api/v1/segments").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(segment["confirmation"], "Tentative")
                self.assertFalse(segment["confirmation_overridden"])


if __name__ == "__main__":
    unittest.main()
