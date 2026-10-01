from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.sql import Base, create_sql_engine  # noqa: E402


SOURCE_REVISION = "0048_identity_admin_audit"
BASELINE_REVISION = "v2_production_baseline"

# Exact differences observed and approved for the frozen 0048 runtime shape.
SOURCE_MISSING_TABLES = {
    "auth_security_audit",
    "break_glass_credentials",
    "work_package_audit",
    "work_package_weekly_loads",
}
SOURCE_MISSING_COLUMNS = {
    "auth_sessions": {"auth_mode"},
    "work_packages": {
        "task_catalog_item_id",
        "version",
        "weekly_load_origin",
        "resource_class_code",
    },
}


class BridgeBlocked(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.resolve().as_posix()}"


def _code_head() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise BridgeBlocked(
            f"Le code doit avoir une seule tête Alembic; trouvé: {heads!r}."
        )
    return str(heads[0])


def _assert_no_wal(path: Path) -> None:
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size:
        raise BridgeBlocked(
            f"Un WAL SQLite actif existe ({wal}, {wal.stat().st_size} octets). "
            "Arrêter le runtime et checkpoint SQLite avant le bridge."
        )


def _revision(engine) -> str | None:
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables:
            return None
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _schema_errors(
    engine,
    *,
    allowed_missing_tables: set[str] | None = None,
    allowed_missing_columns: dict[str, set[str]] | None = None,
) -> list[str]:
    allowed_tables = set(allowed_missing_tables or ())
    allowed_columns = {
        table: set(columns)
        for table, columns in (allowed_missing_columns or {}).items()
    }
    errors: list[str] = []

    with engine.connect() as connection:
        inspector = inspect(connection)
        actual_tables = set(inspector.get_table_names())
        model_tables = set(Base.metadata.tables)
        expected_tables = (model_tables - allowed_tables) | {"alembic_version"}

        missing = sorted(expected_tables - actual_tables)
        extra = sorted(actual_tables - expected_tables)
        if missing:
            errors.append(f"tables manquantes inattendues: {missing}")
        if extra:
            errors.append(f"tables supplémentaires inattendues: {extra}")

        for table_name in sorted(model_tables & actual_tables):
            actual = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            expected = set(Base.metadata.tables[table_name].c.keys())
            expected -= allowed_columns.get(table_name, set())
            missing_columns = sorted(expected - actual)
            extra_columns = sorted(actual - expected)
            if missing_columns:
                errors.append(
                    f"{table_name}: colonnes manquantes inattendues: {missing_columns}"
                )
            if extra_columns:
                errors.append(
                    f"{table_name}: colonnes supplémentaires inattendues: {extra_columns}"
                )

    return errors


def inspect_source(path: Path) -> dict[str, object]:
    source = path.resolve()
    if not source.is_file():
        raise BridgeBlocked(f"SQLite introuvable: {source}")
    _assert_no_wal(source)

    engine = create_sql_engine(_sqlite_url(source))
    try:
        revision = _revision(engine)
        if revision != SOURCE_REVISION:
            raise BridgeBlocked(
                f"Révision source refusée: {revision!r}; attendu {SOURCE_REVISION!r}."
            )
        errors = _schema_errors(
            engine,
            allowed_missing_tables=SOURCE_MISSING_TABLES,
            allowed_missing_columns=SOURCE_MISSING_COLUMNS,
        )
        if errors:
            raise BridgeBlocked(
                "Le schéma n'est pas le profil 0048 approuvé: " + "; ".join(errors)
            )
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = inspector.get_table_names()
            work_packages = int(
                connection.scalar(text("SELECT COUNT(*) FROM work_packages")) or 0
            )
            auth_sessions = int(
                connection.scalar(text("SELECT COUNT(*) FROM auth_sessions")) or 0
            )
    finally:
        engine.dispose()

    return {
        "source": str(source),
        "sha256": _sha256(source),
        "revision": SOURCE_REVISION,
        "tables": len(tables),
        "work_packages": work_packages,
        "auth_sessions": auth_sessions,
        "code_head": _code_head(),
    }


