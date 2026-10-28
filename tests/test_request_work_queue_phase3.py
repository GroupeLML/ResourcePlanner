from __future__ import annotations

from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
    ResourceRequirement,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.request_plan_preparation import SqlRequestPlanPreparer
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


class RequestWorkQueuePhase3Tests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        from app.infrastructure.sql import Project

        session.add(Project(id="P-778-P3", number="P-778-P3", name="Projet 778 Phase 3"))
        session.flush()
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="request-work-queue-phase3.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _approve(client: TestClient, hours: int) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-P3",
                    "desired_start": "2026-10-28",
                    "desired_end": "2026-10-28",
                    "estimated_hours": hours,
                    "submit": True,
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Approbation Phase 3 #778"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _context(session, number: str):
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
        intent = session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        )
        assert intent is not None
        return request, requirement, intent

    @staticmethod
    def _reduce(session, request, requirement, intent, hours: Decimal) -> None:
        session.add(
            RequestCompletionReduction(
                planning_intent_id=intent.id,
                workforce_request_id=request.id,
                historical_approval_revision_id=requirement.approval_revision_id,
                source_request_line_id=intent.source_request_line_id,
                approved_entry_key=intent.approved_entry_key,
                resource_requirement_id=requirement.id,
                original_shift_id="SHIFT-HIST-778",
                shift_snapshot_text="{}",
                reduction_hours=hours,
                effective_need_before=Decimal(requirement.planned_hours),
                effective_need_after=max(
                    Decimal(requirement.planned_hours) - hours,
                    Decimal("0"),
                ),
                actor_user_id=TEST_ADMIN_USER_ID,
                command_id=f"phase3-reduction-{hours}",
                reconciliation_state="ACTIVE",
            )
        )
        session.flush()

    def test_prepare_and_resync_use_reduced_effective_need(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("2.00"),
                    )
                    prepared = SqlRequestPlanPreparer(session).prepare_active(
                        request,
                        current=(requirement,),
                    )
                    self.assertEqual(len(prepared.specs), 1)
                    self.assertEqual(prepared.specs[0].planned_hours, Decimal("6.00"))

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()
                    self.assertEqual(requirement.planned_hours, Decimal("6.00"))
                    self.assertEqual(
                        requirement.approval_revision_id,
                        prepared.approval_revision_id,
                    )
            finally:
                engine.dispose()

    def test_full_reduction_removes_zero_hour_slot_from_active_materialization(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-zero",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("8.00"),
                    )
                    prepared = SqlRequestPlanPreparer(session).prepare_active(
                        request,
                        current=(requirement,),
                    )
                    self.assertEqual(prepared.specs, ())

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()
                    self.assertEqual(requirement.status, "Annulé")
                    active = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(active, [])
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
