from __future__ import annotations

from functools import partial

from datetime import date, time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    Base,
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server import create_api_app
from app.infrastructure.sql.operational_choice_models import (
    PLANNING_TARGET_EXPLICIT_NONE,
    RequestRequirementPlanningIntent,
)
from app.application.security import AuthPrincipal, ROLE_PROJECT_MANAGER
from app.server.security import static_auth_resolver


WORK_DAY = date(2026, 8, 24)  # lundi


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER, test_admin_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ServerCommandRouteTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet API"))
        session.add(Resource(id="R1", name="Alice", active=True))
        session.add(Competency(id="C1", name="PLC", active=True, sort_order=0))
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
        seed_test_approval_routing(
            session,
            map_existing_tasks=True,
            admin_display_name="Jean",
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="api.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> tuple[str, Path]:
        path = Path(directory) / "api.db"
        return self._database_template.copy_to(directory), path

    def test_detail_projects_partial_line_approval_without_global_approval(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "priority": "Normale",
                        "description": "Multi-approbation #276D",
                        "lines": [
                            {
                                "position": 0,
                                "desired_start": "2026-08-24",
                                "desired_end": "2026-08-24",
                                "estimated_hours": 8,
                                "required_resource_class": "AUTOMATION",
                                "required_competency_ids": [],
                                "confirmation": "Confirmée",
                            },
                            {
                                "position": 1,
                                "desired_start": "2026-08-25",
                                "desired_end": "2026-08-25",
                                "estimated_hours": 8,
                                "required_resource_class": "ELECTRICAL",
                                "required_competency_ids": [],
                                "confirmation": "Confirmée",
                            },
                        ],
                        "submit": True,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                detail = client.get(f"/api/v1/demands/{number}/detail")
                self.assertEqual(detail.status_code, 200, detail.text)
                before = detail.json()
                cycle = before["approval_cycle"]
                self.assertIsNotNone(cycle)
                self.assertEqual(cycle["total_requirements"], 2)
                self.assertEqual(cycle["satisfied_requirements"], 0)
                self.assertEqual(len(cycle["actor_approvable_request_line_ids"]), 2)
                self.assertIsNone(before["demand"]["approved_by_name"])
                self.assertIsNone(before["demand"]["approved_at"])
                self.assertIsNone(before["demand"]["approval_comment"])

                first_line_id = cycle["actor_approvable_request_line_ids"][0]
                voted = client.post(
                    f"/api/v1/demands/{number}/approval-votes",
                    headers={"Idempotency-Key": "276d-partial-api"},
                    json={
                        "approval_cycle_id": cycle["approval_cycle_id"],
                        "expected_request_version": before["version"],
                        "request_line_ids": [first_line_id],
                        "decision": "APPROVE",
                        "comment": "Première ligne seulement",
                    },
                )
                self.assertEqual(voted.status_code, 200, voted.text)
                self.assertFalse(voted.json()["quorum_complete"])
                self.assertEqual(voted.json()["satisfied_requirements"], 1)

                refreshed = client.get(f"/api/v1/demands/{number}/detail")
                self.assertEqual(refreshed.status_code, 200, refreshed.text)
                after = refreshed.json()
                self.assertEqual(after["demand"]["status"], "Soumise")
                self.assertEqual(after["approval_cycle"]["satisfied_requirements"], 1)
                self.assertEqual(len(after["approval_cycle"]["actor_approvable_request_line_ids"]), 1)
                self.assertIsNone(after["demand"]["approved_by_name"])
                self.assertIsNone(after["demand"]["approved_at"])
                self.assertIsNone(after["demand"]["approval_comment"])

    def test_create_and_patch_demand_use_canonical_http_fields(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": "2026-08-24",
                        "desired_end": "2026-08-25",
                        "description": "À effacer",
                        "resource_count": 1,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                self.assertEqual(created.json()["status"], "Brouillon")

                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={"description": None},
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                self.assertFalse(patched.json()["reapproval_required"])

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
                    self.assertEqual(request.requester_name, "Jean")
                    self.assertIsNone(request.description)
            finally:
                engine.dispose()

    def test_multi_line_demand_write_resolves_default_hours_and_versions(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "description": "Demande à deux besoins",
                        "lines": [
                            {
                                "required_resource_class": "Automatisation",
                                "required_competency_ids": ["C1"],
                                "proposed_resource_id": "R1",
                                "desired_start": "2026-08-24",
                                "desired_end": "2026-08-26",
                                "desired_active_days": 3
                            },
                            {
                                "required_resource_class": "Électricité",
                                "desired_start": "2026-08-27",
                                "desired_end": "2026-08-28",
                                "desired_active_days": 2,
                                "estimated_hours": 12
                            }
                        ]
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                read = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(read.status_code, 200, read.text)
                demand = read.json()
                self.assertTrue(demand["line_mode"])
                self.assertEqual(demand["version"], 1)
                self.assertEqual(demand["resource_count"], 2)
                self.assertEqual(demand["estimated_hours"], 36.0)
                self.assertEqual(len(demand["lines"]), 2)
                active = [line for line in demand["lines"] if line["active"]]
                self.assertEqual(len(active), 2)
                self.assertEqual(active[0]["estimated_hours"], 24.0)
                self.assertEqual(active[0]["estimated_hours_source"], "DEFAULT_8H")
                self.assertEqual(active[0]["default_hours_per_day"], 8.0)
                self.assertEqual(active[0]["required_competency_ids"], ["C1"])
                self.assertEqual(active[0]["proposed_resource_id"], "R1")
                self.assertEqual(active[0]["proposed_resource"], "Alice")
                self.assertEqual(active[1]["estimated_hours"], 12.0)
                self.assertEqual(active[1]["estimated_hours_source"], "EXPLICIT")

                first_id = active[0]["line_id"]
                removed_id = active[1]["line_id"]
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "expected_version": 1,
                        "lines": [
                            {
                                "id": first_id,
                                "required_resource_class": "Automatisation",
                                "required_competency_ids": ["C1"],
                                "proposed_resource_id": "R1",
                                "desired_start": "2026-08-24",
                                "desired_end": "2026-08-26",
                                "desired_active_days": 3
                            },
                            {
                                "required_resource_class": "Instrumentation",
                                "desired_start": "2026-08-31",
                                "desired_end": "2026-09-01",
                                "estimated_hours": 8
                            }
                        ]
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)

                updated = client.get(f"/api/v1/demands/{number}").json()
                self.assertEqual(updated["version"], 2)
                active = [line for line in updated["lines"] if line["active"]]
                inactive = [line for line in updated["lines"] if not line["active"]]
                self.assertEqual(len(active), 2)
                self.assertEqual(len(inactive), 1)
                self.assertEqual(active[0]["line_id"], first_id)
                self.assertEqual(inactive[0]["line_id"], removed_id)
                self.assertEqual(updated["estimated_hours"], 32.0)

                stale = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "expected_version": 1,
                        "lines": [
                            {
                                "id": first_id,
                                "desired_start": "2026-08-24",
                                "desired_active_days": 1
                            }
                        ]
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "demand_version_conflict",
                )

    def test_legacy_single_line_can_switch_authority_without_changing_line_id(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": "2026-08-24",
                        "estimated_hours": 8
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                legacy = client.get(f"/api/v1/demands/{number}").json()
                self.assertFalse(legacy["line_mode"])
                self.assertEqual(len(legacy["lines"]), 1)
                legacy_line_id = legacy["lines"][0]["line_id"]

                converted = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "expected_version": legacy["version"],
                        "lines": [
                            {
                                "id": legacy_line_id,
                                "desired_start": "2026-08-24",
                                "desired_active_days": 1
                            }
                        ]
                    },
                )
                self.assertEqual(converted.status_code, 200, converted.text)

                current = client.get(f"/api/v1/demands/{number}").json()
                self.assertTrue(current["line_mode"])
                self.assertEqual(current["lines"][0]["line_id"], legacy_line_id)
                self.assertEqual(current["lines"][0]["estimated_hours"], 8.0)
                self.assertEqual(
                    current["lines"][0]["estimated_hours_source"],
                    "DEFAULT_8H",
                )

                flat_edit = client.patch(
                    f"/api/v1/demands/{number}",
                    json={"estimated_hours": 4},
                )
                self.assertEqual(flat_edit.status_code, 422, flat_edit.text)
                self.assertEqual(
                    flat_edit.json()["error"]["code"],
                    "demand_update_invalid",
                )

    def test_multi_line_payload_cannot_mix_legacy_need_fields(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": "2026-08-24",
                        "lines": [
                            {
                                "desired_start": "2026-08-24",
                                "estimated_hours": 8
                            }
                        ]
                    }),
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "request_validation_error",
            )

    def test_line_authored_demand_can_be_approved_with_288e(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "submit": True,
                        "lines": [
                            {
                                "desired_start": "2026-08-24",
                                "desired_active_days": 1
                            }
                        ]
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "test"},
                )

            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "En planification")

    def test_issue_781_four_case_matrix_keeps_proposal_advisory_and_residual_queued(self) -> None:
        cases = (
            ("default-proposed", None, "R1", 8.0, "DEFAULT_8H"),
            ("default-none", None, None, 8.0, "DEFAULT_8H"),
            ("explicit-proposed", 4, "R1", 4.0, "EXPLICIT"),
            ("explicit-none-1h", 1, None, 1.0, "EXPLICIT"),
            ("explicit-none-4h", 4, None, 4.0, "EXPLICIT"),
            ("explicit-none-7h", 7, None, 7.0, "EXPLICIT"),
            ("explicit-none-8h", 8, None, 8.0, "EXPLICIT"),
        )
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    for label, explicit_hours, proposed_resource_id, expected_hours, source in cases:
                        line = {
                            "desired_start": WORK_DAY.isoformat(),
                            "desired_end": WORK_DAY.isoformat(),
                            "proposed_resource_id": proposed_resource_id,
                        }
                        if explicit_hours is None:
                            line["desired_active_days"] = 1
                        else:
                            line["estimated_hours"] = explicit_hours

                        created = client.post(
                            "/api/v1/demands",
                            json=routed_demand_payload({
                                "project_number": "P-1",
                                "description": f"#781 {label}",
                                "submit": True,
                                "lines": [line],
                            }),
                        )
                        self.assertEqual(created.status_code, 201, created.text)
                        number = created.json()["demand_number"]

                        candidate = client.get(f"/api/v1/demands/{number}")
                        self.assertEqual(candidate.status_code, 200, candidate.text)
                        candidate_line = candidate.json()["lines"][0]
                        self.assertEqual(candidate_line["estimated_hours"], expected_hours)
                        self.assertEqual(candidate_line["estimated_hours_source"], source)
                        self.assertEqual(
                            candidate_line["proposed_resource_id"],
                            proposed_resource_id,
                        )

                        approved = client.post(
                            f"/api/v1/demands/{number}/approve",
                            json={"comment": f"Approbation #781 {label}"},
                        )
                        self.assertEqual(approved.status_code, 200, approved.text)

                        actions = client.get(
                            "/api/v1/planning/actions",
                            params={
                                "start": WORK_DAY.isoformat(),
                                "end": WORK_DAY.isoformat(),
                                "scope": "global",
                            },
                        )
                        self.assertEqual(actions.status_code, 200, actions.text)
                        assignment = next(
                            row
                            for row in actions.json()
                            if row["kind"] == "ASSIGNMENT"
                            and row["demand_number"] == number
                        )
                        self.assertEqual(assignment["planned_hours"], expected_hours)

                        segment = client.get(
                            f"/api/v1/segments/{assignment['segment_id']}",
                            params={"scope": "global"},
                        )
                        self.assertEqual(segment.status_code, 200, segment.text)
                        segment_payload = segment.json()
                        self.assertEqual(segment_payload["planned_hours"], expected_hours)
                        self.assertEqual(segment_payload["covered_hours"], 0.0)
                        self.assertEqual(segment_payload["remaining_hours"], expected_hours)
                        self.assertIsNone(
                            segment_payload["automatic_target_resource_id"]
                        )

                        with factory() as session:
                            request = session.scalar(
                                select(WorkforceRequest).where(
                                    WorkforceRequest.legacy_demand_number == number
                                )
                            )
                            self.assertIsNotNone(request)
                            requirement = session.scalar(
                                select(ResourceRequirement).where(
                                    ResourceRequirement.workforce_request_id == request.id,
                                    ResourceRequirement.status != "Annulé",
                                )
                            )
                            self.assertIsNotNone(requirement)
                            self.assertIsNone(requirement.assigned_resource_id)
                            self.assertEqual(
                                float(requirement.planned_hours),
                                expected_hours,
                            )
                            intent = session.scalar(
                                select(RequestRequirementPlanningIntent).where(
                                    RequestRequirementPlanningIntent.current_resource_requirement_id
                                    == requirement.id
                                )
                            )
                            self.assertIsNotNone(intent)
                            self.assertEqual(
                                intent.target_mode,
                                PLANNING_TARGET_EXPLICIT_NONE,
                            )
                            self.assertIsNone(intent.target_resource_id)
                            shift_count = session.scalar(
                                select(func.count())
                                .select_from(Shift)
                                .where(
                                    Shift.resource_requirement_id == requirement.id
                                )
                            )
                            self.assertEqual(int(shift_count or 0), 0)
            finally:
                engine.dispose()

    def test_unknown_project_returns_structured_not_found(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-UNKNOWN",
                        "desired_start": "2026-08-24",
                    }),
                )

            self.assertEqual(response.status_code, 404)
            payload = response.json()["error"]
            self.assertEqual(payload["code"], "demand_create_not_found")
            self.assertIn("Projet P-UNKNOWN", payload["message"])

    def test_unknown_http_field_is_rejected_and_not_silently_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": "2026-08-24",
                        "NumeroProjet": "legacy-field-must-not-work",
                    }),
                )

            self.assertEqual(response.status_code, 422)
            payload = response.json()["error"]
            self.assertEqual(payload["code"], "request_validation_error")
            locations = [item["location"] for item in payload["context"]["errors"]]
            self.assertTrue(any("NumeroProjet" in location for location in locations))

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    count = session.scalar(
                        select(func.count()).select_from(WorkforceRequest)
                    )
                    self.assertEqual(int(count or 0), 0)
            finally:
                engine.dispose()

    def test_segment_patch_rejects_explicit_null_for_non_nullable_boolean(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/segments/SEG-UNKNOWN",
                    json={"outside_standard_hours": None},
                )

            self.assertEqual(response.status_code, 422)
            payload = response.json()["error"]
            self.assertEqual(payload["code"], "request_validation_error")
            locations = [item["location"] for item in payload["context"]["errors"]]
            self.assertTrue(any("outside_standard_hours" in location for location in locations))

    def test_quick_shift_route_creates_locked_shift_without_fake_request(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/quick-shifts",
                    json={
                        "project_number": "P-1",
                        "technician": "Alice",
                        "day": WORK_DAY.isoformat(),
                        "hours": 4,
                        "description": "Urgence",
                    },
                )

            self.assertEqual(response.status_code, 201, response.text)
            result = response.json()
            self.assertTrue(result["segment_id"])
            self.assertTrue(result["allocation_id"])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    requirement = session.scalar(
                        select(ResourceRequirement).where(
                            ResourceRequirement.legacy_segment_id == result["segment_id"]
                        )
                    )
                    shift = session.scalar(
                        select(Shift).where(
                            Shift.legacy_allocation_id == result["allocation_id"]
                        )
                    )
                    request_count = session.scalar(
                        select(func.count()).select_from(WorkforceRequest)
                    )
                    self.assertIsNotNone(requirement)
                    assert requirement is not None
                    self.assertIsNone(requirement.workforce_request_id)
                    self.assertEqual(requirement.origin, "QUICK_SHIFT")
                    self.assertIsNotNone(shift)
                    assert shift is not None
                    self.assertTrue(shift.locked)
                    self.assertEqual(float(shift.hours), 4.0)
                    self.assertEqual(int(request_count or 0), 0)
            finally:
                engine.dispose()


    def test_demand_workflow_policy_rejects_direct_invalid_and_stale_transitions(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": WORK_DAY.isoformat(),
                        "estimated_hours": 8,
                        "priority": "Normale",
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                workflow = client.get(
                    f"/api/v1/demands/{number}/workflow-actions"
                )
                self.assertEqual(workflow.status_code, 200, workflow.text)
                before = workflow.json()
                self.assertEqual(before["status"], "Brouillon")
                self.assertEqual(
                    set(before["available_actions"]),
                    {"modify", "submit", "cancel"},
                )

                invalid_approval = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={
                        "comment": "Contournement direct",
                        "expected_version": before["version"],
                    },
                )
                self.assertEqual(invalid_approval.status_code, 409, invalid_approval.text)
                self.assertEqual(
                    invalid_approval.json()["error"]["code"],
                    "demand_transition_invalid",
                )

                stale_submit = client.post(
                    f"/api/v1/demands/{number}/submit",
                    json={"expected_version": before["version"] + 99},
                )
                self.assertEqual(stale_submit.status_code, 409, stale_submit.text)
                self.assertEqual(
                    stale_submit.json()["error"]["code"],
                    "demand_version_conflict",
                )

                submitted = client.post(
                    f"/api/v1/demands/{number}/submit",
                    json={"expected_version": before["version"]},
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                self.assertEqual(submitted.json()["status"], "Soumise")

                after = client.get(
                    f"/api/v1/demands/{number}/workflow-actions"
                )
                self.assertEqual(after.status_code, 200, after.text)
                state = after.json()
                self.assertEqual(state["status"], "Soumise")
                self.assertIn("approve", state["available_actions"])
                self.assertIn("correction", state["available_actions"])
                self.assertNotIn("submit", state["available_actions"])
                self.assertNotIn("emergency-plan", state["available_actions"])

                repeated_submit = client.post(
                    f"/api/v1/demands/{number}/submit",
                    json={"expected_version": state["version"]},
                )
                self.assertEqual(repeated_submit.status_code, 409, repeated_submit.text)
                self.assertEqual(
                    repeated_submit.json()["error"]["code"],
                    "demand_transition_invalid",
                )


            project_manager = AuthPrincipal.from_roles(
                local_user_id="pm-1",
                issuer="urn:test",
                subject="pm-subject",
                display_name="Chargé de projet",
                email=None,
                roles=(ROLE_PROJECT_MANAGER,),
                auth_mode="test",
            )
            project_manager_app = create_api_app(
                database_url,
                actor_name="Chargé de projet",
                auth_resolver=static_auth_resolver(project_manager),
            )
            with TestClient(
                project_manager_app,
                raise_server_exceptions=False,
            ) as project_manager_client:
                restricted = project_manager_client.get(
                    f"/api/v1/demands/{number}/workflow-actions"
                )
                self.assertEqual(restricted.status_code, 200, restricted.text)
                available = set(restricted.json()["available_actions"])
                self.assertIn("modify", available)
                self.assertIn("cancel", available)
                self.assertNotIn("approve", available)
                self.assertNotIn("correction", available)


    def test_saved_demand_reloads_canonical_version_and_real_concurrency_still_conflicts(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _ = self._database(directory)
            first_app = create_api_app(
                database_url,
                actor_name="Jean",
                auth_resolver=test_admin_auth_resolver("Jean"),
            )
            concurrent_app = create_api_app(
                database_url,
                actor_name="Marie",
                auth_resolver=test_admin_auth_resolver("Marie"),
            )
            with (
                TestClient(first_app, raise_server_exceptions=False) as first_client,
                TestClient(concurrent_app, raise_server_exceptions=False) as concurrent_client,
            ):
                created = first_client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": WORK_DAY.isoformat(),
                        "estimated_hours": 8,
                        "priority": "Normale",
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                before_save = first_client.get(
                    f"/api/v1/demands/{number}/detail"
                )
                self.assertEqual(before_save.status_code, 200, before_save.text)
                stale_version = before_save.json()["version"]

                saved = first_client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "description": "Modification avant soumission",
                        "expected_version": stale_version,
                    },
                )
                self.assertEqual(saved.status_code, 200, saved.text)

                canonical = first_client.get(
                    f"/api/v1/demands/{number}/detail"
                )
                self.assertEqual(canonical.status_code, 200, canonical.text)
                canonical_body = canonical.json()
                self.assertGreater(canonical_body["version"], stale_version)
                self.assertEqual(
                    canonical_body["workflow"]["version"],
                    canonical_body["version"],
                )
                self.assertEqual(
                    canonical_body["policy"]["expected_request_version"],
                    canonical_body["version"],
                )

                submitted = first_client.post(
                    f"/api/v1/demands/{number}/submit",
                    json={"expected_version": canonical_body["workflow"]["version"]},
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                self.assertEqual(submitted.json()["status"], "Soumise")

                created_concurrent = first_client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": WORK_DAY.isoformat(),
                        "estimated_hours": 4,
                        "priority": "Normale",
                    }),
                )
                self.assertEqual(
                    created_concurrent.status_code,
                    201,
                    created_concurrent.text,
                )
                concurrent_number = created_concurrent.json()["demand_number"]
                first_snapshot = first_client.get(
                    f"/api/v1/demands/{concurrent_number}/detail"
                )
                self.assertEqual(first_snapshot.status_code, 200, first_snapshot.text)
                first_body = first_snapshot.json()

                concurrent_change = concurrent_client.patch(
                    f"/api/v1/demands/{concurrent_number}",
                    json={
                        "description": "Modification réellement concurrente",
                        "expected_version": first_body["version"],
                    },
                )
                self.assertEqual(
                    concurrent_change.status_code,
                    200,
                    concurrent_change.text,
                )

                stale_submit = first_client.post(
                    f"/api/v1/demands/{concurrent_number}/submit",
                    json={"expected_version": first_body["workflow"]["version"]},
                )
                self.assertEqual(stale_submit.status_code, 409, stale_submit.text)
                self.assertEqual(
                    stale_submit.json()["error"]["code"],
                    "demand_version_conflict",
                )

                refreshed = first_client.get(
                    f"/api/v1/demands/{concurrent_number}/detail"
                )
                self.assertEqual(refreshed.status_code, 200, refreshed.text)
                refreshed_body = refreshed.json()
                self.assertGreater(refreshed_body["version"], first_body["version"])
                self.assertEqual(
                    refreshed_body["workflow"]["version"],
                    refreshed_body["version"],
                )

                retried_submit = first_client.post(
                    f"/api/v1/demands/{concurrent_number}/submit",
                    json={"expected_version": refreshed_body["workflow"]["version"]},
                )
                self.assertEqual(retried_submit.status_code, 200, retried_submit.text)
                self.assertEqual(retried_submit.json()["status"], "Soumise")


if __name__ == "__main__":
    unittest.main()
