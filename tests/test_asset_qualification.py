from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.infrastructure.sql import (
    AssetAllocation,
    AssetRequirement,
    Base,
    PlanningChangeHistory,
    Project,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import (
    map_asset_type_to_test_approval_scope,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


class AssetQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "asset-qualification.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        with create_session_factory(engine).begin() as session:
            session.add(
                Project(
                    id="PROJECT-292",
                    number="P-292",
                    name="Projet qualification actifs",
                )
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()

        self.client = TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        )
        self.client.__enter__()

        competency = self.client.post(
            "/api/v1/competencies",
            json={"name": "Permis nacelle", "sort_order": 10},
        )
        self.assertEqual(competency.status_code, 201, competency.text)
        self.competency_id = competency.json()["competency_id"]

        asset_type = self.client.post(
            "/api/v1/assets/types",
            json={
                "code": "NACELLE",
                "label": "Nacelle",
                "category": "EQUIPMENT",
            },
        )
        self.assertEqual(asset_type.status_code, 201, asset_type.text)
        self.type_id = asset_type.json()["id"]

        engine = create_sql_engine(self.url)
        with create_session_factory(engine).begin() as session:
            map_asset_type_to_test_approval_scope(session, self.type_id)
        engine.dispose()

        asset = self.client.post(
            "/api/v1/assets",
            json={
                "code": "N-292",
                "label": "Nacelle 292",
                "asset_type_id": self.type_id,
            },
        )
        self.assertEqual(asset.status_code, 201, asset.text)
        self.asset_id = asset.json()["id"]

        version = self.client.get("/api/v1/assets/catalog").json()["planning_version"]
        qualification = self.client.put(
            f"/api/v1/assets/types/{self.type_id}/qualification",
            json={
                "competency_ids": [self.competency_id],
                "qualification_policy": "ANY_ASSIGNED_WORKFORCE",
                "expected_planning_version": version,
            },
        )
        self.assertEqual(qualification.status_code, 200, qualification.text)

        skilled = self.client.post(
            "/api/v1/resources",
            headers={"Idempotency-Key": "resource-skilled-292"},
            json={
                "name": "Technicien qualifié",
                "competency_ids": [self.competency_id],
                "active": True,
            },
        )
        self.assertEqual(skilled.status_code, 201, skilled.text)
        self.skilled_resource_id = skilled.json()["resource_id"]

        unskilled = self.client.post(
            "/api/v1/resources",
            headers={"Idempotency-Key": "resource-unskilled-292"},
            json={"name": "Technicien sans permis", "active": True},
        )
        self.assertEqual(unskilled.status_code, 201, unskilled.text)
        self.unskilled_resource_id = unskilled.json()["resource_id"]

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def _approve(self, *, mixed: bool = True) -> tuple[str, str, str | None]:
        lines = [
            {
                "kind": "ASSET",
                "asset_type_id": self.type_id,
                "desired_start": "2026-09-24",
                "desired_end": "2026-09-26",
            }
        ]
        if mixed:
            lines.append(
                {
                    "kind": "WORKFORCE",
                    "desired_start": "2026-09-24",
                    "desired_end": "2026-09-26",
                    "estimated_hours": 8,
                }
            )
        created = self.client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-292",
                "submit": True,
                "lines": lines,
            }),
        )
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        current = self.client.get(f"/api/v1/demands/{number}").json()
        approved = self.client.post(
            f"/api/v1/demands/{number}/approve",
            json={"expected_version": current["version"]},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory() as session:
            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            self.assertIsNotNone(request)
            assert request is not None
            human_requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id
                )
            )
            result = (
                number,
                request.id,
                human_requirement.id if human_requirement is not None else None,
            )
        engine.dispose()
        return result

    def _add_shift(
        self,
        *,
        requirement_id: str,
        resource_id: str,
        work_date: date,
        shift_id: str,
    ) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                Shift(
                    id=shift_id,
                    resource_requirement_id=requirement_id,
                    resource_id=resource_id,
                    work_date=work_date,
                    hours=Decimal("8"),
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                )
            )
        engine.dispose()

    def _asset_requirement(self, request_id: str) -> tuple[dict, int]:
        state = self.client.get("/api/v1/assets/requirements").json()
        requirement = next(
            row for row in state["requirements"] if row["request_id"] == request_id
        )
        return requirement, state["planning_version"]

    def _reserve(self, request_id: str) -> tuple[dict, dict]:
        requirement, version = self._asset_requirement(request_id)
        reserved = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/reservation",
            headers={"Idempotency-Key": f"reserve-{request_id}"},
            json={
                "asset_id": self.asset_id,
                "start_date": "2026-09-24",
                "end_date": "2026-09-26",
                "expected_planning_version": version,
            },
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)
        return requirement, reserved.json()

    def _snapshot_requirement(self, requirement_id: str) -> dict:
        snapshot = self.client.get(
            "/api/v1/planning/snapshot"
            "?start=2026-09-24&end=2026-09-26"
        )
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        return next(
            row
            for row in snapshot.json()["asset_requirements"]
            if row["requirement_id"] == requirement_id
        )

    def test_type_without_prerequisite_is_satisfied_without_operator(self) -> None:
        version = self.client.get("/api/v1/assets/catalog").json()["planning_version"]
        cleared = self.client.put(
            f"/api/v1/assets/types/{self.type_id}/qualification",
            json={
                "competency_ids": [],
                "qualification_policy": "ANY_ASSIGNED_WORKFORCE",
                "expected_planning_version": version,
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)

        _number, request_id, _ = self._approve(mixed=False)
        requirement, _reserved = self._reserve(request_id)
        projected = self._snapshot_requirement(requirement["id"])

        self.assertEqual(projected["qualification_state"], "SATISFIED")
        self.assertEqual(projected["required_competency_ids"], [])
        self.assertIsNone(projected["operator_resource_id"])

    def test_type_without_prerequisite_still_allows_context_operator(self) -> None:
        version = self.client.get("/api/v1/assets/catalog").json()["planning_version"]
        cleared = self.client.put(
            f"/api/v1/assets/types/{self.type_id}/qualification",
            json={
                "competency_ids": [],
                "qualification_policy": "ANY_ASSIGNED_WORKFORCE",
                "expected_planning_version": version,
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)

        _number, request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.unskilled_resource_id,
            work_date=date(2026, 9, 25),
            shift_id="SHIFT-NO-PREREQ",
        )
        requirement, reserved = self._reserve(request_id)

        candidates = self.client.get(
            f"/api/v1/assets/requirements/{requirement['id']}/operator-candidates"
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(
            [row["resource_id"] for row in candidates.json()["candidates"]],
            [self.unskilled_resource_id],
        )

        assigned = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/operator",
            headers={"Idempotency-Key": "operator-no-prereq"},
            json={
                "operator_resource_id": self.unskilled_resource_id,
                "expected_planning_version": reserved["planning_version"],
            },
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["qualification_state"], "SATISFIED")

    def test_operator_validation_candidates_revalidation_and_audit(self) -> None:
        _number, request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None

        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.unskilled_resource_id,
            work_date=date(2026, 9, 24),
            shift_id="SHIFT-UNSKILLED",
        )
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=date(2026, 9, 23),
            shift_id="SHIFT-SKILLED",
        )
        requirement, reserved = self._reserve(request_id)

        candidates = self.client.get(
            f"/api/v1/assets/requirements/{requirement['id']}/operator-candidates"
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(candidates.json()["qualification_state"], "MISSING_OPERATOR")
        self.assertEqual(candidates.json()["candidates"], [])

        unskilled = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/operator",
            headers={"Idempotency-Key": "operator-unskilled"},
            json={
                "operator_resource_id": self.unskilled_resource_id,
                "expected_planning_version": reserved["planning_version"],
            },
        )
        self.assertEqual(unskilled.status_code, 422, unskilled.text)
        self.assertEqual(
            unskilled.json()["error"]["code"],
            "asset_operator_skill_mismatch",
        )

        no_overlap = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/operator",
            headers={"Idempotency-Key": "operator-no-overlap"},
            json={
                "operator_resource_id": self.skilled_resource_id,
                "expected_planning_version": reserved["planning_version"],
            },
        )
        self.assertEqual(no_overlap.status_code, 422, no_overlap.text)
        self.assertEqual(
            no_overlap.json()["error"]["code"],
            "asset_operator_no_overlap",
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            shift = session.get(Shift, "SHIFT-SKILLED")
            assert shift is not None
            shift.work_date = date(2026, 9, 25)
        engine.dispose()

        candidates = self.client.get(
            f"/api/v1/assets/requirements/{requirement['id']}/operator-candidates"
        ).json()
        self.assertEqual(
            [row["resource_id"] for row in candidates["candidates"]],
            [self.skilled_resource_id],
        )

        assigned = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/operator",
            headers={"Idempotency-Key": "operator-skilled"},
            json={
                "operator_resource_id": self.skilled_resource_id,
                "expected_planning_version": reserved["planning_version"],
            },
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["qualification_state"], "SATISFIED")

        invalid_dates = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/reservation",
            headers={"Idempotency-Key": "operator-retained-invalid-dates"},
            json={
                "asset_id": self.asset_id,
                "start_date": "2026-09-24",
                "end_date": "2026-09-24",
                "expected_planning_version": assigned.json()["planning_version"],
            },
        )
        self.assertEqual(invalid_dates.status_code, 422, invalid_dates.text)
        self.assertEqual(
            invalid_dates.json()["error"]["code"],
            "asset_operator_no_overlap",
        )
        state_after_rejection = self.client.get("/api/v1/assets/requirements").json()
        preserved = next(
            row
            for row in state_after_rejection["allocations"]
            if row["requirement_id"] == requirement["id"]
        )
        self.assertEqual(preserved["start_date"], "2026-09-24")
        self.assertEqual(preserved["end_date"], "2026-09-26")
        self.assertEqual(
            preserved["operator_resource_id"],
            self.skilled_resource_id,
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory() as session:
            audit = session.scalars(
                select(PlanningChangeHistory).where(
                    PlanningChangeHistory.action == "Opérateur qualifiant"
                )
            ).all()
            self.assertEqual(len(audit), 1)
            self.assertIn(self.skilled_resource_id, audit[0].details or "")
        engine.dispose()

        removed_skill = self.client.patch(
            f"/api/v1/resources/{self.skilled_resource_id}",
            json={"competency_ids": []},
        )
        self.assertEqual(removed_skill.status_code, 200, removed_skill.text)
        projected = self._snapshot_requirement(requirement["id"])
        self.assertEqual(projected["qualification_state"], "SKILL_MISMATCH")

        restored_skill = self.client.patch(
            f"/api/v1/resources/{self.skilled_resource_id}",
            json={"competency_ids": [self.competency_id]},
        )
        self.assertEqual(restored_skill.status_code, 200, restored_skill.text)

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.execute(delete(Shift).where(Shift.id == "SHIFT-SKILLED"))
        engine.dispose()

        snapshot = self.client.get(
            "/api/v1/planning/snapshot"
            "?start=2026-09-24&end=2026-09-26"
        )
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        projected = next(
            row
            for row in snapshot.json()["asset_requirements"]
            if row["requirement_id"] == requirement["id"]
        )
        self.assertEqual(projected["qualification_state"], "NO_OVERLAP")
        self.assertIn(
            "ASSET_QUALIFICATION_NO_OVERLAP",
            {row["code"] for row in snapshot.json()["asset_diagnostics"]},
        )

    def test_communication_preview_is_blocked_until_asset_is_qualified(self) -> None:
        _number, request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=date(2026, 9, 24),
            shift_id="SHIFT-COMM",
        )
        self._reserve(request_id)

        preview = self.client.get(
            "/api/v1/communications/project-preview"
            "?week_start=2026-09-23"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        draft = preview.json()["drafts"][0]
        qualification_diagnostics = [
            row
            for row in draft["diagnostics"]
            if row["code"] == "ASSET_QUALIFICATION_MISSING_OPERATOR"
        ]
        self.assertEqual(len(qualification_diagnostics), 1)
        self.assertEqual(qualification_diagnostics[0]["severity"], "BLOCKING")
        self.assertFalse(draft["approvable"])

        prepared = self.client.post(
            "/api/v1/communications/project-batches",
            json={
                "week_start": "2026-09-23",
                "expected_fingerprint": preview.json()["snapshot_fingerprint"],
                "reviews": [],
            },
        )
        self.assertEqual(prepared.status_code, 422, prepared.text)
        self.assertEqual(
            prepared.json()["error"]["code"],
            "project_communication_not_approvable",
        )

    def test_project_direct_asset_qualification_blocks_project_communication(self) -> None:
        _number, _request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=date(2026, 9, 24),
            shift_id="SHIFT-PROJECT-DIRECT-COMM",
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                AssetRequirement(
                    id="AREQ-PROJECT-DIRECT-COMM",
                    project_id="PROJECT-292",
                    origin="PROJECT_DIRECT",
                    asset_type_id=self.type_id,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    status="Planifié",
                )
            )
            session.flush()
            session.add(
                AssetAllocation(
                    id="AALLOC-PROJECT-DIRECT-COMM",
                    asset_requirement_id="AREQ-PROJECT-DIRECT-COMM",
                    asset_id=self.asset_id,
                    operator_resource_id=None,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    locked=True,
                    source="MANUAL",
                )
            )
        engine.dispose()

        preview = self.client.get(
            "/api/v1/communications/project-preview"
            "?week_start=2026-09-23"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        draft = preview.json()["drafts"][0]
        codes = {row["code"] for row in draft["diagnostics"]}
        self.assertIn("ASSET_QUALIFICATION_MISSING_OPERATOR", codes)
        self.assertFalse(draft["approvable"])

    def test_segment_asset_qualification_blocks_project_communication_after_skill_loss(self) -> None:
        _number, _request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=date(2026, 9, 24),
            shift_id="SHIFT-SEGMENT-COMM",
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                AssetRequirement(
                    id="AREQ-SEGMENT-COMM",
                    project_id="PROJECT-292",
                    origin="SEGMENT",
                    resource_requirement_id=human_requirement_id,
                    asset_type_id=self.type_id,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    status="Planifié",
                )
            )
            session.flush()
            session.add(
                AssetAllocation(
                    id="AALLOC-SEGMENT-COMM",
                    asset_requirement_id="AREQ-SEGMENT-COMM",
                    asset_id=self.asset_id,
                    operator_resource_id=self.skilled_resource_id,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    locked=True,
                    source="MANUAL",
                )
            )
        engine.dispose()

        removed_skill = self.client.patch(
            f"/api/v1/resources/{self.skilled_resource_id}",
            json={"competency_ids": []},
        )
        self.assertEqual(removed_skill.status_code, 200, removed_skill.text)

        preview = self.client.get(
            "/api/v1/communications/project-preview"
            "?week_start=2026-09-23"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        draft = preview.json()["drafts"][0]
        codes = {row["code"] for row in draft["diagnostics"]}
        self.assertIn("ASSET_QUALIFICATION_SKILL_MISMATCH", codes)
        self.assertFalse(draft["approvable"])

    def test_projectless_resource_period_qualification_reaches_communication(self) -> None:
        _number, _request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        day = date(2026, 9, 24)
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=day,
            shift_id="SHIFT-RESOURCE-PERIOD-COMM",
        )

        created = self.client.post(
            "/api/v1/assets/resource-period-reservations",
            headers={"Idempotency-Key": "resource-period-comm-615b"},
            json={
                "resource_id": self.skilled_resource_id,
                "project_id": None,
                "asset_type_id": self.type_id,
                "asset_id": self.asset_id,
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "expected_planning_version": self.client.get(
                    "/api/v1/assets/requirements"
                ).json()["planning_version"],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)

        removed_skill = self.client.patch(
            f"/api/v1/resources/{self.skilled_resource_id}",
            json={"competency_ids": []},
        )
        self.assertEqual(removed_skill.status_code, 200, removed_skill.text)

        preview = self.client.get(
            "/api/v1/communications/project-preview"
            "?week_start=2026-09-23"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        draft = preview.json()["drafts"][0]
        codes = {row["code"] for row in draft["diagnostics"]}
        self.assertIn("ASSET_QUALIFICATION_SKILL_MISMATCH", codes)
        self.assertFalse(draft["approvable"])

    def test_shift_owned_asset_qualification_blocks_communication_after_skill_loss(self) -> None:
        _number, _request_id, human_requirement_id = self._approve()
        self.assertIsNotNone(human_requirement_id)
        assert human_requirement_id is not None
        self._add_shift(
            requirement_id=human_requirement_id,
            resource_id=self.skilled_resource_id,
            work_date=date(2026, 9, 24),
            shift_id="SHIFT-ADHOC-COMM",
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                AssetRequirement(
                    id="AREQ-ADHOC-COMM",
                    project_id="PROJECT-292",
                    origin="SHIFT_AD_HOC",
                    shift_id="SHIFT-ADHOC-COMM",
                    asset_type_id=self.type_id,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    status="Planifié",
                )
            )
            session.flush()
            session.add(
                AssetAllocation(
                    id="AALLOC-ADHOC-COMM",
                    asset_requirement_id="AREQ-ADHOC-COMM",
                    asset_id=self.asset_id,
                    operator_resource_id=self.skilled_resource_id,
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    locked=True,
                    source="MANUAL",
                )
            )
        engine.dispose()

        removed_skill = self.client.patch(
            f"/api/v1/resources/{self.skilled_resource_id}",
            json={"competency_ids": []},
        )
        self.assertEqual(removed_skill.status_code, 200, removed_skill.text)

        preview = self.client.get(
            "/api/v1/communications/project-preview"
            "?week_start=2026-09-23"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        draft = preview.json()["drafts"][0]
        codes = {row["code"] for row in draft["diagnostics"]}
        self.assertIn("ASSET_QUALIFICATION_SKILL_MISMATCH", codes)
        self.assertFalse(draft["approvable"])



if __name__ == "__main__":
    unittest.main()
