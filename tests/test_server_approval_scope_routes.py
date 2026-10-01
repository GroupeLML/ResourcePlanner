from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    AuthPrincipal,
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
)
from app.infrastructure.sql import (
    AppUser,
    Asset,
    AssetType,
    Base,
    BusinessContact,
    Project,
    RequestLine,
    ResourceClassConfig,
    TaskCatalogEntry,
    WorkforceRequest,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


def principal(
    role: str,
    user_id: str = "admin-1",
) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name=f"User {user_id}",
        email=None,
        roles=(role,),
        auth_mode="test",
    )


class ServerApprovalScopeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temp.name) / "approval-scopes.db"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, role: str = ROLE_ADMIN):
        app = create_api_app(
            "sqlite+pysqlite:///"
            + self.database_path.as_posix(),
            auth_resolver=static_auth_resolver(principal(role)),
        )
        factory = app.state.session_factory
        Base.metadata.create_all(factory.kw["bind"])
        with factory() as session, session.begin():
            session.add(
                Project(
                    id="project-1",
                    number="P-1",
                    name="Projet",
                )
            )
            session.add_all(
                [
                    AppUser(
                        id="admin-1",
                        issuer="urn:test",
                        subject="subject-admin-1",
                        display_name="Admin",
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    ),
                    AppUser(
                        id="manager-1",
                        issuer="urn:test",
                        subject="subject-manager-1",
                        display_name="Manager",
                        roles_json=json.dumps([ROLE_MANAGER]),
                        active=True,
                    ),
                    AppUser(
                        id="manager-2",
                        issuer="urn:test",
                        subject="subject-manager-2",
                        display_name="Inactive",
                        roles_json=json.dumps([ROLE_MANAGER]),
                        active=False,
                    ),
                    AppUser(
                        id="pm-1",
                        issuer="urn:test",
                        subject="subject-pm-1",
                        display_name="PM",
                        roles_json=json.dumps(
                            [ROLE_PROJECT_MANAGER]
                        ),
                        active=True,
                    ),
                ]
            )
            session.add_all(
                [
                    TaskCatalogEntry(
                        id="task-110",
                        project_number="P-1",
                        task_code="110",
                        label="Installation électrique",
                        active=True,
                    ),
                    TaskCatalogEntry(
                        id="task-210",
                        project_number="P-1",
                        task_code="210",
                        label="Automatisation",
                        active=True,
                    ),
                ]
            )
            session.add(
                WorkforceRequest(
                    id="request-1",
                    legacy_demand_number="DMO-1",
                    project_id="project-1",
                    line_mode=True,
                )
            )
            # Materialize all referenced parents before the RequestLine fixture.
            # The production repositories write these aggregates in separate
            # operations; this keeps the isolated SQLite fixture equally explicit.
            session.flush()
            session.add(
                RequestLine(
                    id="line-1",
                    workforce_request_id="request-1",
                    position=0,
                    task_catalog_item_id="task-110",
                    active=True,
                )
            )
        return app

    @staticmethod
    def _create_scope(
        client: TestClient,
        code: str,
        label: str,
    ) -> dict[str, object]:
        response = client.post(
            "/api/v1/admin/approval-scopes",
            json={"code": code, "label": label},
        )
        assert response.status_code == 201
        return response.json()

    def test_many_to_many_and_explicit_task_mapping_resolve(
        self,
    ) -> None:
        app = self._app()
        with TestClient(app) as client:
            electrical = self._create_scope(
                client,
                "ELECTRICAL_INSTALLATION",
                "Installation électrique",
            )
            automation = self._create_scope(
                client,
                "AUTOMATION",
                "Automatisation",
            )

            electrical = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{electrical['id']}/approvers/manager-1",
                json={
                    "expected_version":
                    electrical["version"]
                },
            ).json()
            electrical = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{electrical['id']}/approvers/admin-1",
                json={
                    "expected_version":
                    electrical["version"]
                },
            ).json()
            automation = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{automation['id']}/approvers/manager-1",
                json={
                    "expected_version":
                    automation["version"]
                },
            ).json()
            electrical = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{electrical['id']}/tasks/task-110",
                json={
                    "expected_version":
                    electrical["version"]
                },
            ).json()
            resolution = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            )

        self.assertEqual(
            set(electrical["approver_user_ids"]),
            {"admin-1", "manager-1"},
        )
        self.assertEqual(
            automation["approver_user_ids"],
            ["manager-1"],
        )
        payload = resolution.json()
        self.assertEqual(
            payload["approval_scope_id"],
            electrical["id"],
        )
        self.assertEqual(
            payload["suggested_scope_code"],
            "ELECTRICAL_INSTALLATION",
        )
        self.assertEqual(
            {
                item["app_user_id"]
                for item in payload["eligible_approvers"]
            },
            {"admin-1", "manager-1"},
        )
        self.assertFalse(payload["blocked"])

    def test_admin_adds_and_removes_covered_resource_class_with_scope_cas(
        self,
    ) -> None:
        app = self._app()
        factory = app.state.session_factory
        with factory() as session, session.begin():
            session.add(
                ResourceClassConfig(
                    code="PROGRAMMEUR",
                    label="Programmeur",
                    average_hourly_cost_cad=100,
                    active=True,
                    version=1,
                )
            )

        with TestClient(app) as client:
            scope = self._create_scope(
                client,
                "AUTOMATION",
                "Automatisation",
            )
            assigned = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/resource-classes/PROGRAMMEUR",
                json={"expected_version": scope["version"]},
            )
            self.assertEqual(assigned.status_code, 200)
            assigned_payload = assigned.json()
            self.assertEqual(
                assigned_payload["resource_class_codes"],
                ["PROGRAMMEUR"],
            )
            self.assertEqual(
                assigned_payload["version"],
                scope["version"] + 1,
            )

            stale = client.request(
                "DELETE",
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/resource-classes/PROGRAMMEUR",
                json={"expected_version": scope["version"]},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                stale.json()["error"]["code"],
                "approval_scope_version_conflict",
            )

            removed = client.request(
                "DELETE",
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/resource-classes/PROGRAMMEUR",
                json={"expected_version": assigned_payload["version"]},
            )
            self.assertEqual(removed.status_code, 200)
            self.assertEqual(
                removed.json()["resource_class_codes"],
                [],
            )

    def test_inadmissible_user_cannot_be_assigned(
        self,
    ) -> None:
        app = self._app()
        with TestClient(app) as client:
            scope = self._create_scope(
                client,
                "AUTOMATION",
                "Automatisation",
            )
            inactive = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/approvers/manager-2",
                json={"expected_version": scope["version"]},
            )
            permissionless = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/approvers/pm-1",
                json={"expected_version": scope["version"]},
            )

        self.assertEqual(inactive.status_code, 422)
        self.assertEqual(
            inactive.json()["error"]["code"],
            "approval_scope_user_not_admissible",
        )
        self.assertEqual(permissionless.status_code, 422)

    def test_ambiguous_and_inactive_scope_are_diagnostics(
        self,
    ) -> None:
        app = self._app()
        with TestClient(app) as client:
            one = self._create_scope(client, "ONE", "One")
            two = self._create_scope(client, "TWO", "Two")
            one = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{one['id']}/tasks/task-110",
                json={"expected_version": one["version"]},
            ).json()
            two = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{two['id']}/tasks/task-110",
                json={"expected_version": two["version"]},
            ).json()
            ambiguous = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            ).json()
            one = client.request(
                "DELETE",
                f"/api/v1/admin/approval-scopes/"
                f"{one['id']}/tasks/task-110",
                json={"expected_version": one["version"]},
            ).json()
            two = client.patch(
                f"/api/v1/admin/approval-scopes/{two['id']}",
                json={
                    "expected_version": two["version"],
                    "active": False,
                },
            ).json()
            inactive = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            ).json()

        self.assertIn(
            "approval_scope_ambiguous",
            ambiguous["diagnostics"],
        )
        self.assertTrue(ambiguous["blocked"])
        self.assertIn(
            "approval_scope_inactive",
            inactive["diagnostics"],
        )
        self.assertTrue(inactive["blocked"])

    def test_configured_user_is_revalidated_at_resolution(
        self,
    ) -> None:
        app = self._app()
        with TestClient(app) as client:
            scope = self._create_scope(
                client,
                "AUTOMATION",
                "Automatisation",
            )
            scope = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/approvers/manager-1",
                json={"expected_version": scope["version"]},
            ).json()
            scope = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/tasks/task-110",
                json={"expected_version": scope["version"]},
            ).json()

            factory = app.state.session_factory
            with factory() as session, session.begin():
                user = session.get(AppUser, "manager-1")
                user.active = False
            inactive = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            ).json()

            with factory() as session, session.begin():
                user = session.get(AppUser, "manager-1")
                user.active = True
                user.roles_json = json.dumps(
                    [ROLE_PROJECT_MANAGER]
                )
            permissionless = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            ).json()

        self.assertIn(
            "approver_inactive:manager-1",
            inactive["diagnostics"],
        )
        self.assertIn(
            "approver_permission_missing:manager-1",
            permissionless["diagnostics"],
        )

    def test_business_contact_and_label_have_no_authority(
        self,
    ) -> None:
        app = self._app()
        factory = app.state.session_factory
        with factory() as session, session.begin():
            session.add(
                BusinessContact(
                    id="contact-1",
                    display_name="Coordonnateur métier",
                    active=True,
                    source="LOCAL",
                    version=1,
                )
            )
            task = session.get(TaskCatalogEntry, "task-110")
            task.coordinator_contact_id = "contact-1"
            task.label = "Libellé libre différent"

        with TestClient(app) as client:
            scope = self._create_scope(
                client,
                "ELECTRICAL_INSTALLATION",
                "Installation électrique",
            )
            scope = client.put(
                f"/api/v1/admin/approval-scopes/"
                f"{scope['id']}/tasks/task-110",
                json={"expected_version": scope["version"]},
            ).json()
            resolution = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/line-1/resolution"
            ).json()

        self.assertEqual(
            resolution["approval_scope_id"],
            scope["id"],
        )
        self.assertEqual(
            resolution["eligible_approvers"],
            [],
        )
        self.assertIn(
            "no_eligible_approver",
            resolution["diagnostics"],
        )

    def test_asset_type_scope_and_specific_asset_approver_are_additive(
        self,
    ) -> None:
        app = self._app()
        factory = app.state.session_factory
        with factory() as session, session.begin():
            session.add(
                AssetType(
                    id="asset-type-1",
                    code="PROGRAMMING_LAPTOP",
                    label="Portable de programmation",
                    category="EQUIPMENT",
                    active=True,
                )
            )
            session.flush()
            session.add(
                Asset(
                    id="asset-1",
                    code="LAPTOP-01",
                    label="Portable 01",
                    asset_type_id="asset-type-1",
                    active=True,
                )
            )
            session.add(
                RequestLine(
                    id="asset-line-1",
                    workforce_request_id="request-1",
                    position=1,
                    kind="ASSET",
                    asset_type_id="asset-type-1",
                    proposed_asset_id="asset-1",
                    active=True,
                )
            )

        with TestClient(app) as client:
            scope = self._create_scope(client, "ASSET_AUTOMATION", "Actifs automation")
            scope = client.put(
                f"/api/v1/admin/approval-scopes/{scope['id']}/approvers/manager-1",
                json={"expected_version": scope["version"]},
            ).json()
            mapped = client.put(
                f"/api/v1/admin/approval-scopes/{scope['id']}/asset-types/asset-type-1",
                json={"expected_version": scope["version"]},
            )
            self.assertEqual(mapped.status_code, 200, mapped.text)
            self.assertEqual(
                mapped.json()["asset_type_ids"],
                ["asset-type-1"],
            )
            assigned = client.put(
                "/api/v1/assets/asset-1/approvers/admin-1",
                json={},
            )
            self.assertEqual(assigned.status_code, 200, assigned.text)

            resolution = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/asset-line-1/resolution"
            )
            self.assertEqual(resolution.status_code, 200, resolution.text)
            payload = resolution.json()
            self.assertFalse(payload["blocked"])
            self.assertEqual(payload["line_kind"], "ASSET")
            self.assertEqual(payload["asset_type_id"], "asset-type-1")
            self.assertEqual(payload["proposed_asset_id"], "asset-1")
            self.assertEqual(
                {
                    item["app_user_id"]
                    for item in payload["eligible_approvers"]
                },
                {"admin-1", "manager-1"},
            )
            specific = next(
                item
                for item in payload["eligible_approvers"]
                if item["app_user_id"] == "admin-1"
            )
            self.assertIn("PROPOSED_ASSET", specific["sources"])

            with factory() as session, session.begin():
                session.get(AppUser, "admin-1").active = False
            revalidated = client.get(
                "/api/v1/admin/approval-scopes/"
                "request-lines/asset-line-1/resolution"
            ).json()

        self.assertFalse(revalidated["blocked"])
        self.assertEqual(
            [
                item["app_user_id"]
                for item in revalidated["eligible_approvers"]
            ],
            ["manager-1"],
        )
        self.assertIn(
            "approver_inactive:admin-1",
            revalidated["diagnostics"],
        )

    def test_admin_settings_permission_protects_surface(
        self,
    ) -> None:
        app = self._app(role=ROLE_MANAGER)
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/admin/approval-scopes"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["context"][
                "required_permission"
            ],
            "admin_settings",
        )


if __name__ == "__main__":
    unittest.main()
