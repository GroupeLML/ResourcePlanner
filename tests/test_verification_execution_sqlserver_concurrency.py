from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import select

from app.application import ApplicationConflictError
from app.application.security import AuthPrincipal, ROLE_TECHNICIAN
from app.application.verification_execution_service import VerificationExecutionService
from app.domain.delivery import (
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryItemType,
    DeliveryPlan,
    DeliveryPlanStatus,
)
from app.domain.verification import (
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationScope,
)
from app.domain.verification_execution import VerificationExecutorAssignment
from app.infrastructure.sql import (
    AppUser,
    Project,
    SqlDeliveryRepository,
    SqlVerificationExecutionRepository,
    SqlVerificationRepository,
    VerificationScopeRow,
    VerificationTestExecutionRow,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


def principal(user_id: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name=user_id,
        email=None,
        roles=(ROLE_TECHNICIAN,),
        auth_mode="test",
    )


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 363D réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class VerificationExecutionSqlServerConcurrencyTests(unittest.TestCase):
    def test_same_verification_version_allows_one_execution_only(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P363D-{marker}"
        work_package_id = f"WP363D-{marker}"
        lead_id = f"L363D-{marker}"
        tech_id = f"T363D-{marker}"
        plan_id = f"DP363D-{marker}"
        story_id = f"ST363D-{marker}"
        scope_id = f"VS363D-{marker}"
        requirement_id = f"RQ363D-{marker}"
        revision_id = f"RV363D-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(Project(id=project_id, number=project_id, name="363D SQL"))
                session.add(
                    WorkPackage(
                        id=work_package_id,
                        project_id=project_id,
                        name="WP 363D SQL",
                    )
                )
                session.add_all(
                    [
                        AppUser(
                            id=lead_id,
                            issuer="urn:test",
                            subject=f"lead-{marker}",
                            display_name="Lead 363D",
                            roles_json=json.dumps([]),
                            active=True,
                        ),
                        AppUser(
                            id=tech_id,
                            issuer="urn:test",
                            subject=f"tech-{marker}",
                            display_name="Tech 363D",
                            roles_json=json.dumps([ROLE_TECHNICIAN]),
                            active=True,
                        ),
                    ]
                )

            with factory.begin() as session:
                delivery = SqlDeliveryRepository(session)
                delivery.add_plan(
                    DeliveryPlan(
                        id=plan_id,
                        work_package_id=work_package_id,
                        status=DeliveryPlanStatus.ACTIVE,
                        lead_user_id=lead_id,
                    )
                )
                delivery.add_item(
                    DeliveryItem(
                        id=story_id,
                        delivery_plan_id=plan_id,
                        item_type=DeliveryItemType.STORY,
                        title="Story 363D",
                        status=DeliveryItemStatus.IN_PROGRESS,
                    )
                )
                verification = SqlVerificationRepository(session)
                verification.add_scope(
                    VerificationScope(
                        id=scope_id,
                        work_package_id=work_package_id,
                        lead_user_id=lead_id,
                    )
                )
                verification.add_requirement(
                    VerificationRequirement(
                        id=requirement_id,
                        verification_scope_id=scope_id,
                        story_id=story_id,
                        phase=VerificationPhase.FAT,
                        current_revision_id=revision_id,
                    ),
                    VerificationRequirementRevision(
                        id=revision_id,
                        requirement_id=requirement_id,
                        revision_number=1,
                        objective="Concurrence",
                        method="Deux soumissions",
                        expected_result="Une seule gagne",
                    ),
                )
                SqlVerificationExecutionRepository(session).assign_executor(
                    VerificationExecutorAssignment(
                        id=f"AS363D-{marker}",
                        requirement_id=requirement_id,
                        executor_user_id=tech_id,
                        assigned_by_user_id=lead_id,
                    )
                )

            def mutate(key: str, result: str) -> tuple[str, str | None]:
                with factory.begin() as session:
                    service = VerificationExecutionService(
                        SqlVerificationExecutionRepository(session)
                    )
                    try:
                        payload = service.record_execution(
                            requirement_id,
                            result=result,
                            executed_at=None,
                            measurements={},
                            comments=key,
                            expected_verification_version=1,
                            idempotency_key=key,
                            principal=principal(tech_id),
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(payload["execution_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(mutate, f"363d-a-{marker}", "PASS")
                second = executor.submit(mutate, f"363d-b-{marker}", "FAIL")
                outcomes = (first.result(timeout=30), second.result(timeout=30))

            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["conflict", "success"],
            )
            conflict = next(row for row in outcomes if row[0] == "conflict")
            self.assertEqual(conflict[1], "verification_version_conflict")

            with factory() as session:
                scope = session.get(VerificationScopeRow, scope_id)
                self.assertIsNotNone(scope)
                assert scope is not None
                self.assertEqual(scope.verification_version, 2)
                executions = tuple(
                    session.scalars(
                        select(VerificationTestExecutionRow).where(
                            VerificationTestExecutionRow.requirement_id
                            == requirement_id
                        )
                    ).all()
                )
                self.assertEqual(len(executions), 1)
                self.assertEqual(executions[0].sequence, 1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
