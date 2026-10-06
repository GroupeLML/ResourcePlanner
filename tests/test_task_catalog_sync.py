from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.application import ApplicationValidationError
from app.application.task_catalog import (
    TaskCatalogItem,
    TaskCatalogProjectSnapshot,
    TaskCatalogSyncService,
)
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    Project,
    ProjectTaskClassOverride,
    RequestLine,
    ResourceClassConfig,

    Shift,
    TaskCatalogEntry,
    TaskClassStandard,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.task_catalog_repository import SqlTaskCatalogRepository
from app.infrastructure.sql.task_catalog_workforce_policy import (
    SqlTaskCatalogWorkforcePolicy,
)


class StubTaskSource:
    def __init__(self, rows: list[TaskCatalogItem]) -> None:
        self.rows = rows

    def list_tasks(self) -> tuple[TaskCatalogItem, ...]:
        return tuple(self.rows)


class TaskCatalogSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync(self, rows: list[TaskCatalogItem]):
        with self.factory.begin() as session:
            return TaskCatalogSyncService(
                StubTaskSource(rows),
                SqlTaskCatalogRepository(session),
            ).synchronize()

    def test_sync_is_idempotent_and_explicit_status_deactivates(self) -> None:
        item = TaskCatalogItem(
            project_number="P-1",
            code="210",
            label="Automatisation",
            status="Actif",
            active=True,
            time_entry_enabled=True,
        )
        first = self._sync([item])
        self.assertEqual(
            (first.created, first.updated, first.unchanged, first.deactivated),
            (1, 0, 0, 0),
        )

        second = self._sync([item])
        self.assertEqual(
            (second.created, second.updated, second.unchanged, second.deactivated),
            (0, 0, 1, 0),
        )

        changed = TaskCatalogItem(
            project_number="P-1",
            code="210",
            label="Automatisation mise à jour",
            status="Actif",
            active=True,
            time_entry_enabled=True,
        )
        third = self._sync([changed])
        self.assertEqual(
            (third.created, third.updated, third.unchanged, third.deactivated),
            (0, 1, 0, 0),
        )

        inactive = TaskCatalogItem(
            project_number="P-1",
            code="210",
            label="Automatisation mise à jour",
            status="Inactif",
            active=False,
            time_entry_enabled=True,
        )
        fourth = self._sync([inactive])
        self.assertEqual(
            (fourth.created, fourth.updated, fourth.unchanged, fourth.deactivated),
            (0, 0, 0, 1),
        )

        with self.factory() as session:
            repository = SqlTaskCatalogRepository(session)
            self.assertEqual(repository.search(project_number="P-1"), ())
            rows = repository.search(project_number="P-1", active_only=False)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].active)
            self.assertEqual(rows[0].label, "Automatisation mise à jour")

    def test_sync_preserves_locally_owned_task_contact_links(self) -> None:
        item = TaskCatalogItem(
            project_number="P-LOCAL",
            code="210",
            label="Tâche initiale",
            active=True,
        )
        self._sync([item])

        with self.factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="BC-RESP",
                        display_name="Responsable local",
                        phone="555-0500",
                    ),
                    BusinessContact(
                        id="BC-COORD",
                        display_name="Coordonnateur local",
                        phone="555-0501",
                    ),
                ]
            )
            session.flush()
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "P-LOCAL",
                    TaskCatalogEntry.task_code == "210",
                )
            )
            assert row is not None
            row.operational_responsible_contact_id = "BC-RESP"
            row.coordinator_contact_id = "BC-COORD"

        changed = TaskCatalogItem(
            project_number="P-LOCAL",
            code="210",
            label="Tâche ERP mise à jour",
            status="Actif",
            active=True,
        )
        self._sync([changed])

        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "P-LOCAL",
                    TaskCatalogEntry.task_code == "210",
                )
            )
            assert row is not None
            self.assertEqual(row.operational_responsible_contact_id, "BC-RESP")
            self.assertEqual(row.coordinator_contact_id, "BC-COORD")
            projected = SqlTaskCatalogRepository(session).search(
                project_number="P-LOCAL"
            )[0]
            self.assertEqual(projected.id, row.id)
            self.assertEqual(
                projected.operational_responsible_contact_id,
                "BC-RESP",
            )
            self.assertEqual(projected.coordinator_contact_id, "BC-COORD")

    def test_same_task_code_is_valid_for_different_projects(self) -> None:
        result = self._sync(
            [
                TaskCatalogItem("P-1", "210", "Service", active=True),
                TaskCatalogItem("P-2", "210", "Achats automatisation", active=True),
            ]
        )
        self.assertEqual(result.created, 2)

        with self.factory() as session:
            repository = SqlTaskCatalogRepository(session)
            p1 = repository.search(project_number="P-1", query="210")
            p2 = repository.search(project_number="P-2", query="achats")
        self.assertEqual(p1[0].label, "Service")
        self.assertEqual(p2[0].label, "Achats automatisation")


