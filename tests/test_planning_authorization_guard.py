from __future__ import annotations

from datetime import date, time, timedelta
from tempfile import TemporaryDirectory
import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    PlanningChangeHistory,
    Project,
    RequestApprovalReference,
    RequestApprovalRevision,
    RequestOperationalState,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    SqlRequestApprovalRevisionRepository,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)
from tests.sqlite_test_template import SqliteDatabaseTemplate


DAY = date(2026, 9, 22)


class PlanningAuthorizationGuardTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet 13F"))
        session.add(Resource(id="R1", name="Alice", active=True))
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="STD-R1",
                resource_id="R1",
                availability_type="Horaire standard",
                weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                start_time=time(8, 0),
                end_time=time(20, 0),
                active=True,
            )
        )
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-authorization-13f.db",
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
        hours: float = 8,
        site_client: str | None = None,
    ) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "desired_start": DAY.isoformat(),
                "desired_end": DAY.isoformat(),
                "estimated_hours": hours,
                "proposed_technician": "Alice",
                **({"site_client": site_client} if site_client is not None else {}),
                "submit": True,
            }),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Autorisation 13F"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _request_and_requirement(factory, number: str):
        with factory() as session:
            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            assert request is not None
            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.status != "Annulé",
                )
            )
            assert requirement is not None
            return request.id, requirement.id, requirement.legacy_segment_id or requirement.id

    def test_direct_request_segment_widening_is_blocked_but_quick_shift_stays_ad_hoc(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13f",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client)

                direct_create = client.post(
                    "/api/v1/segments",
                    json={
                        "demand_number": number,
                        "project_number": "P-1",
                        "start_date": DAY.isoformat(),
                        "end_date": DAY.isoformat(),
                        "planned_hours": 4,
                    },
                    headers={"Idempotency-Key": "13f-direct-create"},
                )
                self.assertEqual(direct_create.status_code, 409, direct_create.text)
                self.assertEqual(
                    direct_create.json()["error"]["code"],
                    "planning_authorization_entry_required",
                )

                quick = client.post(
                    "/api/v1/quick-shifts",
                    json={
                        "project_number": "P-1",
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 2,
                        "project_name": "Projet 13F",
                    },
                    headers={"Idempotency-Key": "13f-quick-shift"},
                )
                self.assertEqual(quick.status_code, 201, quick.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                request_id, _requirement_id, segment_id = self._request_and_requirement(
                    factory,
                    number,
                )
                with TestClient(admin_app, raise_server_exceptions=False) as client:
                    structural = client.patch(
                        f"/api/v1/segments/{segment_id}",
                        json={
                            "start_date": date(2026, 9, 23).isoformat(),
                            "end_date": date(2026, 9, 23).isoformat(),
                        },
                    )
                    self.assertEqual(structural.status_code, 409, structural.text)
                    self.assertEqual(
                        structural.json()["error"]["code"],
                        "planning_authorization_scope_change",
                    )
                with factory() as session:
                    linked = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request_id
                        )
                    ).all()
                    self.assertEqual(len(linked), 1)
                    ad_hoc = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id.is_(None)
                        )
                    ).all()
                    self.assertTrue(ad_hoc)
            finally:
                engine.dispose()

    def test_increase_planned_cannot_bypass_revision_but_keep_exception_can(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-13f",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client, hours=8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                request_id, _requirement_id, segment_id = self._request_and_requirement(
                    factory,
                    number,
                )
                with factory() as session:
                    reference = session.get(RequestApprovalReference, request_id)
                    assert reference is not None
                    revision_id = reference.active_revision_id

                payload = {
                    "technician": "Alice",
                    "day": DAY.isoformat(),
                    "hours": 9,
                    "outside_standard_hours": False,
                    "confirmation": None,
                }
                with TestClient(app, raise_server_exceptions=False) as client:
                    blocked = client.post(
                        f"/api/v1/segments/{segment_id}/allocations",
                        json={**payload, "overallocation_policy": "INCREASE_PLANNED"},
                        headers={"Idempotency-Key": "13f-increase-blocked"},
                    )
                    self.assertEqual(blocked.status_code, 409, blocked.text)
                    self.assertEqual(
                        blocked.json()["error"]["code"],
                        "planning_authorization_revision_required",
                    )

                    kept = client.post(
                        f"/api/v1/segments/{segment_id}/allocations",
                        json={**payload, "overallocation_policy": "KEEP_EXCEPTION"},
                        headers={"Idempotency-Key": "13f-keep-exception"},
                    )
                    self.assertEqual(kept.status_code, 201, kept.text)

                    segment = client.get(f"/api/v1/segments/{segment_id}")
                    self.assertEqual(segment.status_code, 200, segment.text)
                    self.assertEqual(segment.json()["planned_hours"], 8.0)
                    self.assertEqual(segment.json()["locked_hours"], 9.0)
                    self.assertEqual(segment.json()["overallocated_hours"], 1.0)

                with factory() as session:
                    reference = session.get(RequestApprovalReference, request_id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, revision_id)
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request_id
                        )
                    )
                    self.assertEqual(revision_count, 1)
                    audit = session.scalars(
                        select(PlanningChangeHistory).where(
                            PlanningChangeHistory.entity_reference == segment_id
                        )
                    ).all()
                    self.assertTrue(
                        any(
                            row.action == "Dérogation surallocation manuelle"
                            for row in audit
                        )
                    )
            finally:
                engine.dispose()

    def test_project_manager_twenty_percent_updates_active_budget_not_approved_revision(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13f",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client, hours=10)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                request_id, _requirement_id, _segment_id = self._request_and_requirement(
                    factory,
                    number,
                )
                with factory() as session:
                    reference = session.get(RequestApprovalReference, request_id)
                    assert reference is not None
                    revision_id = reference.active_revision_id
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-13f",
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 12,
                        "comment": "Tolérance déléguée active",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertFalse(patched.json()["reapproval_required"])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, request_id)
                    assert request is not None
                    requirement = session.scalar(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request_id,
                            ResourceRequirement.status != "Annulé",
                        )
                    )
                    assert requirement is not None
                    self.assertEqual(float(requirement.planned_hours), 12.0)
                    reference = session.get(RequestApprovalReference, request_id)
                    assert reference is not None
                    self.assertEqual(reference.active_revision_id, revision_id)
                    revision = session.get(RequestApprovalRevision, revision_id)
                    assert revision is not None
                    payload = json.loads(revision.payload_text)
                    approved_entry = payload["authorization"]["entries"][0]
                    self.assertEqual(float(approved_entry["hours"]), 10.0)
                    state = session.get(RequestOperationalState, request_id)
                    assert state is not None
                    overrides = json.loads(state.budget_overrides_text)
                    self.assertEqual(len(overrides), 1)
                    self.assertEqual(float(next(iter(overrides.values()))), 12.0)
                    self.assertGreaterEqual(state.version, 2)
            finally:
                engine.dispose()

    def test_existing_delegated_budget_exception_survives_same_authority_revision(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13f-revision",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client, hours=10)

            pm_app = create_api_app(
                database_url,
                actor_name="pm-13f-revision",
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "estimated_hours": 12,
                        "comment": "Tolérance déléguée persistante",
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertFalse(patched.json()["reapproval_required"])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    self.assertIsNotNone(request)
                    reference = session.get(RequestApprovalReference, request.id)
                    self.assertIsNotNone(reference)
                    previous_revision = session.get(
                        RequestApprovalRevision,
                        reference.active_revision_id,
                    )
                    self.assertIsNotNone(previous_revision)
                    state = session.get(RequestOperationalState, request.id)
                    self.assertIsNotNone(state)

                    previous_payload = json.loads(previous_revision.payload_text)
                    previous_entry = previous_payload["authorization"]["entries"][0]
                    entry_key = str(previous_entry["identity"])
                    self.assertEqual(float(previous_entry["hours"]), 10.0)
                    overrides_before = json.loads(state.budget_overrides_text)
                    self.assertEqual(overrides_before, {entry_key: "12.00"})

                    # Build a genuine successor approval with the same immutable
                    # 10 h local authority. Calling sync_approved() here would
                    # approve the current 12 h candidate and would therefore
                    # test 12-approved/12-operational redundancy instead of
                    # same-authority delegated-exception carry-forward.
                    successor = RequestApprovalRevision(
                        workforce_request_id=request.id,
                        previous_revision_id=previous_revision.id,
                        request_version=previous_revision.request_version + 1,
                        approved_by_external_id=previous_revision.approved_by_external_id,
                        approved_by_name=previous_revision.approved_by_name,
                        approved_at=previous_revision.approved_at,
                        provenance=previous_revision.provenance,
                        payload_format_version=previous_revision.payload_format_version,
                        payload_text=previous_revision.payload_text,
                        authorization_fingerprint=previous_revision.authorization_fingerprint,
                    )
                    session.add(successor)
                    session.flush()

                    successor_payload = json.loads(successor.payload_text)
                    successor_entry = successor_payload["authorization"]["entries"][0]
                    self.assertEqual(str(successor_entry["identity"]), entry_key)
                    self.assertEqual(float(successor_entry["hours"]), 10.0)

                    revisions = SqlRequestApprovalRevisionRepository(session)
                    revisions.bind_materialized_requirements(request, successor)
                    revisions.activate_revision(request, successor)

                    refreshed_reference = session.get(
                        RequestApprovalReference,
                        request.id,
                    )
                    self.assertIsNotNone(refreshed_reference)
                    self.assertEqual(
                        refreshed_reference.active_revision_id,
                        successor.id,
                    )
                    state = session.get(RequestOperationalState, request.id)
                    self.assertIsNotNone(state)
                    self.assertEqual(state.approval_revision_id, successor.id)
                    overrides_after = json.loads(state.budget_overrides_text)
                    self.assertEqual(overrides_after, {entry_key: "12.00"})
            finally:
                engine.dispose()



    def test_delegated_budget_exception_survives_multiple_compatible_reapprovals(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13f-lineage",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "submit": True,
                        "lines": [
                            {
                                "desired_start": DAY.isoformat(),
                                "desired_end": DAY.isoformat(),
                                "estimated_hours": 10,
                                "proposed_resource_id": "R1",
                                "description": "Line A delegated",
                            },
                            {
                                "desired_start": DAY.isoformat(),
                                "desired_end": DAY.isoformat(),
                                "estimated_hours": 8,
                                "description": "Line B revision driver",
                            },
                        ],
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "R1"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                demand = client.get(f"/api/v1/demands/{number}").json()
                active_lines = [row for row in demand["lines"] if row["active"]]
                self.assertEqual(len(active_lines), 2)
                line_a = next(row for row in active_lines if row["description"] == "Line A delegated")
                line_b = next(row for row in active_lines if row["description"] == "Line B revision driver")

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
                    request_id = request.id
                    reference = session.get(RequestApprovalReference, request_id)
                    self.assertIsNotNone(reference)
                    r1 = reference.active_revision_id
                    requirement_a = session.scalar(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request_id,
                            ResourceRequirement.source_request_line_id == line_a["line_id"],
                            ResourceRequirement.status != "Annulé",
                        )
                    )
                    self.assertIsNotNone(requirement_a)
                    segment_a = requirement_a.legacy_segment_id or requirement_a.id

                with TestClient(admin_app, raise_server_exceptions=False) as client:
                    assigned = client.post(
                        f"/api/v1/segments/{segment_a}/assign",
                        json={"resource_id": "R1"},
                    )
                    self.assertEqual(assigned.status_code, 200, assigned.text)

                pm_app = create_api_app(
                    database_url,
                    actor_name="pm-13f-lineage",
                    auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
                )
                with TestClient(pm_app, raise_server_exceptions=False) as client:
                    increased = client.post(
                        f"/api/v1/segments/{segment_a}/allocations",
                        json={
                            "technician": "Alice",
                            "day": DAY.isoformat(),
                            "hours": 2,
                            "outside_standard_hours": False,
                            "confirmation": None,
                            "overallocation_policy": "INCREASE_PLANNED",
                        },
                        headers={"Idempotency-Key": "13f-lineage-increase"},
                    )
                    self.assertEqual(increased.status_code, 201, increased.text)

                def line_payload(row: dict[str, object]) -> dict[str, object]:
                    keys = (
                        "line_id",
                        "position",
                        "kind",
                        "required_resource_class",
                        "required_competency_ids",
                        "desired_start",
                        "desired_end",
                        "desired_active_days",
                        "estimated_hours",
                        "work_package_ref",
                        "task_code",
                        "proposed_resource_id",
                        "confirmation",
                        "description",
                    )
                    payload = {
                        ("id" if key == "line_id" else key): row.get(key)
                        for key in keys
                        if row.get(key) is not None
                    }
                    return payload

                active_revisions = [r1]
                for offset in (1, 2, 3):
                    with TestClient(pm_app, raise_server_exceptions=False) as client:
                        current = client.get(f"/api/v1/demands/{number}")
                        self.assertEqual(current.status_code, 200, current.text)
                        current_body = current.json()
                        lines = [row for row in current_body["lines"] if row["active"]]
                        current_a = next(row for row in lines if row["line_id"] == line_a["line_id"])
                        current_b = next(row for row in lines if row["line_id"] == line_b["line_id"])
                        next_day = DAY + timedelta(days=offset)
                        changed_b = line_payload(current_b)
                        changed_b["desired_start"] = next_day.isoformat()
                        changed_b["desired_end"] = next_day.isoformat()
                        patched = client.patch(
                            f"/api/v1/demands/{number}",
                            json={
                                "expected_version": current_body["version"],
                                "lines": [line_payload(current_a), changed_b],
                                "comment": f"Line B compatible successor {offset}",
                            },
                        )
                        self.assertEqual(patched.status_code, 200, patched.text)
                        self.assertTrue(patched.json()["reapproval_required"])

                    with TestClient(admin_app, raise_server_exceptions=False) as client:
                        reapproved = client.post(
                            f"/api/v1/demands/{number}/approve",
                            json={"comment": f"R{offset + 1}"},
                        )
                        self.assertEqual(reapproved.status_code, 200, reapproved.text)
                        approval_state = client.get(
                            f"/api/v1/demands/{number}/approval-state"
                        )
                        self.assertEqual(
                            approval_state.status_code,
                            200,
                            approval_state.text,
                        )
                        active_revisions.append(
                            approval_state.json()["active_revision_id"]
                        )
                        self.assertEqual(
                            list(
                                approval_state.json()[
                                    "active_budget_overrides"
                                ].values()
                            ),
                            [12.0],
                        )
                        segment = client.get(f"/api/v1/segments/{segment_a}")
                        self.assertEqual(segment.status_code, 200, segment.text)
                        self.assertEqual(segment.json()["planned_hours"], 12.0)

                with factory() as session:
                    revisions = session.scalars(
                        select(RequestApprovalRevision).where(
                            RequestApprovalRevision.workforce_request_id == request_id
                        )
                    ).all()
                    self.assertEqual(len(revisions), 4)
                    self.assertEqual(len(set(active_revisions)), 4)
                    by_id = {row.id: row for row in revisions}
                    for revision_id in active_revisions:
                        payload = json.loads(by_id[revision_id].payload_text)
                        entry_a = next(
                            row
                            for row in payload["authorization"]["entries"]
                            if row["identity"].find(line_a["line_id"]) >= 0
                        )
                        self.assertEqual(float(entry_a["hours"]), 10.0)

                    budget_history = session.scalars(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id == request_id,
                            WorkforceRequestHistory.action == "Budget opérationnel",
                        )
                    ).all()
                    delegated_events = []
                    for row in budget_history:
                        details = json.loads(row.details or "{}")
                        if float(details.get("operational_hours") or 0) == 12.0:
                            delegated_events.append(details)
                    self.assertEqual(len(delegated_events), 1)
                    self.assertEqual(
                        delegated_events[0]["approval_revision_id"],
                        r1,
                    )
                    state = session.get(RequestOperationalState, request_id)
                    self.assertIsNotNone(state)
                    self.assertEqual(state.approval_revision_id, active_revisions[-1])
                    self.assertEqual(
                        list(json.loads(state.budget_overrides_text).values()),
                        ["12.00"],
                    )
            finally:
                engine.dispose()


    def test_delegated_budget_exception_cannot_cross_changed_site_revision(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13f-scope",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(
                    client,
                    hours=10,
                    site_client="Site A",
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                request_id, _requirement_id, segment_id = self._request_and_requirement(
                    factory,
                    number,
                )
                with factory() as session:
                    reference = session.get(RequestApprovalReference, request_id)
                    self.assertIsNotNone(reference)
                    previous_revision_id = reference.active_revision_id
                    previous_revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request_id
                        )
                    )
                    self.assertEqual(previous_revision_count, 1)

                pm_app = create_api_app(
                    database_url,
                    actor_name="pm-13f-scope",
                    auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
                )
                with TestClient(pm_app, raise_server_exceptions=False) as client:
                    delegated = client.patch(
                        f"/api/v1/demands/{number}",
                        json={
                            "estimated_hours": 12,
                            "comment": "Tolérance déléguée Site A",
                        },
                    )
                    self.assertEqual(delegated.status_code, 200, delegated.text)
                    self.assertFalse(delegated.json()["reapproval_required"])

                    changed_scope = client.patch(
                        f"/api/v1/demands/{number}",
                        json={
                            "estimated_hours": 10,
                            "site_client": "Site B",
                            "comment": "Déplacement vers Site B",
                        },
                    )
                    self.assertEqual(changed_scope.status_code, 200, changed_scope.text)
                    self.assertTrue(changed_scope.json()["reapproval_required"])

                with factory() as session:
                    state = session.get(RequestOperationalState, request_id)
                    self.assertIsNotNone(state)
                    self.assertEqual(state.approval_revision_id, previous_revision_id)
                    overrides_before = json.loads(state.budget_overrides_text)
                    self.assertEqual(len(overrides_before), 1)
                    self.assertEqual(float(next(iter(overrides_before.values()))), 12.0)

                with TestClient(admin_app, raise_server_exceptions=False) as client:
                    reapproved = client.post(
                        f"/api/v1/demands/{number}/approve",
                        json={"comment": "Tentative autorisation Site B"},
                    )
                    self.assertEqual(reapproved.status_code, 409, reapproved.text)
                    self.assertEqual(
                        reapproved.json()["error"]["code"],
                        "operational_budget_reconciliation_required",
                    )

                with factory() as session:
                    request = session.get(WorkforceRequest, request_id)
                    self.assertIsNotNone(request)
                    self.assertEqual(request.site_client, "Site B")
                    reference = session.get(RequestApprovalReference, request_id)
                    self.assertIsNotNone(reference)
                    self.assertEqual(reference.active_revision_id, previous_revision_id)
                    state = session.get(RequestOperationalState, request_id)
                    self.assertIsNotNone(state)
                    self.assertEqual(state.approval_revision_id, previous_revision_id)
                    overrides_after = json.loads(state.budget_overrides_text)
                    self.assertEqual(overrides_after, overrides_before)
                    revision_count = session.scalar(
                        select(func.count(RequestApprovalRevision.id)).where(
                            RequestApprovalRevision.workforce_request_id == request_id
                        )
                    )
                    self.assertEqual(revision_count, 1)
                    requirement = session.get(ResourceRequirement, _requirement_id)
                    self.assertIsNotNone(requirement)
                    self.assertEqual(requirement.approval_revision_id, previous_revision_id)
                    self.assertEqual(float(requirement.planned_hours), 12.0)

                with TestClient(admin_app, raise_server_exceptions=False) as client:
                    segment = client.get(f"/api/v1/segments/{segment_id}")
                    self.assertEqual(segment.status_code, 200, segment.text)
                    self.assertEqual(segment.json()["planned_hours"], 12.0)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
