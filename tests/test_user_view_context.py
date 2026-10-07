from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.query_models import ResourceReadModel
from app.application.security import (
    ROLE_ADMIN,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.application.technician_schedule import TechnicianScheduleService
from app.application.user_view_context import (
    LINK_STATUS_LINKED,
    LINK_STATUS_RESOURCE_NOT_FOUND,
    LINK_STATUS_UNLINKED,
    SCOPE_GLOBAL,
    SCOPE_MINE,
    UserViewContextService,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    Resource,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.user_view_context_repository import (
    SqlUserViewContextRepository,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


DAY = date(2026, 9, 21)


class UserViewContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        database = Path(self.temp.name) / "context.db"
        self.database_url = f"sqlite+pysqlite:///{database.as_posix()}"
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

        with self.factory.begin() as session:
            session.add_all(
                [
                    Project(
                        id="P-MANAGED",
                        number="P-100",
                        name="Projet géré",
                        project_manager_external_id="EMP-MULTI",
                        status="Actif",
                    ),
                    Project(
                        id="P-ASSIGNED",
                        number="P-200",
                        name="Projet affecté",
                        status="Actif",
                    ),
                    Project(
                        id="P-SHIFTED",
                        number="P-300",
                        name="Projet shift",
                        status="Actif",
                    ),
                    Project(
                        id="P-PM-ONLY",
                        number="P-400",
                        name="Projet CP sans ressource",
                        project_manager_external_id="EMP-PM-ONLY",
                        status="Actif",
                    ),
                ]
            )
            session.add_all(
                [
                    Resource(
                        id="R-MULTI",
                        external_id="EMP-MULTI",
                        name="Ressource multi-rôle",
                        active=True,
                        sort_order=10,
                    ),
                    Resource(
                        id="R-OTHER",
                        external_id="EMP-OTHER",
                        name="Autre ressource",
                        active=True,
                        sort_order=20,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ-ASSIGNED",
                        project_id="P-ASSIGNED",
                        assigned_resource_id="R-MULTI",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        origin="AD_HOC",
                    ),
                    ResourceRequirement(
                        id="REQ-SHIFTED",
                        project_id="P-SHIFTED",
                        assigned_resource_id="R-OTHER",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        origin="AD_HOC",
                    ),
                ]
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT-MULTI",
                    resource_requirement_id="REQ-SHIFTED",
                    resource_id="R-MULTI",
                    work_date=DAY,
                    hours=Decimal("4"),
                    source="MANUAL",
                    locked=True,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def principal(
        *,
        employee_external_id: str | None,
        roles: tuple[str, ...],
    ) -> AuthPrincipal:
        return AuthPrincipal.from_roles(
            local_user_id="U-1",
            issuer="urn:test",
            subject="user",
            display_name="Utilisateur test",
            email=None,
            employee_external_id=employee_external_id,
            roles=roles,
            auth_mode="local",
        )

    def test_multi_role_unions_managed_and_participating_projects(self) -> None:
        with self.factory() as session:
            context = UserViewContextService(
                SqlUserViewContextRepository(session)
            ).read(
                self.principal(
                    employee_external_id="EMP-MULTI",
                    roles=(ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN),
                )
            )

        self.assertEqual(context.resource.link_status, LINK_STATUS_LINKED)
        self.assertEqual(context.resource.id, "R-MULTI")
        self.assertEqual(context.relations.managed_project_count, 1)
        self.assertEqual(context.relations.participating_project_count, 2)
        self.assertEqual(context.relations.personal_project_count, 3)
        self.assertEqual(context.view_policy.default_scope, SCOPE_MINE)
        self.assertEqual(context.view_policy.available_scopes, (SCOPE_MINE, SCOPE_GLOBAL))

    def test_project_manager_without_resource_keeps_managed_scope(self) -> None:
        with self.factory() as session:
            service = UserViewContextService(SqlUserViewContextRepository(session))
            relations = service.resolve_relations(
                self.principal(
                    employee_external_id="EMP-PM-ONLY",
                    roles=(ROLE_PROJECT_MANAGER,),
                )
            )
            context = service.read(
                self.principal(
                    employee_external_id="EMP-PM-ONLY",
                    roles=(ROLE_PROJECT_MANAGER,),
                )
            )

        self.assertEqual(relations.resource_link_status, LINK_STATUS_RESOURCE_NOT_FOUND)
        self.assertEqual(relations.managed_project_ids, ("P-PM-ONLY",))
        self.assertEqual(relations.participating_project_ids, ())
        self.assertEqual(context.relations.personal_project_count, 1)
        self.assertIn("resource_not_found", context.diagnostics)
        self.assertEqual(context.view_policy.default_scope, SCOPE_MINE)

    def test_co_manager_without_employee_id_has_managed_scope_without_new_permissions(self) -> None:
        with self.factory.begin() as session:
            session.add(
                BusinessContact(
                    id="C-CO",
                    display_name="Co chargé local",
                    active=True,
                    source="LOCAL",
                )
            )
            session.add(
                AppUser(
                    id="U-CO",
                    issuer=None,
                    subject=None,
                    display_name="Co chargé local",
                    email=None,
                    roles_json='["TECHNICIAN"]',
                    active=True,
                    employee_external_id=None,
                    business_contact_id="C-CO",
                )
            )
            session.add(
                Project(
                    id="P-CO",
                    number="P-500",
                    name="Projet co-géré",
                    status="Actif",
                )
            )
            session.flush()
            session.add(
                ProjectCoManager(
                    project_id="P-CO",
                    business_contact_id="C-CO",
                    created_by_user_id="U-CO",
                )
            )

        principal = AuthPrincipal.from_roles(
            local_user_id="U-CO",
            issuer="urn:test",
            subject="co",
            display_name="Co chargé local",
            email=None,
            employee_external_id=None,
            roles=(ROLE_TECHNICIAN,),
            auth_mode="local",
        )
        with self.factory() as session:
            relations = UserViewContextService(
                SqlUserViewContextRepository(session)
            ).resolve_relations(principal)

        self.assertEqual(relations.managed_project_ids, ("P-CO",))
        self.assertEqual(relations.participating_project_ids, ())
        self.assertEqual(relations.personal_project_ids, ("P-CO",))
        self.assertNotIn("manage_demands", principal.permissions)

    def test_unlinked_transverse_identity_defaults_global(self) -> None:
        with self.factory() as session:
            context = UserViewContextService(
                SqlUserViewContextRepository(session)
            ).read(
                self.principal(
                    employee_external_id=None,
                    roles=(ROLE_ADMIN,),
                )
            )

        self.assertEqual(context.resource.link_status, LINK_STATUS_UNLINKED)
        self.assertIsNone(context.resource.id)
        self.assertEqual(context.relations.personal_project_count, 0)
        self.assertEqual(context.view_policy.default_scope, SCOPE_GLOBAL)
        self.assertIn("employee_external_id_missing", context.diagnostics)

    def test_me_context_endpoint_uses_authenticated_principal(self) -> None:
        principal = self.principal(
            employee_external_id="EMP-MULTI",
            roles=(ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN),
        )
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal),
        )

        with TestClient(app) as client:
            response = client.get("/api/v1/me/context")
            planning_policy = client.get("/api/v1/me/planning-policy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(planning_policy.status_code, 200)
        planning_payload = planning_policy.json()
        self.assertEqual(planning_payload["default_scope"], SCOPE_MINE)
        self.assertEqual(planning_payload["available_scopes"], [SCOPE_MINE])
        payload = response.json()
        self.assertEqual(payload["resource"]["id"], "R-MULTI")
        self.assertEqual(payload["resource"]["link_status"], LINK_STATUS_LINKED)
        self.assertEqual(payload["relations"]["managed_project_count"], 1)
        self.assertEqual(payload["relations"]["participating_project_count"], 2)
        self.assertEqual(payload["relations"]["personal_project_count"], 3)
        self.assertEqual(payload["view_policy"]["default_scope"], SCOPE_MINE)
        self.assertEqual(payload["view_policy"]["available_scopes"], [SCOPE_MINE, SCOPE_GLOBAL])


class TechnicianScheduleResolverTests(unittest.TestCase):
    def test_schedule_can_use_direct_resource_resolution_without_listing_resources(self) -> None:
        resource = ResourceReadModel(
            id="R-DIRECT",
            name="Ressource directe",
            active=True,
            external_id="EMP-DIRECT",
        )

        class ContextRepository:
            def get_resource_by_external_id(self, employee_external_id: str):
                self_external = employee_external_id
                return resource if self_external == "EMP-DIRECT" else None

            def list_managed_project_ids(
                self,
                local_user_id: str | None,
                employee_external_id: str | None,
            ):
                return ()

            def list_participating_project_ids(self, resource_id: str):
                return ()

        class Queries:
            def list_resources(self, *, active_only: bool = True):
                raise AssertionError("list_resources ne doit pas être appelé")

            def list_shifts(self, **kwargs):
                self.kwargs = kwargs
                return ()

        queries = Queries()
        principal = AuthPrincipal.from_roles(
            local_user_id="U-DIRECT",
            issuer="urn:test",
            subject="direct",
            display_name="Technicien direct",
            email=None,
            employee_external_id="EMP-DIRECT",
            roles=(ROLE_TECHNICIAN,),
            auth_mode="local",
        )
        result = TechnicianScheduleService(
            queries,
            resource_resolver=ContextRepository(),
        ).read(
            principal=principal,
            start=DAY,
            end=DAY,
        )

        self.assertEqual(result.link_status, LINK_STATUS_LINKED)
        self.assertEqual(result.resource, resource)
        self.assertEqual(queries.kwargs["resource_id"], "R-DIRECT")


if __name__ == "__main__":
    unittest.main()
