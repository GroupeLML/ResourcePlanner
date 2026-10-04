from __future__ import annotations

from functools import partial

from datetime import date, timedelta, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application import DemandLineReadModel, DemandPeriodReadModel, DemandReadModel, emergency_override_eligibility
from app.application.security import PERMISSION_APPROVE_DEMANDS
from app.domain.operational_contacts import (
    PROVENANCE_APPROVAL_CAPTURE,
    RESPONSIBILITY_CONTEXT_VERSION,
)
from app.infrastructure.sql import (
    Base,
    Project,
    RequestApprovalRevision,
    Resource,
    ResourceRequirement,
    ResourceAvailabilityRule,
    Shift,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import required_permission


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER, test_admin_auth_resolver

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class EmergencyOverridePolicyTests(unittest.TestCase):
    def test_only_urgent_submitted_request_overlapping_current_week_is_eligible(self) -> None:
        today = date(2026, 9, 16)
        demand = DemandReadModel(
            number="D-1",
            status="Soumise",
            priority="Urgent",
            desired_start=date(2026, 9, 14),
            desired_end=date(2026, 9, 18),
        )

        eligible, reason = emergency_override_eligibility(demand, today=today)

        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_nonurgent_outside_week_and_active_override_are_rejected(self) -> None:
        today = date(2026, 9, 16)
        base = DemandReadModel(
            number="D-1",
            status="Soumise",
            priority="Normale",
            desired_start=date(2026, 9, 16),
        )
        self.assertEqual(
            emergency_override_eligibility(base, today=today),
            (False, "NOT_URGENT"),
        )

        outside = DemandReadModel(
            number="D-2",
            status="Soumise",
            priority="Urgent",
            desired_start=date(2026, 9, 28),
        )
        self.assertEqual(
            emergency_override_eligibility(outside, today=today),
            (False, "OUTSIDE_CURRENT_WEEK"),
        )

        active = DemandReadModel(
            number="D-3",
            status="Soumise",
            priority="Urgent",
            desired_start=date(2026, 9, 16),
            emergency_override_active=True,
        )
        self.assertEqual(
            emergency_override_eligibility(active, today=today),
            (False, "ALREADY_ACTIVE"),
        )

    def test_detailed_periods_use_only_effective_selected_alternative(self) -> None:
        today = date(2026, 9, 16)
        demand = DemandReadModel(
            number="D-4",
            status="Soumise",
            priority="Urgent",
            desired_start=date(2026, 10, 1),
        )
        periods = (
            DemandPeriodReadModel(
                period_id="A",
                demand_number="D-4",
                sequence=1,
                kind="ALTERNATIVE",
                start_date=date(2026, 9, 16),
                end_date=date(2026, 9, 16),
                hours=8,
                confirmation="Confirmée",
                alternative_group="VISITE",
                selected=True,
            ),
            DemandPeriodReadModel(
                period_id="B",
                demand_number="D-4",
                sequence=2,
                kind="ALTERNATIVE",
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 1),
                hours=8,
                confirmation="Confirmée",
                alternative_group="VISITE",
                selected=False,
            ),
        )

        self.assertEqual(
            emergency_override_eligibility(demand, today=today, periods=periods),
            (True, None),
        )

    def test_multiline_eligibility_uses_each_line_and_its_selected_periods(self) -> None:
        today = date(2026, 9, 16)
        demand = DemandReadModel(
            number="D-LINES",
            status="Soumise",
            priority="Urgent",
            line_mode=True,
            lines=(
                DemandLineReadModel(
                    line_id="L1",
                    position=0,
                    kind="WORKFORCE",
                    desired_start=date(2026, 10, 1),
                    estimated_hours=8,
                ),
                DemandLineReadModel(
                    line_id="L2",
                    position=1,
                    kind="WORKFORCE",
                    desired_start=date(2026, 10, 2),
                    estimated_hours=8,
                ),
            ),
        )
        periods = (
            DemandPeriodReadModel(
                period_id="A",
                demand_number="D-LINES",
                request_line_id="L1",
                sequence=1,
                kind="ALTERNATIVE",
                start_date=today,
                end_date=today,
                hours=8,
                confirmation="Confirmée",
                alternative_group="VISITE",
                selected=True,
            ),
            DemandPeriodReadModel(
                period_id="B",
                demand_number="D-LINES",
                request_line_id="L1",
                sequence=2,
                kind="ALTERNATIVE",
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 1),
                hours=8,
                confirmation="Confirmée",
                alternative_group="VISITE",
                selected=False,
            ),
        )

        self.assertEqual(
            emergency_override_eligibility(demand, today=today, periods=periods),
            (True, None),
        )

    def test_http_permission_matches_normal_approval(self) -> None:
        self.assertEqual(
            required_permission("POST", "/api/v1/demands/D-1/emergency-plan"),
            PERMISSION_APPROVE_DEMANDS,
        )


class EmergencyOverrideHttpTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> str:
        path = Path(directory) / "emergency-override.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(Project(id="P1", number="P-1", name="Projet urgence"))
            session.add(Resource(id="R1", name="Alice", active=True))
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()
        return url

    def test_emergency_plan_stays_submitted_is_audited_and_regular_approval_clears_flag(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            today = date.today()
            app = create_api_app(
                database_url,
                actor_name="coord-urgence",
                auth_resolver=test_admin_auth_resolver("coord-urgence"),
            )

            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": today.isoformat(),
                        "desired_end": today.isoformat(),
                        "estimated_hours": 8,
                        "priority": "Urgent",
                        "proposed_technician": "Alice",
                        "submit": True,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                emergency = client.post(
                    f"/api/v1/demands/{number}/emergency-plan",
                    json={"comment": "Intervention requise avant l'approbation régulière"},
                )
                self.assertEqual(emergency.status_code, 200, emergency.text)
                self.assertEqual(emergency.json()["status"], "Soumise")
                self.assertIsNotNone(emergency.json()["planning"])

                demand = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(demand.status_code, 200, demand.text)
                self.assertEqual(demand.json()["status"], "Soumise")
                self.assertTrue(demand.json()["emergency_override_active"])
                self.assertEqual(
                    demand.json()["emergency_override_by"],
                    "coord-urgence",
                )

                history = client.get(f"/api/v1/demands/{number}/history")
                self.assertEqual(history.status_code, 200, history.text)
                emergency_events = [
                    row
                    for row in history.json()
                    if row["action"] == "Dérogation d'approbation urgente"
                ]
                self.assertEqual(len(emergency_events), 1)
                self.assertEqual(emergency_events[0]["previous_status"], "Soumise")
                self.assertEqual(emergency_events[0]["status"], "Soumise")
                self.assertEqual(emergency_events[0]["actor_name"], "coord-urgence")

                week_start = today - timedelta(days=today.weekday())
                week_end = week_start + timedelta(days=6)
                planning = client.get(
                    "/api/v1/planning/snapshot",
                    params={"start": week_start.isoformat(), "end": week_end.isoformat()},
                )
                self.assertEqual(planning.status_code, 200, planning.text)
                matching_shifts = [
                    row for row in planning.json()["shifts"] if row["demand_number"] == number
                ]
                self.assertEqual(matching_shifts, [])

                duplicate = client.post(
                    f"/api/v1/demands/{number}/emergency-plan",
                    json={"comment": "Deuxième tentative"},
                )
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                self.assertEqual(
                    duplicate.json()["error"]["code"],
                    "demand_emergency_override_already_active",
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
                        requirements = session.scalars(
                            select(ResourceRequirement).where(
                                ResourceRequirement.workforce_request_id == request.id,
                                ResourceRequirement.status != "Annulé",
                            )
                        ).all()
                        # 276C is authoritative: urgency never materializes a new plan
                        # before quorum, so there is no operational responsibility
                        # snapshot to persist yet and no fake approval revision.
                        self.assertEqual(requirements, [])
                        revisions = session.scalars(
                            select(RequestApprovalRevision).where(
                                RequestApprovalRevision.workforce_request_id
                                == request.id
                            )
                        ).all()
                        self.assertEqual(revisions, [])
                finally:
                    engine.dispose()

                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Régularisation formelle"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                regularized = client.get(f"/api/v1/demands/{number}").json()
                self.assertEqual(regularized["status"], "En planification")
                self.assertFalse(regularized["emergency_override_active"])

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
                    self.assertFalse(request.emergency_override_active)
                    self.assertIsNotNone(request.emergency_override_at)
                    self.assertEqual(request.emergency_override_by_name, "coord-urgence")
                    events = session.scalars(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id == request.id,
                            WorkforceRequestHistory.action
                            == "Dérogation d'approbation urgente",
                        )
                    ).all()
                    self.assertEqual(len(events), 1)
                    requirements = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertGreaterEqual(len(requirements), 1)
                    self.assertTrue(
                        all(
                            row.operational_responsibility_context_provenance
                            == PROVENANCE_APPROVAL_CAPTURE
                            for row in requirements
                        )
                    )
                    self.assertTrue(
                        all(
                            row.operational_responsibility_context_version
                            == RESPONSIBILITY_CONTEXT_VERSION
                            for row in requirements
                        )
                    )
                    self.assertTrue(
                        all(row.approval_revision_id is not None for row in requirements)
                    )
                    shifts = session.scalars(select(Shift)).all()
                    self.assertGreaterEqual(len(shifts), 1)
            finally:
                engine.dispose()

    def test_emergency_route_rejects_blank_reason_nonurgent_and_outside_week(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            today = date.today()
            app = create_api_app(
                database_url,
                actor_name="coord-urgence",
                auth_resolver=test_admin_auth_resolver("coord-urgence"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                blank = client.post(
                    "/api/v1/demands/UNKNOWN/emergency-plan",
                    json={"comment": ""},
                )
                self.assertEqual(blank.status_code, 422)

                normal = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": today.isoformat(),
                        "priority": "Normale",
                        "proposed_technician": "Alice",
                        "estimated_hours": 8,
                        "submit": True,
                    }),
                ).json()["demand_number"]
                nonurgent = client.post(
                    f"/api/v1/demands/{normal}/emergency-plan",
                    json={"comment": "Essai"},
                )
                self.assertEqual(nonurgent.status_code, 422, nonurgent.text)
                self.assertEqual(
                    nonurgent.json()["error"]["code"],
                    "demand_emergency_not_urgent",
                )

                later = today + timedelta(days=14)
                outside = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": later.isoformat(),
                        "priority": "Urgent",
                        "proposed_technician": "Alice",
                        "estimated_hours": 8,
                        "submit": True,
                    }),
                ).json()["demand_number"]
                rejected = client.post(
                    f"/api/v1/demands/{outside}/emergency-plan",
                    json={"comment": "Essai hors semaine"},
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(
                    rejected.json()["error"]["code"],
                    "demand_emergency_outside_current_week",
                )


if __name__ == "__main__":
    unittest.main()
