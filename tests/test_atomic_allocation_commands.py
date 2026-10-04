from __future__ import annotations

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application import AllocationDuplicateCommand, ApplicationConflictError
from app.application.security import AuthPrincipal, ROLE_ADMIN, ROLE_PROJECT_MANAGER
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    ORIGIN_AD_HOC,
    PlanningChangeHistory,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.composition import build_sql_facade
from app.server.security import static_auth_resolver
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)


WORK_DAY = date(2026, 9, 22)


def _auth(display_name: str):
    return static_auth_resolver(
        AuthPrincipal.from_roles(
            local_user_id="USER-ATOMIC-1",
            issuer="urn:resourceplanner:test",
            subject="atomic-user",
            display_name=display_name,
            email=None,
            roles=(ROLE_ADMIN,),
            auth_mode="test",
        )
    )


class AtomicAllocationCommandHttpTests(unittest.TestCase):
    @staticmethod
    def _database(
        directory: str,
        *,
        planned_hours: float | None = None,
        source_hours: float | None = None,
        source: str = "MANUAL",
        locked: bool = True,
        allocation_type: str = "Flexible",
        note: str | None = "note-source",
        confirmation: str | None = None,
        operational_responsible_override_contact_id: str | None = None,
    ) -> str:
        path = Path(directory) / "atomic-allocation.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(Project(id="P1", number="P-1", name="Projet atomique"))
            session.add(
                BusinessContact(
                    id="C-RESP-594C",
                    display_name="Responsable 594C",
                    active=True,
                )
            )
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True),
                    Resource(id="R2", name="Bob", active=True),
                ]
            )
            session.flush()
            for resource_id in ("R1", "R2"):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"STD-{resource_id}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                        start_time=time(8, 0),
                        end_time=time(20, 0),
                        active=True,
                    )
                )
            if planned_hours is not None and source_hours is not None:
                session.add(
                    ResourceRequirement(
                        id="REQ1",
                        legacy_segment_id="SEG-1",
                        project_id="P1",
                        workforce_request_id=None,
                        assigned_resource_id="R1",
                        start_date=WORK_DAY,
                        end_date=WORK_DAY,
                        planned_hours=planned_hours,
                        status="Planifié",
                        planning_type="Flexible",
                        confirmation="Confirmée",
                        origin=ORIGIN_AD_HOC,
                    )
                )
                session.flush()
                session.add(
                    Shift(
                        id="SHIFT-SOURCE",
                        legacy_allocation_id="ALLOC-SOURCE",
                        resource_requirement_id="REQ1",
                        resource_id="R1",
                        work_date=WORK_DAY,
                        hours=source_hours,
                        allocation_type=allocation_type,
                        source=source,
                        locked=locked,
                        outside_standard_hours=False,
                        confirmation=confirmation,
                        note=note,
                        operational_responsible_override_contact_id=(
                            operational_responsible_override_contact_id
                        ),
                    )
                )
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()
        return url

    @staticmethod
    def _split_body(version: int = 1) -> dict[str, object]:
        return {
            "resource_id": "R2",
            "day": WORK_DAY.isoformat(),
            "transfer_hours": 3,
            "expected_planning_version": version,
        }

    @staticmethod
    def _duplicate_body(
        version: int = 1,
        policy: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "resource_id": "R2",
            "day": WORK_DAY.isoformat(),
            "expected_planning_version": version,
        }
        if policy is not None:
            body["overallocation_policy"] = policy
        return body

    def test_split_is_exact_atomic_and_replays_before_stale_version_check(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=8,
            )
            headers = {"Idempotency-Key": "split-retry-1"}
            app = create_api_app(
                url,
                actor_name="fallback",
                auth_resolver=_auth("Nom avant"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=self._split_body(),
                    headers=headers,
                )
            self.assertEqual(first.status_code, 201, first.text)
            payload = first.json()
            self.assertEqual(payload["operation"], "SPLIT")
            self.assertEqual(payload["source_hours"], 5.0)
            self.assertEqual(payload["target_hours"], 3.0)
            self.assertEqual(payload["planned_hours"], 8.0)
            self.assertEqual(payload["locked_hours"], 8.0)
            self.assertEqual(payload["excess_hours"], 0.0)
            self.assertEqual(payload["planning_version"], 2)

            # Same stable authenticated identity, different display name. The old
            # expected version is intentionally stale: replay must win first.
            replay_app = create_api_app(
                url,
                actor_name="fallback",
                auth_resolver=_auth("Nom après"),
            )
            with TestClient(replay_app, raise_server_exceptions=False) as client:
                replay = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=self._split_body(),
                    headers=headers,
                )
                stale_other_key = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=self._split_body(),
                    headers={"Idempotency-Key": "split-stale-other-key"},
                )
            self.assertEqual(replay.status_code, 201, replay.text)
            self.assertEqual(replay.json(), payload)
            self.assertEqual(stale_other_key.status_code, 409, stale_other_key.text)
            self.assertEqual(
                stale_other_key.json()["error"]["code"],
                "planning_version_conflict",
            )

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                rows = session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id == "REQ1")
                    .order_by(Shift.id)
                ).all()
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    sum(float(row.hours) for row in rows if row.locked),
                    8.0,
                )
                self.assertTrue(all(row.source == "MANUAL" for row in rows))
                audits = int(
                    session.scalar(
                        select(func.count())
                        .select_from(PlanningChangeHistory)
                        .where(
                            PlanningChangeHistory.action
                            == "Partage atomique de quart"
                        )
                    )
                    or 0
                )
                self.assertEqual(audits, 1)
            engine.dispose()

    def test_split_copies_explicit_shift_responsibility_override(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=8,
                operational_responsible_override_contact_id="C-RESP-594C",
            )
            app = create_api_app(url, auth_resolver=_auth("Coordonnateur"))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=self._split_body(),
                    headers={"Idempotency-Key": "split-responsibility-594c"},
                )
            self.assertEqual(response.status_code, 201, response.text)

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                rows = session.scalars(
                    select(Shift).where(Shift.resource_requirement_id == "REQ1")
                ).all()
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    {
                        row.operational_responsible_override_contact_id
                        for row in rows
                    },
                    {"C-RESP-594C"},
                )
            engine.dispose()

    def test_duplicate_copies_explicit_shift_responsibility_override(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=4,
                operational_responsible_override_contact_id="C-RESP-594C",
            )
            app = create_api_app(url, auth_resolver=_auth("Coordonnateur"))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(),
                    headers={"Idempotency-Key": "duplicate-responsibility-594c"},
                )
            self.assertEqual(response.status_code, 201, response.text)

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                rows = session.scalars(
                    select(Shift).where(Shift.resource_requirement_id == "REQ1")
                ).all()
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    {
                        row.operational_responsible_override_contact_id
                        for row in rows
                    },
                    {"C-RESP-594C"},
                )
            engine.dispose()

    def test_duplicate_requires_explicit_choice_when_exception_increases(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=4,
                source_hours=4,
            )
            app = create_api_app(
                url,
                auth_resolver=_auth("Coordonnateur"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                blocked = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(),
                    headers={"Idempotency-Key": "duplicate-blocked"},
                )
                kept = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(policy="KEEP_EXCEPTION"),
                    headers={"Idempotency-Key": "duplicate-kept"},
                )
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertEqual(
                blocked.json()["error"]["code"],
                "allocation_overallocation_choice_required",
            )
            self.assertEqual(kept.status_code, 201, kept.text)
            payload = kept.json()
            self.assertEqual(payload["source_hours"], 4.0)
            self.assertEqual(payload["target_hours"], 4.0)
            self.assertEqual(payload["planned_hours"], 4.0)
            self.assertEqual(payload["locked_hours"], 8.0)
            self.assertEqual(payload["excess_hours"], 4.0)
            # Failed preflight rolled the planning CAS back; successful retry starts at 1.
            self.assertEqual(payload["planning_version"], 2)

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                actions = set(
                    session.scalars(
                        select(PlanningChangeHistory.action).where(
                            PlanningChangeHistory.entity_reference == "SEG-1"
                        )
                    ).all()
                )
                self.assertIn("Dérogation surallocation manuelle", actions)
            engine.dispose()

    def test_duplicate_converts_counted_auto_source_without_losing_nullable_override(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=4,
                source="AUTO",
                locked=False,
                note="note-auto",
                confirmation=None,
            )
            app = create_api_app(url, auth_resolver=_auth("Coordonnateur"))
            with TestClient(app, raise_server_exceptions=False) as client:
                duplicated = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(),
                    headers={"Idempotency-Key": "duplicate-auto"},
                )
                self.assertEqual(duplicated.status_code, 201, duplicated.text)
                self.assertTrue(duplicated.json()["auto_source_converted"])
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory() as session:
                source = session.get(Shift, "SHIFT-SOURCE")
                self.assertIsNotNone(source)
                self.assertEqual(source.resource_id, "R1")
                self.assertEqual(source.work_date, WORK_DAY)
                self.assertEqual(float(source.hours), 4.0)
                self.assertEqual(source.note, "note-auto")
                self.assertIsNone(source.confirmation)
                self.assertEqual(source.source, "MANUAL")
                self.assertTrue(source.locked)
                rows = session.scalars(
                    select(Shift).where(Shift.resource_requirement_id == "REQ1")
                ).all()
                self.assertEqual(len(rows), 2)
                self.assertEqual(sum(float(row.hours) for row in rows), 8.0)
                self.assertTrue(all(row.locked for row in rows))
            engine.dispose()

    def test_project_manager_increase_uses_active_revision_and_non_compounded_twenty_percent_limit(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            admin_app = create_api_app(
                url,
                actor_name="admin-atomic",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": WORK_DAY.isoformat(),
                        "desired_end": WORK_DAY.isoformat(),
                        "estimated_hours": 10,
                        "proposed_technician": "Alice",
                        "submit": True,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Autorisation atomique"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                approval_state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(approval_state.status_code, 200, approval_state.text)
                revision_id = approval_state.json()["active_revision_id"]
                operational_version = approval_state.json()["operational_version"]
                self.assertTrue(revision_id)
                self.assertEqual(operational_version, 1)

                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": WORK_DAY.isoformat(), "end": WORK_DAY.isoformat()},
                )
                self.assertEqual(shifts.status_code, 200, shifts.text)
                approved_auto = next(
                    row
                    for row in shifts.json()
                    if row["demand_number"] == number and row["source"] == "AUTO"
                )
                segment_id = approved_auto["segment_id"]

                manual = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    json={
                        "resource_id": "R1",
                        "day": WORK_DAY.isoformat(),
                        "hours": 9,
                        "outside_standard_hours": False,
                        "confirmation": None,
                    },
                    headers={"Idempotency-Key": "atomic-pm-manual-9"},
                )
                self.assertEqual(manual.status_code, 201, manual.text)

                snapshot = client.get(
                    "/api/v1/planning/snapshot",
                    params={"start": WORK_DAY.isoformat(), "end": WORK_DAY.isoformat()},
                )
                self.assertEqual(snapshot.status_code, 200, snapshot.text)
                planning_version = snapshot.json()["planning_version"]

                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": WORK_DAY.isoformat(), "end": WORK_DAY.isoformat()},
                )
                source = next(
                    row
                    for row in shifts.json()
                    if row["demand_number"] == number
                    and row["source"] == "AUTO"
                    and abs(float(row["hours"]) - 1.0) < 0.001
                )

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    facade = build_sql_facade(
                        session,
                        actor_name="pm-atomic",
                        roles=(ROLE_PROJECT_MANAGER,),
                    )
                    first = facade.duplicate_allocation(
                        AllocationDuplicateCommand(
                            allocation_id=source["allocation_id"],
                            resource_id="R1",
                            day=WORK_DAY,
                            expected_planning_version=planning_version,
                            overallocation_policy="INCREASE_PLANNED",
                            expected_approval_revision_id=revision_id,
                            expected_operational_version=operational_version,
                        )
                    )
                    self.assertEqual(first.planned_hours, 11.0)
                    self.assertEqual(first.locked_hours, 11.0)
                    self.assertEqual(first.operational_version, 2)

                with factory.begin() as session:
                    facade = build_sql_facade(
                        session,
                        actor_name="pm-atomic",
                        roles=(ROLE_PROJECT_MANAGER,),
                    )
                    second = facade.duplicate_allocation(
                        AllocationDuplicateCommand(
                            allocation_id=first.target_allocation_id,
                            resource_id="R1",
                            day=WORK_DAY,
                            expected_planning_version=first.planning_version,
                            overallocation_policy="INCREASE_PLANNED",
                            expected_approval_revision_id=revision_id,
                            expected_operational_version=first.operational_version,
                        )
                    )
                    self.assertEqual(second.planned_hours, 12.0)
                    self.assertEqual(second.locked_hours, 12.0)
                    self.assertEqual(second.operational_version, 3)

                session = factory()
                transaction = session.begin()
                try:
                    facade = build_sql_facade(
                        session,
                        actor_name="pm-atomic",
                        roles=(ROLE_PROJECT_MANAGER,),
                    )
                    with self.assertRaises(ApplicationConflictError) as caught:
                        facade.duplicate_allocation(
                            AllocationDuplicateCommand(
                                allocation_id=first.target_allocation_id,
                                resource_id="R1",
                                day=WORK_DAY,
                                expected_planning_version=second.planning_version,
                                overallocation_policy="INCREASE_PLANNED",
                                expected_approval_revision_id=revision_id,
                                expected_operational_version=second.operational_version,
                            )
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "planning_authorization_revision_required",
                    )
                    transaction.rollback()
                finally:
                    session.close()
            finally:
                engine.dispose()

            with TestClient(admin_app, raise_server_exceptions=False) as client:
                final_state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(final_state.status_code, 200, final_state.text)
                self.assertEqual(final_state.json()["active_revision_id"], revision_id)
                self.assertEqual(final_state.json()["operational_version"], 3)
                self.assertEqual(
                    list(final_state.json()["active_budget_overrides"].values()),
                    [12.0],
                )

    def test_atomic_routes_require_manage_planning_and_expected_planning_version(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=4,
            )
            pm_app = create_api_app(
                url,
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                denied = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(),
                    headers={"Idempotency-Key": "atomic-permission-denied"},
                )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], "permission_denied")
            self.assertEqual(
                denied.json()["error"]["context"]["required_permission"],
                "manage_planning",
            )

            admin_app = create_api_app(
                url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            missing_version = self._duplicate_body()
            missing_version.pop("expected_planning_version")
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                invalid = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=missing_version,
                    headers={"Idempotency-Key": "atomic-version-required"},
                )
            self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_non_counted_auto_proposal_and_full_split_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=8,
                source="AUTO",
                locked=False,
                allocation_type=MISSING_ALLOCATION_TYPE,
            )
            app = create_api_app(url, auth_resolver=_auth("Coordonnateur"))
            with TestClient(app, raise_server_exceptions=False) as client:
                duplicate = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/duplicate",
                    json=self._duplicate_body(),
                    headers={"Idempotency-Key": "duplicate-missing"},
                )
            self.assertEqual(duplicate.status_code, 422, duplicate.text)
            self.assertEqual(
                duplicate.json()["error"]["code"],
                "allocation_source_not_counted",
            )

        with TemporaryDirectory() as directory:
            url = self._database(
                directory,
                planned_hours=8,
                source_hours=8,
            )
            app = create_api_app(url, auth_resolver=_auth("Coordonnateur"))
            full = self._split_body()
            full["transfer_hours"] = 8
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=full,
                    headers={"Idempotency-Key": "split-full"},
                )
                missing_key = client.post(
                    "/api/v1/allocations/ALLOC-SOURCE/split",
                    json=self._split_body(),
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "allocation_split_hours_invalid",
            )
            self.assertEqual(missing_key.status_code, 422, missing_key.text)


if __name__ == "__main__":
    unittest.main()