def _apply_archived_baseline_delta(path: Path) -> None:
    """Replay only the archived 0049/0050 schema delta on a working copy."""

    engine = create_sql_engine(_sqlite_url(path))
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))

            # Archived 0049_work_package_task_identity semantics.
            with operations.batch_alter_table(
                "work_packages", recreate="always"
            ) as batch:
                batch.add_column(
                    sa.Column(
                        "task_catalog_item_id",
                        sa.String(length=36),
                        nullable=True,
                    )
                )
                batch.add_column(
                    sa.Column(
                        "version",
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("1"),
                    )
                )
                batch.create_foreign_key(
                    "fk_work_packages_task_catalog_item_id_task_catalog_items",
                    "task_catalog_items",
                    ["task_catalog_item_id"],
                    ["id"],
                )
                batch.create_check_constraint(
                    "ck_work_packages_work_package_version_positive",
                    "version >= 1",
                )
            operations.create_index(
                "ix_work_packages_task_catalog_item",
                "work_packages",
                ["task_catalog_item_id"],
                unique=False,
            )
            Base.metadata.tables["work_package_audit"].create(connection)

            # Archived 0050_break_glass_admin semantics.
            with operations.batch_alter_table(
                "auth_sessions", recreate="always"
            ) as batch:
                batch.add_column(
                    sa.Column(
                        "auth_mode",
                        sa.String(length=32),
                        server_default="oidc",
                        nullable=False,
                    )
                )
                batch.create_check_constraint(
                    "ck_auth_sessions_auth_mode_valid",
                    "auth_mode IN ('oidc', 'break_glass', 'local')",
                )
                batch.create_index(
                    "ix_auth_sessions_auth_mode",
                    ["auth_mode"],
                    unique=False,
                )
            Base.metadata.tables["break_glass_credentials"].create(connection)
            Base.metadata.tables["auth_security_audit"].create(connection)

            updated = connection.execute(
                text(
                    "UPDATE alembic_version "
                    "SET version_num = :baseline "
                    "WHERE version_num = :source"
                ),
                {
                    "baseline": BASELINE_REVISION,
                    "source": SOURCE_REVISION,
                },
            )
            if updated.rowcount != 1:
                raise BridgeBlocked(
                    "Impossible de remplacer exactement la révision 0048 par la baseline."
                )
    finally:
        engine.dispose()


def _upgrade_working_copy_to_head(path: Path) -> None:
    environment = os.environ.copy()
    environment["RESOURCEPLANNER_DATABASE_URL"] = _sqlite_url(path)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def _validate_head(path: Path) -> str:
    expected = _code_head()
    engine = create_sql_engine(_sqlite_url(path))
    try:
        revision = _revision(engine)
        if revision != expected:
            raise BridgeBlocked(
                f"Révision après bridge: {revision!r}; head attendu: {expected!r}."
            )
        errors = _schema_errors(engine)
        if errors:
            raise BridgeBlocked(
                "La copie migrée ne correspond pas au schéma courant: "
                + "; ".join(errors)
            )
    finally:
        engine.dispose()
    return expected


def bridge_sqlite_0048(
    source_path: Path,
    *,
    backup_path: Path | None = None,
    apply: bool = False,
    confirm_live_runtime: bool = False,
    fail_before_replace: bool = False,
) -> dict[str, object]:
    source = source_path.resolve()
    report = inspect_source(source)
    report["mode"] = "apply" if apply else "dry-run"
    report["status"] = "ready"

    if not apply:
        return report
    if not confirm_live_runtime:
        raise BridgeBlocked(
            "--confirm-live-runtime est obligatoire avec --apply. "
            "Ne jamais exécuter ce bridge sur le snapshot ENV-492."
        )
    if backup_path is None:
        raise BridgeBlocked("--backup est obligatoire avec --apply.")

    backup = backup_path.resolve()
    if backup == source:
        raise BridgeBlocked("Le backup doit être distinct de la SQLite live.")
    backup.parent.mkdir(parents=True, exist_ok=True)

    source_hash = str(report["sha256"])
    if backup.exists():
        backup_hash = _sha256(backup)
        if backup_hash != source_hash:
            raise BridgeBlocked(
                f"Le backup existant {backup} ne correspond pas à la source live."
            )
    else:
        shutil.copy2(source, backup)
        backup_hash = _sha256(backup)
        if backup_hash != source_hash:
            raise BridgeBlocked("Le backup créé ne correspond pas bit-à-bit à la source.")

    working = source.with_name(
        f".{source.name}.bridge-{uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, working)
        _apply_archived_baseline_delta(working)
        _upgrade_working_copy_to_head(working)
        head = _validate_head(working)

        if fail_before_replace:
            raise RuntimeError("Injected bridge failure before atomic replace")

        _assert_no_wal(source)
        if _sha256(source) != source_hash:
            raise BridgeBlocked(
                "La SQLite live a changé pendant le bridge; remplacement annulé."
            )

        os.replace(working, source)
        report.update(
            {
                "status": "applied",
                "backup": str(backup),
                "backup_sha256": backup_hash,
                "revision_after": head,
                "source_sha256_after": _sha256(source),
            }
        )
        return report
    finally:
        if working.exists():
            working.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bridge temporaire et fail-closed de la SQLite live 0048 vers le "
            "schéma Alembic courant. Le snapshot ENV-492 ne doit jamais être utilisé."
        )
    )
    parser.add_argument("--database", required=True, help="SQLite live à mettre à niveau.")
    parser.add_argument("--backup", help="Backup bit-à-bit distinct, obligatoire avec --apply.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-live-runtime",
        action="store_true",
        help="Confirme explicitement que --database est la SQLite live, pas le snapshot ENV-492.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = bridge_sqlite_0048(
            Path(args.database),
            backup_path=Path(args.backup) if args.backup else None,
            apply=bool(args.apply),
            confirm_live_runtime=bool(args.confirm_live_runtime),
        )
    except BridgeBlocked as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(
            f"ERREUR: Alembic a échoué sur la copie de travail (code {exc.returncode}); "
            "la SQLite live n'a pas été remplacée.",
            file=sys.stderr,
        )
        return 3

    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
