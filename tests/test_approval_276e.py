from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.approval_scopes import ApprovalScopeService
from app.application.security import ROLE_MANAGER
from app.domain.approval_routing import (
    DIAGNOSTIC_APPROVER_INACTIVE,
    DIAGNOSTIC_APPROVER_PERMISSION_MISSING,
    DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
    DIAGNOSTIC_PROPOSED_RESOURCE_INACTIVE,
    DIAGNOSTIC_PROPOSED_RESOURCE_UNKNOWN,
    DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
    DIAGNOSTIC_RESOURCE_CLASS_MISSING,
    DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND,
    DIAGNOSTIC_ROUTING_SOURCE_MISSING,
    ROUTING_SOURCE_PROPOSED_RESOURCE_CLASS,
    ROUTING_SOURCE_REQUIRED_RESOURCE_CLASS,
    DIAGNOSTIC_SCOPE_AMBIGUOUS,
    DIAGNOSTIC_SCOPE_INACTIVE,
    DIAGNOSTIC_SCOPE_UNMAPPED,
)
from app.infrastructure.sql import (
    AppUser,
    ApprovalScope,
    ApprovalScopeApprover,
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceClassApprovalScopeMapping,
    ResourceClassConfig,
    SqlApprovalScopeRepository,
    TaskApprovalScopeMapping,
    TaskCatalogEntry,
    WorkforceRequest,
)


