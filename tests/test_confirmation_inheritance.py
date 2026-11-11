from __future__ import annotations

from functools import partial

from datetime import date, time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.confirmation import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_TENTATIVE,
    effective_confirmation,
    normalize_confirmation,
)
from app.infrastructure.sql import (
    Base,
    Project,
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

    def test_characterization_839_post_approval_demand_edit_leaves_active_planning_confirmation_stale(self) -> None:
        """Characterize #839 without accepting the stale state as the target contract.

        The candidate Demand/RequestLine edit succeeds in both directions, but the
        active operational confirmation and materialized ResourceRequirement remain
        at the approval-time value. Phase 2 is expected to invert the stale Planning
        assertions while preserving the candidate/operational authority split.
        """

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

                        before_detail = client.get(f"/api/v1/demands/{number}")
                        self.assertEqual(
                            before_detail.status_code,
                            200,
                            before_detail.text,
                        )
                        version = before_detail.json()["version"]

                        before_segment = next(
                            row
                            for row in client.get("/api/v1/segments").json()
                            if row["demand_number"] == number
                        )
                        before_shift = next(
                            row
                            for row in client.get("/api/v1/shifts").json()
                            if row["segment_id"] == before_segment["segment_id"]
                        )
                        self.assertEqual(before_segment["confirmation"], initial)
                        self.assertEqual(before_shift["confirmation"], initial)

                        edited = client.patch(
                            f"/api/v1/demands/{number}",
                            json={
                                "confirmation": target,
                                "expected_version": version,
                                "comment": "Caractérisation #839",
                            },
                        )
                        self.assertEqual(edited.status_code, 200, edited.text)

                        after_detail = client.get(f"/api/v1/demands/{number}")
                        self.assertEqual(
                            after_detail.status_code,
                            200,
                            after_detail.text,
                        )
                        self.assertEqual(after_detail.json()["confirmation"], target)
                        self.assertEqual(
                            after_detail.json()["status"],
                            "En planification",
                        )

                        after_segment = next(
                            row
                            for row in client.get("/api/v1/segments").json()
                            if row["demand_number"] == number
                        )
                        after_shift = next(
                            row
                            for row in client.get("/api/v1/shifts").json()
                            if row["segment_id"] == after_segment["segment_id"]
                        )

                        # Current defect: Planning still consumes the materialized
                        # operational value captured at approval.
                        self.assertEqual(after_segment["confirmation"], initial)
                        self.assertEqual(after_shift["confirmation"], initial)
                        self.assertIsNone(after_shift["confirmation_override"])

                    engine = create_sql_engine(database_url)
                    factory = create_session_factory(engine)
                    try:
                        with factory() as session:
                            request = session.scalar(
                                select(WorkforceRequest).where(
                                    WorkforceRequest.legacy_demand_number == number
                                )
                            )
                            self.assertIsNotNone(request)
                            assert request is not None

                            line = session.scalar(
                                select(RequestLine).where(
                                    RequestLine.workforce_request_id == request.id,
                                    RequestLine.active.is_(True),
                                )
                            )
                            requirement = session.scalar(
                                select(ResourceRequirement).where(
                                    ResourceRequirement.workforce_request_id
                                    == request.id,
                                    ResourceRequirement.status != "Annulé",
                                )
                            )
                            operational = session.get(
                                RequestOperationalState,
                                request.id,
                            )
                            self.assertIsNotNone(line)
                            self.assertIsNotNone(requirement)
                            self.assertIsNotNone(operational)
                            assert (
                                line is not None
                                and requirement is not None
                                and operational is not None
                            )

                            # Candidate persistence has the edit.
                            self.assertEqual(request.confirmation, target)
                            self.assertEqual(line.confirmation, target)

                            # Active Planning authority is stale because the normal
                            # demand edit did not update the operational choice state
                            # or resynchronize the materialized requirement.
                            self.assertEqual(requirement.confirmation, initial)
                            self.assertFalse(requirement.confirmation_overridden)
                            self.assertEqual(operational.version, 1)
                            confirmations = json.loads(
                                operational.confirmations_text
                            )
                            self.assertEqual(
                                next(iter(confirmations.values())),
                                initial,
                            )
                    finally:
                        engine.dispose()

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
