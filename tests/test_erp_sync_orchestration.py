from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import sleep
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.application.errors import ApplicationConflictError
from app.application.erp_sync_scheduler import (
    ErpSyncSchedulerSettings,
    TestErpSyncAutomaticSafetyCapability,
)
from app.infrastructure.sql import (
    AcumaticaProjectTaskSyncRun,
    Base,
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_SUCCEEDED,
    ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
    ERP_SYNC_CONTROL_ROW_ID,
    GLOBAL_SYNC_RUN_KEY,
    SYNC_RUN_RUNNING,
    ErpSyncControl,
    ErpSyncAttempt,
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


class BlockingProjectSource(ProjectSource):
    def __init__(self, rows) -> None:
        super().__init__(rows)
        self.started = Event()
        self.release = Event()

    def list_projects(self):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release timeout")
        return super().list_projects()


class FailingProjectSource:
    def list_projects(self):
        raise RuntimeError("controlled project source failure")


class ToggleProjectSource(ProjectSource):
    def __init__(self, rows, *, fail: bool = False) -> None:
        super().__init__(rows)
        self.fail = fail

    def list_projects(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled project source failure")
        return self.rows


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

    def test_terminal_run_now_success_replay_observes_same_attempt(self) -> None:
        projects = ToggleProjectSource((project("A"),))
        tasks = PortfolioTaskSource({"A": snapshot("A", (task("A", "1"),))})
        backend_a = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            system_actor="backend-A",
        )
        backend_b = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            system_actor="backend-B",
        )
        control = SqlErpSyncControlRepository(self.factory)
        accepted_by_b = Event()
        release_b = Event()
        completed_a = Event()
        result_b: list[dict[str, object]] = []
        errors_b: list[BaseException] = []
        original_accept_b = backend_b._control.accept_manual
        original_execute_a = backend_a._execute_attempt

        def delayed_accept_b(*args, **kwargs):
            occurrence = original_accept_b(*args, **kwargs)
            accepted_by_b.set()
            if not release_b.wait(timeout=5):
                raise RuntimeError("stale claimant release timeout")
            return occurrence

        def observed_execute_a(*args, **kwargs):
            try:
                return original_execute_a(*args, **kwargs)
            finally:
                completed_a.set()

        def invoke_b() -> None:
            try:
                result_b.append(
                    backend_b.run_now(
                        intent="PROJECTS_AND_ACTIVE_TASKS",
                        actor_name="operator",
                        idempotency_key="terminal-success",
                    )
                )
            except BaseException as exc:
                errors_b.append(exc)

        try:
            with patch.object(
                backend_b._control,
                "accept_manual",
                side_effect=delayed_accept_b,
            ), patch.object(
                backend_a,
                "_execute_attempt",
                side_effect=observed_execute_a,
            ), patch.object(
                backend_a,
                "_submit",
                wraps=backend_a._submit,
            ) as submit_a, patch.object(
                backend_b,
                "_submit",
                wraps=backend_b._submit,
            ) as submit_b:
                delayed = Thread(target=invoke_b, name="stale-run-now-success")
                delayed.start()
                self.assertTrue(accepted_by_b.wait(timeout=2))

                result_a = backend_a.run_now(
                    intent="PROJECTS_AND_ACTIVE_TASKS",
                    actor_name="operator",
                    idempotency_key="terminal-success",
                )
                self.assertTrue(completed_a.wait(timeout=5))
                attempt_a = control.get_attempt(str(result_a["run_id"]))
                self.assertEqual(attempt_a.status, ERP_ATTEMPT_SUCCEEDED)

                release_b.set()
                delayed.join(timeout=5)
                self.assertFalse(delayed.is_alive())

                self.assertEqual(errors_b, [])
                self.assertEqual(len(result_b), 1)
                self.assertEqual(result_b[0]["run_id"], result_a["run_id"])
                self.assertEqual(submit_a.call_count, 1)
                submit_b.assert_not_called()

            with self.factory() as session:
                attempts = tuple(session.scalars(select(ErpSyncAttempt)))
            self.assertEqual(len(attempts), 1)
            self.assertEqual(projects.calls, 1)
            self.assertEqual(tasks.batch_calls, 1)
        finally:
            release_b.set()
            backend_a.shutdown()
            backend_b.shutdown()

    def test_terminal_run_now_failure_replay_does_not_retry_implicitly(self) -> None:
        projects = ToggleProjectSource((project("A"),), fail=True)
        tasks = PortfolioTaskSource({"A": snapshot("A", (task("A", "1"),))})
        backend_a = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            system_actor="backend-A",
        )
        backend_b = ErpSyncOrchestrator(
            self.factory,
            project_source=projects,
            project_task_source=tasks,
            settings=self.settings,
            system_actor="backend-B",
        )
        control = SqlErpSyncControlRepository(self.factory)
        accepted_by_b = Event()
        release_b = Event()
        completed_a = Event()
        result_b: list[dict[str, object]] = []
        errors_b: list[BaseException] = []
        original_accept_b = backend_b._control.accept_manual
        original_execute_a = backend_a._execute_attempt

        def delayed_accept_b(*args, **kwargs):
            occurrence = original_accept_b(*args, **kwargs)
            accepted_by_b.set()
            if not release_b.wait(timeout=5):
                raise RuntimeError("stale claimant release timeout")
            return occurrence

        def observed_execute_a(*args, **kwargs):
            try:
                return original_execute_a(*args, **kwargs)
            finally:
                completed_a.set()

        def invoke_b() -> None:
            try:
                result_b.append(
                    backend_b.run_now(
                        intent="PROJECTS_AND_ACTIVE_TASKS",
                        actor_name="operator",
                        idempotency_key="terminal-failure",
                    )
                )
            except BaseException as exc:
                errors_b.append(exc)

        try:
            with patch.object(
                backend_b._control,
                "accept_manual",
                side_effect=delayed_accept_b,
            ), patch.object(
                backend_a,
                "_execute_attempt",
                side_effect=observed_execute_a,
            ), patch.object(
                backend_a,
                "_submit",
                wraps=backend_a._submit,
            ) as submit_a, patch.object(
                backend_b,
                "_submit",
                wraps=backend_b._submit,
            ) as submit_b:
                delayed = Thread(target=invoke_b, name="stale-run-now-failure")
                delayed.start()
                self.assertTrue(accepted_by_b.wait(timeout=2))

                result_a = backend_a.run_now(
                    intent="PROJECTS_AND_ACTIVE_TASKS",
                    actor_name="operator",
                    idempotency_key="terminal-failure",
                )
                self.assertTrue(completed_a.wait(timeout=5))
                failed = control.get_attempt(str(result_a["run_id"]))
                self.assertEqual(failed.status, ERP_ATTEMPT_FAILED)

                release_b.set()
                delayed.join(timeout=5)
                self.assertFalse(delayed.is_alive())

                self.assertEqual(errors_b, [])
                self.assertEqual(result_b[0]["run_id"], result_a["run_id"])
                self.assertEqual(submit_a.call_count, 1)
                submit_b.assert_not_called()

                with self.factory() as session:
                    attempts_before_retry = tuple(
                        session.scalars(select(ErpSyncAttempt))
                    )
                self.assertEqual(len(attempts_before_retry), 1)
                self.assertEqual(projects.calls, 1)
                self.assertEqual(tasks.batch_calls, 0)

                projects.fail = False
                retry_completed = Event()

                def observed_retry(*args, **kwargs):
                    try:
                        return original_execute_a(*args, **kwargs)
                    finally:
                        retry_completed.set()

                with patch.object(
                    backend_a,
                    "_execute_attempt",
                    side_effect=observed_retry,
                ):
                    retry = backend_a.retry(
                        occurrence_id=str(result_a["occurrence_id"]),
                        actor_name="operator",
                        idempotency_key="explicit-retry",
                    )
                    self.assertNotEqual(retry["run_id"], result_a["run_id"])
                    self.assertTrue(retry_completed.wait(timeout=5))

                retried = control.get_attempt(str(retry["run_id"]))
                self.assertEqual(retried.status, ERP_ATTEMPT_SUCCEEDED)
                self.assertEqual(retried.trigger_kind, "RETRY")

            with self.factory() as session:
                attempts = tuple(
                    session.scalars(
                        select(ErpSyncAttempt).order_by(ErpSyncAttempt.attempt_number)
                    )
                )
            self.assertEqual(len(attempts), 2)
            self.assertEqual(projects.calls, 2)
            self.assertEqual(tasks.batch_calls, 1)
        finally:
            release_b.set()
            backend_a.shutdown()
            backend_b.shutdown()

    def test_targeted_terminal_replay_observes_same_attempt(self) -> None:
        # Targeted async launch uses fetch_project_snapshot, so use a minimal
        # source object with deterministic counting for that contract.
        class TargetedSource:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_project_snapshot(
                self,
                *,
                project_external_id: str,
                project_number: str,
            ):
                self.calls += 1
                return snapshot(project_number, (task(project_number, "1"),))

        targeted = TargetedSource()
        with transactional_session(self.factory) as session:
            from app.infrastructure.sql import Project
            session.add(
                Project(
                    id="TARGETED-A",
                    erp_external_id="ERP-A",
                    number="A",
                    name="Projet A",
                    status="Actif",
                )
            )

        backend_a = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=targeted,
            settings=self.settings,
            system_actor="backend-A",
        )
        backend_b = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=targeted,
            settings=self.settings,
            system_actor="backend-B",
        )
        control = SqlErpSyncControlRepository(self.factory)
        accepted_by_b = Event()
        release_b = Event()
        completed_a = Event()
        result_b: list[dict[str, object]] = []
        original_accept_b = backend_b._control.accept_manual
        original_execute_a = backend_a._execute_attempt

        def delayed_accept_b(*args, **kwargs):
            occurrence = original_accept_b(*args, **kwargs)
            accepted_by_b.set()
            if not release_b.wait(timeout=5):
                raise RuntimeError("targeted stale claimant release timeout")
            return occurrence

        def observed_execute_a(*args, **kwargs):
            try:
                return original_execute_a(*args, **kwargs)
            finally:
                completed_a.set()

        def invoke_b() -> None:
            result_b.append(
                backend_b.launch_targeted_project_tasks(
                    project_id="TARGETED-A",
                    actor_name="operator",
                    idempotency_key="targeted-terminal",
                )
            )

        try:
            with patch.object(
                backend_b._control,
                "accept_manual",
                side_effect=delayed_accept_b,
            ), patch.object(
                backend_a,
                "_execute_attempt",
                side_effect=observed_execute_a,
            ), patch.object(
                backend_a,
                "_submit",
                wraps=backend_a._submit,
            ) as submit_a, patch.object(
                backend_b,
                "_submit",
                wraps=backend_b._submit,
            ) as submit_b:
                delayed = Thread(target=invoke_b, name="stale-targeted")
                delayed.start()
                self.assertTrue(accepted_by_b.wait(timeout=2))

                result_a = backend_a.launch_targeted_project_tasks(
                    project_id="TARGETED-A",
                    actor_name="operator",
                    idempotency_key="targeted-terminal",
                )
                self.assertTrue(completed_a.wait(timeout=5))
                self.assertEqual(
                    control.get_attempt(str(result_a["run_id"])).status,
                    ERP_ATTEMPT_SUCCEEDED,
                )

                release_b.set()
                delayed.join(timeout=5)
                self.assertFalse(delayed.is_alive())

                self.assertEqual(result_b[0]["run_id"], result_a["run_id"])
                self.assertEqual(submit_a.call_count, 1)
                submit_b.assert_not_called()

            with self.factory() as session:
                attempts = tuple(session.scalars(select(ErpSyncAttempt)))
            self.assertEqual(len(attempts), 1)
            self.assertEqual(targeted.calls, 1)
        finally:
            release_b.set()
            backend_a.shutdown()
            backend_b.shutdown()

    def test_disabled_scheduler_restart_adopts_later_boundary_for_manual_run(self) -> None:
        control = SqlErpSyncControlRepository(self.factory)
        old_settings = ErpSyncSchedulerSettings(
            enabled=False,
            source_identity="acumatica:old-boundary",
        )
        old_now = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=old_now,
        ):
            occurrence = control.accept_manual(
                intent="PROJECTS_ONLY",
                actor_name="operator",
                idempotency_key="old-boundary",
                settings=old_settings,
            )
            attempt = control.claim(
                occurrence_id=occurrence.id,
                trigger="MANUAL",
                actor_name="operator",
                settings=old_settings,
            )
            self.assertIsNotNone(attempt)
            self.assertTrue(
                control.start_attempt(
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    settings=old_settings,
                )
            )
            self.assertTrue(
                control.finish(
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    status=ERP_ATTEMPT_SUCCEEDED,
                    phase="PUBLICATION_COMMITTED",
                    settings=old_settings,
                )
            )

        new_settings = ErpSyncSchedulerSettings(
            enabled=False,
            source_identity="acumatica:new-boundary",
        )
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=ProjectSource((project("A"),)),
            project_task_source=None,
            settings=new_settings,
        )
        next_day = datetime(2026, 1, 16, 15, 0, tzinfo=timezone.utc)
        with patch(
            "app.infrastructure.sql.erp_sync_control_repository._sql_now",
            return_value=next_day,
        ):
            self.assertEqual(orchestrator.reconcile_startup(), 0)
            result = orchestrator.run_projects_only_compat(
                actor_name="operator",
            )

        self.assertEqual(result.received, 1)
        with self.factory() as session:
            authority = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            self.assertIsNotNone(authority)
            self.assertEqual(
                authority.config_fingerprint,
                new_settings.config_fingerprint,
            )
        orchestrator.shutdown()

    def test_startup_reconciliation_rejects_unsupported_protocol_without_mutation(self) -> None:
        control = SqlErpSyncControlRepository(self.factory)
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="owner",
            idempotency_key="unsupported-protocol",
            settings=self.settings,
        )
        owner = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="owner",
            settings=self.settings,
        )
        self.assertIsNotNone(owner)

        with transactional_session(self.factory) as session:
            attempt = session.get(ErpSyncAttempt, owner.id)
            self.assertIsNotNone(attempt)
            child = AcumaticaProjectTaskSyncRun(
                id="UNSUPPORTED-PROTOCOL-CHILD",
                active_key=GLOBAL_SYNC_RUN_KEY,
                status=SYNC_RUN_RUNNING,
                parent_attempt_id=owner.id,
                erp_generation=owner.generation,
                started_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
            session.add(child)
            attempt.task_sync_run_id = child.id
            authority = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            self.assertIsNotNone(authority)
            authority.protocol_version = 999
            authority.lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            original_generation = authority.generation

        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=None,
            project_task_source=None,
            settings=self.settings,
        )
        with self.assertRaises(ApplicationConflictError) as caught:
            orchestrator.reconcile_startup()
        self.assertEqual(caught.exception.code, "erp_sync_protocol_incompatible")

        with self.factory() as session:
            attempt = session.get(ErpSyncAttempt, owner.id)
            child = session.get(
                AcumaticaProjectTaskSyncRun,
                "UNSUPPORTED-PROTOCOL-CHILD",
            )
            authority = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            self.assertIsNotNone(attempt)
            self.assertIsNotNone(child)
            self.assertIsNotNone(authority)
            self.assertEqual(attempt.status, owner.status)
            self.assertEqual(child.status, SYNC_RUN_RUNNING)
            self.assertEqual(child.active_key, GLOBAL_SYNC_RUN_KEY)
            self.assertEqual(authority.owner_attempt_id, owner.id)
            self.assertEqual(authority.generation, original_generation)
            self.assertEqual(authority.protocol_version, 999)
        orchestrator.shutdown()

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

    def test_shutdown_drains_accepted_legacy_compatibility_work(self) -> None:
        source = BlockingProjectSource((project("A"),))
        orchestrator = ErpSyncOrchestrator(
            self.factory,
            project_source=source,
            project_task_source=None,
            settings=ErpSyncSchedulerSettings(
                enabled=False,
                lease_seconds=2,
                heartbeat_seconds=0.2,
                max_attempt_seconds=10,
            ),
        )
        results: list[object] = []
        errors: list[BaseException] = []

        def invoke_compatibility() -> None:
            try:
                results.append(
                    orchestrator.run_projects_only_compat(
                        actor_name="legacy-http",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        caller = Thread(target=invoke_compatibility, name="compatibility-http-test")
        caller.start()
        self.assertTrue(source.started.wait(timeout=2))

        shutdown = Thread(target=orchestrator.shutdown, name="compatibility-shutdown-test")
        shutdown.start()
        sleep(0.05)
        self.assertTrue(shutdown.is_alive())

        source.release.set()
        caller.join(timeout=5)
        shutdown.join(timeout=5)

        self.assertFalse(caller.is_alive())
        self.assertFalse(shutdown.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].received, 1)

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
