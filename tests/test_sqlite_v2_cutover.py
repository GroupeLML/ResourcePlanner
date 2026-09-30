from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.infrastructure.sql import (
    AppUser,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AuthLoginTransaction,
    AuthSession,
    Base,
    DeliveryItemRow,
    DeliveryPlanRow,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SmtpConfigurationRow,
    TaskCatalogProjectSyncState,
    WorkforceRequest,
    WorkPackage,
    WorkPackageWeeklyLoad,
    create_sql_engine,
)
from app.server.runtime import ServerSettings, create_configured_app
from tools.cutover_sqlite_v2_to_sqlserver import (
    DEV_IDENTITY_ISSUER,
    DROP,
    KEEP,
    REBUILD,
    TABLE_POLICIES,
    _code_alembic_head,
    create_readonly_sqlite_engine,
    run_cutover,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _create_schema(path: Path, *, baseline_state: bool) -> None:
    engine = create_sql_engine(_url(path))
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
                {"revision": _code_alembic_head()},
            )
            if baseline_state:
                connection.execute(
                    Base.metadata.tables["planning_mutation_state"].insert(),
                    {"id": "GLOBAL", "version": 1},
                )
    finally:
        engine.dispose()


def _seed_source(path: Path) -> None:
    _create_schema(path, baseline_state=False)
    engine = create_sql_engine(_url(path))
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            connection.execute(
                Base.metadata.tables["planning_mutation_state"].insert(),
                {"id": "GLOBAL", "version": 41},
            )
            connection.execute(
                Project.__table__.insert(),
                [
                    {
                        "id": "PROJECT-REAL",
                        "erp_external_id": "ERP-PROJECT-1",
                        "number": "P-1001",
                        "name": "Projet réel",
                        "status": "active",
                    },
                    {
                        "id": "DEMO-PROJECT",
                        "erp_external_id": None,
                        "number": "DEMO-1001",
                        "name": "Projet démo",
                        "status": "active",
                    },
                ],
            )
            connection.execute(
                Resource.__table__.insert(),
                {
                    "id": "RESOURCE-REAL",
                    "external_id": "EMP-100",
                    "name": "Ressource réelle",
                    "active": True,
                    "erp_active": True,
                },
            )
            connection.execute(
                AppUser.__table__.insert(),
                [
                    {
                        "id": "USER-REAL",
                        "issuer": None,
                        "subject": None,
                        "display_name": "Utilisateur réel",
                        "roles_json": '["ADMIN"]',
                        "active": True,
                    },
                    {
                        "id": "USER-DEV",
                        "issuer": DEV_IDENTITY_ISSUER,
                        "subject": "dev-user",
                        "display_name": "Utilisateur dev",
                        "roles_json": '["ADMIN"]',
                        "active": True,
                    },
                ],
            )
            connection.execute(
                WorkPackage.__table__.insert(),
                {
                    "id": "WP-REAL",
                    "project_id": "PROJECT-REAL",
                    "code": "WP-01",
                    "name": "Lot réel",
                    "planned_hours": Decimal("24"),
                },
            )
            connection.execute(
                WorkPackageWeeklyLoad.__table__.insert(),
                {
                    "work_package_id": "WP-REAL",
                    "week_start": date(2026, 9, 28),
                    "hours": Decimal("24.00"),
                },
            )
            connection.execute(
                WorkforceRequest.__table__.insert(),
                {
                    "id": "REQUEST-REAL",
                    "project_id": "PROJECT-REAL",
                    "work_package_id": "WP-REAL",
                    "requester_user_id": "USER-DEV",
                    "requester_name": "Ancien demandeur dev",
                    "status": "En planification",
                },
            )
            connection.execute(
                RequestLine.__table__.insert(),
                {
                    "id": "LINE-REAL",
                    "workforce_request_id": "REQUEST-REAL",
                    "position": 0,
                    "kind": "WORKFORCE",
                    "slot_count": 1,
                    "work_package_id": "WP-REAL",
                    "estimated_hours": Decimal("8"),
                    "active": True,
                },
            )
            connection.execute(
                ResourceRequirement.__table__.insert(),
                {
                    "id": "REQUIREMENT-REAL",
                    "project_id": "PROJECT-REAL",
                    "workforce_request_id": "REQUEST-REAL",
                    "source_request_line_id": "LINE-REAL",
                    "assigned_resource_id": "RESOURCE-REAL",
                    "start_date": date(2026, 10, 5),
                    "end_date": date(2026, 10, 5),
                    "planned_hours": Decimal("8"),
                    "status": "Planifié",
                    "origin": "REQUEST",
                },
            )
            connection.execute(
                Shift.__table__.insert(),
                {
                    "id": "SHIFT-REAL",
                    "resource_requirement_id": "REQUIREMENT-REAL",
                    "resource_id": "RESOURCE-REAL",
                    "work_date": date(2026, 10, 5),
                    "hours": Decimal("8"),
                    "source": "MANUAL",
                    "locked": True,
                },
            )
            connection.execute(
                DeliveryPlanRow.__table__.insert(),
                {
                    "id": "DELIVERY-PLAN-REAL",
                    "work_package_id": "WP-REAL",
                    "status": "ACTIVE",
                    "delivery_version": 2,
                },
            )
            connection.execute(
                DeliveryItemRow.__table__.insert(),
                {
                    "id": "DELIVERY-ITEM-REAL",
                    "delivery_plan_id": "DELIVERY-PLAN-REAL",
                    "item_type": "STORY",
                    "title": "Story réelle",
                    "status": "IN_PROGRESS",
                    "position": 0,
                },
            )
            connection.execute(
                AssetType.__table__.insert(),
                {
                    "id": "ASSET-TYPE-REAL",
                    "code": "VEHICLE",
                    "label": "Véhicule",
                    "category": "VEHICLE",
                },
            )
            connection.execute(
                Asset.__table__.insert(),
                {
                    "id": "ASSET-REAL",
                    "code": "TRUCK-01",
                    "label": "Camion 01",
                    "asset_type_id": "ASSET-TYPE-REAL",
                },
            )
            connection.execute(
                AssetRequirement.__table__.insert(),
                {
                    "id": "ASSET-REQ-REAL",
                    "project_id": "PROJECT-REAL",
                    "workforce_request_id": "REQUEST-REAL",
                    "source_request_line_id": "LINE-REAL",
                    "approved_entry_key": "asset-entry-1",
                    "slot_index": 0,
                    "asset_type_id": "ASSET-TYPE-REAL",
                    "start_date": date(2026, 10, 5),
                    "end_date": date(2026, 10, 5),
                },
            )
            connection.execute(
                AssetAllocation.__table__.insert(),
                {
                    "id": "ASSET-ALLOC-REAL",
                    "asset_requirement_id": "ASSET-REQ-REAL",
                    "asset_id": "ASSET-REAL",
                    "operator_resource_id": "RESOURCE-REAL",
                    "start_date": date(2026, 10, 5),
                    "end_date": date(2026, 10, 5),
                    "locked": True,
                },
            )
            connection.execute(
                TaskCatalogProjectSyncState.__table__.insert(),
                {
                    "project_number": "P-1001",
                    "last_attempt_at": now,
                    "last_success_at": now,
                    "source_rows": 12,
                    "task_count": 10,
                    "rejected_rows": 2,
                },
            )
            connection.execute(
                AuthLoginTransaction.__table__.insert(),
                {
                    "id": "LOGIN-TX",
                    "state_hash": "a" * 64,
                    "nonce": "nonce-secret-like-temporary",
                    "code_verifier": "verifier-secret-like-temporary",
                    "expires_at": now + timedelta(minutes=5),
                },
            )
            connection.execute(
                AuthSession.__table__.insert(),
                {
                    "id": "SESSION-1",
                    "token_hash": "b" * 64,
                    "csrf_token_hash": "c" * 64,
                    "user_id": "USER-REAL",
                    "expires_at": now + timedelta(hours=8),
                },
            )
            connection.execute(
                SmtpConfigurationRow.__table__.insert(),
                {
                    "id": "smtp",
                    "host": "smtp.example.invalid",
                    "port": 587,
                    "security": "STARTTLS",
                    "username": "service-account",
                    "encrypted_password": "encrypted-secret",
                    "enabled": True,
                },
            )
    finally:
        engine.dispose()


