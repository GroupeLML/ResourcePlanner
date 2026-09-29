"""V2 local API acceptance path for issue #250.

Run only this acceptance scenario with::

    python -m unittest discover -s tests -p "test_v2_local_acceptance.py" -v

The suite intentionally uses a fresh SQLite file and a fake communication
transport.  It must not require SQL Server, Acumatica, Microsoft Graph,
NiceGUI, or Excel.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.communications import (
    CommunicationTransportMessage,
    CommunicationTransportResult,
)
from app.application.security import (
    AuthPrincipal,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.sql import (
    Base,
    Project,
    SqlUserIdentityRepository,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    TaskApprovalScopeMapping,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


ALICE_EMAIL = "alice" + chr(64) + "example.test"
BOB_EMAIL = "bob" + chr(64) + "example.test"


class FakeCommunicationTransport:
    def __init__(self) -> None:
        self.messages: tuple[CommunicationTransportMessage, ...] = ()

    def create_drafts(self, messages):
        self.messages = tuple(messages)
        return CommunicationTransportResult(
            provider="fake_acceptance",
            created_count=len(self.messages),
        )


def _principal(
    role: str,
    *,
    local_user_id: str,
    display_name: str,
    employee_external_id: str | None = None,
) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=local_user_id,
        issuer="urn:resourceplanner:acceptance",
        subject=f"acceptance-{role.lower()}",
        display_name=display_name,
        email=None,
        employee_external_id=employee_external_id,
        roles=(role,),
        auth_mode="test",
    )


class V2LocalAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        database_path = Path(self.temp.name) / "v2-acceptance.db"
        self.database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        self.transport = FakeCommunicationTransport()

        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                Project(
                    id="P-250-ID",
                    number="P-250",
                    name="Projet acceptation V2",
                    client="Client test",
                    project_manager_name="Chargé E2E",
                    status="Actif",
                )
            )
            identities = SqlUserIdentityRepository(session)
            self.user_ids = {}
            for role, display_name, employee_external_id in (
                (ROLE_ADMIN, "Administrateur E2E", None),
                (ROLE_PROJECT_MANAGER, "Chargé E2E", None),
                (ROLE_COORDINATOR, "Coordonnateur E2E", None),
                (ROLE_TECHNICIAN, "Technicien Alice", "EMP-ALICE"),
            ):
                record = identities.upsert(
                    issuer="urn:resourceplanner:acceptance",
                    subject=f"acceptance-{role.lower()}",
                    display_name=display_name,
                    email=None,
                    employee_external_id=employee_external_id,
                    roles=(role,),
                    active=True,
                )
                self.user_ids[role] = record.user_id

            session.add(
                TaskCatalogEntry(
                    id="TASK-P250-APPROVAL",
                    project_number="P-250",
                    task_code="APPROVAL",
                    label="Approbation E2E",
                    active=True,
                )
            )
            session.add(
                ApprovalScope(
                    id="SCOPE-P250",
                    code="P250_APPROVAL",
                    label="Approbation E2E",
                    active=True,
                    version=1,
                )
            )
            session.flush()
            for role in (ROLE_ADMIN, ROLE_COORDINATOR):
                session.add(
                    ApprovalScopeApprover(
                        approval_scope_id="SCOPE-P250",
                        app_user_id=self.user_ids[role],
                    )
                )
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="TASK-P250-APPROVAL",
                    approval_scope_id="SCOPE-P250",
                )
            )
        engine.dispose()

        next_week = date.today() - timedelta(days=date.today().weekday()) + timedelta(days=7)
        self.days = tuple(next_week + timedelta(days=offset) for offset in range(5))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(
        self,
        role: str,
        *,
        display_name: str,
        employee_external_id: str | None = None,
    ):
        return create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(
                _principal(
                    role,
                    local_user_id=self.user_ids[role],
                    display_name=display_name,
                    employee_external_id=employee_external_id,
                )
            ),
            communication_transport=self.transport,
        )

    def _seed_resources_through_api(self) -> None:
        with TestClient(
            self._app(ROLE_ADMIN, display_name="Administrateur E2E"),
            raise_server_exceptions=False,
        ) as client:
            me = client.get("/api/v1/auth/me")
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()["roles"], [ROLE_ADMIN])

            for key, payload in (
                (
                    "resource-alice",
                    {
                        "name": "Alice",
                        "email": ALICE_EMAIL,
                        "resource_class": "Programmation",
                        "competencies": "SCADA; MES",
                        "external_id": "EMP-ALICE",
                    },
                ),
                (
                    "resource-bob",
                    {
                        "name": "Bob",
                        "email": BOB_EMAIL,
                        "resource_class": "Programmation",
                        "competencies": "PLC; SCADA",
                        "external_id": "EMP-BOB",
                    },
                ),
            ):
                response = client.post(
                    "/api/v1/resources",
                    headers={"Idempotency-Key": key},
                    json=payload,
                )
                self.assertEqual(response.status_code, 201, response.text)

            resources = client.get("/api/v1/resources").json()
            resource_ids = {row["name"]: row["id"] for row in resources}
            self.assertEqual(set(resource_ids), {"Alice", "Bob"})

            for name in ("Alice", "Bob"):
                response = client.post(
                    "/api/v1/availability-rules",
                    headers={"Idempotency-Key": f"schedule-{name.lower()}"},
                    json={
                        "availability_type": "Horaire standard",
                        "resource_id": resource_ids[name],
                        "weekdays": "Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                        "start_time": "08:00:00",
                        "end_time": "16:00:00",
                        "active": True,
                    },
                )
                self.assertEqual(response.status_code, 201, response.text)

    @staticmethod
    def _segments_for(client: TestClient, demand_number: str) -> list[dict]:
        response = client.get("/api/v1/segments")
        assert response.status_code == 200, response.text
        return [row for row in response.json() if row["demand_number"] == demand_number]

    def _period_payload(self, *, cumulative_hours: float) -> dict:
        d1, _d2, d3, d4, d5 = self.days
        return {
            "periods": [
                {
                    "period_id": "CUM-1",
                    "start_date": d1.isoformat(),
                    "end_date": d3.isoformat(),
                    "hours": cumulative_hours,
                    "kind": "CUMULATIVE",
                    "confirmation": "Tentative",
                    "proposed_resource": "Alice",
                    "resource_count": 1,
                    "desired_active_days": 3,
                    "note": "Bloc cumulatif",
                },
                {
                    "period_id": "ALT-A",
                    "start_date": d4.isoformat(),
                    "end_date": d4.isoformat(),
                    "hours": 8,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "confirmation": "Confirmée",
                    "proposed_resource": "Bob",
                    "resource_count": 1,
                    "desired_active_days": 1,
                },
                {
                    "period_id": "ALT-B",
                    "start_date": d5.isoformat(),
                    "end_date": d5.isoformat(),
                    "hours": 8,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "confirmation": "Tentative",
                    "proposed_resource": "Bob",
                    "resource_count": 1,
                    "desired_active_days": 1,
                },
            ]
        }

    def test_complete_v2_local_api_acceptance_path(self) -> None:
        self._seed_resources_through_api()
        d1, d2, d3, d4, d5 = self.days

        # PROJECT_MANAGER: work package -> demand -> periods/alternatives -> submit.
        with TestClient(
            self._app(ROLE_PROJECT_MANAGER, display_name="Chargé E2E"),
            raise_server_exceptions=False,
        ) as project_manager:
            package = project_manager.post(
                "/api/v1/work-packages",
                headers={"Idempotency-Key": "wp-250"},
                json={
                    "project_number": "P-250",
                    "task_catalog_item_id": "TASK-P250-APPROVAL",
                    "code": "WP-E2E",
                    "name": "Lot acceptation",
                    "description": "Parcours métier V2",
                    "start_date": d1.isoformat(),
                    "end_date": d5.isoformat(),
                    "planned_hours": 40,
                },
            )
            self.assertEqual(package.status_code, 201, package.text)
            packages = project_manager.get(
                "/api/v1/work-packages",
                params={"project_number": "P-250"},
            )
            self.assertEqual(packages.status_code, 200, packages.text)
            work_package = next(row for row in packages.json() if row["code"] == "WP-E2E")
            work_package_ref = work_package["reference"]

            created = project_manager.post(
                "/api/v1/demands",
                headers={"Idempotency-Key": "demand-250"},
                json={
                    "project_number": "P-250",
                    "task_code": "APPROVAL",
                    "work_package_ref": work_package_ref,
                    "desired_start": d1.isoformat(),
                    "desired_end": d5.isoformat(),
                    "description": "Demande acceptation locale V2",
                    "resource_count": 1,
                    "estimated_hours": 20,
                    "estimated_days": 4,
                    "confirmation": "Confirmée",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            demand_number = created.json()["demand_number"]

            demand = project_manager.get(f"/api/v1/demands/{demand_number}")
            self.assertEqual(demand.status_code, 200, demand.text)
            self.assertEqual(demand.json()["project_number"], "P-250")
            self.assertEqual(demand.json()["work_package_ref"], work_package_ref)
            self.assertEqual(demand.json()["estimated_days"], 4.0)

            periods = project_manager.put(
                f"/api/v1/demands/{demand_number}/periods",
                json={
                    **self._period_payload(cumulative_hours=12),
                    "expected_request_version": demand.json()["version"],
                },
            )
            self.assertEqual(periods.status_code, 200, periods.text)
            self.assertEqual(periods.json()["period_count"], 3)

            selected = project_manager.put(
                f"/api/v1/demands/{demand_number}/alternative-groups/VISITE/selection",
                json={"period_id": "ALT-A"},
            )
            self.assertEqual(selected.status_code, 200, selected.text)
            current_periods = project_manager.get(
                f"/api/v1/demands/{demand_number}/periods"
            ).json()
            self.assertEqual(
                [row["period_id"] for row in current_periods if row["selected"]],
                ["ALT-A"],
            )

            submitted = project_manager.post(f"/api/v1/demands/{demand_number}/submit")
            self.assertEqual(submitted.status_code, 200, submitted.text)
            denied_approval = project_manager.post(
                f"/api/v1/demands/{demand_number}/approve",
                json={"comment": "Le chargé ne doit pas pouvoir approuver"},
            )
            self.assertEqual(denied_approval.status_code, 403, denied_approval.text)
            self.assertEqual(
                denied_approval.json()["error"]["context"]["required_permission"],
                "approve_demands",
            )

        # COORDINATOR: approval materializes requirements/shifts and mixed confirmation.
        with TestClient(
            self._app(ROLE_COORDINATOR, display_name="Coordonnateur E2E"),
            raise_server_exceptions=False,
        ) as coordinator:
            approved = coordinator.post(
                f"/api/v1/demands/{demand_number}/approve",
                json={"comment": "Acceptation initiale"},
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "En planification")

            segments = self._segments_for(coordinator, demand_number)
            self.assertEqual(len(segments), 2)
            cumulative = next(row for row in segments if row["start_date"] == d1.isoformat())
            alternative = next(row for row in segments if row["start_date"] == d4.isoformat())
            self.assertEqual(cumulative["planned_hours"], 12.0)
            self.assertEqual(cumulative["confirmation"], "Tentative")
            self.assertEqual(cumulative["desired_active_days"], 3)
            self.assertEqual(cumulative["planned_active_days"], 3)
            self.assertEqual(alternative["planned_hours"], 8.0)
            self.assertEqual(alternative["confirmation"], "Confirmée")

            shifts = coordinator.get(
                "/api/v1/shifts",
                params={"start": d1.isoformat(), "end": d5.isoformat()},
            )
            self.assertEqual(shifts.status_code, 200, shifts.text)
            demand_shifts = [
                row for row in shifts.json() if row["demand_number"] == demand_number
            ]
            self.assertAlmostEqual(sum(row["hours"] for row in demand_shifts), 20.0)

        # An approved demand change must keep the old plan until reapproval and expose delta.
        with TestClient(
            self._app(ROLE_PROJECT_MANAGER, display_name="Chargé E2E"),
            raise_server_exceptions=False,
        ) as project_manager:
            current = project_manager.get(
                f"/api/v1/demands/{demand_number}"
            )
            self.assertEqual(current.status_code, 200, current.text)
            changed = project_manager.put(
                f"/api/v1/demands/{demand_number}/periods",
                json={
                    **self._period_payload(cumulative_hours=16),
                    "expected_request_version": current.json()["version"],
                },
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertTrue(changed.json()["reapproval_required"])
            self.assertEqual(changed.json()["status"], "Soumise")

            approval_state = project_manager.get(
                f"/api/v1/demands/{demand_number}/approval-state"
            )
            self.assertEqual(
                approval_state.status_code,
                200,
                approval_state.text,
            )
            authorization = approval_state.json()
            self.assertEqual(
                authorization["approval_reference_status"],
                "CAPTURED",
            )
            self.assertFalse(authorization["candidate_matches_approved"])
            self.assertEqual(
                authorization["envelope_decision"],
                "REAPPROVAL_REQUIRED",
            )
            self.assertEqual(
                authorization["active_planned_hours"],
                20.0,
            )

            delta = project_manager.get(f"/api/v1/demands/{demand_number}/plan-delta")
            self.assertEqual(delta.status_code, 200, delta.text)
            preview = delta.json()
            self.assertTrue(preview["available"])
            self.assertTrue(preview["has_changes"])
            self.assertGreater(len(preview["items"]), 0)
            self.assertEqual(
                preview["active_revision_id"],
                authorization["active_revision_id"],
            )
            self.assertEqual(
                preview["authorization_fingerprint"],
                authorization["authorization_fingerprint"],
            )
            self.assertEqual(
                preview["candidate_authorization_fingerprint"],
                authorization["candidate_authorization_fingerprint"],
            )

            preserved = self._segments_for(project_manager, demand_number)
            self.assertAlmostEqual(sum(row["planned_hours"] for row in preserved), 20.0)

            reselected = project_manager.put(
                f"/api/v1/demands/{demand_number}/alternative-groups/VISITE/selection",
                json={"period_id": "ALT-A"},
            )
            self.assertEqual(reselected.status_code, 200, reselected.text)

        with TestClient(
            self._app(ROLE_COORDINATOR, display_name="Coordonnateur E2E"),
            raise_server_exceptions=False,
        ) as coordinator:
            reapproved = coordinator.post(
                f"/api/v1/demands/{demand_number}/approve",
                json={"comment": "Réapprobation après delta"},
            )
            self.assertEqual(reapproved.status_code, 200, reapproved.text)

            segments = self._segments_for(coordinator, demand_number)
            self.assertEqual(len(segments), 2)
            cumulative = next(row for row in segments if row["start_date"] == d1.isoformat())
            alternative = next(row for row in segments if row["start_date"] == d4.isoformat())
            self.assertEqual(cumulative["planned_hours"], 16.0)
            self.assertEqual(cumulative["desired_active_days"], 3)
            self.assertAlmostEqual(sum(row["planned_hours"] for row in segments), 24.0)

            # Segment edit + load profile + rebuild + audit.
            updated_segment = coordinator.patch(
                f"/api/v1/segments/{cumulative['segment_id']}",
                json={
                    "description": "Profil en cloche validé E2E",
                    "load_profile": "BELL",
                },
            )
            self.assertEqual(updated_segment.status_code, 200, updated_segment.text)
            rebuilt = coordinator.post("/api/v1/planning/rebuild")
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

            cumulative_after = coordinator.get(
                f"/api/v1/segments/{cumulative['segment_id']}"
            )
            self.assertEqual(cumulative_after.status_code, 200, cumulative_after.text)
            self.assertEqual(cumulative_after.json()["load_profile"], "BELL")
            self.assertEqual(cumulative_after.json()["desired_active_days"], 3)
            self.assertEqual(cumulative_after.json()["planned_active_days"], 3)

            profiled_shifts = coordinator.get(
                "/api/v1/shifts",
                params={
                    "start": d1.isoformat(),
                    "end": d3.isoformat(),
                    "resource_name": "Alice",
                },
            )
            self.assertEqual(profiled_shifts.status_code, 200, profiled_shifts.text)
            profiled_rows = [
                row
                for row in profiled_shifts.json()
                if row["segment_id"] == cumulative["segment_id"]
            ]
            self.assertEqual(len(profiled_rows), 3)
            self.assertAlmostEqual(sum(row["hours"] for row in profiled_rows), 16.0)
            hours_by_day = {
                row["work_date"]: row["hours"]
                for row in profiled_rows
            }
            self.assertGreater(hours_by_day[d2.isoformat()], hours_by_day[d1.isoformat()])
            self.assertGreater(hours_by_day[d2.isoformat()], hours_by_day[d3.isoformat()])

            segment_history = coordinator.get(
                f"/api/v1/segments/{cumulative['segment_id']}/history"
            )
            self.assertEqual(segment_history.status_code, 200, segment_history.text)
            self.assertTrue(
                any(row["actor_name"] == "Coordonnateur E2E" for row in segment_history.json())
            )

            # Manual shift overallocation requires an explicit decision, is editable and audited.
            first_manual = coordinator.post(
                f"/api/v1/segments/{alternative['segment_id']}/allocations",
                headers={"Idempotency-Key": "alt-first-manual"},
                json={
                    "technician": "Bob",
                    "day": d4.isoformat(),
                    "hours": 8,
                    "outside_standard_hours": False,
                    "note": "Quart manuel principal",
                    "confirmation": "Confirmée",
                },
            )
            self.assertEqual(first_manual.status_code, 201, first_manual.text)

            blocked = coordinator.post(
                f"/api/v1/segments/{alternative['segment_id']}/allocations",
                headers={"Idempotency-Key": "alt-overallocation-blocked"},
                json={
                    "technician": "Bob",
                    "day": d4.isoformat(),
                    "hours": 2,
                    "outside_standard_hours": False,
                    "note": "Exception explicite requise",
                    "confirmation": None,
                },
            )
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertEqual(
                blocked.json()["error"]["code"],
                "allocation_overallocation_choice_required",
            )

            kept = coordinator.post(
                f"/api/v1/segments/{alternative['segment_id']}/allocations",
                headers={"Idempotency-Key": "alt-overallocation-kept"},
                json={
                    "technician": "Bob",
                    "day": d4.isoformat(),
                    "hours": 2,
                    "outside_standard_hours": False,
                    "note": "Exception conservée",
                    "confirmation": None,
                    "overallocation_policy": "KEEP_EXCEPTION",
                },
            )
            self.assertEqual(kept.status_code, 201, kept.text)
            kept_id = kept.json()["allocation_id"]

            edited_shift = coordinator.put(
                f"/api/v1/allocations/{kept_id}",
                json={
                    "technician": "Bob",
                    "day": d4.isoformat(),
                    "hours": 1,
                    "outside_standard_hours": False,
                    "note": "Exception réduite",
                    "confirmation": None,
                },
            )
            self.assertEqual(edited_shift.status_code, 200, edited_shift.text)

            alternative_after = coordinator.get(
                f"/api/v1/segments/{alternative['segment_id']}"
            ).json()
            self.assertEqual(alternative_after["locked_hours"], 9.0)
            self.assertEqual(alternative_after["overallocated_hours"], 1.0)
            self.assertTrue(alternative_after["overallocated"])

            shift_history = coordinator.get(f"/api/v1/shifts/{kept_id}/history")
            self.assertEqual(shift_history.status_code, 200, shift_history.text)
            self.assertGreaterEqual(len(shift_history.json()), 2)
            alternative_history = coordinator.get(
                f"/api/v1/segments/{alternative['segment_id']}/history"
            ).json()
            self.assertIn(
                "Dérogation surallocation manuelle",
                [row["action"] for row in alternative_history],
            )

            # Communications stay local: prepare -> approve -> fake transport draft creation.
            preview = coordinator.get(
                "/api/v1/communications/preview",
                params={"week_start": d1.isoformat()},
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertGreaterEqual(len(preview.json()["drafts"]), 1)
            reviews = [
                {
                    "audience": row["audience"],
                    "recipient_id": row["recipient_id"],
                    "include": True,
                }
                for row in preview.json()["drafts"]
            ]
            prepared = coordinator.post(
                "/api/v1/communications/batches",
                json={
                    "week_start": d1.isoformat(),
                    "expected_fingerprint": preview.json()["snapshot_fingerprint"],
                    "reviews": reviews,
                },
            )
            self.assertEqual(prepared.status_code, 201, prepared.text)
            batch_id = prepared.json()["id"]
            approved_batch = coordinator.post(
                f"/api/v1/communications/batches/{batch_id}/approve"
            )
            self.assertEqual(approved_batch.status_code, 200, approved_batch.text)
            drafts = coordinator.post(
                f"/api/v1/communications/batches/{batch_id}/create-drafts"
            )
            self.assertEqual(drafts.status_code, 200, drafts.text)
            self.assertEqual(drafts.json()["drafts_provider"], "fake_acceptance")
            self.assertEqual(
                drafts.json()["drafts_created_count"],
                len(self.transport.messages),
            )
            self.assertGreater(len(self.transport.messages), 0)

        # TECHNICIAN: personal schedule is linked by employee_external_id and mutations are denied.
        with TestClient(
            self._app(
                ROLE_TECHNICIAN,
                display_name="Technicien Alice",
                employee_external_id="EMP-ALICE",
            ),
            raise_server_exceptions=False,
        ) as technician:
            schedule = technician.get(
                "/api/v1/me/schedule",
                params={"start": d1.isoformat(), "end": d5.isoformat()},
            )
            self.assertEqual(schedule.status_code, 200, schedule.text)
            self.assertEqual(schedule.json()["link_status"], "LINKED")
            self.assertEqual(schedule.json()["employee_external_id"], "EMP-ALICE")
            self.assertEqual(schedule.json()["resource"]["name"], "Alice")
            self.assertGreaterEqual(len(schedule.json()["shifts"]), 1)
            self.assertAlmostEqual(
                sum(row["hours"] for row in schedule.json()["shifts"]),
                16.0,
            )

            denied_mutation = technician.post(
                "/api/v1/demands",
                json={
                    "project_number": "P-250",
                    "desired_start": d1.isoformat(),
                },
            )
            self.assertEqual(denied_mutation.status_code, 403, denied_mutation.text)
            denied_contacts = technician.get("/api/v1/communications/contacts")
            self.assertEqual(denied_contacts.status_code, 403, denied_contacts.text)

        # Emergency marker: urgency cannot materialize before the 276C quorum.
        today = date.today()
        with TestClient(
            self._app(ROLE_PROJECT_MANAGER, display_name="Chargé E2E"),
            raise_server_exceptions=False,
        ) as project_manager:
            urgent = project_manager.post(
                "/api/v1/demands",
                headers={"Idempotency-Key": "urgent-250"},
                json={
                    "project_number": "P-250",
                    "task_code": "APPROVAL",
                    "desired_start": today.isoformat(),
                    "desired_end": today.isoformat(),
                    "estimated_hours": 4,
                    "priority": "Urgent",
                    "confirmation": "Confirmée",
                    "proposed_technician": "Alice",
                    "submit": True,
                },
            )
            self.assertEqual(urgent.status_code, 201, urgent.text)
            urgent_number = urgent.json()["demand_number"]

        with TestClient(
            self._app(ROLE_COORDINATOR, display_name="Coordonnateur E2E"),
            raise_server_exceptions=False,
        ) as coordinator:
            emergency = coordinator.post(
                f"/api/v1/demands/{urgent_number}/emergency-plan",
                json={"comment": "Intervention urgente E2E"},
            )
            self.assertEqual(emergency.status_code, 200, emergency.text)
            self.assertEqual(emergency.json()["status"], "Soumise")
            emergency_planning = emergency.json().get("planning") or {}
            self.assertEqual(emergency_planning.get("segments", 0), 0)
            self.assertEqual(
                self._segments_for(coordinator, urgent_number),
                [],
            )

            urgent_state = coordinator.get(f"/api/v1/demands/{urgent_number}").json()
            self.assertTrue(urgent_state["emergency_override_active"])
            urgent_history = coordinator.get(
                f"/api/v1/demands/{urgent_number}/history"
            ).json()
            self.assertIn(
                "Dérogation d'approbation urgente",
                [row["action"] for row in urgent_history],
            )

            regularized = coordinator.post(
                f"/api/v1/demands/{urgent_number}/approve",
                json={"comment": "Approbation régulière après urgence"},
            )
            self.assertEqual(regularized.status_code, 200, regularized.text)
            final_urgent = coordinator.get(f"/api/v1/demands/{urgent_number}").json()
            self.assertEqual(final_urgent["status"], "En planification")
            self.assertFalse(final_urgent["emergency_override_active"])


if __name__ == "__main__":
    unittest.main()
