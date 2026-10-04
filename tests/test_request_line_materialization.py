from __future__ import annotations

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    BusinessContact,
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkPackage,
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


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)
D3 = date(2026, 9, 23)


class RequestLineMaterializationHttpTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet lignes"))
        session.add_all(
            [
                Resource(
                    id="R1",
                    name="Alice",
                    active=True,
                    resource_class="Programmation",
                ),
                Resource(
                    id="R2",
                    name="Bob",
                    active=True,
                    resource_class="Programmation",
                ),
                BusinessContact(
                    id="BC-OVR-1",
                    display_name="Responsable initial",
                    phone="555-0101",
                ),
                BusinessContact(
                    id="BC-OVR-2",
                    display_name="Responsable révisé",
                    phone="555-0102",
                ),
                BusinessContact(
                    id="BC-LOCAL",
                    display_name="Responsable local segment",
                    phone="555-0103",
                ),
                Competency(id="C1", name="Ignition", active=True),
                Competency(id="C2", name="AVEVA", active=True),
                TaskCatalogEntry(
                    id="T210",
                    project_number="P-1",
                    task_code="210",
                    label="Tâche 210",
                    active=True,
                ),
                TaskCatalogEntry(
                    id="T220",
                    project_number="P-1",
                    task_code="220",
                    label="Tâche 220",
                    active=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ResourceCompetency(resource_id="R1", competency_id="C1"),
                ResourceCompetency(resource_id="R2", competency_id="C2"),
                WorkPackage(
                    id="WP1",
                    project_id="P1",
                    legacy_effort_id="EFF-1",
                    code="WP-1",
                    name="Effort 1",
                ),
                WorkPackage(
                    id="WP2",
                    project_id="P1",
                    legacy_effort_id="EFF-2",
                    code="WP-2",
                    name="Effort 2",
                ),
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-R2",
                    resource_id="R2",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                ),
            ]
        )
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="request-line-materialization.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _create(
        client: TestClient,
        *,
        lines: list[dict[str, object]],
        submit: bool = True,
    ) -> str:
        response = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "description": "Demande multi-lignes",
                "submit": submit,
                "lines": lines,
            }),
        )
        assert response.status_code == 201, response.text
        return response.json()["demand_number"]

    @staticmethod
    def _demand(client: TestClient, number: str) -> dict[str, object]:
        response = client.get(f"/api/v1/demands/{number}")
        assert response.status_code == 200, response.text
        return response.json()

    def test_approval_materializes_plain_lines_with_provenance_and_competencies(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-288e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create(
                    client,
                    lines=[
                        {
                            "desired_start": D1.isoformat(),
                            "desired_end": D1.isoformat(),
                            "estimated_hours": 8,
                            "desired_active_days": 1,
                            "work_package_ref": "WP1",
                            "proposed_resource_id": "R1",
                            "required_resource_class": "Programmation",
                            "required_competency_ids": ["C1"],
                            "description": "Ligne Ignition",
                        },
                        {
                            "desired_start": D2.isoformat(),
                            "desired_end": D2.isoformat(),
                            "estimated_hours": 8,
                            "desired_active_days": 1,
                            "work_package_ref": "WP2",
                            "proposed_resource_id": "R2",
                            "required_resource_class": "Programmation",
                            "required_competency_ids": ["C2"],
                            "description": "Ligne AVEVA",
                        },
                    ],
                )
                demand = self._demand(client, number)
                line_ids = [row["line_id"] for row in demand["lines"]]

                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Approbation 288E"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    rows = session.scalars(
                        select(ResourceRequirement)
                        .where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                        .order_by(ResourceRequirement.start_date)
                    ).all()
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(
                        [row.source_request_line_id for row in rows],
                        line_ids,
                    )
                    self.assertEqual(
                        [float(row.planned_hours) for row in rows],
                        [8.0, 8.0],
                    )
                    self.assertEqual(
                        [row.source_effort_id for row in rows],
                        ["EFF-1", "EFF-2"],
                    )
                    self.assertEqual(
                        [row.required_resource_class for row in rows],
                        ["Programmation", "Programmation"],
                    )
                    self.assertEqual(
                        [row.assigned_resource_id for row in rows],
                        ["R1", "R2"],
                    )
                    links = session.execute(
                        select(
                            ResourceRequirementCompetency.resource_requirement_id,
                            ResourceRequirementCompetency.competency_id,
                        ).order_by(
                            ResourceRequirementCompetency.resource_requirement_id,
                            ResourceRequirementCompetency.competency_id,
                        )
                    ).all()
                    by_requirement = {
                        requirement.id: {
                            competency_id
                            for requirement_id, competency_id in links
                            if requirement_id == requirement.id
                        }
                        for requirement in rows
                    }
                    self.assertEqual(by_requirement[rows[0].id], {"C1"})
                    self.assertEqual(by_requirement[rows[1].id], {"C2"})
            finally:
                engine.dispose()

    def test_approval_snapshots_task_label_before_request_description(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-290a",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "description": "Description générale",
                        "submit": True,
                        "lines": [
                            {
                                "desired_start": D1.isoformat(),
                                "desired_end": D1.isoformat(),
                                "estimated_hours": 8,
                                "desired_active_days": 1,
                                "task_code": "210",
                                "proposed_resource_id": "R1",
                            }
                        ],
                    }),
                )
                self.assertEqual(response.status_code, 201, response.text)
                number = response.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Approbation 290A"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
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
                    self.assertEqual(requirement.description, "Tâche 210")
                    self.assertEqual(
                        requirement.approved_task_catalog_item_id,
                        "T210",
                    )
            finally:
                engine.dispose()

    def test_same_named_alternatives_materialize_independently_per_line(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-288e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create(
                    client,
                    lines=[
                        {
                            "desired_start": D1.isoformat(),
                            "desired_end": D2.isoformat(),
                            "estimated_hours": 8,
                            "proposed_resource_id": "R1",
                        },
                        {
                            "desired_start": D2.isoformat(),
                            "desired_end": D3.isoformat(),
                            "estimated_hours": 8,
                            "proposed_resource_id": "R2",
                        },
                    ],
                )
                line_a, line_b = [
                    row["line_id"] for row in self._demand(client, number)["lines"]
                ]

                alternatives = {
                    line_a: [
                        ("OPT-A", D1),
                        ("OPT-B", D2),
                    ],
                    line_b: [
                        ("OPT-A", D2),
                        ("OPT-B", D3),
                    ],
                }
                for line_id, options in alternatives.items():
                    request_version = client.get(
                        f"/api/v1/demands/{number}"
                    ).json()["version"]
                    replaced = client.put(
                        f"/api/v1/demands/{number}/lines/{line_id}/periods",
                        json={
                            "expected_request_version": request_version,
                            "periods": [
                                {
                                    "period_id": period_id,
                                    "start_date": day.isoformat(),
                                    "end_date": day.isoformat(),
                                    "hours": 8,
                                    "kind": "ALTERNATIVE",
                                    "alternative_group": "VISITE",
                                    "confirmation": "Confirmée",
                                    "resource_count": 1,
                                }
                                for period_id, day in options
                            ]
                        },
                    )
                    self.assertEqual(replaced.status_code, 200, replaced.text)

                selected_a = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/alternative-groups/VISITE/selection",
                    json={"period_id": "OPT-A"},
                )
                selected_b = client.put(
                    f"/api/v1/demands/{number}/lines/{line_b}/alternative-groups/VISITE/selection",
                    json={"period_id": "OPT-B"},
                )
                self.assertEqual(selected_a.status_code, 200, selected_a.text)
                self.assertEqual(selected_b.status_code, 200, selected_b.text)

                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Alternatives indépendantes"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    rows = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(len(rows), 2)
                    by_line = {row.source_request_line_id: row for row in rows}
                    self.assertEqual(by_line[line_a].start_date, D1)
                    self.assertEqual(by_line[line_b].start_date, D3)

                    period_rows = session.execute(
                        select(
                            WorkforceRequestPeriodRequirement.resource_requirement_id,
                            WorkforceRequestPeriod,
                        ).join(
                            WorkforceRequestPeriod,
                            WorkforceRequestPeriodRequirement.period_id
                            == WorkforceRequestPeriod.id,
                        )
                    ).all()
                    linked = {
                        requirement_id: period
                        for requirement_id, period in period_rows
                        if requirement_id in {row.id for row in rows}
                    }
                    self.assertEqual(linked[by_line[line_a].id].period_key, "OPT-A")
                    self.assertEqual(linked[by_line[line_a].id].request_line_id, line_a)
                    self.assertEqual(linked[by_line[line_b].id].period_key, "OPT-B")
                    self.assertEqual(linked[by_line[line_b].id].request_line_id, line_b)
            finally:
                engine.dispose()

    def test_reapproval_keeps_current_plan_until_approval_then_reuses_requirement(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-288e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create(
                    client,
                    lines=[
                        {
                            "desired_start": D1.isoformat(),
                            "desired_end": D1.isoformat(),
                            "estimated_hours": 8,
                            "desired_active_days": 1,
                            "proposed_resource_id": "R1",
                            "task_code": "210",
                        }
                    ],
                )
                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    request.operational_responsible_override_contact_id = "BC-OVR-1"
                engine.dispose()

                first_approval = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Version initiale"},
                )
                self.assertEqual(first_approval.status_code, 200, first_approval.text)

                demand = self._demand(client, number)
                line_id = demand["lines"][0]["line_id"]
                version = demand["version"]

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
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
                    requirement_id = requirement.id
                    requirement.operational_responsible_override_contact_id = "BC-LOCAL"
                    session.flush()
                    initial_shift = session.scalar(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    )
                    assert initial_shift is not None
                    self.assertEqual(requirement.start_date, D1)
                    self.assertEqual(requirement.approved_task_catalog_item_id, "T210")
                    self.assertEqual(
                        requirement.approved_operational_responsible_override_contact_id,
                        "BC-OVR-1",
                    )
                    self.assertEqual(requirement.approved_request_version, 1)
                    self.assertEqual(
                        requirement.approved_contact_context_status,
                        "CAPTURED",
                    )
                    self.assertEqual(initial_shift.work_date, D1)
                engine.dispose()

                pm_app = create_api_app(
                    database_url,
                    actor_name="pm-288e",
                    auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
                )
                with TestClient(pm_app, raise_server_exceptions=False) as pm_client:
                    changed = pm_client.patch(
                        f"/api/v1/demands/{number}",
                        json={
                            "expected_version": version,
                            "comment": "Déplacer au mardi",
                            "lines": [
                                {
                                    "id": line_id,
                                    "desired_start": D2.isoformat(),
                                    "desired_end": D2.isoformat(),
                                    "estimated_hours": 8,
                                    "desired_active_days": 1,
                                    "proposed_resource_id": "R1",
                                    "task_code": "220",
                                }
                            ],
                        },
                    )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertTrue(changed.json()["reapproval_required"])
                self.assertEqual(changed.json()["status"], "Soumise")

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    request.operational_responsible_override_contact_id = "BC-OVR-2"
                engine.dispose()

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory() as session:
                    requirement = session.get(ResourceRequirement, requirement_id)
                    assert requirement is not None
                    shift = session.scalar(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement_id
                        )
                    )
                    assert shift is not None
                    self.assertEqual(requirement.start_date, D1)
                    self.assertEqual(requirement.approved_task_catalog_item_id, "T210")
                    self.assertEqual(requirement.approved_request_version, 1)
                    self.assertEqual(
                        requirement.approved_operational_responsible_override_contact_id,
                        "BC-OVR-1",
                    )
                    self.assertEqual(
                        requirement.approved_contact_context_status,
                        "CAPTURED",
                    )
                    self.assertEqual(
                        requirement.operational_responsible_override_contact_id,
                        "BC-LOCAL",
                    )
                    self.assertEqual(shift.work_date, D1)
                engine.dispose()

                delta = client.get(f"/api/v1/demands/{number}/plan-delta")
                self.assertEqual(delta.status_code, 200, delta.text)
                self.assertTrue(delta.json()["available"])
                self.assertTrue(delta.json()["has_changes"])
                self.assertEqual(delta.json()["move_count"], 1)

                reapproved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Nouvelle version approuvée"},
                )
                self.assertEqual(reapproved.status_code, 200, reapproved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    requirement = session.get(ResourceRequirement, requirement_id)
                    assert requirement is not None
                    self.assertEqual(requirement.start_date, D2)
                    self.assertEqual(requirement.source_request_line_id, line_id)
                    self.assertEqual(requirement.approved_task_catalog_item_id, "T220")
                    self.assertEqual(requirement.approved_request_version, 4)
                    self.assertEqual(
                        requirement.approved_operational_responsible_override_contact_id,
                        "BC-OVR-2",
                    )
                    self.assertEqual(
                        requirement.approved_contact_context_status,
                        "CAPTURED",
                    )
                    self.assertEqual(
                        requirement.operational_responsible_override_contact_id,
                        "BC-LOCAL",
                    )
                    shift = session.scalar(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement_id
                        )
                    )
                    assert shift is not None
                    self.assertEqual(shift.work_date, D2)
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    self.assertEqual(request.status, "En planification")
            finally:
                engine.dispose()

    def test_materialized_work_requires_cancellation_request_without_destructive_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-288e",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create(
                    client,
                    lines=[
                        {
                            "desired_start": D1.isoformat(),
                            "desired_end": D1.isoformat(),
                            "estimated_hours": 8,
                            "proposed_resource_id": "R1",
                        }
                    ],
                )
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Plan initial"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory.begin() as session:
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
                    requirement_id = requirement.id
                    shift = session.scalar(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    )
                    assert shift is not None
                    shift.locked = True
                    shift.source = "MANUAL"
                    shift_id = shift.id
                engine.dispose()

                locked_blocked = client.post(f"/api/v1/demands/{number}/cancel")
                self.assertEqual(locked_blocked.status_code, 409, locked_blocked.text)
                self.assertEqual(
                    locked_blocked.json()["error"]["code"],
                    "active_operational_decisions",
                )

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory.begin() as session:
                    shift = session.get(Shift, shift_id)
                    assert shift is not None
                    self.assertTrue(shift.locked)
                    shift.locked = False
                engine.dispose()

                unlocked_blocked = client.post(f"/api/v1/demands/{number}/cancel")
                self.assertEqual(
                    unlocked_blocked.status_code,
                    409,
                    unlocked_blocked.text,
                )
                self.assertEqual(
                    unlocked_blocked.json()["error"]["code"],
                    "active_operational_decisions",
                )

                current = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(current.status_code, 200, current.text)
                current_version = current.json()["version"]
                requested = client.post(
                    f"/api/v1/demands/{number}/request-cancellation",
                    json={
                        "reason": "Annuler sans détruire le plan dans #399A",
                        "expected_version": current_version,
                    },
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                self.assertEqual(requested.json()["status"], "En planification")
                self.assertEqual(requested.json()["cancellation_state"], "PENDING")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    requirement = session.get(ResourceRequirement, requirement_id)
                    shift = session.get(Shift, shift_id)
                    assert request is not None
                    assert requirement is not None
                    assert shift is not None
                    self.assertEqual(request.status, "En planification")
                    self.assertEqual(request.cancellation_state, "PENDING")
                    self.assertNotEqual(requirement.status, "Annulé")
                    self.assertFalse(shift.locked)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
