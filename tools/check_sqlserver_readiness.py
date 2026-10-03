from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Iterable

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import bindparam, delete, insert, or_, select, true, update
from sqlalchemy.dialects import mssql
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.sqltypes import Integer, String, Text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.sql import Base  # noqa: E402
from app.infrastructure.sql.asset_models import (  # noqa: E402
    Asset,
    AssetAllocation,
    AssetRequirement,
)
from app.infrastructure.sql.models import (  # noqa: E402
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
    WorkPackage,
    WorkPackageLoadInterval,
    WorkPackageWeeklyLoad,
)


MSSQL_IDENTIFIER_MAX = 128
MSSQL_ALEMBIC_VERSION_NUM_LENGTH = 128
# Keep index keys inside the conservative historical SQL Server limit. Current
# SQL Server versions allow larger nonclustered keys, but staying under 900 bytes
# avoids depending on server-version details before #162 validates the target.
MSSQL_CONSERVATIVE_INDEX_KEY_BYTES = 900
OFFLINE_MSSQL_URL = (
    "mssql+pyodbc://localhost/ResourcePlanner"
    "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
)


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str
    details: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class SqlServerReadinessError(RuntimeError):
    pass


def _run(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if len(output) > 4000:
            output = output[-4000:]
        raise SqlServerReadinessError(
            f"{' '.join(command)} a échoué avec le code {result.returncode}: {output}"
        )
    return result


def _alembic_head() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise SqlServerReadinessError(
            f"La chaîne Alembic doit avoir une seule tête; trouvé: {', '.join(heads) or '<aucune>'}"
        )
    return heads[0]


def check_sqlite_migrations() -> ReadinessCheck:
    """Run the full online migration chain from an empty SQLite database."""

    with TemporaryDirectory(prefix="resourceplanner-migration-readiness-") as directory:
        database = Path(directory) / "migration-readiness.db"
        env = dict(os.environ)
        env["RESOURCEPLANNER_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
        _run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)

        # Ask Alembic itself for the current revision so the check validates the
        # migration table, not only that a database file was created.
        result = _run([sys.executable, "-m", "alembic", "current"], env=env)
        head = _alembic_head()
        if head not in result.stdout:
            raise SqlServerReadinessError(
                f"SQLite n'est pas rendue à la tête Alembic {head}."
            )
    return ReadinessCheck(
        "sqlite_migrations_from_empty",
        "ok",
        f"upgrade head complet jusqu'à {head}",
    )


def check_mssql_offline_migrations() -> ReadinessCheck:
    """Render every Alembic migration using the SQL Server dialect without DBAPI."""

    env = dict(os.environ)
    env["RESOURCEPLANNER_DATABASE_URL"] = OFFLINE_MSSQL_URL
    result = _run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        env=env,
    )
    sql = result.stdout
    if "CREATE TABLE" not in sql.upper():
        raise SqlServerReadinessError("La génération MSSQL offline ne contient aucun CREATE TABLE.")
    upper_sql = sql.upper()
    if "ALEMBIC_VERSION" not in upper_sql:
        raise SqlServerReadinessError(
            "La génération MSSQL offline ne contient pas la table/version Alembic."
        )

    normalized_sql = upper_sql.replace("[", "").replace("]", "")
    expected_version_column = (
        f"VERSION_NUM VARCHAR({MSSQL_ALEMBIC_VERSION_NUM_LENGTH})"
    )
    if expected_version_column not in normalized_sql:
        raise SqlServerReadinessError(
            "La table alembic_version MSSQL n'utilise pas la capacité attendue "
            f"VARCHAR({MSSQL_ALEMBIC_VERSION_NUM_LENGTH})."
        )

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    revisions = [
        revision.revision
        for revision in ScriptDirectory.from_config(config).walk_revisions()
        if revision.revision
    ]
    longest_revision = max(revisions, key=len, default="")
    if len(longest_revision) > MSSQL_ALEMBIC_VERSION_NUM_LENGTH:
        raise SqlServerReadinessError(
            "Un identifiant Alembic dépasse la capacité de alembic_version: "
            f"{longest_revision} ({len(longest_revision)} caractères)."
        )

    return ReadinessCheck(
        "mssql_offline_migrations",
        "ok",
        (
            f"{len(sql.splitlines())} lignes de DDL générées; "
            f"alembic_version=VARCHAR({MSSQL_ALEMBIC_VERSION_NUM_LENGTH}); "
            f"révision max={len(longest_revision)} caractères"
        ),
    )


