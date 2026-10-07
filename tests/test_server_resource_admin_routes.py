from __future__ import annotations

from functools import partial

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.infrastructure.sql import AppUser, Base, create_session_factory, create_sql_engine
from app.server import create_api_app as build_api_app
from app.server.security import static_auth_resolver
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(build_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ServerResourceAdminRouteTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "resource-admin.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return url

    def test_resource_profile_and_availability_lifecycle(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                test_email = "auto1" + chr(64) + "example.test"
                created = client.post(
                    "/api/v1/resources",
                    json={
                        "name": "Automatisation 1",
                        "email": test_email,
                        "resource_class": "Programmation",
                        "competencies": "PLC; SCADA",
                        "note": "Ressource de test",
                        "sort_order": 20,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                resource_id = created.json()["resource_id"]

                resources = client.get("/api/v1/resources?active_only=false")
                self.assertEqual(resources.status_code, 200, resources.text)
                self.assertEqual(len(resources.json()), 1)
                row = resources.json()[0]
                self.assertEqual(row["id"], resource_id)
                self.assertEqual(row["email"], test_email)
                self.assertEqual(row["resource_class"], "Programmation")
                self.assertEqual(row["competencies"], "PLC; SCADA")
                self.assertEqual(row["sort_order"], 20)

                duplicate = client.post(
                    "/api/v1/resources",
                    json={"name": "AUTOMATISATION 1"},
                )
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                self.assertEqual(
                    duplicate.json()["error"]["code"],
                    "resource_name_conflict",
                )

                updated = client.patch(
                    f"/api/v1/resources/{resource_id}",
                    json={
                        "competencies": "PLC; SCADA; MES",
                        "sort_order": 10,
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)

                null_active = client.patch(
                    f"/api/v1/resources/{resource_id}",
                    json={"active": None},
                )
                self.assertEqual(null_active.status_code, 422, null_active.text)

                standard = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Horaire standard",
                        "resource_id": resource_id,
                        "start_date": "2026-01-01",
                        "end_date": "2027-12-31",
                        "weekdays": "Lun,Mar,Mer,Jeu,Ven",
                        "start_time": "07:00:00",
                        "end_time": "15:30:00",
                        "note": "Horaire régulier",
                    },
                )
                self.assertEqual(standard.status_code, 201, standard.text)
                standard_id = standard.json()["rule_id"]

                holiday = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Jour férié",
                        "start_date": "2026-12-25",
                        "end_date": "2026-12-25",
                        "note": "Noël",
                    },
                )
                self.assertEqual(holiday.status_code, 201, holiday.text)
                holiday_id = holiday.json()["rule_id"]

                missing_resource = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Vacances",
                        "start_date": "2026-10-01",
                        "end_date": "2026-10-02",
                    },
                )
                self.assertEqual(missing_resource.status_code, 422, missing_resource.text)
                self.assertEqual(
                    missing_resource.json()["error"]["code"],
                    "availability_resource_required",
                )

                invalid_weekday = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Horaire standard",
                        "resource_id": resource_id,
                        "weekdays": "Lundi,Mardi",
                        "start_time": "07:00:00",
                        "end_time": "15:30:00",
                    },
                )
                self.assertEqual(invalid_weekday.status_code, 422, invalid_weekday.text)
                self.assertEqual(
                    invalid_weekday.json()["error"]["code"],
                    "availability_weekdays_invalid",
                )

                scoped = client.get(
                    "/api/v1/availability-rules",
                    params={
                        "resource_id": resource_id,
                        "include_global": "true",
                        "active_only": "true",
                    },
                )
                self.assertEqual(scoped.status_code, 200, scoped.text)
                self.assertEqual(
                    {item["id"] for item in scoped.json()},
                    {standard_id, holiday_id},
                )
                standard_row = next(
                    item for item in scoped.json() if item["id"] == standard_id
                )
                self.assertEqual(standard_row["resource_name"], "Automatisation 1")
                self.assertEqual(standard_row["start_time"], "07:00:00")

                resource_only = client.get(
                    "/api/v1/availability-rules",
                    params={
                        "resource_id": resource_id,
                        "include_global": "false",
                    },
                )
                self.assertEqual(
                    [item["id"] for item in resource_only.json()],
                    [standard_id],
                )

                deactivated_rule = client.post(
                    f"/api/v1/availability-rules/{standard_id}/deactivate"
                )
                self.assertEqual(deactivated_rule.status_code, 200, deactivated_rule.text)

                active_rules = client.get(
                    "/api/v1/availability-rules",
                    params={"resource_id": resource_id, "include_global": "true"},
                )
                self.assertEqual(
                    [item["id"] for item in active_rules.json()],
                    [holiday_id],
                )
                all_rules = client.get(
                    "/api/v1/availability-rules",
                    params={
                        "resource_id": resource_id,
                        "include_global": "true",
                        "active_only": "false",
                    },
                )
                self.assertEqual(len(all_rules.json()), 2)
                inactive_standard = next(
                    item for item in all_rules.json() if item["id"] == standard_id
                )
                self.assertFalse(inactive_standard["active"])

                deactivated_resource = client.post(
                    f"/api/v1/resources/{resource_id}/deactivate"
                )
                self.assertEqual(
                    deactivated_resource.status_code,
                    200,
                    deactivated_resource.text,
                )
                self.assertEqual(client.get("/api/v1/resources").json(), [])
                inactive_resources = client.get(
                    "/api/v1/resources?active_only=false"
                ).json()
                self.assertEqual(len(inactive_resources), 1)
                self.assertFalse(inactive_resources[0]["active"])
                self.assertEqual(inactive_resources[0]["sort_order"], 10)


    def test_class_scoped_holiday_create_patch_and_validation_contract(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                for code, label in (
                    ("PROGRAMMEUR", "Programmeur"),
                    ("INSTALLATION", "Installation"),
                ):
                    response = client.post(
                        "/api/v1/admin/resource-classes",
                        json={
                            "code": code,
                            "label": label,
                            "average_hourly_cost_cad": "100.00",
                            "active": True,
                        },
                    )
                    self.assertEqual(response.status_code, 201, response.text)

                resource = client.post(
                    "/api/v1/resources",
                    json={
                        "name": "Alice",
                        "resource_class": "PROGRAMMEUR",
                    },
                )
                self.assertEqual(resource.status_code, 201, resource.text)
                resource_id = resource.json()["resource_id"]

                targeted = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Jour férié",
                        "start_date": "2026-12-25",
                        "end_date": "2026-12-25",
                        "resource_class_codes": ["PROGRAMMEUR"],
                    },
                )
                self.assertEqual(targeted.status_code, 201, targeted.text)
                rule_id = targeted.json()["rule_id"]

                rows = client.get(
                    "/api/v1/availability-rules",
                    params={"active_only": "false"},
                )
                self.assertEqual(rows.status_code, 200, rows.text)
                holiday = next(row for row in rows.json() if row["id"] == rule_id)
                self.assertEqual(holiday["resource_class_codes"], ["PROGRAMMEUR"])

                unknown = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Jour férié",
                        "start_date": "2026-12-26",
                        "resource_class_codes": ["INCONNUE"],
                    },
                )
                self.assertEqual(unknown.status_code, 422, unknown.text)
                self.assertEqual(
                    unknown.json()["error"]["code"],
                    "availability_resource_class_unknown",
                )

                ambiguous = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Jour férié",
                        "resource_id": resource_id,
                        "start_date": "2026-12-26",
                        "resource_class_codes": ["PROGRAMMEUR"],
                    },
                )
                self.assertEqual(ambiguous.status_code, 422, ambiguous.text)
                self.assertEqual(
                    ambiguous.json()["error"]["code"],
                    "availability_resource_class_scope_ambiguous",
                )

                non_holiday = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Vacances",
                        "resource_id": resource_id,
                        "start_date": "2026-12-26",
                        "resource_class_codes": ["PROGRAMMEUR"],
                    },
                )
                self.assertEqual(non_holiday.status_code, 422, non_holiday.text)
                self.assertEqual(
                    non_holiday.json()["error"]["code"],
                    "availability_resource_classes_holiday_only",
                )

                deactivate_class = client.patch(
                    "/api/v1/admin/resource-classes/PROGRAMMEUR",
                    json={"expected_version": 1, "active": False},
                )
                self.assertEqual(deactivate_class.status_code, 200, deactivate_class.text)

                patch_without_scope = client.patch(
                    f"/api/v1/availability-rules/{rule_id}",
                    json={"note": "Conserver la classe inactive référencée"},
                )
                self.assertEqual(patch_without_scope.status_code, 200, patch_without_scope.text)
                preserved = client.get(
                    "/api/v1/availability-rules",
                    params={"active_only": "false"},
                )
                preserved_holiday = next(
                    row for row in preserved.json() if row["id"] == rule_id
                )
                self.assertEqual(
                    preserved_holiday["resource_class_codes"],
                    ["PROGRAMMEUR"],
                )

                inactive_new = client.post(
                    "/api/v1/availability-rules",
                    json={
                        "availability_type": "Jour férié",
                        "start_date": "2026-12-27",
                        "resource_class_codes": ["PROGRAMMEUR"],
                    },
                )
                self.assertEqual(inactive_new.status_code, 422, inactive_new.text)
                self.assertEqual(
                    inactive_new.json()["error"]["code"],
                    "availability_resource_class_inactive",
                )

                globalized = client.patch(
                    f"/api/v1/availability-rules/{rule_id}",
                    json={"resource_class_codes": []},
                )
                self.assertEqual(globalized.status_code, 200, globalized.text)
                after = client.get(
                    "/api/v1/availability-rules",
                    params={"active_only": "false"},
                )
                global_holiday = next(row for row in after.json() if row["id"] == rule_id)
                self.assertEqual(global_holiday["resource_class_codes"], [])

    def test_manual_planning_reorder_is_persisted_per_app_user_with_global_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            second_user_id = "00000000-0000-0000-0000-000000000656"
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add_all(
                    [
                        AppUser(
                            id=TEST_ADMIN_USER_ID,
                            issuer="urn:resourceplanner:test",
                            subject="explicit-test-admin",
                            display_name="Administrateur de test explicite",
                            email=None,
                            roles_json='["ADMIN"]',
                            active=True,
                        ),
                        AppUser(
                            id=second_user_id,
                            issuer="urn:resourceplanner:test",
                            subject="issue-656-second-user",
                            display_name="Deuxième utilisateur 656",
                            email=None,
                            roles_json='["ADMIN"]',
                            active=True,
                        ),
                    ]
                )
            engine.dispose()

            app = create_api_app(database_url)
            with TestClient(app) as client:
                def create_resource(name: str, resource_class: str, sort_order: int) -> str:
                    response = client.post(
                        "/api/v1/resources",
                        json={
                            "name": name,
                            "resource_class": resource_class,
                            "sort_order": sort_order,
                        },
                    )
                    self.assertEqual(response.status_code, 201, response.text)
                    return response.json()["resource_id"]

                alice_id = create_resource("Alice", "PROGRAMMEUR", 10)
                bob_id = create_resource("Bob", "PROGRAMMEUR", 20)
                electrician_id = create_resource("Émile", "ÉLECTRICIEN", 7)

                fallback = client.get("/api/v1/planning/resource-order")
                self.assertEqual(fallback.status_code, 200, fallback.text)
                self.assertEqual(fallback.json()["positions"][alice_id], 10)
                self.assertEqual(fallback.json()["positions"][bob_id], 20)

                headers = {"Idempotency-Key": "issue-656-admin-bob-up"}
                moved = client.post(
                    f"/api/v1/planning/resources/{bob_id}/reorder",
                    json={"direction": "up"},
                    headers=headers,
                )
                self.assertEqual(moved.status_code, 200, moved.text)
                self.assertEqual(moved.json()["action"], "reordered")
                replay = client.post(
                    f"/api/v1/planning/resources/{bob_id}/reorder",
                    json={"direction": "up"},
                    headers=headers,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json(), moved.json())

                personalized = client.get("/api/v1/planning/resource-order").json()["positions"]
                self.assertEqual(personalized[bob_id], 10)
                self.assertEqual(personalized[alice_id], 20)

                charlie_id = create_resource("Charlie", "PROGRAMMEUR", 15)
                with_new_resource = client.get("/api/v1/planning/resource-order").json()["positions"]
                self.assertEqual(with_new_resource[bob_id], 10)
                self.assertEqual(with_new_resource[charlie_id], 15)
                self.assertEqual(with_new_resource[alice_id], 20)

                resources = client.get("/api/v1/resources?active_only=false")
                self.assertEqual(resources.status_code, 200, resources.text)
                global_order = {row["id"]: row["sort_order"] for row in resources.json()}
                self.assertEqual(global_order[alice_id], 10)
                self.assertEqual(global_order[bob_id], 20)
                self.assertEqual(global_order[charlie_id], 15)
                self.assertEqual(global_order[electrician_id], 7)

                deactivated = client.post(f"/api/v1/resources/{bob_id}/deactivate")
                self.assertEqual(deactivated.status_code, 200, deactivated.text)
                inactive_positions = client.get("/api/v1/planning/resource-order").json()["positions"]
                self.assertNotIn(bob_id, inactive_positions)
                reactivated = client.patch(
                    f"/api/v1/resources/{bob_id}",
                    json={"active": True},
                )
                self.assertEqual(reactivated.status_code, 200, reactivated.text)
                restored_positions = client.get("/api/v1/planning/resource-order").json()["positions"]
                self.assertEqual(restored_positions[bob_id], 10)

                invalid = client.post(
                    f"/api/v1/planning/resources/{alice_id}/reorder",
                    json={"direction": "sideways"},
                    headers={"Idempotency-Key": "issue-656-invalid"},
                )
                self.assertEqual(invalid.status_code, 422, invalid.text)

            second_resolver = static_auth_resolver(
                AuthPrincipal.from_roles(
                    local_user_id=second_user_id,
                    issuer="urn:resourceplanner:test",
                    subject="issue-656-second-user",
                    display_name="Deuxième utilisateur 656",
                    email=None,
                    roles=(ROLE_ADMIN,),
                    auth_mode="test",
                )
            )
            second_app = build_api_app(database_url, auth_resolver=second_resolver)
            with TestClient(second_app) as second_client:
                second_positions = second_client.get(
                    "/api/v1/planning/resource-order"
                ).json()["positions"]
                self.assertEqual(second_positions[alice_id], 10)
                self.assertEqual(second_positions[charlie_id], 15)
                self.assertEqual(second_positions[bob_id], 20)

            reloaded_app = create_api_app(database_url)
            with TestClient(reloaded_app) as reloaded_client:
                persisted = reloaded_client.get(
                    "/api/v1/planning/resource-order"
                ).json()["positions"]
                self.assertEqual(persisted[bob_id], 10)
                self.assertEqual(persisted[charlie_id], 15)
                self.assertEqual(persisted[alice_id], 20)



if __name__ == "__main__":
    unittest.main()
