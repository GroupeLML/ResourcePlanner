from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import perf_counter
from typing import Any, Iterable
from urllib.parse import quote

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, Index, Table, UniqueConstraint, create_engine, func, inspect, select, text
from sqlalchemy.engine import Connection


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.sql import Base, create_sql_engine  # noqa: E402


DATABASE_ENV = "RESOURCEPLANNER_DATABASE_URL"
DEFAULT_REPORT = ROOT / "sqlite_v2_cutover_report.json"
POLICY_VERSION = "492-v3"
DEV_IDENTITY_ISSUER = "urn:resourceplanner:dev"

KEEP = "KEEP"
REBUILD = "REBUILD"
DROP = "DROP"

# The real pre-go-live SQLite captured for ENV-492 is still on the last
# pre-baseline identity revision. Compatibility is deliberately explicit and
# revision-scoped so any other schema drift continues to fail closed.
SOURCE_COMPATIBILITY_PROFILES: dict[str, dict[str, Any]] = {
    "0048_identity_admin_audit": {
        "missing_tables": {
            "auth_security_audit",
            "break_glass_credentials",
            "work_package_audit",
            "work_package_weekly_loads",
            "work_package_load_intervals",
            "asset_type_approval_scope_mappings",
            "asset_approvers",
            "acumatica_project_task_sync_runs",
            "acumatica_project_task_sync_project_results",
            "project_co_managers",
            "project_manager_audit",
        },
        "missing_columns": {
            "auth_sessions": {
                "auth_mode": None,
            },
            "projects": {
                "co_managers_version": 1,
                "operational_responsible_override_contact_id": None,
                "operational_responsible_override_version": 1,
            },
            "resource_requirements": {
                "operational_responsible_override_contact_id": None,
                "captured_operational_responsible_contact_id": None,
                "captured_operational_responsible_source_type": None,
                "captured_operational_responsible_source_entity_id": None,
                "captured_operational_responsible_status": None,
                "captured_operational_responsible_diagnostics": None,
                "operational_responsibility_context_provenance": "LEGACY_UNKNOWN",
                "operational_responsibility_context_version": None,
            },
            "shifts": {
                "operational_responsible_override_contact_id": None,
            },
            "work_packages": {
                "task_catalog_item_id": None,
                "version": 1,
                "weekly_load_origin": None,
                "resource_class_code": None,
                "terminal_status": None,
            },
            "approval_requirements": {
                "asset_type_id": None,
                "proposed_asset_id": None,
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class TablePolicy:
    classification: str
    rationale: str
    post_action: str | None = None


def _keep(rationale: str, *, post_action: str | None = None) -> TablePolicy:
    return TablePolicy(KEEP, rationale, post_action)


def _rebuild(rationale: str) -> TablePolicy:
    return TablePolicy(REBUILD, rationale)


def _drop(rationale: str) -> TablePolicy:
    return TablePolicy(DROP, rationale)


# Deliberately exhaustive. A schema change must fail closed until #492 is revalidated.
TABLE_POLICIES: dict[str, TablePolicy] = {
    "approval_decisions": _keep("Décisions d'approbation métier V2."),
    "approval_requirement_approvers": _keep("Snapshot des approbateurs admissibles."),
    "approval_requirements": _keep("Exigences d'approbation par ligne."),
    "approval_scopes": _keep("Configuration locale des périmètres d'approbation."),
    "approval_scope_approvers": _keep("Configuration locale des approbateurs."),
    "asset_type_approval_scope_mappings": _keep("Routage local type d'actif → périmètre."),
    "resource_class_approval_scope_mappings": _keep("Routage local classe → périmètre."),
    "task_approval_scope_mappings": _keep("Overrides locaux tâche → périmètre."),
    "asset_types": _keep("Catalogue local des types d'actifs."),
    "asset_type_competencies": _keep("Qualifications requises par type d'actif."),
    "assets": _keep("Catalogue local des actifs réservables."),
    "asset_unavailability": _keep("Indisponibilités d'actifs saisies localement."),
    "asset_requirements": _keep("Besoins d'actifs matérialisés."),
    "asset_allocations": _keep("Réservations réelles d'actifs."),
    "asset_approvers": _keep("Autorités locales spécifiques par unité d'actif."),
    "app_users": _keep(
        "Comptes, rôles et activation locaux; les identités dev sont filtrées."
    ),
    "erp_user_directory": _keep(
        "Conserve UserID et configuration locale d'activation/rôles.",
        post_action="RESYNC_ACUMATICA_RP_USERS",
    ),
    "identity_admin_audit": _keep("Audit durable des mutations d'identité."),
    "business_contacts": _keep("Contacts métier locaux et identités stables référencées."),
    "auth_login_transactions": _drop("Nonce/PKCE/login temporaire; jamais transféré."),
    "break_glass_credentials": _drop(
        "Credential d'authentification local de secours; les credentials/secrets ne sont jamais transférés et le bootstrap production recrée explicitement l'accès."
    ),
    "auth_security_audit": _drop(
        "Audit de sécurité pré-go-live lié aux credentials locaux exclus; l'audit production repart après bootstrap sur la cible."
    ),
    "auth_sessions": _drop("Sessions, hashes de token et CSRF temporaires."),
    "command_idempotency_receipts": _drop(
        "Reçus techniques de replay pré-go-live; ne sont pas des faits métier."
    ),
    "communication_batches": _keep("Lots de communication V2 et état métier."),
    "communication_contacts": _keep("Configuration locale des destinataires."),
    "communication_messages": _keep("Messages préparés/validés du workflow V2."),
    "communication_snapshot_lines": _keep("Snapshot métier associé aux communications."),
    "communication_deliveries": _keep("Historique de livraison des communications."),
    "competencies": _keep("Catalogue local de compétences."),
    "delivery_plans": _keep("Plans Delivery réellement utilisés."),
    "delivery_items": _keep("Epics/Stories Delivery."),
    "delivery_change_history": _keep("Historique Delivery durable."),
    "planning_change_history": _keep("Audit métier Planning V2."),
    "planning_mutation_state": _rebuild(
        "Singleton technique de version globale créé par la baseline."
    ),
    "projects": _keep(
        "Préserve les UUID/FK et champs locaux; les attributs ERP sont réconciliés après.",
        post_action="RESYNC_ACUMATICA_PROJECTS",
    ),
    "project_co_managers": _keep(
        "Nominations locales durables des co-chargés RP; les associations doivent survivre au cutover."
    ),
    "project_manager_audit": _keep(
        "Audit durable des nominations de co-chargés et de leur version CAS."
    ),
    "request_approval_cycles": _keep("Cycles d'approbation persistants."),
    "request_approval_references": _keep("Références approuvées historisées."),
    "request_approval_revisions": _keep("Révisions approuvées historisées."),
    "request_operational_states": _keep("Choix opérationnels locaux des demandes."),
    "request_lines": _keep("Lignes métier des demandes V2."),
    "request_line_competencies": _keep("Compétences demandées par ligne."),
    "resource_requirement_competencies": _keep("Snapshot des compétences matérialisées."),
    "resource_class_configs": _keep("Configuration locale des classes de ressources."),
    "project_task_class_overrides": _keep("Overrides locaux projet/tâche."),
    "resources": _keep(
        "Préserve UUID, activation et configuration locale; état ERP réconcilié après.",
        post_action="RESYNC_ACUMATICA_RP_EMPLOYEES",
    ),
    "work_packages": _keep("WorkPackages planifiés localement."),
    "work_package_weekly_loads": _keep(
        "Intentions hebdomadaires historiques WorkPackage; conservées pour la migration ADR-019."
    ),
    "work_package_load_intervals": _keep(
        "Intentions explicites datées WorkPackage canoniques selon ADR-019."
    ),
    "work_package_audit": _keep("Audit durable des mutations WorkPackage et de leur version CAS."),
    "workforce_requests": _keep("Demandes métier V2."),
    "workforce_request_competencies": _keep("Compétences historiques des demandes."),
    "workforce_request_history": _keep("Historique métier des demandes."),
    "workforce_request_periods": _keep("Périodes proposées/soumises."),
    "workforce_request_period_selections": _keep("Sélections de périodes."),
    "workforce_request_period_requirements": _keep("Liens période → besoin matérialisé."),
    "resource_availability_rules": _keep("Disponibilités et horaires locaux."),
    "resource_competencies": _keep("Compétences locales des ressources."),
    "resource_requirements": _keep("Besoins/budgets matérialisés."),
    "smtp_configuration": _drop(
        "Configuration contenant un credential chiffré; reconfiguration explicite requise."
    ),
    "smtp_configuration_audit": _keep(
        "Audit SMTP sans données sensibles; type d'événement et noms de champs modifiés."
    ),
    "shifts": _keep("Affectations réelles et décisions Planning."),
    "task_catalog_items": _keep(
        "Préserve les UUID référencés; valeurs ERP/budgets resynchronisées ensuite.",
        post_action="RESYNC_ACUMATICA_PROJECT_TASKS",
    ),
    "task_catalog_project_sync_state": _rebuild(
        "Télémétrie/curseur de synchronisation reconstructible."
    ),
    "acumatica_project_task_sync_runs": _rebuild(
        "État technique de run global; ne pas transférer un traitement pré-cutover."
    ),
    "acumatica_project_task_sync_project_results": _rebuild(
        "Résultats techniques rattachés aux runs globaux; reconstruits par les prochains runs."
    ),
    "task_class_standards": _keep("Configuration locale des standards tâche → classe."),
}


RELATIONSHIP_CONTROLS: dict[str, tuple[str, ...]] = {
    "projects": ("id",),
    "project_co_managers": (
        "project_id",
        "business_contact_id",
        "created_by_user_id",
    ),
    "project_manager_audit": (
        "id",
        "project_id",
        "actor_user_id",
    ),
    "work_packages": ("id", "project_id"),
    "work_package_weekly_loads": ("work_package_id", "week_start"),
    "work_package_load_intervals": ("id", "work_package_id"),
    "workforce_requests": ("id", "project_id", "work_package_id"),
    "request_lines": ("id", "workforce_request_id", "work_package_id"),
    "resource_requirements": (
        "id",
        "project_id",
        "workforce_request_id",
        "source_request_line_id",
    ),
    "shifts": ("id", "resource_requirement_id", "resource_id"),
    "delivery_plans": ("id", "work_package_id"),
    "delivery_items": ("id", "delivery_plan_id", "parent_id"),
    "asset_requirements": (
        "id",
        "project_id",
        "workforce_request_id",
        "source_request_line_id",
        "asset_type_id",
    ),
    "asset_allocations": ("id", "asset_requirement_id", "asset_id", "operator_resource_id"),
    "asset_type_approval_scope_mappings": ("asset_type_id", "approval_scope_id"),
    "asset_approvers": ("asset_id", "app_user_id"),
}


class CutoverBlocked(RuntimeError):
    pass


class CutoverTransferError(RuntimeError):
    pass


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_source_info(path: Path) -> dict[str, Any]:
    return {
        "kind": "sqlite-v2",
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
    }


def _safe_target_info(engine: Engine) -> dict[str, Any]:
    return {
        "dialect": engine.dialect.name,
        "driver": engine.dialect.driver,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _code_alembic_head() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise CutoverBlocked(
            "Le dépôt doit exposer exactement une tête Alembic avant le cutover; "
            f"trouvé: {', '.join(heads) or '<aucune>'}."
        )
    return heads[0]


def _assert_policy_complete() -> None:
    schema_tables = set(Base.metadata.tables)
    policy_tables = set(TABLE_POLICIES)
    missing = sorted(schema_tables - policy_tables)
    stale = sorted(policy_tables - schema_tables)
    if missing or stale:
        details = []
        if missing:
            details.append("non classées=" + ", ".join(missing))
        if stale:
            details.append("absentes du modèle=" + ", ".join(stale))
        raise CutoverBlocked(
            "La matrice #492 doit être revalidée contre le schéma courant: "
            + "; ".join(details)
        )


def create_readonly_sqlite_engine(source_path: str | Path) -> Engine:
    path = Path(source_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise CutoverBlocked(f"Source SQLite absente ou invalide: {path.name}")
    uri_path = quote(path.as_posix(), safe="/:")

    def _connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    engine = create_engine(
        "sqlite+pysqlite://",
        creator=_connect,
        future=True,
    )
    with engine.connect() as connection:
        query_only = int(connection.exec_driver_sql("PRAGMA query_only").scalar_one())
        if query_only != 1:
            engine.dispose()
            raise CutoverBlocked("La source SQLite n'est pas réellement ouverte en lecture seule.")
    return engine


def _active_wal(path: Path) -> dict[str, Any] | None:
    wal = Path(str(path) + "-wal")
    if not wal.exists():
        return None
    size = wal.stat().st_size
    if size <= 0:
        return None
    return {
        "code": "active_sqlite_wal",
        "severity": "BLOCKING",
        "message": (
            "Un fichier WAL SQLite non vide est présent. Geler/checkpointer la source "
            "avant sauvegarde et cutover afin que le backup soit autonome."
        ),
        "size_bytes": size,
    }


def _verify_backup(source: Path, backup: Path | None, source_hash: str) -> dict[str, Any]:
    if backup is None:
        return {"verified": False, "reason": "not_provided"}
    candidate = backup.expanduser().resolve()
    if candidate == source:
        raise CutoverBlocked("Le backup SQLite doit être distinct du fichier source.")
    if not candidate.exists() or not candidate.is_file():
        raise CutoverBlocked("Le backup SQLite requis est absent.")
    if candidate.stat().st_size <= 0:
        raise CutoverBlocked("Le backup SQLite est vide.")
    backup_hash = _sha256(candidate)
    if backup_hash != source_hash:
        raise CutoverBlocked(
            "Le backup SQLite ne correspond pas octet pour octet à la source gelée."
        )
    return {
        "verified": True,
        "file_name": candidate.name,
        "size_bytes": candidate.stat().st_size,
        "sha256": backup_hash,
    }


def _database_revision(engine: Engine) -> str | None:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        values = list(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
    if len(values) != 1:
        return None
    return str(values[0])


def _detected_tables(engine: Engine) -> list[str]:
    return sorted(inspect(engine).get_table_names())


def _schema_compatibility(engine: Engine, *, label: str) -> list[dict[str, Any]]:
    inspector = inspect(engine)
    detected = set(inspector.get_table_names())
    anomalies: list[dict[str, Any]] = []
    expected_database_tables = set(Base.metadata.tables) | {"alembic_version"}
    unexpected_tables = sorted(
        table_name
        for table_name in detected - expected_database_tables
        if not table_name.startswith("sqlite_")
    )
    for table_name in unexpected_tables:
        anomalies.append(
            {
                "code": f"{label}_unmapped_table",
                "severity": "BLOCKING",
                "table": table_name,
                "message": (
                    f"{label}: table non classifiée par #492; mapping/baseline à revalider."
                ),
            }
        )
    for table_name, model_table in Base.metadata.tables.items():
        if table_name not in detected:
            anomalies.append(
                {
                    "code": f"{label}_missing_table",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "message": f"{label}: table absente du schéma.",
                }
            )
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in model_table.columns}
        missing_columns = sorted(expected_columns - actual_columns)
        extra_columns = sorted(actual_columns - expected_columns)
        if missing_columns:
            anomalies.append(
                {
                    "code": f"{label}_missing_columns",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "columns": missing_columns,
                    "message": f"{label}: colonnes attendues absentes.",
                }
            )
        if extra_columns:
            anomalies.append(
                {
                    "code": f"{label}_extra_columns",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "columns": extra_columns,
                    "message": (
                        f"{label}: colonnes non connues du modèle courant; mapping #492 à revalider."
                    ),
                }
            )
    return anomalies


def _source_schema_compatibility(
    engine: Engine,
    *,
    source_revision: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inspector = inspect(engine)
    detected = set(inspector.get_table_names())
    profile = SOURCE_COMPATIBILITY_PROFILES.get(source_revision or "", {})
    allowed_missing_tables = set(profile.get("missing_tables", set()))
    allowed_missing_columns = profile.get("missing_columns", {})

    anomalies: list[dict[str, Any]] = []
    adaptations: list[dict[str, Any]] = []
    expected_database_tables = set(Base.metadata.tables) | {"alembic_version"}

    for table_name in sorted(
        name
        for name in detected - expected_database_tables
        if not name.startswith("sqlite_")
    ):
        anomalies.append(
            {
                "code": "source_unmapped_table",
                "severity": "BLOCKING",
                "table": table_name,
                "message": (
                    "source: table non classifiée par #492; mapping/baseline à revalider."
                ),
            }
        )

    for table_name, model_table in Base.metadata.tables.items():
        if table_name not in detected:
            if table_name in allowed_missing_tables:
                adaptations.append(
                    {
                        "kind": "missing_table_as_empty",
                        "table": table_name,
                        "source_revision": source_revision,
                    }
                )
            else:
                anomalies.append(
                    {
                        "code": "source_missing_table",
                        "severity": "BLOCKING",
                        "table": table_name,
                        "message": "source: table absente du schéma.",
                    }
                )
            continue

        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in model_table.columns}
        missing_columns = expected_columns - actual_columns
        defaults = allowed_missing_columns.get(table_name, {})
        accepted_missing = sorted(missing_columns & set(defaults))
        blocking_missing = sorted(missing_columns - set(defaults))
        extra_columns = sorted(actual_columns - expected_columns)

        if accepted_missing:
            adaptations.append(
                {
                    "kind": "missing_columns_with_defaults",
                    "table": table_name,
                    "columns": accepted_missing,
                    "defaults": {
                        column: defaults[column]
                        for column in accepted_missing
                    },
                    "source_revision": source_revision,
                }
            )
        if blocking_missing:
            anomalies.append(
                {
                    "code": "source_missing_columns",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "columns": blocking_missing,
                    "message": "source: colonnes attendues absentes.",
                }
            )
        if extra_columns:
            anomalies.append(
                {
                    "code": "source_extra_columns",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "columns": extra_columns,
                    "message": (
                        "source: colonnes non connues du modèle courant; mapping #492 à revalider."
                    ),
                }
            )

    return anomalies, adaptations


def _primary_key_tuple(table: Table, row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column.name] for column in table.primary_key.columns)


def _identity_columns(table: Table) -> set[str]:
    columns = {column.name for column in table.primary_key.columns}
    for column in table.columns:
        if column.name == "id" or column.name.endswith("_id") or column.name.endswith("_number"):
            columns.add(column.name)
    for fk in table.foreign_key_constraints:
        columns.update(element.parent.name for element in fk.elements)
    explicit = {
        "number",
        "code",
        "task_code",
        "project_number",
        "legacy_demand_number",
        "legacy_effort_id",
        "legacy_segment_id",
        "legacy_allocation_id",
        "external_id",
        "erp_external_id",
        "period_key",
        "recipient_id",
        "segment_id",
    }
    columns.update(name for name in explicit if name in table.c)
    return columns


def _initial_exclusion(table: Table, row: dict[str, Any]) -> str | None:
    if table.name == "app_users" and row.get("issuer") == DEV_IDENTITY_ISSUER:
        return "dev_identity_issuer"
    for name in _identity_columns(table):
        value = row.get(name)
        if isinstance(value, str) and value.strip().upper().startswith("DEMO-"):
            return f"demo_identifier:{name}"
    return None


def _referenced_key(
    fk,
    row: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[Any, ...], tuple[str, ...]]:
    child_columns = tuple(element.parent.name for element in fk.elements)
    parent_columns = tuple(element.column.name for element in fk.elements)
    values = tuple(row.get(name) for name in child_columns)
    return child_columns, values, parent_columns


def _prepare_keep_rows(
    source_rows: dict[str, list[dict[str, Any]]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    states: dict[str, list[dict[str, Any]]] = {}
    exclusions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table_name, rows in source_rows.items():
        table = Base.metadata.tables[table_name]
        states[table_name] = []
        for row in rows:
            reason = _initial_exclusion(table, row)
            state = {"row": dict(row), "excluded": reason}
            states[table_name].append(state)
            if reason:
                exclusions[table_name].append(
                    {
                        "pk": _primary_key_tuple(table, row),
                        "reason": reason,
                    }
                )

    missing_dependencies: list[dict[str, Any]] = []
    sanitizations: list[dict[str, Any]] = []
    seen_missing: set[tuple[Any, ...]] = set()
    changed = True
    while changed:
        changed = False
        for table_name, table_states in states.items():
            table = Base.metadata.tables[table_name]
            for state in table_states:
                if state["excluded"]:
                    continue
                row = state["row"]
                for fk in table.foreign_key_constraints:
                    parent_name = next(iter(fk.elements)).column.table.name
                    if TABLE_POLICIES[parent_name].classification != KEEP:
                        continue
                    child_columns, values, parent_columns = _referenced_key(fk, row)
                    if all(value is None for value in values):
                        continue
                    parent_states = states[parent_name]
                    parent_matches = [
                        parent_state
                        for parent_state in parent_states
                        if tuple(
                            parent_state["row"].get(column)
                            for column in parent_columns
                        )
                        == values
                    ]
                    if not parent_matches:
                        key = (
                            table_name,
                            _primary_key_tuple(table, row),
                            parent_name,
                            parent_columns,
                            values,
                        )
                        if key not in seen_missing:
                            seen_missing.add(key)
                            missing_dependencies.append(
                                {
                                    "table": table_name,
                                    "pk": _primary_key_tuple(table, row),
                                    "parent_table": parent_name,
                                    "parent_columns": parent_columns,
                                    "parent_values": values,
                                    "code": "missing_keep_dependency",
                                    "severity": "BLOCKING",
                                }
                            )
                        continue
                    if any(not parent_state["excluded"] for parent_state in parent_matches):
                        continue

                    nullable = all(table.c[name].nullable for name in child_columns)
                    if nullable:
                        old_values = tuple(row.get(name) for name in child_columns)
                        if any(value is not None for value in old_values):
                            for name in child_columns:
                                row[name] = None
                            sanitizations.append(
                                {
                                    "table": table_name,
                                    "pk": _primary_key_tuple(table, row),
                                    "columns": child_columns,
                                    "reason": f"excluded_parent:{parent_name}",
                                }
                            )
                            changed = True
                    else:
                        reason = f"required_excluded_parent:{parent_name}"
                        state["excluded"] = reason
                        exclusions[table_name].append(
                            {
                                "pk": _primary_key_tuple(table, row),
                                "reason": reason,
                            }
                        )
                        changed = True
                        break

    eligible: dict[str, list[dict[str, Any]]] = {}
    for table_name, table_states in states.items():
        eligible[table_name] = [
            state["row"] for state in table_states if not state["excluded"]
        ]
    return eligible, dict(exclusions), missing_dependencies, sanitizations


def _unique_key_specs(table: Table) -> tuple[list[tuple[str, tuple[str, ...]]], list[str]]:
    specs: list[tuple[str, tuple[str, ...]]] = []
    deferred: list[str] = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            specs.append(
                (
                    constraint.name or f"unique:{table.name}",
                    tuple(column.name for column in constraint.columns),
                )
            )
    for index in table.indexes:
        if not isinstance(index, Index) or not index.unique:
            continue
        partial = any(
            index.dialect_options[dialect].get("where") is not None
            for dialect in ("sqlite", "postgresql", "mssql")
        )
        if partial:
            deferred.append(index.name or f"partial:{table.name}")
            continue
        columns = tuple(
            expression.name
            for expression in index.expressions
            if getattr(expression, "name", None)
        )
        if columns:
            specs.append((index.name or f"unique-index:{table.name}", columns))
    return specs, deferred


def _source_uniqueness_anomalies(
    eligible: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    anomalies: list[dict[str, Any]] = []
    deferred: list[str] = []
    for table_name, rows in eligible.items():
        table = Base.metadata.tables[table_name]
        specs, partial = _unique_key_specs(table)
        deferred.extend(f"{table_name}.{name}" for name in partial)
        pk_columns = tuple(column.name for column in table.primary_key.columns)
        if pk_columns:
            specs.insert(0, (f"pk:{table_name}", pk_columns))
        for name, columns in specs:
            seen: dict[tuple[Any, ...], int] = {}
            for row in rows:
                key = tuple(row.get(column) for column in columns)
                if any(value is None for value in key):
                    continue
                seen[key] = seen.get(key, 0) + 1
            duplicates = [key for key, count in seen.items() if count > 1]
            if duplicates:
                anomalies.append(
                    {
                        "code": "source_unique_collision",
                        "severity": "BLOCKING",
                        "table": table_name,
                        "constraint": name,
                        "columns": columns,
                        "collision_count": len(duplicates),
                    }
                )
    return anomalies, deferred


def _target_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table_name, table in Base.metadata.tables.items():
            counts[table_name] = int(
                connection.scalar(select(func.count()).select_from(table)) or 0
            )
    return counts


def _target_precondition_anomalies(
    engine: Engine,
    target_counts: dict[str, int],
    eligible: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anomalies: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []

    for table_name, count in target_counts.items():
        policy = TABLE_POLICIES[table_name]
        if policy.classification == REBUILD:
            continue
        if count:
            anomalies.append(
                {
                    "code": "target_not_empty",
                    "severity": "BLOCKING",
                    "table": table_name,
                    "row_count": count,
                }
            )

    planning_count = target_counts.get("planning_mutation_state", 0)
    if planning_count != 1:
        anomalies.append(
            {
                "code": "baseline_planning_state_invalid",
                "severity": "BLOCKING",
                "table": "planning_mutation_state",
                "row_count": planning_count,
                "message": "La baseline doit créer exactement le singleton GLOBAL.",
            }
        )
    else:
        table = Base.metadata.tables["planning_mutation_state"]
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
        if len(rows) != 1 or rows[0].get("id") != "GLOBAL":
            anomalies.append(
                {
                    "code": "baseline_planning_state_invalid",
                    "severity": "BLOCKING",
                    "table": "planning_mutation_state",
                    "message": "Le singleton de baseline doit avoir id=GLOBAL.",
                }
            )

    for rebuild_table in (
        "task_catalog_project_sync_state",
        "acumatica_project_task_sync_runs",
        "acumatica_project_task_sync_project_results",
    ):
        if target_counts.get(rebuild_table, 0):
            anomalies.append(
                {
                    "code": "rebuild_table_not_clean",
                    "severity": "BLOCKING",
                    "table": rebuild_table,
                    "row_count": target_counts[rebuild_table],
                }
            )

    # Report concrete PK collisions even though clean-target mode already blocks.
    with engine.connect() as connection:
        for table_name, rows in eligible.items():
            if not rows or not target_counts.get(table_name):
                continue
            table = Base.metadata.tables[table_name]
            pk_columns = tuple(column.name for column in table.primary_key.columns)
            if not pk_columns:
                continue
            target_keys = {
                tuple(row[column] for column in pk_columns)
                for row in connection.execute(
                    select(*(table.c[column] for column in pk_columns))
                ).mappings()
            }
            source_keys = {
                tuple(row.get(column) for column in pk_columns)
                for row in rows
            }
            overlap = target_keys & source_keys
            if overlap:
                collisions.append(
                    {
                        "table": table_name,
                        "kind": "primary_key",
                        "collision_count": len(overlap),
                    }
                )
    return anomalies, collisions


def _transfer_order() -> list[str]:
    return [
        table.name
        for table in Base.metadata.sorted_tables
        if TABLE_POLICIES[table.name].classification == KEEP
    ]


def _table_report(
    source_counts: dict[str, int],
    eligible: dict[str, list[dict[str, Any]]],
    exclusions: dict[str, list[dict[str, Any]]],
    target_counts: dict[str, int],
) -> list[dict[str, Any]]:
    rows = []
    for table_name in Base.metadata.tables:
        policy = TABLE_POLICIES[table_name]
        source_count = source_counts.get(table_name, 0)
        eligible_count = (
            len(eligible.get(table_name, []))
            if policy.classification == KEEP
            else 0
        )
        rows.append(
            {
                "table": table_name,
                "classification": policy.classification,
                "rationale": policy.rationale,
                "post_action": policy.post_action,
                "source_rows": source_count,
                "eligible_rows": eligible_count,
                "excluded_rows": (
                    len(exclusions.get(table_name, []))
                    if policy.classification == KEEP
                    else source_count
                ),
                "target_rows_before": target_counts.get(table_name, 0),
            }
        )
    return rows


def _blocking(anomalies: Iterable[dict[str, Any]]) -> bool:
    return any(item.get("severity") == "BLOCKING" for item in anomalies)


def _insert_batches(
    connection: Connection,
    table: Table,
    rows: list[dict[str, Any]],
    *,
    batch_size: int = 500,
) -> None:
    for offset in range(0, len(rows), batch_size):
        connection.execute(table.insert(), rows[offset : offset + batch_size])


def _relationship_signature_from_rows(
    rows: list[dict[str, Any]],
    columns: tuple[str, ...],
) -> set[tuple[Any, ...]]:
    return {tuple(row.get(column) for column in columns) for row in rows}


def _validate_transferred_data(
    connection: Connection,
    eligible: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    count_controls: dict[str, dict[str, int]] = {}
    id_controls: dict[str, dict[str, int]] = {}
    relationship_controls: dict[str, dict[str, Any]] = {}

    for table_name in _transfer_order():
        table = Base.metadata.tables[table_name]
        expected = len(eligible.get(table_name, []))
        actual = int(
            connection.scalar(select(func.count()).select_from(table)) or 0
        )
        count_controls[table_name] = {"source_eligible": expected, "target": actual}
        if expected != actual:
            raise CutoverTransferError(
                f"Validation de compte échouée pour {table_name}: {expected} != {actual}."
            )

        pk_columns = tuple(column.name for column in table.primary_key.columns)
        source_ids = {
            tuple(row.get(column) for column in pk_columns)
            for row in eligible.get(table_name, [])
        }
        target_ids = {
            tuple(row[column] for column in pk_columns)
            for row in connection.execute(
                select(*(table.c[column] for column in pk_columns))
            ).mappings()
        }
        if source_ids != target_ids:
            raise CutoverTransferError(
                f"Validation des identifiants échouée pour {table_name}."
            )
        id_controls[table_name] = {
            "source_ids": len(source_ids),
            "target_ids": len(target_ids),
        }

    for table_name, columns in RELATIONSHIP_CONTROLS.items():
        table = Base.metadata.tables[table_name]
        source_signature = _relationship_signature_from_rows(
            eligible.get(table_name, []),
            columns,
        )
        target_signature = {
            tuple(row[column] for column in columns)
            for row in connection.execute(
                select(*(table.c[column] for column in columns))
            ).mappings()
        }
        if source_signature != target_signature:
            raise CutoverTransferError(
                f"Validation des liens métier échouée pour {table_name}."
            )
        relationship_controls[table_name] = {
            "columns": columns,
            "row_count": len(source_signature),
            "identical": True,
        }

    return {
        "counts": count_controls,
        "ids": id_controls,
        "business_relationships": relationship_controls,
        "foreign_keys": "enforced_by_target_constraints_and_preflight_dependency_check",
        "unique_constraints": "enforced_by_target_constraints_and_preflight_unique_check",
    }


def _copy_source_inventory(
    source_engine: Engine,
    *,
    source_revision: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    inspector = inspect(source_engine)
    detected = set(inspector.get_table_names())
    profile = SOURCE_COMPATIBILITY_PROFILES.get(source_revision or "", {})
    column_defaults = profile.get("missing_columns", {})

    source_rows: dict[str, list[dict[str, Any]]] = {}
    source_counts: dict[str, int] = {}

    with source_engine.connect() as connection:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in detected:
                source_counts[table_name] = 0
                if TABLE_POLICIES[table_name].classification == KEEP:
                    source_rows[table_name] = []
                continue

            source_counts[table_name] = int(
                connection.exec_driver_sql(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).scalar_one()
            )
            if TABLE_POLICIES[table_name].classification != KEEP:
                continue

            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            selected_columns = [
                column
                for column in table.columns
                if column.name in actual_columns
            ]
            rows = [
                dict(row)
                for row in connection.execute(
                    select(*selected_columns)
                ).mappings()
            ]
            defaults = column_defaults.get(table_name, {})
            for row in rows:
                for column_name, default_value in defaults.items():
                    row.setdefault(column_name, default_value)
            source_rows[table_name] = rows

    return source_rows, source_counts


def _summary(payload: dict[str, Any]) -> None:
    mode = payload.get("mode")
    print(f"Cutover SQLite V2 → SQL Server — {mode}")
    print(f"Statut: {payload.get('status')}")
    print(f"Source: {payload.get('source', {}).get('file_name', '<inconnue>')}")
    target = payload.get("target") or {}
    print(
        "Cible: "
        f"dialect={target.get('dialect', '<inconnu>')} "
        f"revision={target.get('revision', '<inconnue>')}"
    )
    print(
        "Anomalies bloquantes: "
        f"{sum(1 for row in payload.get('anomalies', []) if row.get('severity') == 'BLOCKING')}"
    )
    if payload.get("transaction"):
        print(f"Transaction: {payload['transaction'].get('status')}")
    print(f"Rapport: {payload.get('report_file')}")


def run_cutover(
    *,
    source_path: str | Path,
    target_database_url: str,
    report_path: str | Path,
    backup_path: str | Path | None = None,
    expected_target_revision: str | None = None,
    apply: bool = False,
    baseline_ready: bool = False,
    allow_test_target: bool = False,
    fail_after_table: str | None = None,
) -> int:
    started = perf_counter()
    report_file = Path(report_path).expanduser().resolve()
    source = Path(source_path).expanduser().resolve()
    backup = Path(backup_path) if backup_path is not None else None
    payload: dict[str, Any] = {
        "tool": "sqlite-v2-to-sqlserver",
        "policy_version": POLICY_VERSION,
        "mode": "apply" if apply else "dry-run",
        "started_at": _utc_iso(),
        "report_file": report_file.name,
        "status": "starting",
        "anomalies": [],
        "collisions": [],
        "missing_dependencies": [],
        "sanitizations": [],
    }
    source_engine: Engine | None = None
    target_engine: Engine | None = None

    try:
        _assert_policy_complete()
        if not source.exists() or not source.is_file():
            raise CutoverBlocked("Source SQLite absente ou invalide.")

        source_hash = _sha256(source)
        payload["source"] = {
            **_safe_source_info(source),
            "sha256": source_hash,
            "read_only": True,
        }
        wal_anomaly = _active_wal(source)
        if wal_anomaly:
            payload["anomalies"].append(wal_anomaly)

        backup_result = _verify_backup(source, backup, source_hash)
        payload["source"]["backup"] = backup_result
        if apply and not backup_result.get("verified"):
            raise CutoverBlocked(
                "Un backup SQLite distinct et vérifié est obligatoire avec --apply."
            )

        source_engine = create_readonly_sqlite_engine(source)
        source_revision = _database_revision(source_engine)
        payload["source"]["alembic_revision"] = source_revision
        payload["source"]["detected_tables"] = _detected_tables(source_engine)
        source_anomalies, compatibility_adaptations = _source_schema_compatibility(
            source_engine,
            source_revision=source_revision,
        )
        payload["anomalies"].extend(source_anomalies)
        payload["source"]["compatibility_profile"] = (
            source_revision
            if source_revision in SOURCE_COMPATIBILITY_PROFILES
            else None
        )
        payload["source"]["compatibility_adaptations"] = compatibility_adaptations
        if _blocking(payload["anomalies"]):
            raise CutoverBlocked("Le schéma/source SQLite n'est pas compatible avec le mapping #492.")

        source_rows, source_counts = _copy_source_inventory(
            source_engine,
            source_revision=source_revision,
        )
        (
            eligible,
            exclusions,
            missing_dependencies,
            sanitizations,
        ) = _prepare_keep_rows(source_rows)
        payload["missing_dependencies"] = missing_dependencies
        payload["sanitizations"] = sanitizations
        payload["anomalies"].extend(missing_dependencies)

        unique_anomalies, deferred_partial_indexes = _source_uniqueness_anomalies(
            eligible
        )
        payload["anomalies"].extend(unique_anomalies)
        payload["partial_unique_indexes_deferred_to_target"] = deferred_partial_indexes

        code_head = _code_alembic_head()
        payload["code_alembic_head"] = code_head
        expected_revision = expected_target_revision or code_head
        payload["expected_target_revision"] = expected_revision
        if apply and not expected_target_revision:
            raise CutoverBlocked(
                "--expected-target-revision est obligatoire avec --apply afin de figer le contrat #457."
            )
        if expected_revision != code_head:
            payload["anomalies"].append(
                {
                    "code": "expected_revision_not_code_head",
                    "severity": "BLOCKING",
                    "expected_target_revision": expected_revision,
                    "code_head": code_head,
                }
            )
        if apply and not baseline_ready:
            raise CutoverBlocked(
                "--baseline-ready est obligatoire avec --apply; il atteste que #457 a stabilisé la baseline cible."
            )

        if not str(target_database_url or "").strip():
            raise CutoverBlocked(f"La variable {DATABASE_ENV} est requise.")
        target_engine = create_sql_engine(target_database_url)
        payload["target"] = _safe_target_info(target_engine)
        payload["target"]["detected_tables"] = _detected_tables(target_engine)
        if target_engine.dialect.name != "mssql" and not allow_test_target:
            raise CutoverBlocked(
                "La cible réelle #492 doit être SQL Server (dialecte mssql)."
            )

        payload["target"]["revision"] = _database_revision(target_engine)
        payload["target"]["expected_revision"] = expected_revision
        if payload["target"]["revision"] != expected_revision:
            payload["anomalies"].append(
                {
                    "code": "target_revision_mismatch",
                    "severity": "BLOCKING",
                    "expected": expected_revision,
                    "actual": payload["target"]["revision"],
                }
            )
        payload["anomalies"].extend(
            _schema_compatibility(target_engine, label="target")
        )

        target_counts = _target_counts(target_engine)
        target_anomalies, collisions = _target_precondition_anomalies(
            target_engine,
            target_counts,
            eligible,
        )
        payload["anomalies"].extend(target_anomalies)
        payload["collisions"] = collisions
        payload["transfer_order"] = _transfer_order()
        payload["tables"] = _table_report(
            source_counts,
            eligible,
            exclusions,
            target_counts,
        )
        payload["exclusions"] = {
            table: values
            for table, values in sorted(exclusions.items())
            if values
        }
        payload["post_cutover_actions"] = [
            {
                "table": table_name,
                "action": policy.post_action,
            }
            for table_name, policy in TABLE_POLICIES.items()
            if policy.post_action
        ]

        # Recheck the frozen source after the entire read-only inventory.
        if _sha256(source) != source_hash:
            raise CutoverBlocked(
                "La source SQLite a changé pendant l'inventaire; geler la source et recommencer."
            )

        if _blocking(payload["anomalies"]):
            payload["status"] = "blocked"
            payload["ready_for_apply"] = False
            return 2

        payload["ready_for_apply"] = True
        if not apply:
            payload["status"] = "ready"
            return 0

        # Single target transaction. Baseline-created REBUILD rows are left untouched.
        payload["transaction"] = {
            "strategy": "single_transaction_clean_target",
            "status": "started",
            "rollback": "full_on_error",
            "replay": "recreate_clean_target_from_baseline_then_rerun",
        }
        transferred: dict[str, int] = {}
        with target_engine.connect() as connection:
            transaction = connection.begin()
            try:
                for table_name in payload["transfer_order"]:
                    table = Base.metadata.tables[table_name]
                    rows = eligible.get(table_name, [])
                    _insert_batches(connection, table, rows)
                    transferred[table_name] = len(rows)
                    if fail_after_table == table_name:
                        raise CutoverTransferError(
                            f"Injected failure after {table_name}"
                        )

                if _sha256(source) != source_hash:
                    raise CutoverTransferError(
                        "La source SQLite a changé avant le commit cible."
                    )

                validation = _validate_transferred_data(connection, eligible)
                payload["validation"] = validation
                transaction.commit()
            except Exception:
                transaction.rollback()
                payload["transaction"]["status"] = "rolled_back"
                raise

        payload["transaction"]["status"] = "committed"
        payload["transfer"] = {
            "transferred_rows_by_table": transferred,
            "transferred_rows_total": sum(transferred.values()),
        }
        payload["target_counts_after"] = {
            table_name: details["target"]
            for table_name, details in payload["validation"]["counts"].items()
        }
        payload["status"] = "applied"
        payload["applied"] = True
        return 0
    except CutoverBlocked as exc:
        payload["status"] = "blocked"
        payload["ready_for_apply"] = False
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)
        return 2
    except Exception as exc:
        payload["status"] = "failed"
        payload["applied"] = False
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)
        if payload.get("transaction", {}).get("status") == "started":
            payload["transaction"]["status"] = "rolled_back"
        return 3
    finally:
        if source_engine is not None:
            source_engine.dispose()
        if target_engine is not None:
            target_engine.dispose()
        payload["finished_at"] = _utc_iso()
        payload["duration_seconds"] = round(perf_counter() - started, 3)
        _write_report(report_file, payload)
        _summary(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventaire/dry-run et transfert contrôlé SQLite V2 → SQL Server. "
            "Aucun import Excel/V1 n'est utilisé."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Fichier SQLite V2 source. Il est toujours ouvert en lecture seule.",
    )
    parser.add_argument(
        "--backup",
        help=(
            "Backup distinct de la source. Obligatoire avec --apply et vérifié par SHA-256."
        ),
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help=f"Rapport JSON désensibilisé (défaut: {DEFAULT_REPORT.name}).",
    )
    parser.add_argument(
        "--expected-target-revision",
        help=(
            "Révision Alembic cible attendue. Obligatoire avec --apply; "
            "doit être la tête du code exécuté."
        ),
    )
    parser.add_argument(
        "--baseline-ready",
        action="store_true",
        help=(
            "Atteste explicitement que #457 a stabilisé la baseline cible. "
            "Obligatoire avec --apply."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Exécute le transfert dans une transaction unique. Sans ce flag, "
            "le mode est strictement dry-run."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = os.environ.get(DATABASE_ENV, "").strip()
    return run_cutover(
        source_path=args.source,
        backup_path=args.backup,
        target_database_url=database_url,
        report_path=args.report,
        expected_target_revision=args.expected_target_revision,
        apply=bool(args.apply),
        baseline_ready=bool(args.baseline_ready),
    )


if __name__ == "__main__":
    raise SystemExit(main())
