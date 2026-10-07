from __future__ import annotations

from functools import partial

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.security import AuthPrincipal, ROLE_PROJECT_MANAGER
from app.infrastructure.sql import (
    Base,
    CompetencyResourceClassAudit,
    Project,
    ResourceClassConfig,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app


def project_manager_resolver(_request):
    return AuthPrincipal.from_roles(
        local_user_id="PM-TEST",
        issuer="urn:resourceplanner:test",
        subject="pm-test",
        display_name="Chargé test",
        email=None,
        roles=(ROLE_PROJECT_MANAGER,),
        auth_mode="test",
    )


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_ADMIN_USER_ID,
)

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class CompetencyCatalogApiTests(unittest.TestCase):
    def _database_url(self, directory: str) -> str:
        database = Path(directory) / "competencies.db"
        database_url = f"sqlite+pysqlite:///{database.as_posix()}"
        engine = create_sql_engine(database_url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                Project(
                    id="PROJECT-272",
                    number="P-272",
                    name="Projet compétences",
                    status="Actif",
                )
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
            session.add_all(
                [
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        average_hourly_cost_cad="125.00",
                        active=True,
                        version=1,
                    ),
                    ResourceClassConfig(
                        code="HISTORIQUE",
                        label="Historique",
                        average_hourly_cost_cad="90.00",
                        active=False,
                        version=1,
                    ),
                ]
            )
        engine.dispose()
        return database_url

    def test_catalog_assignments_and_compatibility_snapshots(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database_url(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                scada = client.post(
                    "/api/v1/competencies",
                    json={
                        "name": "SCADA",
                        "description": "Supervision",
                        "sort_order": 10,
                    },
                )
                self.assertEqual(scada.status_code, 201)
                scada_id = scada.json()["competency_id"]

                plc = client.post(
                    "/api/v1/competencies",
                    json={
                        "name": "PLC",
                        "description": "Automates",
                        "sort_order": 20,
                    },
                )
                self.assertEqual(plc.status_code, 201)
                plc_id = plc.json()["competency_id"]

                duplicate = client.post(
                    "/api/v1/competencies",
                    json={"name": "scada"},
                )
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(
                    duplicate.json()["error"]["code"],
                    "competency_name_conflict",
                )

                created_resource = client.post(
                    "/api/v1/resources",
                    headers={"Idempotency-Key": "competency-resource-1"},
                    json={
                        "name": "Technicien compétences",
                        "competencies": "ancienne valeur ignorée",
                        "competency_ids": [scada_id, plc_id],
                        "active": True,
                        "sort_order": 10,
                    },
                )
                self.assertEqual(created_resource.status_code, 201)
                resource_id = created_resource.json()["resource_id"]

                resources = client.get(
                    "/api/v1/resources",
                    params={"active_only": "false"},
                )
                self.assertEqual(resources.status_code, 200)
                resource = next(
                    row for row in resources.json() if row["id"] == resource_id
                )
                self.assertEqual(
                    set(resource["competency_ids"]),
                    {scada_id, plc_id},
                )
                self.assertEqual(resource["competencies"], "SCADA; PLC")

                created_demand = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "competency-demand-1"},
                    json=routed_demand_payload({
                        "project_number": "P-272",
                        "desired_start": "2026-09-21",
                        "desired_end": "2026-09-22",
                        "description": "Besoin SCADA",
                        "estimated_hours": 8,
                        "required_competency_ids": [scada_id],
                    }),
                )
                self.assertEqual(created_demand.status_code, 201)
                demand_number = created_demand.json()["demand_number"]

                demand = client.get(f"/api/v1/demands/{demand_number}")
                self.assertEqual(demand.status_code, 200)
                self.assertEqual(demand.json()["required_competency_ids"], [scada_id])
                self.assertEqual(demand.json()["required_competencies"], "SCADA")

                submitted = client.post(
                    f"/api/v1/demands/{demand_number}/submit"
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                approved = client.post(
                    f"/api/v1/demands/{demand_number}/approve",
                    json={"comment": "Matérialisation compétence"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                segment_row = next(
                    row
                    for row in client.get(
                        "/api/v1/segments",
                        params={"include_cancelled": "false"},
                    ).json()
                    if row["demand_number"] == demand_number
                )
                segment_id = segment_row["segment_id"]
                segment = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(segment.status_code, 200)
                self.assertEqual(segment.json()["required_competency_id"], scada_id)
                self.assertEqual(segment.json()["required_competency"], "SCADA")

                renamed = client.patch(
                    f"/api/v1/competencies/{scada_id}",
                    json={"name": "SCADA / HMI"},
                )
                self.assertEqual(renamed.status_code, 200)

                resource = next(
                    row
                    for row in client.get(
                        "/api/v1/resources",
                        params={"active_only": "false"},
                    ).json()
                    if row["id"] == resource_id
                )
                self.assertEqual(resource["competencies"], "SCADA / HMI; PLC")
                self.assertEqual(
                    client.get(f"/api/v1/demands/{demand_number}").json()[
                        "required_competencies"
                    ],
                    "SCADA / HMI",
                )
                self.assertEqual(
                    client.get(f"/api/v1/segments/{segment_id}").json()[
                        "required_competency"
                    ],
                    "SCADA / HMI",
                )

                deactivated = client.post(
                    f"/api/v1/competencies/{scada_id}/deactivate"
                )
                self.assertEqual(deactivated.status_code, 200)
                all_rows = client.get(
                    "/api/v1/competencies",
                    params={"active_only": "false"},
                ).json()
                self.assertFalse(
                    next(row for row in all_rows if row["id"] == scada_id)["active"]
                )

                rejected = client.post(
                    "/api/v1/resources",
                    headers={"Idempotency-Key": "competency-resource-inactive"},
                    json={
                        "name": "Ressource invalide",
                        "competency_ids": [scada_id],
                    },
                )
                self.assertEqual(rejected.status_code, 422)
                self.assertEqual(
                    rejected.json()["error"]["code"],
                    "competency_inactive",
                )

    def test_competency_class_grouping_is_nullable_versioned_audited_and_analytic_only(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database_url(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/competencies",
                    json={"name": "Fibre optique", "sort_order": 30},
                )
                self.assertEqual(created.status_code, 201, created.text)
                competency_id = created.json()["competency_id"]

                initial = next(
                    row
                    for row in client.get(
                        "/api/v1/competencies",
                        params={"active_only": "false"},
                    ).json()
                    if row["id"] == competency_id
                )
                self.assertIsNone(initial["resource_class_code"])
                self.assertIsNone(initial["resource_class_label"])
                self.assertIsNone(initial["resource_class_active"])
                self.assertEqual(initial["resource_class_version"], 1)

                assigned = client.patch(
                    f"/api/v1/competencies/{competency_id}/resource-class",
                    json={
                        "resource_class_code": "PROGRAMMEUR",
                        "expected_version": 1,
                    },
                )
                self.assertEqual(assigned.status_code, 200, assigned.text)
                self.assertEqual(
                    assigned.json(),
                    {
                        "competency_id": competency_id,
                        "resource_class_code": "PROGRAMMEUR",
                        "version": 2,
                        "action": "COMPETENCY_RESOURCE_CLASS_SET",
                    },
                )

                stale = client.patch(
                    f"/api/v1/competencies/{competency_id}/resource-class",
                    json={
                        "resource_class_code": None,
                        "expected_version": 1,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "competency_resource_class_version_conflict",
                )

                inactive = client.patch(
                    f"/api/v1/competencies/{competency_id}/resource-class",
                    json={
                        "resource_class_code": "HISTORIQUE",
                        "expected_version": 2,
                    },
                )
                self.assertEqual(inactive.status_code, 422, inactive.text)
                self.assertEqual(
                    inactive.json()["error"]["code"],
                    "competency_resource_class_inactive",
                )

                missing = client.patch(
                    f"/api/v1/competencies/{competency_id}/resource-class",
                    json={
                        "resource_class_code": "INCONNUE",
                        "expected_version": 2,
                    },
                )
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertEqual(
                    missing.json()["error"]["code"],
                    "competency_resource_class_not_found",
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                resource_class = session.get(ResourceClassConfig, "PROGRAMMEUR")
                assert resource_class is not None
                resource_class.active = False
            engine.dispose()

            app = create_api_app(database_url)
            with TestClient(app) as client:
                historical = next(
                    row
                    for row in client.get(
                        "/api/v1/competencies",
                        params={"active_only": "false"},
                    ).json()
                    if row["id"] == competency_id
                )
                self.assertEqual(historical["resource_class_code"], "PROGRAMMEUR")
                self.assertEqual(historical["resource_class_label"], "Programmeur")
                self.assertFalse(historical["resource_class_active"])
                self.assertEqual(historical["resource_class_version"], 2)

                cleared = client.patch(
                    f"/api/v1/competencies/{competency_id}/resource-class",
                    json={
                        "resource_class_code": None,
                        "expected_version": 2,
                    },
                )
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(cleared.json()["version"], 3)
                self.assertIsNone(cleared.json()["resource_class_code"])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory() as session:
                audits = list(
                    session.scalars(
                        select(CompetencyResourceClassAudit)
                        .where(
                            CompetencyResourceClassAudit.competency_id
                            == competency_id
                        )
                        .order_by(
                            CompetencyResourceClassAudit.resulting_version
                        )
                    ).all()
                )
                self.assertEqual(
                    [
                        (
                            row.action,
                            row.old_resource_class_code,
                            row.new_resource_class_code,
                            row.resulting_version,
                            row.actor_user_id,
                        )
                        for row in audits
                    ],
                    [
                        (
                            "COMPETENCY_RESOURCE_CLASS_SET",
                            None,
                            "PROGRAMMEUR",
                            2,
                            TEST_ADMIN_USER_ID,
                        ),
                        (
                            "COMPETENCY_RESOURCE_CLASS_CLEARED",
                            "PROGRAMMEUR",
                            None,
                            3,
                            TEST_ADMIN_USER_ID,
                        ),
                    ],
                )
            engine.dispose()

    def test_catalog_mutations_require_resource_admin_permission(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database_url(directory)
            app = create_api_app(
                database_url,
                auth_resolver=project_manager_resolver,
            )
            with TestClient(app) as client:
                read = client.get("/api/v1/competencies")
                self.assertEqual(read.status_code, 200)

                write = client.post(
                    "/api/v1/competencies",
                    json={"name": "SCADA"},
                )
                self.assertEqual(write.status_code, 403)
                self.assertEqual(
                    write.json()["error"]["context"]["required_permission"],
                    "manage_resources",
                )

                grouping = client.patch(
                    "/api/v1/competencies/any/resource-class",
                    json={
                        "resource_class_code": None,
                        "expected_version": 1,
                    },
                )
                self.assertEqual(grouping.status_code, 403)
                self.assertEqual(
                    grouping.json()["error"]["context"]["required_permission"],
                    "manage_resources",
                )


if __name__ == "__main__":
    unittest.main()
