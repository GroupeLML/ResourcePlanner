from __future__ import annotations

from datetime import date
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import AuthPrincipal, ROLE_TECHNICIAN
from app.infrastructure.sql import Project, Resource
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class RequestLinePeriodApiTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet lignes"))
        session.add_all(
            [
                Resource(id="R1", name="Alice", active=True),
                Resource(id="R2", name="Bob", active=True),
            ]
        )
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="line-periods.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _create_multiline(client: TestClient) -> tuple[str, list[str]]:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "lines": [
                    {
                        "required_resource_class": "Automatisation",
                        "desired_start": D1.isoformat(),
                        "desired_active_days": 1,
                    },
                    {
                        "required_resource_class": "Électricité",
                        "desired_start": D2.isoformat(),
                        "desired_active_days": 1,
                    },
                ],
            }),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        demand = client.get(f"/api/v1/demands/{number}")
        assert demand.status_code == 200, demand.text
        line_ids = [
            row["line_id"]
            for row in demand.json()["lines"]
            if row["active"]
        ]
        assert len(line_ids) == 2
        return number, line_ids

    @staticmethod
    def _with_request_version(
        client: TestClient,
        number: str,
        payload: dict,
    ) -> dict:
        demand = client.get(f"/api/v1/demands/{number}")
        assert demand.status_code == 200, demand.text
        return {
            **payload,
            "expected_request_version": demand.json()["version"],
        }

    @staticmethod
    def _alternatives(prefix: str) -> dict:
        return {
            "periods": [
                {
                    "period_id": f"{prefix}-A",
                    "start_date": D1.isoformat(),
                    "end_date": D1.isoformat(),
                    "hours": 8,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "desired_active_days": 1,
                },
                {
                    "period_id": f"{prefix}-B",
                    "start_date": D2.isoformat(),
                    "end_date": D2.isoformat(),
                    "hours": 8,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "desired_active_days": 1,
                },
            ]
        }

    def test_same_alternative_group_is_independent_on_two_lines(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="coord")
            with TestClient(app, raise_server_exceptions=False) as client:
                number, (line_a, line_b) = self._create_multiline(client)

                first = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods",
                    json=self._with_request_version(
                        client,
                        number,
                        self._alternatives("OPT"),
                    ),
                )
                second = client.put(
                    f"/api/v1/demands/{number}/lines/{line_b}/periods",
                    json=self._with_request_version(
                        client,
                        number,
                        self._alternatives("OPT"),
                    ),
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(second.status_code, 200, second.text)

                legacy = client.get(f"/api/v1/demands/{number}/periods")
                self.assertEqual(legacy.status_code, 409, legacy.text)
                self.assertEqual(
                    legacy.json()["error"]["code"],
                    "demand_line_period_scope_required",
                )

                line_a_periods = client.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                )
                line_b_periods = client.get(
                    f"/api/v1/demands/{number}/lines/{line_b}/periods"
                )
                self.assertEqual(line_a_periods.status_code, 200, line_a_periods.text)
                self.assertEqual(line_b_periods.status_code, 200, line_b_periods.text)
                self.assertEqual(
                    [row["period_id"] for row in line_a_periods.json()],
                    ["OPT-A", "OPT-B"],
                )
                self.assertEqual(
                    [row["period_id"] for row in line_b_periods.json()],
                    ["OPT-A", "OPT-B"],
                )
                self.assertTrue(
                    all(row["request_line_id"] == line_a for row in line_a_periods.json())
                )
                self.assertTrue(
                    all(row["request_line_id"] == line_b for row in line_b_periods.json())
                )

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

                selected_a_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                ).json()
                selected_b_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_b}/periods"
                ).json()
                self.assertEqual(
                    [row["period_id"] for row in selected_a_rows if row["selected"]],
                    ["OPT-A"],
                )
                self.assertEqual(
                    [row["period_id"] for row in selected_b_rows if row["selected"]],
                    ["OPT-B"],
                )

                submitted = client.post(f"/api/v1/demands/{number}/submit")
                self.assertEqual(submitted.status_code, 200, submitted.text)
                snapshot = client.get(
                    "/api/v1/planning/snapshot",
                    params={"start": D1.isoformat(), "end": D2.isoformat()},
                )
                self.assertEqual(snapshot.status_code, 200, snapshot.text)
                pending = next(
                    row
                    for row in snapshot.json()["pending_loads"]
                    if row["demand_number"] == number
                )
                self.assertEqual(pending["projected_hours"], 16.0)
                self.assertEqual(len(pending["periods"]), 4)
                self.assertEqual(
                    {row["request_line_id"] for row in pending["periods"]},
                    {line_a, line_b},
                )
                self.assertEqual(
                    {
                        (row["request_line_id"], row["period_id"])
                        for row in pending["periods"]
                        if row["selected"]
                    },
                    {(line_a, "OPT-A"), (line_b, "OPT-B")},
                )

    def test_cumulative_periods_are_kept_on_their_own_line(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="coord")
            with TestClient(app, raise_server_exceptions=False) as client:
                number, (line_a, line_b) = self._create_multiline(client)
                payload = {
                    "periods": [
                        {
                            "period_id": "FIXED-1",
                            "start_date": D1.isoformat(),
                            "end_date": D1.isoformat(),
                            "hours": 4,
                            "kind": "CUMULATIVE",
                            "desired_active_days": 1,
                        },
                        {
                            "period_id": "FIXED-2",
                            "start_date": D2.isoformat(),
                            "end_date": D2.isoformat(),
                            "hours": 6,
                            "kind": "CUMULATIVE",
                            "desired_active_days": 1,
                        },
                    ]
                }
                response = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods",
                    json=self._with_request_version(client, number, payload),
                )
                self.assertEqual(response.status_code, 200, response.text)

                line_a_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                ).json()
                line_b_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_b}/periods"
                ).json()
                self.assertEqual(
                    [row["period_id"] for row in line_a_rows],
                    ["FIXED-1", "FIXED-2"],
                )
                self.assertEqual(sum(row["hours"] for row in line_a_rows), 10)
                self.assertEqual(line_b_rows, [])

    def test_replace_is_line_scoped_and_validates_slot_semantics_and_ownership(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="coord")
            with TestClient(app, raise_server_exceptions=False) as client:
                number, (line_a, line_b) = self._create_multiline(client)
                self.assertEqual(
                    client.put(
                        f"/api/v1/demands/{number}/lines/{line_a}/periods",
                        json=self._with_request_version(
                            client,
                            number,
                            self._alternatives("A"),
                        ),
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.put(
                        f"/api/v1/demands/{number}/lines/{line_b}/periods",
                        json=self._with_request_version(
                            client,
                            number,
                            self._alternatives("B"),
                        ),
                    ).status_code,
                    200,
                )

                changed = self._alternatives("A")
                changed["periods"][0]["desired_active_days"] = 2
                changed["periods"][0]["end_date"] = D2.isoformat()
                replaced = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods",
                    json=self._with_request_version(client, number, changed),
                )
                self.assertEqual(replaced.status_code, 200, replaced.text)
                a_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                ).json()
                b_rows = client.get(
                    f"/api/v1/demands/{number}/lines/{line_b}/periods"
                ).json()
                self.assertEqual(a_rows[0]["desired_active_days"], 2)
                self.assertEqual(
                    [row["period_id"] for row in b_rows],
                    ["B-A", "B-B"],
                )

                invalid_count = self._alternatives("COUNT")
                invalid_count["periods"][0]["resource_count"] = 2
                rejected = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods",
                    json=self._with_request_version(
                        client,
                        number,
                        invalid_count,
                    ),
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(
                    rejected.json()["error"]["code"],
                    "demand_line_period_resource_count_invalid",
                )

                other_created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "lines": [
                            {
                                "desired_start": D1.isoformat(),
                                "desired_active_days": 1,
                            }
                        ],
                    }),
                )
                other_number = other_created.json()["demand_number"]
                foreign_line = client.get(
                    f"/api/v1/demands/{other_number}"
                ).json()["lines"][0]["line_id"]
                foreign = client.put(
                    f"/api/v1/demands/{number}/lines/{foreign_line}/periods",
                    json=self._with_request_version(
                        client,
                        number,
                        self._alternatives("FOREIGN"),
                    ),
                )
                self.assertEqual(foreign.status_code, 404, foreign.text)
                self.assertEqual(
                    foreign.json()["error"]["code"],
                    "demand_line_not_found",
                )

    def test_line_period_routes_keep_existing_rbac(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(database_url, actor_name="coord")
            with TestClient(admin_app, raise_server_exceptions=False) as admin:
                number, (line_a, _line_b) = self._create_multiline(admin)

            technician = AuthPrincipal.from_roles(
                local_user_id="tech",
                issuer="urn:test",
                subject="tech",
                display_name="Technicien",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                auth_mode="test",
            )
            read_only_app = create_api_app(
                database_url,
                auth_resolver=static_auth_resolver(technician),
            )
            with TestClient(read_only_app, raise_server_exceptions=False) as client:
                read = client.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                )
                write = client.put(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods",
                    json=self._alternatives("RBAC"),
                )

            # ADR-030: reading demand periods requires read_demands.
            self.assertEqual(read.status_code, 403, read.text)
            self.assertEqual(read.json()["error"]["code"], "permission_denied")
            self.assertEqual(write.status_code, 403, write.text)
            self.assertEqual(write.json()["error"]["code"], "permission_denied")

            with TestClient(
                create_api_app(database_url, actor_name="coord"),
                raise_server_exceptions=False,
            ) as admin:
                allowed_read = admin.get(
                    f"/api/v1/demands/{number}/lines/{line_a}/periods"
                )
            self.assertEqual(allowed_read.status_code, 200, allowed_read.text)


if __name__ == "__main__":
    unittest.main()
