from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from app.infrastructure.sql import (
    Base,
    Project,
    RequestApprovalRevision,
    RequestLine,
    RequestRequirementPlanningIntent,
    ResourceRequirement,
    SqlRequestRequirementPlanningIntentRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


DAY = date(2026, 10, 28)


class RequestPlanningIntentRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet 778"))
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-778",
                    project_id="P1",
                    line_mode=True,
                    status="Approuvée",
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=2,
                    desired_start=DAY,
                    desired_end=DAY,
                    estimated_hours=Decimal("80"),
                    confirmation="Confirmée",
                    active=True,
                )
            )
            session.add(
                RequestApprovalRevision(
                    id="REV1",
                    workforce_request_id="D1",
                    request_version=1,
                    approved_at=datetime.now(timezone.utc),
                    payload_text="{}",
                    authorization_fingerprint="a" * 64,
                )
            )
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ1",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approval_revision_id="REV1",
                        approved_entry_key='["LINE","L1",""]',
                        approval_reference_status="CAPTURED",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("40"),
                        status="À assigner",
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ2",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approval_revision_id="REV1",
                        approved_entry_key='["LINE","L1",""]',
                        approval_reference_status="CAPTURED",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("40"),
                        status="À assigner",
                        origin="REQUEST",
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_slot_identity_distinguishes_two_slots_of_same_approved_entry(self) -> None:
        with transactional_session(self.factory) as session:
            repo = SqlRequestRequirementPlanningIntentRepository(session)
            first = repo.ensure(
                workforce_request_id="D1",
                approved_entry_key='["LINE","L1",""]',
                slot_index=0,
                source_request_line_id="L1",
                source_period_id=None,
                approval_revision_id="REV1",
                current_resource_requirement_id="REQ1",
            )
            second = repo.ensure(
                workforce_request_id="D1",
                approved_entry_key='["LINE","L1",""]',
                slot_index=1,
                source_request_line_id="L1",
                source_period_id=None,
                approval_revision_id="REV1",
                current_resource_requirement_id="REQ2",
            )
            self.assertNotEqual(first.id, second.id)
            self.assertNotEqual(first.slot_identity_key, second.slot_identity_key)

    def test_rematerialization_rebinds_current_requirement_without_resetting_intent(self) -> None:
        with transactional_session(self.factory) as session:
            repo = SqlRequestRequirementPlanningIntentRepository(session)
            intent = repo.ensure(
                workforce_request_id="D1",
                approved_entry_key='["LINE","L1",""]',
                slot_index=0,
                source_request_line_id="L1",
                source_period_id=None,
                approval_revision_id="REV1",
                current_resource_requirement_id="REQ1",
            )
            intent.intent_mode = "PRESERVE_EXISTING"
            session.flush()
            rebound = repo.ensure(
                workforce_request_id="D1",
                approved_entry_key='["LINE","L1",""]',
                slot_index=0,
                source_request_line_id="L1",
                source_period_id=None,
                approval_revision_id="REV1",
                current_resource_requirement_id="REQ2",
            )
            self.assertEqual(rebound.id, intent.id)
            self.assertEqual(rebound.intent_mode, "PRESERVE_EXISTING")
            self.assertEqual(rebound.current_resource_requirement_id, "REQ2")
            self.assertEqual(
                session.query(RequestRequirementPlanningIntent).count(),
                1,
            )


if __name__ == "__main__":
    unittest.main()