def _schema_objects() -> Iterable[tuple[str, object]]:
    for table in Base.metadata.sorted_tables:
        yield f"table:{table.name}", table
        for constraint in table.constraints:
            yield f"constraint:{table.name}:{constraint.name or '<unnamed>'}", constraint
        for index in table.indexes:
            yield f"index:{table.name}:{index.name or '<unnamed>'}", index


def _validate_identifier(name: str | None, *, kind: str) -> None:
    if not name:
        return
    if len(name) > MSSQL_IDENTIFIER_MAX:
        raise SqlServerReadinessError(
            f"{kind} dépasse {MSSQL_IDENTIFIER_MAX} caractères: {name}"
        )


def _estimated_key_bytes(expressions, *, name: str) -> int:
    total = 0
    for expression in expressions:
        sql_type = getattr(expression, "type", None)
        if isinstance(sql_type, Text):
            raise SqlServerReadinessError(
                f"La clé {name} cible une colonne Text non bornée."
            )
        if isinstance(sql_type, String):
            if sql_type.length is None:
                raise SqlServerReadinessError(
                    f"La clé {name} cible une chaîne sans longueur explicite."
                )
            total += int(sql_type.length)
        elif isinstance(sql_type, Integer):
            total += 4
        else:
            # Date/time/boolean/numeric keys in this schema are all far below the
            # string-heavy key limit; compilation below remains the authority.
            total += 32
    return total


def check_mssql_cascade_paths() -> ReadinessCheck:
    """Reject ON DELETE CASCADE graphs that SQL Server cannot create."""

    graph: dict[str, list[str]] = {}
    cascade_edges = 0
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if str(constraint.ondelete or "").upper() != "CASCADE":
                continue
            parent_tables = {
                element.column.table.name
                for element in constraint.elements
            }
            if len(parent_tables) != 1:
                raise SqlServerReadinessError(
                    f"FK cascade composite ambiguë sur {table.name}: {constraint.name or '<unnamed>'}"
                )
            parent = next(iter(parent_tables))
            graph.setdefault(parent, []).append(table.name)
            cascade_edges += 1

    for source in graph:
        path_counts: dict[str, int] = {}
        stack: list[tuple[str, tuple[str, ...]]] = [(source, (source,))]
        while stack:
            node, path = stack.pop()
            for target in graph.get(node, ()):
                if target in path:
                    cycle = " -> ".join((*path, target))
                    raise SqlServerReadinessError(
                        f"Cycle ON DELETE CASCADE incompatible SQL Server: {cycle}"
                    )
                path_counts[target] = path_counts.get(target, 0) + 1
                if path_counts[target] > 1:
                    raise SqlServerReadinessError(
                        "Chemins ON DELETE CASCADE multiples incompatibles SQL Server: "
                        f"{source} atteint {target} par plus d'un chemin."
                    )
                stack.append((target, (*path, target)))

    return ReadinessCheck(
        "mssql_cascade_paths",
        "ok",
        f"{cascade_edges} relations ON DELETE CASCADE sans cycle ni chemin multiple",
    )


