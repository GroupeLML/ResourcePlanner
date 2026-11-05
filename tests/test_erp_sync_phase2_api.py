from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import sleep
import unittest

from fastapi.testclient import TestClient

from app.application import (
    ExternalProjectRecord,
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
from app.application.security import AuthPrincipal, PERMISSION_SYNC_PROJECTS
from app.infrastructure.sql import Base, create_session_factory, create_sql_engine
from app.server import create_api_app
from app.server.security import static_auth_resolver


class ToggleProjectSource:
    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    def list_projects(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled phase2 source failure")
        return (
            ExternalProjectRecord(
                external_id="ERP-PHASE2",
                number="PHASE2",
                name="Projet Phase 2",
                status="Actif",
            ),
        )


class BlockingProjectSource(ToggleProjectSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def list_projects(self):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("phase2 blocking source timeout")
        return super().list_projects()


class TaskSource:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_project_snapshots(self, *, project_targets):
        self.calls += 1
        snapshots = tuple(
            TaskCatalogProjectSnapshot(
                project_number=project_number,
                source_rows=1,
                rejected_rows=0,
                items=(
                    TaskCatalogItem(
                        project_number=project_number,
                        code="100",
                        label="Phase 2",
                        active=True,
                    ),
                ),
            )
            for _external_id, project_number in project_targets
        )
        return TaskCatalogPortfolioSnapshot(
            source_rows=len(snapshots),
            source_pages=1,
            snapshots=snapshots,
        )


SYNC_PRINCIPAL = AuthPrincipal(
    local_user_id="PHASE2",
    issuer="urn:test",
    subject="phase2",
    display_name="Opérateur Phase 2",
    email=None,
    employee_external_id=None,
    roles=(),
    permissions=(PERMISSION_SYNC_PROJECTS,),
    auth_mode="test",
)


class ErpSyncPhase2ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "phase2-api.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        self.projects = ToggleProjectSource()
        self.tasks = TaskSource()
        self.app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(SYNC_PRINCIPAL),
            project_source=self.projects,
            project_task_source=self.tasks,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _wait_terminal(client: TestClient, run_id: str) -> dict[str, object]:
        payload: dict[str, object] = {}
        for _ in range(100):
            response = client.get(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{run_id}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["status"] not in {"PENDING", "RUNNING"}:
                return payload
            sleep(0.02)
        raise AssertionError(f"ERP run {run_id} did not reach a terminal state")

    def test_run_now_duplicate_and_terminal_replay_share_authoritative_attempt(self) -> None:
        with TestClient(self.app) as client:
            first = client.post(
                "/api/v1/integrations/acumatica/erp-sync/run-now",
                headers={"Idempotency-Key": "phase2-run-now"},
            )
            second = client.post(
                "/api/v1/integrations/acumatica/erp-sync/run-now",
                headers={"Idempotency-Key": "phase2-run-now"},
            )
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(second.status_code, 202, second.text)
            self.assertEqual(first.json()["run_id"], second.json()["run_id"])

            terminal = self._wait_terminal(client, first.json()["run_id"])
            replay = client.post(
                "/api/v1/integrations/acumatica/erp-sync/run-now",
                headers={"Idempotency-Key": "phase2-run-now"},
            )

        self.assertEqual(terminal["status"], "COMPLETED")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["run_id"], first.json()["run_id"])
        self.assertEqual(self.projects.calls, 1)
        self.assertEqual(self.tasks.calls, 1)

    def test_run_now_conflict_does_not_create_duplicate_attempt(self) -> None:
        self.app.state.erp_sync_orchestrator.shutdown()
        blocking = BlockingProjectSource()
        self.projects = blocking
        self.app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(SYNC_PRINCIPAL),
            project_source=blocking,
            project_task_source=self.tasks,
        )
        try:
            with TestClient(self.app) as client:
                first = client.post(
                    "/api/v1/integrations/acumatica/erp-sync/run-now",
                    headers={"Idempotency-Key": "phase2-active-owner"},
                )
                self.assertEqual(first.status_code, 202, first.text)
                self.assertTrue(blocking.started.wait(timeout=2))

                conflict = client.post(
                    "/api/v1/integrations/acumatica/erp-sync/run-now",
                    headers={"Idempotency-Key": "phase2-conflicting-owner"},
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn(
                    conflict.json()["error"]["code"],
                    {"erp_sync_run_active", "erp_sync_command_not_claimed"},
                )

                blocking.release.set()
                self._wait_terminal(client, first.json()["run_id"])
        finally:
            blocking.release.set()

    def test_retry_creates_new_attempt_and_retry_command_is_idempotent(self) -> None:
        self.projects.fail = True
        with TestClient(self.app) as client:
            accepted = client.post(
                "/api/v1/integrations/acumatica/erp-sync/run-now",
                headers={"Idempotency-Key": "phase2-failed-run"},
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            failed = self._wait_terminal(client, accepted.json()["run_id"])
            self.assertEqual(failed["status"], "FAILED")
            occurrence_id = str(failed["occurrence_id"])

            self.projects.fail = False
            retry = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/occurrences/{occurrence_id}/retry",
                headers={"Idempotency-Key": "phase2-retry"},
            )
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertNotEqual(retry.json()["run_id"], accepted.json()["run_id"])
            completed = self._wait_terminal(client, retry.json()["run_id"])

            replay = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/occurrences/{occurrence_id}/retry",
                headers={"Idempotency-Key": "phase2-retry"},
            )

        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["run_id"], retry.json()["run_id"])


if __name__ == "__main__":
    unittest.main()
