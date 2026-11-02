from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
)
from app.infrastructure.sql import (
    Base,
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_SUCCEEDED,
    ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
    ErpSyncControl,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.erp_sync_control_repository import (
    SqlErpSyncControlRepository,
)
from app.server.erp_sync_orchestration import ErpSyncOrchestrator


class ProjectSource:
    def __init__(self, rows) -> None:
        self.rows = tuple(rows)
        self.calls = 0

    def list_projects(self):
        self.calls += 1
        return self.rows


class FailingProjectSource:
    def list_projects(self):
        raise RuntimeError("controlled project source failure")


class MutableAutomaticSafety:
    def __init__(self) -> None:
        self.available = True

    def automatic_execution_available(self) -> bool:
        return self.available

    def diagnostic(self) -> str:
        return "test_automatic_safety_unavailable"


class OrderedProjectSource(ProjectSource):
    def __init__(self, rows, order: list[str]) -> None:
        super().__init__(rows)
        self.order = order

    def list_projects(self):
        self.order.append("projects")
        return super().list_projects()


class PortfolioTaskSource:
    def __init__(
        self,
        snapshots: dict[str, TaskCatalogProjectSnapshot],
        *,
        order: list[str] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.batch_calls = 0
        self.order = order

    def fetch_project_snapshots(
        self,
        *,
        project_targets: tuple[tuple[str, str], ...],
    ) -> TaskCatalogPortfolioSnapshot:
        self.batch_calls += 1
        if self.order is not None:
            self.order.append("tasks")
        return TaskCatalogPortfolioSnapshot(
            source_rows=sum(
                self.snapshots[number].source_rows
                for _external_id, number in project_targets
            ),
            source_pages=1,
            snapshots=tuple(
                self.snapshots[number]
                for _external_id, number in project_targets
            ),
        )


def project(number: str) -> ExternalProjectRecord:
    return ExternalProjectRecord(
        external_id=f"ERP-{number}",
        number=number,
        name=f"Projet {number}",
        status="Actif",
    )


def task(number: str, code: str) -> TaskCatalogItem:
    return TaskCatalogItem(
        project_number=number,
        code=code,
        label=f"Tâche {number}-{code}",
        active=True,
    )


def snapshot(number: str, items: tuple[TaskCatalogItem, ...]) -> TaskCatalogProjectSnapshot:
    return TaskCatalogProjectSnapshot(
        project_number=number,
        source_rows=len(items),
        rejected_rows=0,
        items=items,
    )


class ErpSyncOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        database = Path(self.tmp.name) / "orchestration.db"
        self.engine = create_sql_engine(
            f"sqlite+pysqlite:///{database.as_posix()}"
        )
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(ErpSyncControl(id="ERP_PROJECTS_TASKS", generation=0))
        self.settings = ErpSyncSchedulerSettings(enabled=False)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def _attempt(
        self,
        *,
        intent: str = "PROJECTS_AND_ACTIVE_TASKS",
        key: str = "orchestration",
    ):
        control = SqlErpSyncControlRepository(self.factory)
        occurrence = control.accept_manual(
            intent=intent,
            actor_name="test-operator",
            idempotency_key=key,
            settings=self.settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="test-operator",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        return control, attempt

    def test_scheduler_exposes_safe_configuration_incompatibility_diagnostic(self) -> None:
        authority_settings = ErpSyncSchedulerSettings(
            enabled=False,
            source_identity="acumatica:authority",
        )
        control = SqlErpSyncControlRepository(self.factory)
        occurrence = control.accept_manual(
            intent="PROJECTS_ONLY",
            actor_name="owner",
            idempotency_key="compat-owner",
            settings=authority_settings,
        )
        owner = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="owner",
            settings=authority_settings,
        )
        self.assertIsNotNone(owner)

        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=None,
            settings=ErpSyncSchedulerSettings(
                enabled=True,
                source_identity="acumatica:incompatible",
            ),
            automatic_safety=MutableAutomaticSafety(),
        )
        result = orchestrator.scheduler_tick()

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            result["diagnostic"],
            "erp_sync_configuration_incompatible",
        )
        self.assertNotIn("acumatica:authority", str(result))
        self.assertNotIn("acumatica:incompatible", str(result))
        orchestrator.shutdown()

    def test_automatic_capability_is_rechecked_before_business_execution(self) -> None:
        settings = ErpSyncSchedulerSettings(enabled=True)
        capability = MutableAutomaticSafety()
        source = ProjectSource((project("A"),))
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=PortfolioTaskSource(
                {"A": snapshot("A", (task("A", "1"),))}
            ),
            settings=settings,
            automatic_safety=capability,
        )
        control = SqlErpSyncControlRepository(self.factory)
        fixed_now = datetime(
            2026, 1, 15, 5, 37,
            tzinfo=timezone.utc,
        )
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=fixed_now,
        ):
            occurrence = control.record_daily_decision(settings, capability)
            self.assertIsNotNone(occurrence)
            attempt = control.claim(
                occurrence_id=occurrence.id,
                trigger="CATCHUP",
                actor_name="erp-scheduler",
                settings=settings,
                automatic=True,
                automatic_safety=capability,
            )
        self.assertIsNotNone(attempt)

        capability.available = False
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=fixed_now,
        ):
            orchestrator._execute_attempt(
                attempt.id,
                attempt.generation,
                automatic=True,
            )

        self.assertEqual(source.calls, 0)
        self.assertEqual(
            control.get_attempt(attempt.id).status,
            ERP_ATTEMPT_FAILED,
        )
        self.assertEqual(
            control.get_attempt(attempt.id).error_code,
            "erp_sync_automatic_safety_unavailable",
        )
        orchestrator.shutdown()

    def test_global_workflow_executes_projects_before_tasks(self) -> None:
        order: list[str] = []
        source = OrderedProjectSource((project("A"),), order)
        tasks = PortfolioTaskSource(
            {"A": snapshot("A", (task("A", "1"),))},
            order=order,
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=tasks,
            settings=self.settings,
        )
        control, attempt = self._attempt(key="projects-before-tasks")

        orchestrator._execute_attempt(attempt.id, attempt.generation)

        self.assertEqual(order, ["projects", "tasks"])
        self.assertEqual(
            control.get_attempt(attempt.id).status,
            ERP_ATTEMPT_SUCCEEDED,
        )
        orchestrator.shutdown()

    def test_project_phase_failure_prevents_task_phase(self) -> None:
        tasks = PortfolioTaskSource({})
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=FailingProjectSource(),
            project_task_source=tasks,
            settings=self.settings,
        )
        control, attempt = self._attempt(key="project-failure")

        orchestrator._execute_attempt(attempt.id, attempt.generation)

        self.assertEqual(tasks.batch_calls, 0)
        self.assertEqual(
            control.get_attempt(attempt.id).status,
            ERP_ATTEMPT_FAILED,
        )
        orchestrator.shutdown()

    def test_project_business_mutation_and_parent_progress_roll_back_together(self) -> None:
        source = ProjectSource((project("A"),))
        tasks = PortfolioTaskSource({"A": snapshot("A", (task("A", "1"),))})
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=tasks,
            settings=self.settings,
        )
        control, attempt = self._attempt(key="project-atomic")

        with patch(
            "app.server.erp_sync_orchestration.SqlErpSyncControlRepository.update_phase_in_session",
            side_effect=RuntimeError("crash after project mutation"),
        ):
            orchestrator._execute_attempt(attempt.id, attempt.generation)

        with self.factory() as session:
            projects = tuple(session.scalars(select(
                __import__(
                    "app.infrastructure.sql.models",
                    fromlist=["Project"],
                ).Project
            )))
        self.assertEqual(projects, ())
        self.assertEqual(
            control.get_attempt(attempt.id).status,
            ERP_ATTEMPT_FAILED,
        )
        orchestrator.shutdown()

    def test_task_projects_preserve_a_and_c_roll_back_b_and_parent_reports_errors(self) -> None:
        source = ProjectSource((project("A"), project("B"), project("C")))
        tasks = PortfolioTaskSource(
            {
                "A": snapshot("A", (task("A", "1"),)),
                "B": snapshot("B", (task("B", "1"), task("B", "1"))),
                "C": snapshot("C", (task("C", "1"),)),
            }
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=tasks,
            settings=self.settings,
        )
        control, attempt = self._attempt(key="abc-isolation")

        orchestrator._execute_attempt(attempt.id, attempt.generation)

        completed = control.get_attempt(attempt.id)
        self.assertEqual(completed.status, ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS)
        with self.factory() as session:
            rows = tuple(
                session.scalars(
                    select(TaskCatalogEntry).order_by(
                        TaskCatalogEntry.project_number,
                        TaskCatalogEntry.task_code,
                    )
                )
            )
        self.assertEqual(
            [(row.project_number, row.task_code) for row in rows],
            [("A", "1"), ("C", "1")],
        )
        occurrence = control.get_occurrence(attempt.occurrence_id)
        self.assertIsNone(occurrence.success_attempt_id)
        with self.factory() as session:
            control_row = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertIsNone(control_row.last_success_attempt_id)

        tasks.snapshots["B"] = snapshot("B", (task("B", "1"),))
        with patch.object(orchestrator, "_submit") as submit:
            retry_payload = orchestrator.retry(
                occurrence_id=attempt.occurrence_id,
                actor_name="test-operator",
                idempotency_key="abc-retry",
            )
        retry_attempt = control.get_attempt(str(retry_payload["run_id"]))
        self.assertNotEqual(retry_attempt.id, attempt.id)
        submit.assert_called_once_with(
            retry_attempt.id,
            retry_attempt.generation,
        )

        orchestrator._execute_attempt(
            retry_attempt.id,
            retry_attempt.generation,
        )
        retried = control.get_attempt(retry_attempt.id)
        self.assertEqual(retried.status, ERP_ATTEMPT_SUCCEEDED)
        self.assertEqual(
            control.get_occurrence(attempt.occurrence_id).success_attempt_id,
            retry_attempt.id,
        )
        with self.factory() as session:
            control_row = session.get(ErpSyncControl, "ERP_PROJECTS_TASKS")
            self.assertEqual(
                control_row.last_success_attempt_id,
                retry_attempt.id,
            )
        orchestrator.shutdown()

    def test_task_business_mutation_and_project_result_are_atomic(self) -> None:
        source = ProjectSource((project("A"),))
        tasks = PortfolioTaskSource({"A": snapshot("A", (task("A", "1"),))})
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=tasks,
            settings=self.settings,
        )
        control, attempt = self._attempt(key="task-atomic")

        original = ErpSyncOrchestrator._record_task_outcome_in_session

        def fail_after_business(session, run_id, outcome):
            if outcome.get("status") == "synchronized":
                raise RuntimeError("crash before project result")
            return original(session, run_id, outcome)

        with patch.object(
            ErpSyncOrchestrator,
            "_record_task_outcome_in_session",
            side_effect=fail_after_business,
        ):
            orchestrator._execute_attempt(attempt.id, attempt.generation)

        with self.factory() as session:
            rows = tuple(session.scalars(select(TaskCatalogEntry)))
        self.assertEqual(rows, ())
        self.assertEqual(
            control.get_attempt(attempt.id).status,
            ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
        )
        orchestrator.shutdown()


if __name__ == "__main__":
    unittest.main()
