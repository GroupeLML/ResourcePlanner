from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.planning_window import resolve_effective_planning_window
from app.infrastructure.sql import (
    PLANNING_WINDOW_OVERRIDE_ABSORBED,
    PLANNING_WINDOW_OVERRIDE_SUPERSEDED,
    PlanningWindowOverride,
    Project,
    RequestApprovalReference,
    RequestApprovalRevision,
    ResourceRequirement,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    SqlPlanningWindowOverrideRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.request_plan_preparation import SqlRequestPlanPreparer
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


DAY = date(2026, 10, 6)


class PlanningWindowOverrideTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P-613B", number="P-613B", name="Projet 613B"))
        session.flush()
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-window-613b.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _approve(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-613B",
                    "desired_start": DAY.isoformat(),
                    "desired_end": DAY.isoformat(),
                    "estimated_hours": 8,
                    "submit": True,
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Approbation 613B"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _active_context(session, number: str):
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
        reference = session.get(RequestApprovalReference, request.id)
        assert reference is not None
        assert reference.active_revision_id
        revision = session.get(RequestApprovalRevision, reference.active_revision_id)
        assert revision is not None
        assert requirement.approved_entry_key
        return request, requirement, revision

    @staticmethod
    def _add_override(session, request, requirement, revision) -> PlanningWindowOverride:
        row = PlanningWindowOverride(
            workforce_request_id=request.id,
            resource_requirement_id=requirement.id,
            approval_revision_id=revision.id,
            approved_entry_key=requirement.approved_entry_key,
            approved_start_date=DAY,
            approved_end_date=DAY,
            effective_start_date=DAY - timedelta(days=1),
            effective_end_date=DAY + timedelta(days=1),
            actor_user_id=TEST_ADMIN_USER_ID,
            reason="Intervention opérationnelle confirmée",
            correlation_id="613B-test-correlation",
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _new_revision(
        session,
        request,
        previous,
        *,
        entries,
    ) -> RequestApprovalRevision:
        payload = json.loads(previous.payload_text)
        payload["authorization"]["entries"] = entries
        row = RequestApprovalRevision(
            workforce_request_id=request.id,
            previous_revision_id=previous.id,
            request_version=previous.request_version + 1,
            approved_by_external_id=previous.approved_by_external_id,
            approved_by_name=previous.approved_by_name,
            approved_at=utc_now(),
            provenance=previous.provenance,
            payload_format_version=previous.payload_format_version,
            payload_text=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            authorization_fingerprint="6" * 64,
        )
        session.add(row)
        session.flush()
        return row

    def test_policy_accepts_only_strict_widening(self) -> None:
        widened = resolve_effective_planning_window(
            approved_start=DAY,
            approved_end=DAY,
            override_start=DAY - timedelta(days=1),
            override_end=DAY + timedelta(days=1),
        )
        self.assertEqual(widened.start_date, DAY - timedelta(days=1))
        self.assertEqual(widened.end_date, DAY + timedelta(days=1))

        with self.assertRaisesRegex(ValueError, "ne peut que l'élargir"):
            resolve_effective_planning_window(
                approved_start=DAY,
                approved_end=DAY + timedelta(days=2),
                override_start=DAY + timedelta(days=1),
                override_end=DAY + timedelta(days=2),
            )
        with self.assertRaisesRegex(ValueError, "doit élargir"):
            resolve_effective_planning_window(
                approved_start=DAY,
                approved_end=DAY,
                override_start=DAY,
                override_end=DAY,
            )

    def test_active_override_is_the_common_window_and_survives_resync(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-613b",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, revision = self._active_context(session, number)
                    self._add_override(session, request, requirement, revision)

                    prepared = SqlRequestPlanPreparer(session).prepare_active(
                        request,
                        current=(requirement,),
                    )
                    self.assertEqual(len(prepared.specs), 1)
                    self.assertEqual(
                        prepared.specs[0].start_date,
                        DAY - timedelta(days=1),
                    )
                    self.assertEqual(
                        prepared.specs[0].end_date,
                        DAY + timedelta(days=1),
                    )

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()
                    self.assertEqual(
                        requirement.start_date,
                        DAY - timedelta(days=1),
                    )
                    self.assertEqual(
                        requirement.end_date,
                        DAY + timedelta(days=1),
                    )
                    self.assertEqual(revision.id, requirement.approval_revision_id)
            finally:
                engine.dispose()

    def test_reapproval_absorbs_override_when_new_window_covers_it(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-613b",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, revision = self._active_context(session, number)
                    override = self._add_override(
                        session,
                        request,
                        requirement,
                        revision,
                    )
                    payload = json.loads(revision.payload_text)
                    entries = deepcopy(payload["authorization"]["entries"])
                    target = next(
                        row
                        for row in entries
                        if row["identity"] == requirement.approved_entry_key
                    )
                    target["start_date"] = (DAY - timedelta(days=1)).isoformat()
                    target["end_date"] = (DAY + timedelta(days=1)).isoformat()
                    new_revision = self._new_revision(
                        session,
                        request,
                        revision,
                        entries=entries,
                    )

                    count = SqlPlanningWindowOverrideRepository(
                        session
                    ).reconcile_for_new_revision(
                        request.id,
                        previous_revision_id=revision.id,
                        new_revision=new_revision,
                    )
                    self.assertEqual(count, 1)
                    self.assertEqual(
                        override.status,
                        PLANNING_WINDOW_OVERRIDE_ABSORBED,
                    )
                    self.assertEqual(
                        override.resolution_reason,
                        "REAPPROVAL_ABSORBED",
                    )
                    self.assertEqual(
                        override.resolved_by_revision_id,
                        new_revision.id,
                    )
            finally:
                engine.dispose()

    def test_missing_entry_supersedes_override_instead_of_transferring_it(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-613b",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, revision = self._active_context(session, number)
                    override = self._add_override(
                        session,
                        request,
                        requirement,
                        revision,
                    )
                    new_revision = self._new_revision(
                        session,
                        request,
                        revision,
                        entries=[],
                    )

                    SqlPlanningWindowOverrideRepository(
                        session
                    ).reconcile_for_new_revision(
                        request.id,
                        previous_revision_id=revision.id,
                        new_revision=new_revision,
                    )
                    self.assertEqual(
                        override.status,
                        PLANNING_WINDOW_OVERRIDE_SUPERSEDED,
                    )
                    self.assertEqual(
                        override.resolution_reason,
                        "ENTRY_NOT_PRESENT",
                    )
                    self.assertEqual(
                        SqlPlanningWindowOverrideRepository(
                            session
                        ).active_by_requirement_ids(
                            (requirement.id,),
                            approval_revision_id=revision.id,
                        ),
                        {},
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