class StubProjectTaskSource:
    def __init__(self, snapshot: TaskCatalogProjectSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str]] = []

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSnapshot:
        self.calls.append((project_external_id, project_number))
        return self.snapshot


class TargetedTaskCatalogSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync_project(
        self,
        snapshot: TaskCatalogProjectSnapshot,
        project_number: str = "P-1",
        project_external_id: str = "1001",
    ):
        source = StubProjectTaskSource(snapshot)
        with self.factory.begin() as session:
            repository = SqlTaskCatalogRepository(session)
            result = TaskCatalogSyncService(
                source,
                repository,
                sync_metadata_repository=repository,
            ).synchronize_project(
                project_external_id=project_external_id,
                project_number=project_number,
            )
        return result, source

    def test_taskid_replay_is_idempotent_and_never_creates_duplicate_task(self) -> None:
        item = TaskCatalogItem(
            project_number="P-1",
            code="216",
            label="Programmation",
            erp_task_id="9001",
            account_group="DEPMO",
            budget_amount_cad=Decimal("1234.5678901234"),
            budget_actual_cad=Decimal("234.0000000000"),
        )
        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(item,),
        )

        first, first_source = self._sync_project(snapshot)
        second, _ = self._sync_project(snapshot)

        self.assertEqual(first_source.calls, [("1001", "P-1")])
        self.assertEqual((first.created, first.updated, first.unchanged), (1, 0, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 1))
        with self.factory() as session:
            rows = session.scalars(
                select(TaskCatalogEntry).where(TaskCatalogEntry.erp_task_id == "9001")
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].budget_amount_cad, Decimal("1234.5678901234"))
            self.assertEqual(rows[0].budget_actual_cad, Decimal("234.0000000000"))
            metadata = SqlTaskCatalogRepository(session).get_project_sync_metadata("P-1")
            assert metadata is not None
            self.assertEqual(metadata.source_rows, 1)
            self.assertEqual(metadata.task_count, 1)
            self.assertEqual(metadata.rejected_rows, 0)
            self.assertIsNotNone(metadata.duration_ms)

    def test_erp_replay_refreshes_task_budget_freshness_without_changing_idempotent_counts(self) -> None:
        item = TaskCatalogItem(
            project_number="P-1",
            code="216",
            label="Programmation",
            erp_task_id="9001",
            account_group="DEPMO",
            budget_amount_cad=Decimal("1000.00"),
            budget_actual_cad=Decimal("250.00"),
        )
        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(item,),
        )
        self._sync_project(snapshot)

        stale = datetime(2000, 1, 1, tzinfo=timezone.utc)
        with self.factory.begin() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(TaskCatalogEntry.erp_task_id == "9001")
            )
            assert row is not None
            row.erp_budget_last_success_at = stale

        replay, _ = self._sync_project(snapshot)
        self.assertEqual((replay.created, replay.updated, replay.unchanged), (0, 0, 1))
        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(TaskCatalogEntry.erp_task_id == "9001")
            )
            assert row is not None
            refreshed = row.erp_budget_last_success_at
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreater(refreshed.year, 2000)

        with self.factory.begin() as session:
            action = SqlTaskCatalogRepository(session).upsert(
                TaskCatalogItem(
                    project_number="P-1",
                    code="216",
                    label="Libellé import local",
                )
            )
            self.assertEqual(action, "updated")

        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(TaskCatalogEntry.erp_task_id == "9001")
            )
            assert row is not None
            self.assertEqual(row.erp_budget_last_success_at, refreshed)

    def test_odata_identity_is_adopted_by_legacy_row_without_breaking_historical_reference(self) -> None:
        with self.factory.begin() as session:
            session.add(Project(id="PROJECT-1", number="P-1", name="Projet 1", status="Actif"))
            session.add(
                TaskCatalogEntry(
                    id="LEGACY-TASK",
                    project_number="P-1",
                    task_code="216",
                    label="Programmation historique",
                    status="Actif",
                    active=True,
                )
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id="DEMAND-1",
                    project_id="PROJECT-1",
                    status="Approuvée",
                    aggregate_version=7,
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="LINE-1",
                    workforce_request_id="DEMAND-1",
                    position=0,
                    task_catalog_item_id="LEGACY-TASK",
                    erp_task_code="216",
                    erp_task_label="Programmation historique",
                    estimated_hours=Decimal("40.00"),
                    estimated_hours_source="MANUAL",
                )
            )

        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="P-1",
                    code="216",
                    label="Programmation ERP",
                    erp_task_id="9001",
                    account_group="DEPMO",
                    budget_amount_cad=Decimal("8000.0000000000"),
                    budget_actual_cad=Decimal("1000.0000000000"),
                ),
            ),
        )
        result, _ = self._sync_project(snapshot)

        self.assertEqual((result.created, result.updated), (0, 1))
        with self.factory() as session:
            task = session.get(TaskCatalogEntry, "LEGACY-TASK")
            assert task is not None
            self.assertEqual(task.erp_task_id, "9001")
            self.assertEqual(task.label, "Programmation ERP")
            self.assertEqual(task.budget_amount_cad, Decimal("8000.0000000000"))

            line = session.get(RequestLine, "LINE-1")
            assert line is not None
            self.assertEqual(line.task_catalog_item_id, "LEGACY-TASK")
            self.assertEqual(line.erp_task_code, "216")
            self.assertEqual(line.erp_task_label, "Programmation historique")
            self.assertEqual(line.estimated_hours, Decimal("40.00"))

            demand = session.get(WorkforceRequest, "DEMAND-1")
            assert demand is not None
            self.assertEqual(demand.status, "Approuvée")
            self.assertEqual(demand.aggregate_version, 7)
            self.assertEqual(session.query(Shift).count(), 0)

    def test_partial_project_snapshot_never_deactivates_absent_task(self) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    TaskCatalogEntry(
                        id="TASK-117",
                        project_number="P-1",
                        task_code="117",
                        label="Installation",
                        status="Actif",
                        active=True,
                        erp_task_id="11701",
                    ),
                    TaskCatalogEntry(
                        id="TASK-216",
                        project_number="P-1",
                        task_code="216",
                        label="Programmation",
                        status="Actif",
                        active=True,
                        erp_task_id="21601",
                    ),
                ]
            )

        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="P-1",
                    code="216",
                    label="Programmation",
                    erp_task_id="21601",
                    account_group="DEPMO",
                    budget_amount_cad=Decimal("1000.00"),
                ),
            ),
        )
        self._sync_project(snapshot)

        with self.factory() as session:
            absent = session.get(TaskCatalogEntry, "TASK-117")
            assert absent is not None
            self.assertTrue(absent.active)
            self.assertEqual(absent.status, "Actif")

    def test_project_snapshot_mismatch_fails_before_persistence(self) -> None:
        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-2",
            source_rows=1,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="P-2",
                    code="216",
                    label="Programmation",
                    erp_task_id="9002",
                ),
            ),
        )

        with self.assertRaises(ApplicationValidationError) as raised:
            self._sync_project(snapshot, project_number="P-1")

        self.assertEqual(raised.exception.code, "task_catalog_project_mismatch")
        with self.factory() as session:
            self.assertEqual(session.query(TaskCatalogEntry).count(), 0)

    def test_file_fallback_replay_does_not_clear_odata_identity_or_budgets(self) -> None:
        odata_item = TaskCatalogItem(
            project_number="P-1",
            code="216",
            label="Programmation",
            erp_task_id="9001",
            account_group="DEPMO",
            budget_amount_cad=Decimal("-50.0000000000"),
            budget_actual_cad=Decimal("0.0000000000"),
            budget_diagnostic="budget_amount_negative",
        )
        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(odata_item,),
        )
        self._sync_project(snapshot)

        with self.factory.begin() as session:
            result = TaskCatalogSyncService(
                StubTaskSource(
                    [
                        TaskCatalogItem(
                            project_number="P-1",
                            code="216",
                            label="Programmation fichier",
                        )
                    ]
                ),
                SqlTaskCatalogRepository(session),
            ).synchronize()
            self.assertEqual(result.updated, 1)

        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "P-1",
                    TaskCatalogEntry.task_code == "216",
                )
            )
            assert row is not None
            self.assertEqual(row.erp_task_id, "9001")
            self.assertEqual(row.budget_amount_cad, Decimal("-50.0000000000"))
            self.assertEqual(row.budget_actual_cad, Decimal("0E-10"))
            self.assertEqual(row.budget_diagnostic, "budget_amount_negative")


class TaskCatalogResourceClassIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed_project(self) -> None:
        with self.factory.begin() as session:
            session.add(
                Project(
                    id="PROJECT-1",
                    number="P-1",
                    name="Projet 1",
                    status="Actif",
                )
            )

    def _seed_programmer_standard(
        self,
        *,
        cost: Decimal | None = Decimal("125.0000"),
    ) -> None:
        with self.factory.begin() as session:
            session.add_all(
                [
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        average_hourly_cost_cad=cost,
                        active=True,
                        version=1,
                    ),
                    TaskClassStandard(
                        task_code="216",
                        resource_class_code="PROGRAMMEUR",
                        active=True,
                        version=1,
                    ),
                ]
            )

    def _sync(self, item: TaskCatalogItem):
        snapshot = TaskCatalogProjectSnapshot(
            project_number="P-1",
            source_rows=1,
            rejected_rows=0,
            items=(item,),
        )
        with self.factory.begin() as session:
            repository = SqlTaskCatalogRepository(session)
            return TaskCatalogSyncService(
                StubProjectTaskSource(snapshot),
                repository,
                sync_metadata_repository=repository,
                workforce_policy=SqlTaskCatalogWorkforcePolicy(session),
            ).synchronize_project(
                project_external_id="1001",
                project_number="P-1",
            )

    def test_classified_task_persists_class_cost_and_budget_hours(self) -> None:
        self._seed_project()
        self._seed_programmer_standard()

        result = self._sync(
            TaskCatalogItem(
                project_number="P-1",
                code="216",
                label="Programmation",
                erp_task_id="9001",
                account_group="DEPMO",
                budget_amount_cad=Decimal("1000.0000000000"),
                budget_actual_cad=Decimal("250.0000000000"),
            )
        )

        self.assertEqual((result.created, result.ignored), (1, 0))
        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "9001"
                )
            )
            assert row is not None
            self.assertTrue(row.workforce_eligible)
            self.assertEqual(row.resource_class_code, "PROGRAMMEUR")
            self.assertEqual(
                row.average_hourly_cost_cad,
                Decimal("125.0000"),
            )
            self.assertEqual(
                row.budget_hours,
                Decimal("8.000000000000000000"),
            )
            projected = SqlTaskCatalogRepository(session).search(
                project_number="P-1"
            )[0]
            self.assertEqual(projected.resource_class_code, "PROGRAMMEUR")
            self.assertEqual(
                projected.budget_hours,
                Decimal("8.000000000000000000"),
            )
            self.assertEqual(projected.workforce_diagnostics, ())

    def test_unclassified_new_task_is_not_imported_into_workforce_catalog(self) -> None:
        self._seed_project()

        result = self._sync(
            TaskCatalogItem(
                project_number="P-1",
                code="999",
                label="Sans standard",
                erp_task_id="99901",
                account_group="DEPMO",
                budget_amount_cad=Decimal("500.00"),
            )
        )

        self.assertEqual((result.created, result.ignored), (0, 1))
        with self.factory() as session:
            self.assertIsNone(
                session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.erp_task_id == "99901"
                    )
                )
            )

    def test_class_without_cost_keeps_task_but_never_invents_budget_hours(self) -> None:
        self._seed_project()
        self._seed_programmer_standard(cost=None)

        result = self._sync(
            TaskCatalogItem(
                project_number="P-1",
                code="216",
                label="Programmation",
                erp_task_id="9002",
                account_group="DEPMO",
                budget_amount_cad=Decimal("1000.00"),
            )
        )

        self.assertEqual((result.created, result.ignored), (1, 0))
        with self.factory() as session:
            row = session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == "9002"
                )
            )
            assert row is not None
            self.assertTrue(row.workforce_eligible)
            self.assertEqual(row.resource_class_code, "PROGRAMMEUR")
            self.assertIsNone(row.average_hourly_cost_cad)
            self.assertIsNone(row.budget_hours)
            projected = SqlTaskCatalogRepository(session).search(
                project_number="P-1"
            )[0]
            self.assertIn(
                "resource_class_cost_missing",
                projected.workforce_diagnostics,
            )

    def test_project_exclude_hides_historical_task_without_breaking_reference(self) -> None:
        self._seed_project()
        self._seed_programmer_standard()
        with self.factory.begin() as session:
            session.add(
                ProjectTaskClassOverride(
                    project_id="PROJECT-1",
                    task_code="216",
                    resource_class_code=None,
                    excluded=True,
                    version=1,
                )
            )
            session.add(
                TaskCatalogEntry(
                    id="LEGACY-TASK",
                    project_number="P-1",
                    task_code="216",
                    label="Programmation historique",
                    active=True,
                    status="Actif",
                )
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id="DEMAND-1",
                    project_id="PROJECT-1",
                    status="Approuvée",
                    aggregate_version=4,
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="LINE-1",
                    workforce_request_id="DEMAND-1",
                    position=0,
                    task_catalog_item_id="LEGACY-TASK",
                    erp_task_code="216",
                    erp_task_label="Programmation historique",
                    estimated_hours=Decimal("12.00"),
                    estimated_hours_source="MANUAL",
                )
            )

        result = self._sync(
            TaskCatalogItem(
                project_number="P-1",
                code="216",
                label="Programmation ERP",
                erp_task_id="9003",
                account_group="DEPMO",
                budget_amount_cad=Decimal("1500.00"),
            )
        )

        self.assertEqual((result.created, result.updated, result.ignored), (0, 1, 0))
        with self.factory() as session:
            row = session.get(TaskCatalogEntry, "LEGACY-TASK")
            assert row is not None
            self.assertEqual(row.erp_task_id, "9003")
            self.assertFalse(row.workforce_eligible)
            self.assertIsNone(row.resource_class_code)
            self.assertIn(
                "task_excluded",
                SqlTaskCatalogRepository(session)
                .search(project_number="P-1", active_only=False)[0]
                .workforce_diagnostics,
            )
            self.assertEqual(
                SqlTaskCatalogRepository(session).search(project_number="P-1"),
                (),
            )
            line = session.get(RequestLine, "LINE-1")
            demand = session.get(WorkforceRequest, "DEMAND-1")
            assert line is not None
            assert demand is not None
            self.assertEqual(line.task_catalog_item_id, "LEGACY-TASK")
            self.assertEqual(line.estimated_hours, Decimal("12.00"))
            self.assertEqual(demand.status, "Approuvée")
            self.assertEqual(demand.aggregate_version, 4)
            self.assertEqual(session.query(Shift).count(), 0)


if __name__ == "__main__":
    unittest.main()