def _seed_target(path: Path) -> None:
    _create_schema(path, baseline_state=True)


def _reshape_source_as_0048(path: Path) -> None:
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.executescript(
            """
            DROP TABLE IF EXISTS auth_security_audit;
            DROP TABLE IF EXISTS break_glass_credentials;
            DROP TABLE IF EXISTS work_package_audit;
            DROP TABLE IF EXISTS work_package_weekly_loads;

            CREATE TABLE auth_sessions_0048 AS
            SELECT
                id,
                token_hash,
                csrf_token_hash,
                user_id,
                expires_at,
                revoked_at,
                created_at,
                updated_at
            FROM auth_sessions;
            DROP TABLE auth_sessions;
            ALTER TABLE auth_sessions_0048 RENAME TO auth_sessions;

            CREATE TABLE work_packages_0048 AS
            SELECT
                id,
                project_id,
                code,
                name,
                description,
                start_date,
                end_date,
                planned_hours,
                status,
                legacy_effort_id,
                created_at,
                updated_at
            FROM work_packages;
            DROP TABLE work_packages;
            ALTER TABLE work_packages_0048 RENAME TO work_packages;

            UPDATE alembic_version
            SET version_num = '0048_identity_admin_audit';
            """
        )
        raw.commit()
    finally:
        raw.close()


