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
    DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
    DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
    DIAGNOSTIC_RESOURCE_CLASS_MISSING,
    DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND,
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
                    active=True,
                )
                for index, (task_id, _, _) in enumerate(task_specs)
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

    def test_explicit_task_override_has_priority_and_preserves_historical_mapping(self) -> None:
        with self.factory() as session, session.begin():
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T216",
                    approval_scope_id="S-ELEC",
                )
            )
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T-NO-CLASS",
                    approval_scope_id="S-AUTO",
                )
            )

        overridden = self._resolve("L-T216")
        historical = self._resolve("L-T-NO-CLASS")
        self.assertEqual(
            overridden.resolution.approval_scope_id,
            "S-ELEC",
        )
        self.assertEqual(
            [row.user_id for row in overridden.resolution.eligible_approvers],
            ["U-ELEC"],
        )
        self.assertFalse(historical.resolution.blocked)
        self.assertEqual(
            historical.resolution.approval_scope_id,
            "S-AUTO",
        )

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