def check_mssql_boolean_predicates() -> ReadinessCheck:
    """Reject boolean IS predicates that compile to invalid MSSQL IS 1 / IS 0."""

    forbidden = (
        ".is_(True)",
        ".is_(False)",
        ".is_(true())",
        ".is_(false())",
        ".is_(sa.true())",
        ".is_(sa.false())",
        ".is_(1)",
        ".is_(0)",
    )
    offenders: list[str] = []
    sql_root = ROOT / "app" / "infrastructure" / "sql"
    for path in sorted(sql_root.glob("*.py")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if any(token in line for token in forbidden):
                offenders.append(f"{path.relative_to(ROOT)}:{line_number}")

    if offenders:
        raise SqlServerReadinessError(
            "Prédicats booléens .is_(true/false) incompatibles MSSQL: "
            + ", ".join(offenders)
        )

    return ReadinessCheck(
        "mssql_boolean_predicates",
        "ok",
        "aucun prédicat booléen susceptible de compiler en IS 1 / IS 0",
    )


def check_mssql_schema_compilation() -> ReadinessCheck:
    """Compile all tables/indexes and enforce conservative SQL Server invariants."""

    dialect = mssql.dialect()
    compiled = 0
    max_identifier = 0
    max_index_key = 0
    for table in Base.metadata.sorted_tables:
        _validate_identifier(table.name, kind="table")
        max_identifier = max(max_identifier, len(table.name))
        sql = str(CreateTable(table).compile(dialect=dialect))
        if not sql.strip():
            raise SqlServerReadinessError(f"DDL vide pour la table {table.name}.")
        compiled += 1

        for column in table.columns:
            _validate_identifier(column.name, kind=f"colonne {table.name}")
            max_identifier = max(max_identifier, len(column.name))
            if column.primary_key and isinstance(column.type, Integer):
                raise SqlServerReadinessError(
                    f"{table.name}.{column.name} dépend d'un PK entier/autoincrement; "
                    "le schéma V2 doit rester à identifiants applicatifs portables."
                )

        for constraint in table.constraints:
            _validate_identifier(constraint.name, kind=f"contrainte {table.name}")
            if constraint.name:
                max_identifier = max(max_identifier, len(constraint.name))
            if isinstance(constraint, UniqueConstraint):
                key_bytes = _estimated_key_bytes(
                    tuple(constraint.columns),
                    name=constraint.name or f"unique:{table.name}",
                )
                max_index_key = max(max_index_key, key_bytes)
                if key_bytes > MSSQL_CONSERVATIVE_INDEX_KEY_BYTES:
                    raise SqlServerReadinessError(
                        f"La contrainte {constraint.name} estime une clé de {key_bytes} octets, "
                        f"au-dessus du budget conservateur {MSSQL_CONSERVATIVE_INDEX_KEY_BYTES}."
                    )

        for index in table.indexes:
            _validate_identifier(index.name, kind=f"index {table.name}")
            if index.name:
                max_identifier = max(max_identifier, len(index.name))
            key_bytes = _estimated_key_bytes(index.expressions, name=index.name or f"index:{table.name}")
            max_index_key = max(max_index_key, key_bytes)
            if key_bytes > MSSQL_CONSERVATIVE_INDEX_KEY_BYTES:
                raise SqlServerReadinessError(
                    f"L'index {index.name} estime une clé de {key_bytes} octets, "
                    f"au-dessus du budget conservateur {MSSQL_CONSERVATIVE_INDEX_KEY_BYTES}."
                )
            index_sql = str(CreateIndex(index).compile(dialect=dialect))
            if not index_sql.strip():
                raise SqlServerReadinessError(f"DDL vide pour l'index {index.name}.")
            compiled += 1

    return ReadinessCheck(
        "mssql_schema_compilation",
        "ok",
        (
            f"{compiled} objets table/index compilés; identifiant max={max_identifier}; "
            f"clé d'index estimée max={max_index_key} octets"
        ),
    )


def _critical_statements():
    start = bindparam("start_date", type_=Project.__table__.c.created_at.type)
    # Date parameters use an explicit Date-bearing mapped column in the actual
    # representative statements below; the first bind only exercises timestamp binds.
    yield "projects", select(Project).where(Project.status == "Actif").order_by(Project.number)
    yield "resources", (
        select(Resource)
        .where(Resource.active == true())
        .order_by(Resource.sort_order, Resource.name)
    )
    yield "demands", (
        select(WorkforceRequest, Project, Resource)
        .join(Project, WorkforceRequest.project_id == Project.id)
        .outerjoin(Resource, WorkforceRequest.proposed_resource_id == Resource.id)
        .order_by(WorkforceRequest.desired_start, WorkforceRequest.id)
    )
    yield "segments", (
        select(ResourceRequirement, Project, WorkforceRequest, Resource)
        .join(Project, ResourceRequirement.project_id == Project.id)
        .outerjoin(
            WorkforceRequest,
            ResourceRequirement.workforce_request_id == WorkforceRequest.id,
        )
        .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
        .where(
            ResourceRequirement.end_date >= bindparam(
                "window_start", type_=ResourceRequirement.__table__.c.start_date.type
            ),
            ResourceRequirement.start_date <= bindparam(
                "window_end", type_=ResourceRequirement.__table__.c.end_date.type
            ),
        )
        .order_by(ResourceRequirement.start_date, ResourceRequirement.id)
    )
    yield "shifts", (
        select(Shift, ResourceRequirement, Resource)
        .join(ResourceRequirement, Shift.resource_requirement_id == ResourceRequirement.id)
        .join(Resource, Shift.resource_id == Resource.id)
        .where(
            Shift.work_date >= bindparam(
                "shift_start", type_=Shift.__table__.c.work_date.type
            ),
            Shift.work_date <= bindparam(
                "shift_end", type_=Shift.__table__.c.work_date.type
            ),
        )
        .order_by(Shift.work_date, Shift.id)
    )
    yield "asset_planning_scoped_requirements", (
        select(AssetRequirement)
        .where(
            AssetRequirement.status != "Annulé",
            AssetRequirement.start_date <= bindparam(
                "asset_window_end",
                type_=AssetRequirement.__table__.c.end_date.type,
            ),
            AssetRequirement.end_date >= bindparam(
                "asset_window_start",
                type_=AssetRequirement.__table__.c.start_date.type,
            ),
            or_(
                AssetRequirement.project_id == bindparam("asset_project_id"),
                (
                    (AssetRequirement.origin == "RESOURCE_PERIOD")
                    & AssetRequirement.project_id.is_(None)
                    & (
                        AssetRequirement.context_resource_id
                        == bindparam("asset_context_resource_id")
                    )
                ),
            ),
        )
        .order_by(AssetRequirement.start_date, AssetRequirement.id)
    )
    yield "asset_global_occupancy", (
        select(AssetAllocation, AssetRequirement, Asset)
        .join(
            AssetRequirement,
            AssetAllocation.asset_requirement_id == AssetRequirement.id,
        )
        .join(Asset, AssetAllocation.asset_id == Asset.id)
        .where(
            AssetAllocation.start_date <= bindparam(
                "asset_occupancy_end",
                type_=AssetAllocation.__table__.c.end_date.type,
            ),
            AssetAllocation.end_date >= bindparam(
                "asset_occupancy_start",
                type_=AssetAllocation.__table__.c.start_date.type,
            ),
        )
        .order_by(AssetAllocation.asset_id, AssetAllocation.start_date)
    )
    yield "history", (
        select(WorkforceRequestHistory)
        .where(WorkforceRequestHistory.workforce_request_id == bindparam("request_id"))
        .order_by(
            WorkforceRequestHistory.occurred_at.desc(),
            WorkforceRequestHistory.id.desc(),
        )
    )
    yield "project_insert", insert(Project).values(
        id=bindparam("id"),
        number=bindparam("number"),
        name=bindparam("name"),
        status=bindparam("status"),
    )
    yield "resource_update", (
        update(Resource)
        .where(Resource.id == bindparam("resource_id"))
        .values(active=bindparam("active"))
    )
    yield "medium_term_budget_tasks", (
        select(TaskCatalogEntry)
        .where(TaskCatalogEntry.project_number == bindparam("medium_term_project_number"))
        .order_by(TaskCatalogEntry.task_code, TaskCatalogEntry.id)
    )
    yield "medium_term_budget_work_packages", (
        select(WorkPackage)
        .where(WorkPackage.project_id == bindparam("medium_term_project_id"))
        .order_by(WorkPackage.start_date, WorkPackage.name, WorkPackage.id)
    )
    yield "medium_term_load_intervals", (
        select(WorkPackageLoadInterval)
        .where(
            WorkPackageLoadInterval.work_package_id == bindparam("interval_work_package_id")
        )
        .order_by(
            WorkPackageLoadInterval.start_date,
            WorkPackageLoadInterval.end_date,
            WorkPackageLoadInterval.id,
        )
    )
    yield "medium_term_load_interval_delete", (
        delete(WorkPackageLoadInterval).where(
            WorkPackageLoadInterval.work_package_id == bindparam("interval_delete_work_package_id")
        )
    )
    yield "medium_term_weekly_loads", (
        select(WorkPackageWeeklyLoad)
        .where(
            WorkPackageWeeklyLoad.work_package_id == bindparam("weekly_work_package_id")
        )
        .order_by(WorkPackageWeeklyLoad.week_start)
    )
    yield "medium_term_weekly_load_delete", (
        delete(WorkPackageWeeklyLoad).where(
            WorkPackageWeeklyLoad.work_package_id == bindparam("weekly_delete_work_package_id")
        )
    )
    yield "work_package_weekly_load_cas", (
        update(WorkPackage)
        .where(
            WorkPackage.id == bindparam("weekly_cas_work_package_id"),
            WorkPackage.version == bindparam("weekly_expected_version"),
        )
        .values(
            weekly_load_origin=bindparam("weekly_load_origin"),
            version=WorkPackage.version + 1,
        )
    )
    yield "work_package_dependency_guard", (
        update(WorkPackage)
        .where(WorkPackage.id == bindparam("work_package_id"))
        .values(
            version=WorkPackage.version,
            updated_at=WorkPackage.updated_at,
        )
    )
    yield "work_package_version_cas", (
        update(WorkPackage)
        .where(
            WorkPackage.id == bindparam("work_package_id"),
            WorkPackage.version == bindparam("expected_version"),
        )
        .values(version=WorkPackage.version + 1)
    )
    yield "project_co_manager_version_cas", (
        update(Project)
        .where(
            Project.id == bindparam("project_co_manager_project_id"),
            Project.co_managers_version
            == bindparam("project_co_manager_expected_version"),
        )
        .values(co_managers_version=Project.co_managers_version + 1)
    )
    # Prevent accidental removal of timestamp compilation coverage.
    yield "timestamp_bind", select(Project.id).where(Project.created_at >= start)


def check_mssql_query_compilation() -> ReadinessCheck:
    dialect = mssql.dialect()
    names: list[str] = []
    for name, statement in _critical_statements():
        try:
            sql = str(statement.compile(dialect=dialect))
        except Exception as exc:  # pragma: no cover - exact compiler errors vary by SQLAlchemy
            raise SqlServerReadinessError(
                f"La requête critique {name} ne compile pas pour MSSQL: {type(exc).__name__}"
            ) from exc
        if not sql.strip():
            raise SqlServerReadinessError(f"La requête critique {name} compile en SQL vide.")
        normalized_sql = " ".join(sql.upper().split())
        if " IS 1" in normalized_sql or " IS 0" in normalized_sql:
            raise SqlServerReadinessError(
                f"La requête critique {name} compile un prédicat booléen invalide pour MSSQL: "
                f"{normalized_sql}"
            )
        names.append(name)
    return ReadinessCheck(
        "mssql_critical_query_compilation",
        "ok",
        f"{len(names)} requêtes compilées: {', '.join(names)}",
    )


def run_checks() -> list[ReadinessCheck]:
    return [
        check_sqlite_migrations(),
        check_mssql_offline_migrations(),
        check_mssql_cascade_paths(),
        check_mssql_boolean_predicates(),
        check_mssql_schema_compilation(),
        check_mssql_query_compilation(),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Préflight local SQL Server: migrations, DDL et requêtes MSSQL sans "
            "installer pyodbc ni contacter un serveur."
        )
    )
    parser.add_argument("--json", action="store_true", help="Émettre un rapport JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        checks = run_checks()
    except SqlServerReadinessError as exc:
        if args.json:
            print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"SQL Server readiness: FAIL\n{exc}", file=sys.stderr)
        return 1

    payload = {
        "status": "ok",
        "alembic_head": _alembic_head(),
        "driver_installed_or_required": False,
        "checks": [check.to_dict() for check in checks],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("SQL Server readiness: PASS")
        for check in checks:
            print(f"- {check.name}: {check.details}")
        print("- pyodbc/ODBC: non requis pour cette validation offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
