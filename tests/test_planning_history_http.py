from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.query_models import PlanningHistoryReadModel, ShiftReadModel
from app.application.security import ROLE_MANAGER, AuthPrincipal
from app.application.read_models import SegmentReadModel
from app.server.routes_reads import build_read_router


class _Queries:
    def get_segment(self, segment_id: str):
        if segment_id != "SEG-2026-9001":
            return None
        return SegmentReadModel(
            segment_id=segment_id,
            demand_number="DMO-2026-9001",
            project_number="P-100",
            project_name="Projet audit",
            resource_name=None,
            start_date=date(2026, 9, 16),
            end_date=date(2026, 9, 18),
            planned_hours=8,
            status="À assigner",
        )

    def list_shifts(self, *, allocation_id: str | None = None, **_kwargs):
        if allocation_id != "MAN-AUDIT-1":
            return ()
        return (
            ShiftReadModel(
                allocation_id="MAN-AUDIT-1",
                segment_id="SEG-2026-9001",
                resource_id="RES-1",
                resource_name="Ressource Test",
                work_date=date(2026, 9, 16),
                hours=8.0,
                project_id="PROJ-1",
                project_number="P-100",
            ),
        )

    def list_planning_history(self, entity_type: str, reference: str):
        return (
            PlanningHistoryReadModel(
                entity_type=entity_type,
                entity_reference=reference,
                parent_reference="SEG-2026-9001" if entity_type == "SHIFT" else None,
                action="Modification segment" if entity_type == "SEGMENT" else "Création quart manuel",
                occurred_at=datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc),
                details='{"changes":{"planned_hours":{"before":8,"after":10}}}',
                actor_name="Planificateur Test",
            ),
        )


class PlanningHistoryHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        queries = _Queries()
        app = FastAPI()

        @app.middleware("http")
        async def inject_principal(request, call_next):
            request.state.auth_principal = AuthPrincipal.from_roles(
                local_user_id="USER-1",
                issuer="urn:test",
                subject="manager",
                display_name="Gestionnaire",
                email=None,
                roles=(ROLE_MANAGER,),
                auth_mode="test",
            )
            return await call_next(request)

        app.include_router(
            build_read_router(
                lambda: queries,
                lambda: object(),
            )
        )
        self.client = TestClient(app)

    def test_segment_history_endpoint_returns_backend_audit(self) -> None:
        response = self.client.get("/api/v1/segments/SEG-2026-9001/history")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("SEGMENT", payload[0]["entity_type"])
        self.assertEqual("Planificateur Test", payload[0]["actor_name"])

    def test_shift_history_endpoint_keeps_parent_segment_reference(self) -> None:
        response = self.client.get("/api/v1/shifts/MAN-AUDIT-1/history")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("SHIFT", payload[0]["entity_type"])
        self.assertEqual("SEG-2026-9001", payload[0]["parent_reference"])


if __name__ == "__main__":
    unittest.main()
