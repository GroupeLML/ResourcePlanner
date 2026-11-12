from __future__ import annotations

from datetime import date, time
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.security import AuthPrincipal, ROLE_PROJECT_MANAGER
from app.infrastructure.sql import (
    Project,
    RequestApprovalReference,
    RequestApprovalRevision,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.operational_choice_models import RequestOperationalState
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)


def _project_manager_auth_resolver():
    return static_auth_resolver(
        AuthPrincipal.from_roles(
            local_user_id=None,
            issuer="urn:resourceplanner:test",
            subject="project-manager-13e",
            display_name="Chargé de projet 13E",
            email=None,
            roles=(ROLE_PROJECT_MANAGER,),
            auth_mode="test",
        )
    )


class DemandEnvelopeReapprovalPolicyTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet 13E"))
        session.add(Resource(id="R1", name="Alice", active=True))
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="STD-R1-13E",
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
            filename="demand-envelope-13e.db",
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
        database_url: str,
        *,
        hours: float = 100,
        confirmation: str = "Confirmée",
        proposed_technician: str | None = None,
    ) -> str:
        app = create_api_app(
            database_url,
            actor_name="admin-13e",
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            created = client.post(
                "/api/v1/demands",
                json=routed_demand_payload({
                    "project_number": "P-1",
                    "desired_start": D1.isoformat(),
                    "desired_end": D2.isoformat(),
                    "estimated_hours": hours,
                    "confirmation": confirmation,
                    "proposed_technician": proposed_technician,
                    "submit": True,
                }),
            )
            assert created.status_code == 201, created.text
            number = created.json()["demand_number"]
            approved = client.post(
                f"/api/v1/demands/{number}/approve",
                json={"comment": "Autorisation initiale 13E"},
            )
            assert approved.status_code == 200, approved.text
            return number

    @staticmethod
    def _request(session, number: str) -> WorkforceRequest:
        request = session.scalar(
            select(WorkforceRequest).where(
                WorkforceRequest.legacy_demand_number == number
            )
        )
        assert request is not None
        return request

    @staticmethod
    def _active_requirement(
        session,
        request_id: str,
    ) -> ResourceRequirement:
        rows = session.scalars(
            select(ResourceRequirement).where(
                ResourceRequirement.workforce_request_id == request_id,
                ResourceRequirement.status != "Annulé",
            )
        ).all()
        assert len(rows) == 1, rows
        return rows[0]

    @staticmethod
    def _assign_auto_shift(client: TestClient, number: str) -> dict[str, object]:
        actions = client.get(
            "/api/v1/planning/actions",
            params={
                "start": D1.isoformat(),
                "end": D2.isoformat(),
                "scope": "global",
            },
        )
        assert actions.status_code == 200, actions.text
        assignment = next(
            row
            for row in actions.json()
            if row["kind"] == "ASSIGNMENT" and row["demand_number"] == number
        )
        assigned = client.post(
            f"/api/v1/segments/{assignment['segment_id']}/assign",
            json={"resource_id": "R1"},
        )
        assert assigned.status_code == 200, assigned.text
        shifts = [
            row
            for row in client.get("/api/v1/shifts").json()
            if row["segment_id"] == assignment["segment_id"]
        ]
        assert shifts, assignment
        return shifts[0]

    @staticmethod
    def _operational_state(
        session,
        number: str,
    ) -> tuple[WorkforceRequest, RequestOperationalState, ResourceRequirement]:
        request = DemandEnvelopeReapprovalPolicyTests._request(session, number)
        state = session.get(RequestOperationalState, request.id)
        assert state is not None
        requirement = DemandEnvelopeReapprovalPolicyTests._active_requirement(
            session,
            request.id,
        )
        return request, state, requirement

    def test_project_manager_twenty_percent_is_within_envelope_and_audited(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(database_url)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
                    self.assertEqual(
                        self._active_requirement(session, request.id).planned_hours,
                        100,
                    )
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-13e",
                auth_resolver=_project_manager_auth_resolver(),
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 120,
                        "comment": "Tolérance PM exacte +20%",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertFalse(patched.json()["reapproval_required"])
                demand = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(demand.status_code, 200, demand.text)
                self.assertEqual(demand.json()["status"], "En planification")
                self.assertEqual(demand.json()["estimated_hours"], 120)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, original_revision)
                    self.assertEqual(
                        self._active_requirement(session, request.id).planned_hours,
                        120,
                    )
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                    )
                    self.assertEqual(revision_count, 1)
                    audit = session.scalar(
                        select(WorkforceRequestHistory)
                        .where(
                            WorkforceRequestHistory.workforce_request_id == request.id,
                            WorkforceRequestHistory.action
                            == "Comparaison enveloppe approuvée",
                        )
                        .order_by(WorkforceRequestHistory.occurred_at.desc())
                    )
                    self.assertIsNotNone(audit)
                    assert audit is not None
                    details = json.loads(audit.details or "{}")
                    decision = details["envelope_decision"]
                    self.assertEqual(decision["decision"], "WITHIN_ENVELOPE")
                    self.assertEqual(decision["reason"], "DELEGATED_TOLERANCE")
                    delegated = next(
                        row
                        for row in decision["changes"]
                        if row["code"] == "DELEGATED_TOLERANCE"
                    )
                    self.assertEqual(delegated["reference_hours"], "100.00")
                    self.assertEqual(delegated["candidate_hours"], "120.00")
                    self.assertEqual(delegated["delta_percent"], "20.00")
            finally:
                engine.dispose()

    def test_839_existing_delegated_budget_does_not_rebuild_on_confirmation_only_patch(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(
                database_url,
                hours=4,
                confirmation="Tentative",
                proposed_technician="Alice",
            )

            admin_app = create_api_app(
                database_url,
                actor_name="admin-839-budget",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                before_effort = client.get(f"/api/v1/demands/{number}/detail").json()
                self._assign_auto_shift(client, number)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, state, _requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
                    initial_operational_version = state.version
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-839-budget",
                auth_resolver=_project_manager_auth_resolver(),
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                effort_patch = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 4.8,
                        "expected_version": before_effort["version"],
                        "expected_operational_version": initial_operational_version,
                        "comment": "Tolérance déléguée #839",
                    },
                )
                self.assertEqual(effort_patch.status_code, 200, effort_patch.text)
                self.assertFalse(effort_patch.json()["reapproval_required"])

                after_effort = client.get(f"/api/v1/demands/{number}/detail").json()
                self.assertEqual(
                    after_effort["approval_state"]["operational_version"],
                    initial_operational_version + 1,
                )
                effort_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(effort_shift["hours"], 4.8)

                confirmed = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "confirmation": "Confirmée",
                        "expected_version": after_effort["version"],
                        "expected_operational_version":
                            after_effort["policy"]["expected_operational_version"],
                        "comment": "Confirmation sans nouveau budget #839",
                    },
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                confirmed_detail = client.get(
                    f"/api/v1/demands/{number}/detail"
                ).json()
                self.assertEqual(
                    confirmed_detail["approval_state"]["operational_version"],
                    after_effort["approval_state"]["operational_version"] + 1,
                )
                confirmed_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(confirmed_shift["confirmation"], "Confirmée")
                self.assertEqual(
                    (
                        confirmed_shift["allocation_id"],
                        confirmed_shift["segment_id"],
                        confirmed_shift["resource_id"],
                        confirmed_shift["hours"],
                    ),
                    (
                        effort_shift["allocation_id"],
                        effort_shift["segment_id"],
                        effort_shift["resource_id"],
                        effort_shift["hours"],
                    ),
                )

                tentative = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "confirmation": "Tentative",
                        "expected_version": confirmed_detail["version"],
                        "expected_operational_version":
                            confirmed_detail["policy"]["expected_operational_version"],
                        "comment": "Retour confirmation sans nouveau budget #839",
                    },
                )
                self.assertEqual(tentative.status_code, 200, tentative.text)
                tentative_detail = client.get(
                    f"/api/v1/demands/{number}/detail"
                ).json()
                self.assertEqual(
                    tentative_detail["approval_state"]["operational_version"],
                    confirmed_detail["approval_state"]["operational_version"] + 1,
                )
                tentative_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(tentative_shift["confirmation"], "Tentative")
                self.assertEqual(
                    (
                        tentative_shift["allocation_id"],
                        tentative_shift["segment_id"],
                        tentative_shift["resource_id"],
                        tentative_shift["hours"],
                    ),
                    (
                        effort_shift["allocation_id"],
                        effort_shift["segment_id"],
                        effort_shift["resource_id"],
                        effort_shift["hours"],
                    ),
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, state, requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, original_revision)
                    self.assertEqual(requirement.planned_hours, 4.8)
                    self.assertEqual(requirement.start_date, D1)
                    self.assertEqual(requirement.end_date, D2)
                    self.assertEqual(requirement.confirmation, "Tentative")
                    budget = json.loads(state.budget_overrides_text)
                    self.assertEqual(next(iter(budget.values())), "4.80")
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                    )
                    self.assertEqual(revision_count, 1)
            finally:
                engine.dispose()

    def test_839_combined_delegated_effort_and_confirmation_patch_composes_cas(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(
                database_url,
                hours=4,
                confirmation="Tentative",
                proposed_technician="Alice",
            )

            admin_app = create_api_app(
                database_url,
                actor_name="admin-839-combined",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                detail = client.get(f"/api/v1/demands/{number}/detail").json()
                baseline_shift = self._assign_auto_shift(client, number)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, state, _requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
                    original_operational_version = state.version
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-839-combined",
                auth_resolver=_project_manager_auth_resolver(),
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                combined = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 4.8,
                        "confirmation": "Confirmée",
                        "expected_version": detail["version"],
                        "expected_operational_version": original_operational_version,
                        "comment": "Effort + confirmation #839",
                    },
                )
                self.assertEqual(combined.status_code, 200, combined.text)
                self.assertFalse(combined.json()["reapproval_required"])

                after = client.get(f"/api/v1/demands/{number}/detail").json()
                self.assertEqual(after["demand"]["estimated_hours"], 4.8)
                self.assertEqual(after["demand"]["confirmation"], "Confirmée")
                self.assertEqual(
                    after["approval_state"]["operational_version"],
                    original_operational_version + 2,
                )
                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(shift["hours"], 4.8)
                self.assertEqual(shift["confirmation"], "Confirmée")
                self.assertEqual(shift["resource_id"], baseline_shift["resource_id"])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, state, requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, original_revision)
                    self.assertEqual(requirement.planned_hours, 4.8)
                    self.assertEqual(requirement.start_date, D1)
                    self.assertEqual(requirement.end_date, D2)
                    self.assertEqual(requirement.confirmation, "Confirmée")
                    self.assertEqual(
                        state.version,
                        original_operational_version + 2,
                    )
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                    )
                    self.assertEqual(revision_count, 1)
                    budget = json.loads(state.budget_overrides_text)
                    confirmations = json.loads(state.confirmations_text)
                    self.assertEqual(next(iter(budget.values())), "4.80")
                    self.assertEqual(next(iter(confirmations.values())), "Confirmée")
                    self.assertGreater(state.version, original_operational_version)
            finally:
                engine.dispose()

    def test_839_genuinely_stale_operational_version_rolls_back_combined_patch(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(
                database_url,
                hours=4,
                confirmation="Tentative",
                proposed_technician="Alice",
            )

            admin_app = create_api_app(
                database_url,
                actor_name="admin-839-stale",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                stale_detail = client.get(f"/api/v1/demands/{number}/detail").json()
                baseline_shift = self._assign_auto_shift(client, number)
                intervening = client.put(
                    f"/api/v1/demands/{number}/operational-confirmation",
                    json={
                        "confirmation": "Confirmée",
                        "expected_operational_version":
                            stale_detail["policy"]["expected_operational_version"],
                    },
                )
                self.assertEqual(intervening.status_code, 200, intervening.text)
                current_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(
                    current_shift["allocation_id"],
                    baseline_shift["allocation_id"],
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, _state, _requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-839-stale",
                auth_resolver=_project_manager_auth_resolver(),
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                stale = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 4.8,
                        "confirmation": "Confirmée",
                        "expected_version": stale_detail["version"],
                        "expected_operational_version":
                            stale_detail["policy"]["expected_operational_version"],
                        "comment": "Commande périmée #839",
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "operational_choice_version_conflict",
                )
                demand = client.get(f"/api/v1/demands/{number}").json()
                self.assertEqual(demand["estimated_hours"], 4)
                self.assertEqual(demand["confirmation"], "Tentative")
                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["demand_number"] == number
                )
                self.assertEqual(shift["allocation_id"], baseline_shift["allocation_id"])
                self.assertEqual(shift["hours"], 4)
                self.assertEqual(shift["resource_id"], baseline_shift["resource_id"])
                self.assertEqual(shift["confirmation"], "Confirmée")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request, state, requirement = self._operational_state(
                        session,
                        number,
                    )
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, original_revision)
                    self.assertEqual(requirement.planned_hours, 4)
                    self.assertEqual(requirement.start_date, D1)
                    self.assertEqual(requirement.end_date, D2)
                    self.assertEqual(requirement.confirmation, "Confirmée")
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                    )
                    self.assertEqual(revision_count, 1)
                    self.assertEqual(json.loads(state.budget_overrides_text), {})
                    self.assertEqual(
                        next(iter(json.loads(state.confirmations_text).values())),
                        "Confirmée",
                    )
            finally:
                engine.dispose()

    def test_project_manager_above_twenty_percent_requires_reapproval_and_keeps_old_plan(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(database_url)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-13e",
                auth_resolver=_project_manager_auth_resolver(),
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 121,
                        "comment": "Au-delà de la tolérance PM",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertTrue(patched.json()["reapproval_required"])
                self.assertEqual(patched.json()["status"], "Soumise")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    self.assertEqual(request.status, "Soumise")
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, original_revision)
                    self.assertEqual(
                        self._active_requirement(session, request.id).planned_hours,
                        100,
                    )
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                    )
                    self.assertEqual(revision_count, 1)
            finally:
                engine.dispose()

            admin_app = create_api_app(
                database_url,
                actor_name="admin-13e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Réapprobation 121h"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertNotEqual(reference.active_revision_id, original_revision)
                    current_revision = session.get(
                        RequestApprovalRevision,
                        reference.active_revision_id,
                    )
                    assert current_revision is not None
                    self.assertEqual(
                        current_revision.previous_revision_id,
                        original_revision,
                    )
                    self.assertEqual(
                        self._active_requirement(session, request.id).planned_hours,
                        121,
                    )
            finally:
                engine.dispose()

    def test_admin_widening_directly_creates_and_activates_new_revision(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            number = self._create_and_approve(database_url)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    original_revision = reference.active_revision_id
            finally:
                engine.dispose()

            admin_app = create_api_app(
                database_url,
                actor_name="admin-13e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 150,
                        "comment": "Élargissement direct par approbateur",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertFalse(patched.json()["reapproval_required"])
                demand = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(demand.status_code, 200, demand.text)
                self.assertEqual(demand.json()["status"], "En planification")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = self._request(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    assert reference is not None
                    self.assertNotEqual(reference.active_revision_id, original_revision)
                    current_revision = session.get(
                        RequestApprovalRevision,
                        reference.active_revision_id,
                    )
                    assert current_revision is not None
                    self.assertEqual(
                        current_revision.previous_revision_id,
                        original_revision,
                    )
                    self.assertEqual(
                        self._active_requirement(session, request.id).planned_hours,
                        150,
                    )
                    revisions = session.scalars(
                        select(RequestApprovalRevision)
                        .where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                        .order_by(RequestApprovalRevision.created_at)
                    ).all()
                    self.assertEqual(len(revisions), 2)
                    self.assertEqual(revisions[0].id, original_revision)
                    direct_audit = session.scalar(
                        select(WorkforceRequestHistory)
                        .where(
                            WorkforceRequestHistory.workforce_request_id == request.id,
                            WorkforceRequestHistory.action
                            == "Autorisation élargie par approbateur",
                        )
                        .order_by(WorkforceRequestHistory.occurred_at.desc())
                    )
                    self.assertIsNotNone(direct_audit)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