def _run(
    root: Path,
    *,
    apply: bool = False,
    source: Path | None = None,
    target: Path | None = None,
    backup: Path | None = None,
    report_name: str = "report.json",
    expected_revision: str | None = None,
    fail_after_table: str | None = None,
) -> tuple[int, dict]:
    source_path = source or root / "source.db"
    target_path = target or root / "target.db"
    report_path = root / report_name
    code = run_cutover(
        source_path=source_path,
        target_database_url=_url(target_path),
        report_path=report_path,
        backup_path=backup,
        expected_target_revision=(
            expected_revision if expected_revision is not None else _code_alembic_head()
        ),
        apply=apply,
        baseline_ready=apply,
        allow_test_target=True,
        fail_after_table=fail_after_table,
    )
    return code, json.loads(report_path.read_text(encoding="utf-8"))


class SqliteV2CutoverTests(unittest.TestCase):
    def test_policy_classifies_every_current_table(self) -> None:
        self.assertEqual(set(TABLE_POLICIES), set(Base.metadata.tables))
        self.assertEqual(
            {policy.classification for policy in TABLE_POLICIES.values()},
            {KEEP, REBUILD, DROP},
        )

    def test_source_engine_is_really_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            _seed_source(path)
            before = _hash(path)
            engine = create_readonly_sqlite_engine(path)
            try:
                with engine.connect() as connection:
                    with self.assertRaises(OperationalError):
                        connection.execute(
                            Project.__table__.insert(),
                            {
                                "id": "MUST-NOT-WRITE",
                                "number": "NO-WRITE",
                                "name": "No write",
                            },
                        )
            finally:
                engine.dispose()
            self.assertEqual(_hash(path), before)

    def test_dry_run_writes_nothing_and_reports_inventory_classification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)
            source_before = _hash(source)
            target_before = _hash(target)

            code, report = _run(root)

            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "ready")
            self.assertTrue(report["ready_for_apply"])
            self.assertEqual(_hash(source), source_before)
            self.assertEqual(_hash(target), target_before)
            by_table = {row["table"]: row for row in report["tables"]}
            self.assertEqual(by_table["projects"]["classification"], KEEP)
            self.assertEqual(by_table["projects"]["source_rows"], 2)
            self.assertEqual(by_table["projects"]["eligible_rows"], 1)
            self.assertEqual(by_table["auth_sessions"]["classification"], DROP)
            self.assertEqual(by_table["auth_sessions"]["eligible_rows"], 0)
            self.assertEqual(by_table["planning_mutation_state"]["classification"], REBUILD)
            self.assertIn("projects", report["transfer_order"])
            self.assertNotIn("auth_sessions", report["transfer_order"])
            self.assertNotIn("planning_mutation_state", report["transfer_order"])

    def test_real_0048_shape_is_normalized_read_only_and_ready(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _reshape_source_as_0048(source)
            _seed_target(target)
            source_before = _hash(source)

            code, report = _run(root)

            self.assertEqual(code, 0, report)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(
                report["source"]["alembic_revision"],
                "0048_identity_admin_audit",
            )
            self.assertEqual(
                report["source"]["compatibility_profile"],
                "0048_identity_admin_audit",
            )
            self.assertEqual(_hash(source), source_before)

            adaptations = {
                (item["kind"], item["table"])
                for item in report["source"]["compatibility_adaptations"]
            }
            self.assertIn(
                ("missing_table_as_empty", "work_package_audit"),
                adaptations,
            )
            self.assertIn(
                ("missing_table_as_empty", "work_package_weekly_loads"),
                adaptations,
            )
            self.assertIn(
                ("missing_columns_with_defaults", "work_packages"),
                adaptations,
            )

            by_table = {row["table"]: row for row in report["tables"]}
            self.assertEqual(by_table["work_packages"]["source_rows"], 1)
            self.assertEqual(by_table["work_packages"]["eligible_rows"], 1)
            self.assertEqual(by_table["work_package_audit"]["source_rows"], 0)
            self.assertEqual(by_table["work_package_weekly_loads"]["source_rows"], 0)
            self.assertEqual(by_table["auth_sessions"]["source_rows"], 1)
            self.assertEqual(by_table["auth_sessions"]["eligible_rows"], 0)

    def test_0048_apply_preserves_work_package_with_explicit_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            backup = root / "source.backup.db"
            _seed_source(source)
            _reshape_source_as_0048(source)
            _seed_target(target)
            shutil.copy2(source, backup)
            source_before = _hash(source)

            code, report = _run(root, apply=True, backup=backup)

            self.assertEqual(code, 0, report)
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["transaction"]["status"], "committed")
            self.assertEqual(_hash(source), source_before)

            engine = create_sql_engine(_url(target))
            try:
                with engine.connect() as connection:
                    work_package = connection.execute(
                        select(
                            WorkPackage.id,
                            WorkPackage.task_catalog_item_id,
                            WorkPackage.version,
                            WorkPackage.weekly_load_origin,
                        ).where(WorkPackage.id == "WP-REAL")
                    ).one()
                    self.assertEqual(work_package.id, "WP-REAL")
                    self.assertIsNone(work_package.task_catalog_item_id)
                    self.assertEqual(work_package.version, 1)
                    self.assertIsNone(work_package.weekly_load_origin)
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(
                                    WorkPackageWeeklyLoad
                                )
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(AuthSession)
                            )
                            or 0
                        ),
                        0,
                    )
            finally:
                engine.dispose()

    def test_legacy_shape_with_unapproved_revision_stays_blocked(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _reshape_source_as_0048(source)
            _seed_target(target)

            raw = sqlite3.connect(source)
            try:
                raw.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("0047_unapproved",),
                )
                raw.commit()
            finally:
                raw.close()

            code, report = _run(root)

            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "blocked")
            self.assertIsNone(report["source"]["compatibility_profile"])
            self.assertTrue(
                any(
                    item.get("code") in {"source_missing_table", "source_missing_columns"}
                    for item in report["anomalies"]
                )
            )

    def test_missing_source_is_blocked_without_target_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.db"
            _seed_target(target)
            target_before = _hash(target)

            code, report = _run(root, source=root / "missing.db")

            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(_hash(target), target_before)

    def test_invalid_source_fails_without_target_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            source.write_text("not a sqlite database", encoding="utf-8")
            target = root / "target.db"
            _seed_target(target)
            target_before = _hash(target)

            code, report = _run(root, source=source)

            self.assertNotEqual(code, 0)
            self.assertIn(report["status"], {"blocked", "failed"})
            self.assertEqual(_hash(target), target_before)

    def test_wrong_target_revision_is_blocked(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)
            engine = create_sql_engine(_url(target))
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("UPDATE alembic_version SET version_num = 'wrong-baseline'")
                    )
            finally:
                engine.dispose()

            code, report = _run(root)

            self.assertEqual(code, 2)
            codes = {item["code"] for item in report["anomalies"]}
            self.assertIn("target_revision_mismatch", codes)

    def test_non_empty_target_and_pk_collision_are_reported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)
            engine = create_sql_engine(_url(target))
            try:
                with engine.begin() as connection:
                    connection.execute(
                        Project.__table__.insert(),
                        {
                            "id": "PROJECT-REAL",
                            "number": "TARGET-COLLISION",
                            "name": "Collision",
                        },
                    )
            finally:
                engine.dispose()

            code, report = _run(root)

            self.assertEqual(code, 2)
            self.assertTrue(
                any(
                    item.get("code") == "target_not_empty"
                    and item.get("table") == "projects"
                    for item in report["anomalies"]
                )
            )
            self.assertTrue(
                any(
                    item.get("table") == "projects"
                    and item.get("kind") == "primary_key"
                    for item in report["collisions"]
                )
            )

    def test_missing_required_keep_fk_is_blocking(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)

            raw = sqlite3.connect(source)
            try:
                raw.execute("PRAGMA foreign_keys=OFF")
                raw.execute(
                    "INSERT INTO work_packages "
                    "(id, project_id, name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    ("WP-ORPHAN", "MISSING-PROJECT", "Orphelin", "planned"),
                )
                raw.commit()
            finally:
                raw.close()

            code, report = _run(root)

            self.assertEqual(code, 2)
            self.assertTrue(
                any(
                    item.get("table") == "work_packages"
                    and item.get("parent_table") == "projects"
                    for item in report["missing_dependencies"]
                )
            )

    def test_apply_preserves_ids_links_excludes_demo_dev_sessions_and_secrets(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            backup = root / "source.backup.db"
            _seed_source(source)
            _seed_target(target)
            shutil.copy2(source, backup)
            source_before = _hash(source)

            code, report = _run(root, apply=True, backup=backup)

            self.assertEqual(code, 0, report)
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["transaction"]["status"], "committed")
            self.assertEqual(_hash(source), source_before)

            engine = create_sql_engine(_url(target))
            try:
                with engine.connect() as connection:
                    projects = set(connection.execute(select(Project.id)).scalars())
                    self.assertEqual(projects, {"PROJECT-REAL"})
                    users = {
                        row.id: row.issuer
                        for row in connection.execute(
                            select(AppUser.id, AppUser.issuer)
                        )
                    }
                    self.assertEqual(users, {"USER-REAL": None})
                    weekly = connection.execute(
                        select(
                            WorkPackageWeeklyLoad.work_package_id,
                            WorkPackageWeeklyLoad.week_start,
                            WorkPackageWeeklyLoad.hours,
                        ).where(
                            WorkPackageWeeklyLoad.work_package_id == "WP-REAL"
                        )
                    ).one()
                    self.assertEqual(weekly.work_package_id, "WP-REAL")
                    self.assertEqual(weekly.week_start, date(2026, 9, 28))
                    self.assertEqual(Decimal(weekly.hours), Decimal("24.00"))
                    request = connection.execute(
                        select(
                            WorkforceRequest.id,
                            WorkforceRequest.requester_user_id,
                        ).where(WorkforceRequest.id == "REQUEST-REAL")
                    ).one()
                    self.assertEqual(request.id, "REQUEST-REAL")
                    self.assertIsNone(request.requester_user_id)

                    line = connection.execute(
                        select(
                            RequestLine.id,
                            RequestLine.workforce_request_id,
                        ).where(RequestLine.id == "LINE-REAL")
                    ).one()
                    requirement = connection.execute(
                        select(
                            ResourceRequirement.id,
                            ResourceRequirement.source_request_line_id,
                        ).where(ResourceRequirement.id == "REQUIREMENT-REAL")
                    ).one()
                    shift = connection.execute(
                        select(
                            Shift.id,
                            Shift.resource_requirement_id,
                            Shift.resource_id,
                        ).where(Shift.id == "SHIFT-REAL")
                    ).one()
                    self.assertEqual(line.workforce_request_id, "REQUEST-REAL")
                    self.assertEqual(requirement.source_request_line_id, "LINE-REAL")
                    self.assertEqual(shift.resource_requirement_id, "REQUIREMENT-REAL")
                    self.assertEqual(shift.resource_id, "RESOURCE-REAL")

                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(AuthSession)
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(AuthLoginTransaction)
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(SmtpConfigurationRow)
                            )
                            or 0
                        ),
                        0,
                    )
                    planning = connection.execute(
                        select(Base.metadata.tables["planning_mutation_state"])
                    ).mappings().one()
                    self.assertEqual(planning["id"], "GLOBAL")
                    self.assertEqual(planning["version"], 1)
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(
                                    TaskCatalogProjectSyncState
                                )
                            )
                            or 0
                        ),
                        0,
                    )
                    asset_allocation = connection.execute(
                        select(
                            AssetAllocation.id,
                            AssetAllocation.asset_requirement_id,
                        ).where(AssetAllocation.id == "ASSET-ALLOC-REAL")
                    ).one()
                    self.assertEqual(
                        asset_allocation.asset_requirement_id,
                        "ASSET-REQ-REAL",
                    )
            finally:
                engine.dispose()

            self.assertTrue(
                report["validation"]["business_relationships"]["shifts"]["identical"]
            )
            self.assertTrue(
                report["validation"]["business_relationships"]["delivery_items"]["identical"]
            )
            self.assertTrue(
                report["validation"]["business_relationships"]["asset_allocations"]["identical"]
            )

    def test_apply_requires_verified_backup_before_target_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)
            target_before = _hash(target)

            code, report = _run(root, apply=True, backup=None)

            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(_hash(target), target_before)

    def test_apply_rolls_back_every_keep_insert_on_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            backup = root / "source.backup.db"
            _seed_source(source)
            _seed_target(target)
            shutil.copy2(source, backup)

            code, report = _run(
                root,
                apply=True,
                backup=backup,
                fail_after_table="workforce_requests",
            )

            self.assertEqual(code, 3)
            self.assertEqual(report["transaction"]["status"], "rolled_back")
            engine = create_sql_engine(_url(target))
            try:
                with engine.connect() as connection:
                    self.assertEqual(
                        int(
                            connection.scalar(
                                select(func.count()).select_from(Project)
                            )
                            or 0
                        ),
                        0,
                    )
                    planning = connection.execute(
                        select(Base.metadata.tables["planning_mutation_state"])
                    ).mappings().one()
                    self.assertEqual(planning["id"], "GLOBAL")
                    self.assertEqual(planning["version"], 1)
            finally:
                engine.dispose()

    def test_replay_is_fail_closed_until_target_is_recreated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            backup = root / "source.backup.db"
            _seed_source(source)
            _seed_target(target)
            shutil.copy2(source, backup)

            first_code, first_report = _run(
                root,
                apply=True,
                backup=backup,
                report_name="first.json",
            )
            second_code, second_report = _run(
                root,
                apply=True,
                backup=backup,
                report_name="second.json",
            )

            self.assertEqual(first_code, 0, first_report)
            self.assertEqual(second_code, 2)
            self.assertTrue(
                any(
                    item.get("code") == "target_not_empty"
                    for item in second_report["anomalies"]
                )
            )

    def test_report_counts_keep_drop_rebuild_and_resync_actions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            _seed_source(source)
            _seed_target(target)

            code, report = _run(root)

            self.assertEqual(code, 0)
            by_table = {row["table"]: row for row in report["tables"]}
            self.assertEqual(by_table["projects"]["source_rows"], 2)
            self.assertEqual(by_table["projects"]["eligible_rows"], 1)
            self.assertEqual(by_table["projects"]["excluded_rows"], 1)
            self.assertEqual(by_table["auth_sessions"]["source_rows"], 1)
            self.assertEqual(by_table["auth_sessions"]["eligible_rows"], 0)
            self.assertEqual(
                by_table["task_catalog_project_sync_state"]["classification"],
                REBUILD,
            )
            actions = {
                row["table"]: row["action"]
                for row in report["post_cutover_actions"]
            }
            self.assertEqual(actions["projects"], "RESYNC_ACUMATICA_PROJECTS")
            self.assertEqual(
                actions["resources"],
                "RESYNC_ACUMATICA_RP_EMPLOYEES",
            )
            self.assertEqual(
                actions["erp_user_directory"],
                "RESYNC_ACUMATICA_RP_USERS",
            )
            self.assertEqual(
                actions["task_catalog_items"],
                "RESYNC_ACUMATICA_PROJECT_TASKS",
            )

    def test_application_can_start_on_resulting_target(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "target.db"
            backup = root / "source.backup.db"
            _seed_source(source)
            _seed_target(target)
            shutil.copy2(source, backup)

            code, report = _run(root, apply=True, backup=backup)
            self.assertEqual(code, 0, report)

            app = create_configured_app(ServerSettings(database_url=_url(target)))
            with TestClient(app) as client:
                response = client.get("/ready")
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