class Approval276ERoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        path = Path(self._temp.name) / "approval-276e.db"
        self.engine = create_engine("sqlite+pysqlite:///" + path.as_posix())
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as session, session.begin():
            self._seed(session)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp.cleanup()

    @staticmethod
    def _seed(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet"))
        session.add_all(
            [
                AppUser(
                    id="U-AUTO",
                    issuer="urn:test",
                    subject="u-auto",
                    display_name="Automation approver",
                    roles_json=json.dumps([ROLE_MANAGER]),
                    active=True,
                ),
                AppUser(
                    id="U-ELEC",
                    issuer="urn:test",
                    subject="u-elec",
                    display_name="Electrical approver",
                    roles_json=json.dumps([ROLE_MANAGER]),
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                ResourceClassConfig(
                    code="PROGRAMMEUR",
                    label="Programmeur",
                    average_hourly_cost_cad=100,
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="INSTALLATEUR_AUTOMATISATION",
                    label="Installateur automatisation",
                    average_hourly_cost_cad=90,
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="INSTALLATEUR_ELECTRIQUE",
                    label="Installateur électrique",
                    average_hourly_cost_cad=80,
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="NO_SCOPE",
                    label="Sans périmètre",
                    average_hourly_cost_cad=70,
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="NO_APPROVER",
                    label="Sans approbateur",
                    average_hourly_cost_cad=70,
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="INACTIVE_CLASS",
                    label="Classe inactive",
                    average_hourly_cost_cad=70,
                    active=False,
                    version=1,
                ),
            ]
        )
        session.add_all(
            [
                Resource(
                    id="R-PROG",
                    name="Programmeur proposé",
                    resource_class="PROGRAMMEUR",
                    active=True,
                ),
                Resource(
                    id="R-ELEC",
                    name="Installateur proposé",
                    resource_class="INSTALLATEUR_ELECTRIQUE",
                    active=True,
                ),
                Resource(
                    id="R-NO-CLASS",
                    name="Ressource sans classe",
                    resource_class=None,
                    active=True,
                ),
                Resource(
                    id="R-INACTIVE",
                    name="Ressource inactive",
                    resource_class="PROGRAMMEUR",
                    active=False,
                ),
            ]
        )
        session.add_all(
            [
                ApprovalScope(
                    id="S-AUTO",
                    code="AUTOMATION",
                    label="Automatisation",
                    active=True,
                    version=1,
                ),
                ApprovalScope(
                    id="S-ELEC",
                    code="ELECTRICAL_INSTALLATION",
                    label="Installation électrique",
                    active=True,
                    version=1,
                ),
                ApprovalScope(
                    id="S-OTHER",
                    code="OTHER",
                    label="Autre",
                    active=True,
                    version=1,
                ),
                ApprovalScope(
                    id="S-NO-APPROVER",
                    code="NO_APPROVER",
                    label="Sans approbateur",
                    active=True,
                    version=1,
                ),
            ]
        )
        session.add_all(
            [
                ApprovalScopeApprover(
                    approval_scope_id="S-AUTO",
                    app_user_id="U-AUTO",
                ),
                ApprovalScopeApprover(
                    approval_scope_id="S-ELEC",
                    app_user_id="U-ELEC",
                ),
                ApprovalScopeApprover(
                    approval_scope_id="S-OTHER",
                    app_user_id="U-AUTO",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="PROGRAMMEUR",
                    approval_scope_id="S-AUTO",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="INSTALLATEUR_AUTOMATISATION",
                    approval_scope_id="S-AUTO",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="INSTALLATEUR_ELECTRIQUE",
                    approval_scope_id="S-ELEC",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="INACTIVE_CLASS",
                    approval_scope_id="S-AUTO",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="NO_APPROVER",
                    approval_scope_id="S-NO-APPROVER",
                ),
            ]
        )
        task_specs = (
            ("T216", "216", "PROGRAMMEUR"),
            ("T217", "217", "INSTALLATEUR_AUTOMATISATION"),
            ("T117", "117", "INSTALLATEUR_ELECTRIQUE"),
            ("T-NO-SCOPE", "998", "NO_SCOPE"),
            ("T-NO-APPROVER", "997", "NO_APPROVER"),
            ("T-INACTIVE-CLASS", "996", "INACTIVE_CLASS"),
            ("T-UNKNOWN-CLASS", "995", "UNKNOWN_CLASS"),
            ("T-NO-CLASS", "994", None),
        )
        session.add_all(
            [
                TaskCatalogEntry(
                    id=task_id,
                    project_number="P-1",
                    task_code=task_code,
                    label=task_code,
                    active=True,
                    resource_class_code=class_code,
                )
                for task_id, task_code, class_code in task_specs
            ]
        )
        session.add(
            WorkforceRequest(
                id="D1",
                project_id="P1",
                status="Soumise",
                line_mode=True,
            )
        )
        session.flush()
        session.add_all(
            [
                RequestLine(
                    id=f"L-{task_id}",
                    workforce_request_id="D1",
                    position=index,
                    task_catalog_item_id=task_id,
                    erp_task_code=task_code,
                    erp_task_label=task_code,
                    active=True,
                )
                for index, (task_id, task_code, _) in enumerate(task_specs)
            ]
        )

    def _resolve(self, line_id: str):
        with self.factory() as session:
            return ApprovalScopeService(
                SqlApprovalScopeRepository(session)
            ).resolve_request_line(line_id)

    def test_effective_resource_classes_route_216_217_and_117(self) -> None:
        cases = {
            "L-T216": ("S-AUTO", "U-AUTO"),
            "L-T217": ("S-AUTO", "U-AUTO"),
            "L-T117": ("S-ELEC", "U-ELEC"),
        }
        for line_id, (scope_id, approver_id) in cases.items():
            with self.subTest(line_id=line_id):
                resolved = self._resolve(line_id)
                self.assertFalse(resolved.resolution.blocked)
                self.assertEqual(
                    resolved.resolution.approval_scope_id,
                    scope_id,
                )
                self.assertEqual(
                    [row.user_id for row in resolved.resolution.eligible_approvers],
                    [approver_id],
                )

    def test_programmeur_routes_to_automation_without_proposed_resource(self) -> None:
        with self.factory() as session:
            line = session.get(RequestLine, "L-T216")
            self.assertEqual(line.task_catalog_item_id, "T216")
            self.assertIsNone(line.proposed_resource_id)

        resolved = self._resolve("L-T216")

        self.assertFalse(resolved.resolution.blocked)
        self.assertEqual(resolved.task_code, "216")
        self.assertEqual(resolved.effective_resource_class, "PROGRAMMEUR")
        self.assertEqual(
            [scope.code for scope in resolved.approval_scope_candidates],
            ["AUTOMATION"],
        )
        self.assertEqual(resolved.resolution.approval_scope_id, "S-AUTO")
        self.assertEqual(
            [row.user_id for row in resolved.resolution.eligible_approvers],
            ["U-AUTO"],
        )

    def test_no_routing_source_keeps_erp_snapshot_but_reports_real_cause(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.task_catalog_item_id = None
            line.required_resource_class = None
            line.proposed_resource_id = None

        resolved = self._resolve("L-T216")

        self.assertTrue(resolved.resolution.blocked)
        self.assertEqual(resolved.task_code, "216")
        self.assertEqual(resolved.task_label, "216")
        self.assertIsNone(resolved.task_catalog_item_id)
        self.assertIn(
            DIAGNOSTIC_ROUTING_SOURCE_MISSING,
            resolved.resolution.diagnostics,
        )
        self.assertNotIn(
            "task_reference_missing",
            resolved.resolution.diagnostics,
        )

    def test_explicit_class_routes_without_task(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.task_catalog_item_id = None
            line.required_resource_class = "PROGRAMMEUR"
            line.proposed_resource_id = None

        resolved = self._resolve("L-T216")

        self.assertFalse(resolved.resolution.blocked)
        self.assertEqual(resolved.effective_resource_class, "PROGRAMMEUR")
        self.assertEqual(
            resolved.routing_sources,
            (ROUTING_SOURCE_REQUIRED_RESOURCE_CLASS,),
        )
        self.assertEqual(resolved.resolution.approval_scope_id, "S-AUTO")
        self.assertEqual(
            [row.user_id for row in resolved.resolution.eligible_approvers],
            ["U-AUTO"],
        )

    def test_proposed_resource_routes_without_task_from_canonical_class(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.task_catalog_item_id = None
            line.required_resource_class = None
            line.proposed_resource_id = "R-PROG"

        resolved = self._resolve("L-T216")

        self.assertFalse(resolved.resolution.blocked)
        self.assertEqual(resolved.effective_resource_class, "PROGRAMMEUR")
        self.assertEqual(
            resolved.routing_sources,
            (ROUTING_SOURCE_PROPOSED_RESOURCE_CLASS,),
        )
        self.assertEqual(resolved.resolution.approval_scope_id, "S-AUTO")

    def test_explicit_class_is_authoritative_over_resource_and_task_class(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.required_resource_class = "INSTALLATEUR_ELECTRIQUE"
            line.proposed_resource_id = "R-PROG"

        resolved = self._resolve("L-T216")

        self.assertFalse(resolved.resolution.blocked)
        self.assertEqual(
            resolved.effective_resource_class,
            "INSTALLATEUR_ELECTRIQUE",
        )
        self.assertEqual(
            resolved.routing_sources,
            (ROUTING_SOURCE_REQUIRED_RESOURCE_CLASS,),
        )
        self.assertEqual(resolved.resolution.approval_scope_id, "S-ELEC")

    def test_explicit_class_without_mapping_fails_closed_without_task(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.task_catalog_item_id = None
            line.required_resource_class = "NO_SCOPE"
            line.proposed_resource_id = None

        resolved = self._resolve("L-T216")

        self.assertTrue(resolved.resolution.blocked)
        self.assertEqual(resolved.effective_resource_class, "NO_SCOPE")
        self.assertIn(
            DIAGNOSTIC_SCOPE_UNMAPPED,
            resolved.resolution.diagnostics,
        )

    def test_invalid_explicit_class_never_falls_back_to_task(self) -> None:
        with self.factory() as session, session.begin():
            line = session.get(RequestLine, "L-T216")
            line.required_resource_class = "INACTIVE_CLASS"

        resolved = self._resolve("L-T216")

        self.assertTrue(resolved.resolution.blocked)
        self.assertEqual(resolved.effective_resource_class, "INACTIVE_CLASS")
        self.assertIn(
            DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
            resolved.resolution.diagnostics,
        )

    def test_resource_routing_reports_missing_inactive_and_classless_resources(self) -> None:
        cases = {
            "R-MISSING": DIAGNOSTIC_PROPOSED_RESOURCE_UNKNOWN,
            "R-INACTIVE": DIAGNOSTIC_PROPOSED_RESOURCE_INACTIVE,
            "R-NO-CLASS": DIAGNOSTIC_RESOURCE_CLASS_MISSING,
        }
        for resource_id, diagnostic in cases.items():
            with self.subTest(resource_id=resource_id):
                with self.factory() as session, session.begin():
                    line = session.get(RequestLine, "L-T216")
                    line.task_catalog_item_id = None
                    line.required_resource_class = None
                    line.proposed_resource_id = resource_id

                resolved = self._resolve("L-T216")
                self.assertTrue(resolved.resolution.blocked)
                self.assertIn(diagnostic, resolved.resolution.diagnostics)

    def test_inactive_scope_approver_is_reported_before_no_eligible_approver(self) -> None:
        with self.factory() as session, session.begin():
            session.get(AppUser, "U-AUTO").active = False

        resolved = self._resolve("L-T216")

        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            f"{DIAGNOSTIC_APPROVER_INACTIVE}:U-AUTO",
            resolved.resolution.diagnostics,
        )
        self.assertIn(
            DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
            resolved.resolution.diagnostics,
        )

    def test_scope_approver_without_permission_is_reported(self) -> None:
        with self.factory() as session, session.begin():
            session.get(AppUser, "U-AUTO").roles_json = json.dumps([])

        resolved = self._resolve("L-T216")

        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            f"{DIAGNOSTIC_APPROVER_PERMISSION_MISSING}:U-AUTO",
            resolved.resolution.diagnostics,
        )
        self.assertIn(
            DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
            resolved.resolution.diagnostics,
        )

    def test_selected_class_overrides_task_scope_without_mutating_erp_class(self) -> None:
        with self.factory() as session, session.begin():
            session.add(TaskApprovalScopeMapping(
                task_catalog_item_id="T216", approval_scope_id="S-AUTO",
            ))
            session.add(TaskApprovalScopeMapping(
                task_catalog_item_id="T-NO-CLASS", approval_scope_id="S-AUTO",
            ))
            session.get(RequestLine, "L-T216").required_resource_class = "INSTALLATEUR_ELECTRIQUE"

        overridden = self._resolve("L-T216")
        self.assertFalse(overridden.resolution.blocked)
        self.assertEqual(overridden.effective_resource_class, "INSTALLATEUR_ELECTRIQUE")
        self.assertEqual(overridden.resolution.approval_scope_id, "S-ELEC")
        self.assertEqual(
            [row.user_id for row in overridden.resolution.eligible_approvers],
            ["U-ELEC"],
        )
        with self.factory() as session:
            self.assertEqual(session.get(TaskCatalogEntry, "T216").resource_class_code, "PROGRAMMEUR")
            self.assertEqual(session.get(RequestLine, "L-T216").task_catalog_item_id, "T216")

        historical = self._resolve("L-T-NO-CLASS")
        self.assertFalse(historical.resolution.blocked)
        self.assertEqual(historical.resolution.approval_scope_id, "S-AUTO")

    def test_proposed_resource_keeps_task_scope_without_explicit_class(self) -> None:
        with self.factory() as session, session.begin():
            session.add(TaskApprovalScopeMapping(
                task_catalog_item_id="T216", approval_scope_id="S-AUTO",
            ))
            line = session.get(RequestLine, "L-T216")
            line.required_resource_class = None
            line.proposed_resource_id = "R-ELEC"

        resolved = self._resolve("L-T216")
        self.assertFalse(resolved.resolution.blocked)
        self.assertEqual(resolved.resolution.approval_scope_id, "S-AUTO")

    def test_selected_class_missing_scope_blocks_even_with_task_scope(self) -> None:
        with self.factory() as session, session.begin():
            session.add(TaskApprovalScopeMapping(
                task_catalog_item_id="T216", approval_scope_id="S-AUTO",
            ))
            session.get(RequestLine, "L-T216").required_resource_class = "NO_SCOPE"

        resolution = self._resolve("L-T216")
        self.assertTrue(resolution.resolution.blocked)
        self.assertEqual(resolution.effective_resource_class, "NO_SCOPE")
        self.assertIn(DIAGNOSTIC_SCOPE_UNMAPPED, resolution.resolution.diagnostics)

    def test_missing_class_mapping_is_blocking(self) -> None:
        resolved = self._resolve("L-T-NO-SCOPE")
        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            DIAGNOSTIC_SCOPE_UNMAPPED,
            resolved.resolution.diagnostics,
        )

    def test_multiple_active_scopes_for_class_fail_closed(self) -> None:
        with self.factory() as session, session.begin():
            session.add(
                ResourceClassApprovalScopeMapping(
                    resource_class_code="PROGRAMMEUR",
                    approval_scope_id="S-OTHER",
                )
            )
        resolved = self._resolve("L-T216")
        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            DIAGNOSTIC_SCOPE_AMBIGUOUS,
            resolved.resolution.diagnostics,
        )

    def test_missing_unknown_and_inactive_effective_class_fail_closed(self) -> None:
        cases = {
            "L-T-NO-CLASS": DIAGNOSTIC_RESOURCE_CLASS_MISSING,
            "L-T-UNKNOWN-CLASS": DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND,
            "L-T-INACTIVE-CLASS": DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
        }
        for line_id, diagnostic in cases.items():
            with self.subTest(line_id=line_id):
                resolved = self._resolve(line_id)
                self.assertTrue(resolved.resolution.blocked)
                self.assertIn(
                    diagnostic,
                    resolved.resolution.diagnostics,
                )

    def test_inactive_scope_fails_closed_without_class_fallback(self) -> None:
        with self.factory() as session, session.begin():
            scope = session.get(ApprovalScope, "S-OTHER")
            scope.active = False
            session.add(
                ResourceClassApprovalScopeMapping(
                    resource_class_code="NO_SCOPE",
                    approval_scope_id="S-OTHER",
                )
            )
        resolved = self._resolve("L-T-NO-SCOPE")
        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            DIAGNOSTIC_SCOPE_INACTIVE,
            resolved.resolution.diagnostics,
        )

    def test_scope_without_active_admissible_approver_fails_closed(self) -> None:
        resolved = self._resolve("L-T-NO-APPROVER")
        self.assertTrue(resolved.resolution.blocked)
        self.assertIn(
            DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
            resolved.resolution.diagnostics,
        )


if __name__ == "__main__":
    unittest.main()
