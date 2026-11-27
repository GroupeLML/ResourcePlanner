"""Offline safety contracts for #869. No database or application imports.

All observations are untrusted input to pure validators, not proof that SQL ran.
Q2b/Q2c must supply independently verified physical evidence and owned adapters.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from uuid import UUID
from urllib.parse import parse_qsl, urlsplit


URL_ENV = "RESOURCEPLANNER_869_QUALIFICATION_URL"
OPT_ENV = "RESOURCEPLANNER_869_QUALIFICATION_OPT_IN"
OPT_IN = "RUN_869_SQLSERVER_QUALIFICATION"
PROTECTED = {
    "app/infrastructure/sql/communication_canonical_acquisition.py": "69a24bfaeeb7aa4ab8090f6d95e61fe243ecc3a1",
    "app/infrastructure/sql/communication_generator_capture.py": "61b75dd5c43e07f20404184181a6964cc1a4aad5",
    "app/infrastructure/sql/communication_read_uow.py": "b60fad6acb84a764c2395468a588838da618cc68",
    "tests/test_sql_communication_canonical_generator_integration.py": "582bff2b010d115c89e9ade0a5be2aef73c27917",
}
TIERS = {"T0", "T1", "T2", "T3", "T4", "T5"}
SCENARIOS = frozenset(f"{group}{i:02d}" for group, n in
                      zip("ABCDEFG", (6, 13, 10, 5, 11, 8, 11)) for i in range(1, n+1))
# Candidate values only. Never used as implicit approval. Sizes: projects,
# Shifts, eligible superintendents, allocations, distinct overrides, rules.
CANDIDATE_FIXTURE_SIZES = {
    "T0": (3, 12, 8, 12, 4, 12), "T1": (25, 250, 50, 250, 25, 100),
    "T2": (100, 901, 901, 901, 100, 901), "T3": (1101, 1101, 1101, 2202, 100, 1101),
    "T4": (1802, 1802, 1802, 3604, 200, 1802), "T5": (25, 250, 50, 250, (0, 25, 100, 250), 100),
}
CANDIDATE_PERFORMANCE = {
    "T0": ("empirical_p95", 10, 1.5, 3), "T1": ("empirical_p95", 10, 3, 5),
    "T2": ("median", 3, 8, 12), "T3": ("median", 3, 15, 25),
    "T4": ("median", 3, 25, 40), "T1_concurrent": ("empirical_p95", 10, 5, 8),
    "checkout": ("empirical_p95", 10, .25, 1), "release": ("empirical_p95", 10, .25, 1),
    "ordinary_generator": ("median", 10, .5, None), "large_generator_50": ("median", 3, None, 3),
}
CANDIDATE_LIMITS = {
    "connections": 3, "statement_seconds": 15, "large_statement_seconds": 25,
    "lock_seconds": 2, "barrier_seconds": 5, "reader_seconds": 45,
    "mutation_seconds": 10, "block_seconds": 300, "window_seconds": 2700,
    "recovery_reserve_seconds": 300,  # proposed Q2a refinement; not live approval
    "sql_cpu_percent": 80, "sql_cpu_delta_points": 20,
    "app_cpu_percent": 80, "app_memory_mib": 1024, "disk_free_percent": 20,
    "disk_free_mib": 2048, "version_store_growth_mib": 512, "log_percent": 75,
    "blocking_seconds": 2, "sample_interval_seconds": 5, "sustained_samples": 3,
}


class SafetyError(ValueError):
    """Fixed reason codes only; never interpolate inputs or exception messages."""


def require(condition, code):
    if not condition:
        raise SafetyError(code)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "INVALID_FIELDS")


def exact_bool(value):
    require(type(value) is bool, "INVALID_BOOLEAN")
    return value


def number(value, *, positive=True):
    require(type(value) in (int, float) and abs(value) <= 10**15 and math.isfinite(value) and
            (value > 0 if positive else value >= 0), "INVALID_NUMBER")
    return value


def token(value, pattern=r"[A-Za-z0-9_.-]{1,128}"):
    require(type(value) is str and re.fullmatch(pattern, value) is not None, "INVALID_TOKEN")
    return value


def sha(value, size=64):
    return token(value, rf"[0-9a-f]{{{size}}}")


def utc(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value), "INVALID_TIME")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SafetyError("INVALID_TIME") from None


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (ValueError, TypeError, OverflowError):
        raise SafetyError("INVALID_JSON") from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_FIELD")
            result[key] = value
        return result
    try:
        with open(path, "rb") as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        require(len(data) <= 8 * 1024 * 1024, "DOCUMENT_TOO_LARGE")
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: require(False, "INVALID_JSON"))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, SafetyError):
            raise
        raise SafetyError("INVALID_DOCUMENT") from None


def validate_target(target):
    fields(target, "server database classification")
    token(target["server"], r"[A-Za-z0-9_.-]{1,128}(?:\\[A-Za-z0-9_.-]{1,128})?")
    token(target["database"])
    require(target["classification"] == "DEDICATED_ISOLATED", "PROHIBITED_ENVIRONMENT")
    require(target["database"].casefold() not in {"rplanner", "rplanner_dev", "master", "model", "msdb", "tempdb"},
            "PROHIBITED_DATABASE")


def validate_approval(a):
    fields(a, "schema_version source_head source_tree protected target routing prerequisites mode window recovery_until manifest_digest grants fixture_limits limits performance monitoring operator approval_ref it_approval_ref recovery_approval_ref capacity_tiers deadlock_approval")
    require(type(a["schema_version"]) is int and a["schema_version"] == 1, "VERSION")
    sha(a["source_head"], 40); sha(a["source_tree"], 40)
    require(a["protected"] == PROTECTED, "PROTECTED_SOURCE")
    validate_target(a["target"])
    fields(a["routing"], "host port")
    token(a["routing"]["host"], r"[A-Za-z0-9_.-]{1,128}")
    port = a["routing"]["port"]
    require(port is None or type(port) is int and 1 <= port <= 65535, "INVALID_ROUTING")
    require(a["mode"] in ("WORKLOAD", "RECOVERY_ONLY"), "INVALID_MODE")
    fields(a["prerequisites"], "version alembic_revision driver")
    token(a["prerequisites"]["version"], r"14\.0\.\d+\.\d+")
    token(a["prerequisites"]["alembic_revision"])
    token(a["prerequisites"]["driver"])
    fields(a["window"], "start end")
    start, end, recovery = utc(a["window"]["start"]), utc(a["window"]["end"]), utc(a["recovery_until"])
    require(start < end < recovery, "INVALID_WINDOW")
    if a["manifest_digest"] is not None:
        sha(a["manifest_digest"])
    require(a["mode"] != "RECOVERY_ONLY" or a["manifest_digest"] is not None, "RECOVERY_NOT_AUTHORIZED")
    token(a["operator"]); token(a["approval_ref"]); token(a["it_approval_ref"]); token(a["recovery_approval_ref"])
    fields(a["monitoring"], "authority reserved monitoring_available recovery_authorized")
    token(a["monitoring"]["authority"])
    for key in ("reserved", "monitoring_available", "recovery_authorized"):
        require(exact_bool(a["monitoring"][key]), "MONITORING_OR_RECOVERY_NOT_AUTHORIZED")
    require(type(a["capacity_tiers"]) is dict and set(a["capacity_tiers"]) <= {"T3", "T4"}, "CAPACITY_TIERS")
    for decision in list(a["capacity_tiers"].values()) + ([a["deadlock_approval"]] if a["deadlock_approval"] is not None else []):
        fields(decision, "it_ref po_ref")
        token(decision["it_ref"]); token(decision["po_ref"])
    require(type(a["grants"]) is list and bool(a["grants"]), "NO_GRANTS")
    grants = set()
    for g in a["grants"]:
        fields(g, "scenario tier")
        require(type(g["scenario"]) is str and g["scenario"] in SCENARIOS and type(g["tier"]) is str and g["tier"] in TIERS, "INVALID_GRANT")
        require(g["tier"] not in {"T3", "T4"} or g["tier"] in a["capacity_tiers"], "CAPACITY_NOT_APPROVED")
        require(g["scenario"] != "E10" or a["deadlock_approval"], "DEADLOCK_NOT_APPROVED")
        require((g["scenario"], g["tier"]) not in grants, "DUPLICATE_GRANT")
        grants.add((g["scenario"], g["tier"]))
    require(type(a["fixture_limits"]) is dict and set(a["fixture_limits"]) == {tier for _, tier in grants}, "INVALID_FIXTURE_LIMITS")
    for limits_by_model in a["fixture_limits"].values():
        require(type(limits_by_model) is dict and bool(limits_by_model) and set(limits_by_model) <= set(MODELS), "INVALID_FIXTURE_LIMITS")
        for limit in limits_by_model.values():
            require(type(limit) is int and limit > 0, "INVALID_FIXTURE_LIMITS")
    require(type(a["limits"]) is dict and set(a["limits"]) == set(CANDIDATE_LIMITS), "INVALID_LIMITS")
    limits = a["limits"]
    for key, value in limits.items():
        number(value)
        if key in {"connections", "sustained_samples"}:
            require(type(value) is int, "INVALID_LIMITS")
        if key.endswith("percent") or key.endswith("points"):
            require(value <= 100, "INVALID_LIMITS")
    require(limits["connections"] <= 3 and limits["recovery_reserve_seconds"] < limits["window_seconds"] and
            limits["lock_seconds"] <= limits["statement_seconds"] <= limits["large_statement_seconds"] <= limits["reader_seconds"] <= limits["block_seconds"] <= limits["window_seconds"] and
            limits["mutation_seconds"] <= limits["block_seconds"] and limits["barrier_seconds"] <= limits["reader_seconds"] and
            limits["sample_interval_seconds"] * limits["sustained_samples"] <= limits["block_seconds"] and
            (end-start).total_seconds() <= limits["window_seconds"] and
            (recovery-end).total_seconds() >= limits["recovery_reserve_seconds"], "INVALID_LIMITS")
    require(type(a["performance"]) is dict and set(a["performance"]) == set(CANDIDATE_PERFORMANCE), "INVALID_PERFORMANCE")
    for key, rule in a["performance"].items():
        fields(rule, "metric samples target maximum")
        require(rule["metric"] == CANDIDATE_PERFORMANCE[key][0] and type(rule["samples"]) is int and rule["samples"] >= CANDIDATE_PERFORMANCE[key][1], "INVALID_PERFORMANCE")
        for name, index in (("target", 2), ("maximum", 3)):
            if CANDIDATE_PERFORMANCE[key][index] is None:
                require(rule[name] is None, "INVALID_PERFORMANCE")
            else:
                number(rule[name])
        require(rule["target"] is None or rule["maximum"] is None or rule["target"] <= rule["maximum"], "INVALID_PERFORMANCE")
    return json.loads(canonical(a))


def authorize_window(a, now, *, recovery=False):
    require(now.tzinfo is not None, "INVALID_TIME")
    start, end = utc(a["window"]["start"]), utc(a["window"]["end"])
    if recovery:
        require(a["manifest_digest"] is not None and a["monitoring"]["recovery_authorized"] and start <= now < utc(a["recovery_until"]), "RECOVERY_NOT_AUTHORIZED")
    else:
        require(a["mode"] == "WORKLOAD" and start <= now < end, "WINDOW_CLOSED")


def validate_configuration(env, target=None, routing=None):
    require(env.get(OPT_ENV) == OPT_IN, "OPT_IN_REQUIRED")
    # Reject ambient DB routing even if it appears to equal the dedicated URL.
    for name, value in env.items():
        upper = name.upper()
        if name != URL_ENV and value and ("DATABASE_URL" in upper or "DATABASE_URI" in upper or upper in {"DB_URL", "DB_URI", "ODBC_DSN", "MSSQL_URL"} or "SQLSERVER" in upper and ("URL" in upper or "DSN" in upper)):
            raise SafetyError("AMBIENT_DATABASE_CONFIGURATION")
    value = env.get(URL_ENV)
    require(type(value) is str and value.startswith("mssql+pyodbc://") and len(value) > 16 and
            not any(c in value for c in "\r\n\x00"), "DEDICATED_URL_REQUIRED")
    try:
        parsed = urlsplit(value)
        require(parsed.scheme == "mssql+pyodbc" and bool(parsed.hostname) and not parsed.fragment and
                re.fullmatch(r"/[A-Za-z0-9_.-]{1,128}", parsed.path) is not None, "INVALID_DEDICATED_URL")
        port = parsed.port
        require(routing is None or parsed.hostname.casefold() == routing["host"].casefold() and port == routing["port"], "CONFIGURED_ENDPOINT_MISMATCH")
        opts = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        names = [key.casefold() for key, _ in opts]
        require(len(set(names)) == len(names) and set(names) <= {"driver", "encrypt", "trustservercertificate", "longasmax", "authentication", "connect_timeout", "timeout"}, "AMBIGUOUS_URL_ROUTING")
        # No opaque ODBC strings or delimiters that could add hidden attributes.
        require(all(re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", val) is not None for _, val in opts), "AMBIGUOUS_URL_ROUTING")
        require(target is None or parsed.path[1:] == target["database"], "CONFIGURED_DATABASE_MISMATCH")
    except (ValueError, UnicodeError) as error:
        if isinstance(error, SafetyError):
            raise
        raise SafetyError("INVALID_DEDICATED_URL") from None
    # Deliberately not returned, logged or treated as actual target identity.


def verify_source(a, observation):
    fields(observation, "head tree clean protected")
    require(observation["head"] == a["source_head"] and observation["tree"] == a["source_tree"] and
            observation["clean"] is True and observation["protected"] == PROTECTED, "SOURCE_MISMATCH")


def observe_source(root):
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=10,
                                  env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")}).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise SafetyError("SOURCE_UNAVAILABLE") from None
    require(Path(git("rev-parse", "--show-toplevel")).resolve() == Path(root).resolve(), "SOURCE_UNAVAILABLE")
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    clean = not git("status", "--porcelain", "--untracked-files=all")
    # Reject index flags that can hide tracked-file changes from status.
    clean = clean and all(line.startswith("H ") for line in git("ls-files", "-v").splitlines())
    protected = {path: git("rev-parse", f"HEAD:{path}") for path in PROTECTED}
    for path, blob in protected.items():
        worktree_file = Path(root)/path
        require(worktree_file.is_file() and not worktree_file.is_symlink() and
                git("hash-object", "--no-filters", "--", path) == blob, "SOURCE_MISMATCH")
    require(git("rev-parse", "HEAD") == head, "SOURCE_MISMATCH")
    clean = clean and not git("status", "--porcelain", "--untracked-files=all")
    return dict(head=head, tree=tree, clean=clean, protected=protected)


def validate_preflight(a, observed):
    fields(observed, "target version edition compatibility collation snapshot rcsi recovery alembic_revision python sqlalchemy pyodbc alembic driver constraints_verified permissions_verified reservation_verified monitoring_verified no_consumers no_operational_records")
    validate_target(observed["target"])
    require(observed["target"] == a["target"], "TARGET_MISMATCH")
    require(observed["version"] == a["prerequisites"]["version"] and observed["edition"] == "Standard" and
            type(observed["compatibility"]) is int and observed["compatibility"] == 140 and
            observed["collation"] == "SQL_Latin1_General_CP1_CI_AI" and observed["snapshot"] == "ON" and observed["rcsi"] is False and observed["recovery"] == "FULL", "ENGINE_PREREQUISITE")
    require(observed["alembic_revision"] == a["prerequisites"]["alembic_revision"], "SCHEMA_REVISION")
    require(type(observed["python"]) is str and re.fullmatch(r"3\.12\.\d+", observed["python"]) and
            (observed["sqlalchemy"], observed["pyodbc"], observed["alembic"]) == ("2.0.52", "5.3.0", "1.19.1"), "DEPENDENCY_MISMATCH")
    require(observed["driver"] == a["prerequisites"]["driver"], "DRIVER_MISMATCH")
    for key in ("constraints_verified", "permissions_verified", "reservation_verified", "monitoring_verified", "no_consumers", "no_operational_records"):
        require(observed[key] is True, "PREFLIGHT_INCOMPLETE")


# Model -> exact table, primary key columns, allowed alternate key columns.
# The actual complete deployed unique/FK graph must still be verified by Q2b/Q3.
MODELS = {
    "BusinessContact": ("business_contacts", ("id",), (("external_system", "external_entity", "external_id"),)),
    "AppUser": ("app_users", ("id",), (("issuer", "subject"),)),
    "Resource": ("resources", ("id",), (("external_id",),)),
    "Project": ("projects", ("id",), (("number",),)),
    "ProjectCoManager": ("project_co_managers", ("project_id", "business_contact_id"), ()),
    "ResourceClassConfig": ("resource_class_configs", ("code",), ()),
    "Competency": ("competencies", ("id",), (("name",),)),
    "ResourceCompetency": ("resource_competencies", ("resource_id", "competency_id"), ()),
    "TaskCatalogEntry": ("task_catalog_items", ("id",), (("project_number", "task_code"),)),
    "WorkforceRequest": ("workforce_requests", ("id",), ()),
    "RequestLine": ("request_lines", ("id",), ()),
    "ResourceRequirement": ("resource_requirements", ("id",), ()),
    "Shift": ("shifts", ("id",), ()),
    "ResourceAvailabilityRule": ("resource_availability_rules", ("id",), ()),
    "AvailabilityRuleResourceClass": ("availability_rule_resource_classes", ("availability_rule_id", "resource_class_code"), ()),
    "AssetType": ("asset_types", ("id",), (("code",),)),
    "AssetTypeCompetency": ("asset_type_competencies", ("asset_type_id", "competency_id"), ()),
    "Asset": ("assets", ("id",), (("code",),)),
    "AssetRequirement": ("asset_requirements", ("id",), ()),
    "AssetAllocation": ("asset_allocations", ("id",), (("asset_requirement_id",),)),
    "CommunicationBatchRow": ("communication_batches", ("id",), ()),
}


# Exact relationship field allowlist. Q2b must complete the deployed schema check;
# unsupported dependencies fail closed rather than extending this registry at runtime.
RELATIONS = {
    "AppUser": {"business_contact_id": "BusinessContact"},
    "Resource": {"coordinator_contact_id": "BusinessContact", "resource_class": "ResourceClassConfig"},
    "Project": {"project_manager_contact_id": "BusinessContact", "operational_responsible_override_contact_id": "BusinessContact"},
    "ProjectCoManager": {"project_id": "Project", "business_contact_id": "BusinessContact", "created_by_user_id": "AppUser"},
    "Competency": {"resource_class_code": "ResourceClassConfig"},
    "ResourceCompetency": {"resource_id": "Resource", "competency_id": "Competency"},
    "TaskCatalogEntry": {"operational_responsible_contact_id": "BusinessContact", "coordinator_contact_id": "BusinessContact", "preferred_resource_id": "Resource"},
    "WorkforceRequest": {"project_id": "Project", "requester_user_id": "AppUser", "operational_responsible_override_contact_id": "BusinessContact", "proposed_resource_id": "Resource"},
    "RequestLine": {"workforce_request_id": "WorkforceRequest", "task_catalog_item_id": "TaskCatalogEntry", "proposed_resource_id": "Resource", "asset_type_id": "AssetType", "proposed_asset_id": "Asset"},
    "ResourceRequirement": {"project_id": "Project", "workforce_request_id": "WorkforceRequest", "source_request_line_id": "RequestLine", "approved_task_catalog_item_id": "TaskCatalogEntry", "approved_operational_responsible_override_contact_id": "BusinessContact", "operational_responsible_override_contact_id": "BusinessContact", "captured_operational_responsible_contact_id": "BusinessContact", "assigned_resource_id": "Resource"},
    "Shift": {"resource_requirement_id": "ResourceRequirement", "resource_id": "Resource", "operational_responsible_override_contact_id": "BusinessContact"},
    "ResourceAvailabilityRule": {"resource_id": "Resource"},
    "AvailabilityRuleResourceClass": {"availability_rule_id": "ResourceAvailabilityRule", "resource_class_code": "ResourceClassConfig"},
    "AssetTypeCompetency": {"asset_type_id": "AssetType", "competency_id": "Competency"},
    "Asset": {"asset_type_id": "AssetType"},
    "AssetRequirement": {"project_id": "Project", "shift_id": "Shift", "resource_requirement_id": "ResourceRequirement", "context_resource_id": "Resource", "workforce_request_id": "WorkforceRequest", "source_request_line_id": "RequestLine", "asset_type_id": "AssetType"},
    "AssetAllocation": {"asset_requirement_id": "AssetRequirement", "asset_id": "Asset", "operator_resource_id": "Resource"},
}
REQUIRED_RELATIONS = {
    "ProjectCoManager": {"project_id", "business_contact_id", "created_by_user_id"},
    "ResourceCompetency": {"resource_id", "competency_id"},
    "WorkforceRequest": {"project_id"}, "RequestLine": {"workforce_request_id"},
    "ResourceRequirement": {"project_id"}, "Shift": {"resource_requirement_id", "resource_id"},
    "AvailabilityRuleResourceClass": {"availability_rule_id", "resource_class_code"},
    "AssetTypeCompetency": {"asset_type_id", "competency_id"}, "Asset": {"asset_type_id"},
    "AssetRequirement": {"asset_type_id"}, "AssetAllocation": {"asset_requirement_id", "asset_id"},
}


def uuid_text(value):
    try:
        require(type(value) is str and str(UUID(value)) == value, "INVALID_UUID")
    except (ValueError, AttributeError, TypeError):
        raise SafetyError("INVALID_UUID") from None
    return value


def row_key(value):
    fields(value, "model key")
    model = value["model"]
    require(type(model) is str and model in MODELS, "MODEL_NOT_ALLOWED")
    key = value["key"]
    require(type(key) is dict and set(key) == set(MODELS[model][1]), "KEY_NOT_ALLOWED")
    for column, val in key.items():
        if column in {"code", "resource_class_code"}:
            token(val, r"Q869[-_][A-Za-z0-9_-]{1,58}")
        else:
            uuid_text(val)
    return digest(value)


def validate_plan(a, plan, *, bound=True):
    fields(plan, "schema_version run_id source_head target tier scenarios rows")
    require(type(plan["schema_version"]) is int and plan["schema_version"] == 1, "VERSION")
    uuid_text(plan["run_id"])
    require(plan["source_head"] == a["source_head"] and plan["target"] == a["target"] and
            any(g["tier"] == plan["tier"] for g in a["grants"]), "PLAN_AUTHORITY")
    require(type(plan["scenarios"]) is list and bool(plan["scenarios"]) and
            all(type(x) is str and dict(scenario=x, tier=plan["tier"]) in a["grants"] for x in plan["scenarios"]) and
            len(set(plan["scenarios"])) == len(plan["scenarios"]), "PLAN_SCENARIOS")
    require(type(plan["rows"]) is list and bool(plan["rows"]), "EMPTY_PLAN")
    counts = {}
    keys, references, alternate = set(), [], set()
    for row in plan["rows"]:
        fields(row, "identity alternate_keys expected_row_digest references")
        key = row_key(row["identity"])
        require(key not in keys, "DUPLICATE_ROW")
        keys.add(key)
        model = row["identity"]["model"]
        counts[model] = counts.get(model, 0)+1
        require(counts[model] <= a["fixture_limits"][plan["tier"]].get(model, 0), "FIXTURE_LIMIT")
        require(model != "CommunicationBatchRow" or any(x.startswith("F") for x in plan["scenarios"]), "PROBE_NOT_AUTHORIZED")
        sha(row["expected_row_digest"])
        require(type(row["alternate_keys"]) is list and type(row["references"]) is list, "INVALID_ROW")
        seen = set()
        for alt in row["alternate_keys"]:
            require(type(alt) is dict and tuple(sorted(alt)) in {tuple(sorted(x)) for x in MODELS[model][2]}, "ALTERNATE_KEY_NOT_ALLOWED")
            signature = tuple(sorted(alt))
            require(signature not in seen, "DUPLICATE_ALTERNATE")
            seen.add(signature)
            for col, val in alt.items():
                if col == "asset_requirement_id":
                    uuid_text(val)
                elif col == "external_system":
                    require(val in ("RESOURCEPLANNER", "ACUMATICA"), "ALTERNATE_VALUE_NOT_ALLOWED")
                elif col == "external_entity":
                    require(val in ("EMPLOYEE", "CONTACT", "USER"), "ALTERNATE_VALUE_NOT_ALLOWED")
                else:
                    token(val, r"[Qq]869[-_][A-Za-z0-9_.-]{1,123}")
            ak = digest([model, alt])
            require(ak not in alternate, "ALTERNATE_COLLISION")
            alternate.add(ak)
        if model in {"Project", "Competency", "TaskCatalogEntry", "AssetType", "Asset", "AssetAllocation"}:
            require(seen == {tuple(sorted(x)) for x in MODELS[model][2]}, "MISSING_ALTERNATE_KEY")
        relation_columns = set()
        for ref in row["references"]:
            fields(ref, "column identity")
            column = ref["column"]
            require(type(column) is str and column in RELATIONS.get(model, {}) and column not in relation_columns, "RELATION_NOT_ALLOWED")
            relation_columns.add(column)
            parent_key = row_key(ref["identity"])
            require(ref["identity"]["model"] == RELATIONS[model][column], "RELATION_NOT_ALLOWED")
            parent_value = next(iter(ref["identity"]["key"].values()))
            identity_values = row["identity"]["key"]
            if column in identity_values:
                require(identity_values[column] == parent_value, "RELATION_KEY_MISMATCH")
            for alt in row["alternate_keys"]:
                if column in alt:
                    require(alt[column] == parent_value, "RELATION_KEY_MISMATCH")
            references.append((key, parent_key))
        require(REQUIRED_RELATIONS.get(model, set()) <= relation_columns, "MISSING_RELATION")
    require(all(parent in keys and parent != child for child, parent in references), "UNOWNED_REFERENCE")
    # Local duplicate/collision checks are exact Python comparisons only. The future
    # adapter must perform independent deployed-collation and complete FK checks.
    if bound:
        require(a["manifest_digest"] is not None and digest(plan) == a["manifest_digest"], "PLAN_DIGEST_MISMATCH")
    return keys


def authorize_mutation(a, plan, observation, preflight, env, now, *, recovery=False):
    # Validation contract ONLY: no connection, SQL, mutation or capability token.
    a = validate_approval(a); validate_configuration(env, a["target"], a["routing"]); verify_source(a, observation)
    validate_preflight(a, preflight); authorize_window(a, now, recovery=recovery)
    validate_plan(a, plan)


STATES = {
    "PLANNED": {"SETUP_PENDING", "RECOVERY_REQUIRED"},
    "SETUP_PENDING": {"COMMITTED", "RECOVERY_REQUIRED"},
    "COMMITTED": {"RUNNING", "CLEANUP_PENDING", "RECOVERY_REQUIRED"},
    "RUNNING": {"CLEANUP_PENDING", "RECOVERY_REQUIRED"},
    "CLEANUP_PENDING": {"VERIFIED_CLEAN", "RECOVERY_REQUIRED"},
    "RECOVERY_REQUIRED": {"CLEANUP_PENDING"}, "VERIFIED_CLEAN": set(),
}


def initial_journal(plan):
    return dict(schema_version=1, plan_digest=digest(plan), run_id=plan["run_id"], revision=0,
                state="PLANNED", owned=[], pending=[], uncertain=False, history=[])


def validate_journal(plan, journal):
    fields(journal, "schema_version plan_digest run_id revision state owned pending uncertain history")
    require(type(journal["schema_version"]) is int and journal["schema_version"] == 1 and
            journal["plan_digest"] == digest(plan) and journal["run_id"] == plan["run_id"] and
            type(journal["revision"]) is int and journal["revision"] >= 0 and
            type(journal["state"]) is str and journal["state"] in STATES, "JOURNAL_BINDING")
    keys = {row_key(r["identity"]) for r in plan["rows"]}
    for name in ("owned", "pending"):
        values = journal[name]
        require(type(values) is list and all(type(k) is str and k in keys for k in values) and
                len(values) == len(set(values)), "JOURNAL_KEYS")
    exact_bool(journal["uncertain"])
    require(type(journal["history"]) is list and len(journal["history"]) == journal["revision"], "JOURNAL_HISTORY")
    previous_state = "PLANNED"
    for rev, event in enumerate(journal["history"], 1):
        fields(event, "revision state owned pending uncertain observation_digest")
        require(type(event["revision"]) is int and event["revision"] == rev and type(event["state"]) is str and event["state"] in STATES[previous_state], "JOURNAL_HISTORY")
        for name in ("owned", "pending"):
            require(type(event[name]) is list and all(type(k) is str and k in keys for k in event[name]) and len(event[name]) == len(set(event[name])), "JOURNAL_HISTORY")
        exact_bool(event["uncertain"])
        if event["observation_digest"] is not None:
            sha(event["observation_digest"])
        previous_state = event["state"]
    if journal["history"]:
        require(all(journal[name] == journal["history"][-1][name] for name in ("revision", "state", "owned", "pending", "uncertain")), "JOURNAL_HISTORY")
    else:
        require(journal == initial_journal(plan), "JOURNAL_HISTORY")
    require(journal["state"] != "VERIFIED_CLEAN" or not (journal["owned"] or journal["pending"] or journal["uncertain"]), "FALSE_CLEAN")
    return keys


def transition(plan, journal, state, *, confirmed=(), pending=(), independent=False, uncertain=False, observations=None):
    keys = validate_journal(plan, journal)
    require(type(state) is str and state in STATES[journal["state"]], "ILLEGAL_TRANSITION")
    require(type(independent) is bool and type(uncertain) is bool, "INVALID_BOOLEAN")
    confirmed, pending = set(confirmed), set(pending)
    require(confirmed <= keys and pending <= keys, "UNOWNED_MUTATION")
    old_owned = set(journal["owned"])
    if state == "SETUP_PENDING":
        require(not reconcile(plan, journal, observations), "PREEXISTING_ROW")
        require(pending == keys and not confirmed and not uncertain, "SETUP_INTENT_REQUIRED")
    elif state == "COMMITTED":
        # Check independent physical outcome against the exact recorded setup intent.
        require(type(observations) is dict and set(observations) == keys, "MISSING_OWNERSHIP_EVIDENCE")
        for row in plan["rows"]:
            obs = observations[row_key(row["identity"])]
            fields(obs, "exists actual_digest alternate_collision unexpected_references independent transaction_confirmed")
            require(obs["exists"] is True and obs["independent"] is True and obs["transaction_confirmed"] is True and
                    obs["actual_digest"] == row["expected_row_digest"] and obs["alternate_collision"] is False and obs["unexpected_references"] is False, "OWNERSHIP_NOT_CONFIRMED")
        require(independent and confirmed == set(journal["pending"]) == keys and not pending and not uncertain, "OWNERSHIP_NOT_CONFIRMED")
    elif state == "RUNNING":
        require(confirmed == old_owned == keys and not pending and not uncertain and not journal["uncertain"], "OWNERSHIP_NOT_CONFIRMED")
    elif state == "CLEANUP_PENDING":
        require(reconcile(plan, journal, observations) == old_owned, "RECOVERY_UNCERTAIN")
        require(independent and not journal["uncertain"] and not uncertain and confirmed == old_owned and pending == old_owned, "RECOVERY_UNCERTAIN")
    elif state == "VERIFIED_CLEAN":
        require(not reconcile(plan, journal, observations), "RESIDUAL_ROWS")
        require(independent and not confirmed and not pending and not uncertain and not journal["uncertain"], "FALSE_CLEAN")
    else:
        require(confirmed == old_owned and pending == set(journal["pending"]), "RECOVERY_LOST_INTENT")
        uncertain = uncertain or journal["uncertain"] or journal["state"] in {"SETUP_PENDING", "CLEANUP_PENDING"}
    event = dict(revision=journal["revision"]+1, state=state, owned=sorted(confirmed),
                 pending=sorted(pending), uncertain=uncertain,
                 observation_digest=None if observations is None else digest(observations))
    return dict(journal, revision=event["revision"], state=state, owned=event["owned"],
                pending=event["pending"], uncertain=uncertain, history=journal["history"]+[event])


def reconcile(plan, journal, observations):
    """Return only proven owned row digests; never infer ownership from namespace.

    Unknown commit, partial cleanup, or missing physical evidence is terminal here.
    Q2b must establish actual durable independently verified transaction evidence.
    """
    keys = validate_journal(plan, journal)
    require(not journal["uncertain"], "RECOVERY_UNCERTAIN")
    require(type(observations) is dict and set(observations) == keys, "MISSING_OWNERSHIP_EVIDENCE")
    owned = set(journal["owned"])
    deletable = set()
    for row in plan["rows"]:
        key = row_key(row["identity"])
        obs = observations[key]
        fields(obs, "exists actual_digest alternate_collision unexpected_references independent transaction_confirmed")
        require(obs["independent"] is True and obs["alternate_collision"] is False and obs["unexpected_references"] is False, "COLLISION_OR_REFERENCE")
        if exact_bool(obs["exists"]):
            require(key in owned and obs["transaction_confirmed"] is True and obs["actual_digest"] == row["expected_row_digest"], "OWNERSHIP_NOT_CONFIRMED")
            deletable.add(key)
        else:
            require(obs["actual_digest"] is None and obs["transaction_confirmed"] is False, "CONTRADICTORY_OBSERVATION")
            require(key not in owned or journal["state"] == "CLEANUP_PENDING", "UNEXPECTED_MISSING_ROW")
    return frozenset(deletable)


def store_journal(path, plan, *, previous=None, state=None, **transition_evidence):
    """Single local writer; durable replace with stale-revision check. Never auto-break lock."""
    record = initial_journal(plan) if previous is None else transition(plan, previous, state, **transition_evidence)
    validate_journal(plan, record)
    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    temp = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        raise SafetyError("JOURNAL_LOCKED") from None
    try:
        os.close(fd)
        if previous is None:
            require(not path.exists() and record == initial_journal(plan), "JOURNAL_CONFLICT")
        else:
            validate_journal(plan, previous)
            require(read_json(path) == previous and record["revision"] == previous["revision"]+1 and
                    record["state"] in STATES[previous["state"]], "JOURNAL_CONFLICT")
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".q869-", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(canonical(record)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        raise SafetyError("JOURNAL_DURABILITY_UNCERTAIN") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return record


class SafetyController:
    """Thread-safe admission/stop bookkeeping; no driver cancellation implementation."""
    def __init__(self, approval):
        self.approval = validate_approval(approval)
        self.lock = threading.RLock()
        self.cancel_requested = threading.Event()
        self.active = {}
        self.block_started = None
        self.stopped = False
        self.uncertain = False
        self.reason = None
        self.last_sample = None
        self.sustained = {"sql": 0, "app": 0}

    def stop(self, reason="OPERATOR_STOP", *, uncertain=False):
        with self.lock:
            self.stopped = True
            self.cancel_requested.set()
            self.uncertain = self.uncertain or uncertain or bool(self.active)
            self.reason = reason if reason in {"OPERATOR_STOP", "ENVIRONMENT_ABORT", "MONITORING_LOST", "WORKLOAD_ERROR", "TIMEOUT", "WINDOW_CLOSED"} else "WORKLOAD_ERROR"

    def sample(self, values, now):
        with self.lock:
            try:
                fields(values, "sql_cpu sql_cpu_baseline app_cpu memory_mib disk_free_percent disk_free_mib tempdb_free_percent tempdb_free_mib version_store_growth_mib log_percent blocking_seconds external_activity available")
                require(values["available"] is True and values["external_activity"] is False, "MONITORING_LOST")
                for key in set(values)-{"external_activity", "available"}:
                    number(values[key], positive=False)
                for key in ("sql_cpu", "sql_cpu_baseline", "app_cpu", "disk_free_percent", "tempdb_free_percent", "log_percent"):
                    require(values[key] <= 100, "MONITORING_LOST")
                lim = self.approval["limits"]
                require(now.tzinfo is not None, "MONITORING_LOST")
                if self.last_sample is not None:
                    elapsed = (now-self.last_sample).total_seconds()
                    require(0 < elapsed <= lim["sample_interval_seconds"]*2, "MONITORING_LOST")
                    require(elapsed >= lim["sample_interval_seconds"], "MONITORING_LOST")
                self.last_sample = now
                for key, high in (("sql", values["sql_cpu"] >= lim["sql_cpu_percent"] or values["sql_cpu"]-values["sql_cpu_baseline"] >= lim["sql_cpu_delta_points"]), ("app", values["app_cpu"] >= lim["app_cpu_percent"])):
                    self.sustained[key] = self.sustained[key]+1 if high else 0
                if (max(self.sustained.values()) >= lim["sustained_samples"] or
                    values["memory_mib"] < lim["app_memory_mib"] or values["disk_free_percent"] < lim["disk_free_percent"] or
                    values["disk_free_mib"] < lim["disk_free_mib"] or
                    values["tempdb_free_percent"] < lim["disk_free_percent"] or values["tempdb_free_mib"] < lim["disk_free_mib"] or values["version_store_growth_mib"] >= lim["version_store_growth_mib"] or
                    values["log_percent"] >= lim["log_percent"] or values["blocking_seconds"] > lim["blocking_seconds"]):
                    self.stop("ENVIRONMENT_ABORT")
            except (SafetyError, TypeError, AttributeError):
                self.stop("MONITORING_LOST")
            return not self.stopped

    def check_deadlines(self, now):
        """Future scheduler must invoke this while workers are active; no timer or SQL here."""
        with self.lock:
            if self.stopped:
                return False
            lim = self.approval["limits"]
            try:
                authorize_window(self.approval, now)
            except SafetyError:
                self.stop("WINDOW_CLOSED")
                return False
            if self.last_sample is None or not 0 <= (now-self.last_sample).total_seconds() <= lim["sample_interval_seconds"]:
                self.stop("MONITORING_LOST")
            elif (self.block_started is not None and (now-self.block_started).total_seconds() >= lim["block_seconds"] or
                  any((now-started).total_seconds() >= lim["mutation_seconds" if kind == "mutation" else "reader_seconds"] for started, kind in self.active.values())):
                self.stop("TIMEOUT")
            return not self.stopped

    def admit(self, scenario, tier, now, *, kind="reader"):
        with self.lock:
            require(not self.stopped, "STOPPED")
            require(self.check_deadlines(now), "STOPPED")
            require(kind in ("reader", "mutation"), "INVALID_WORKLOAD_KIND")
            require(dict(scenario=scenario, tier=tier) in self.approval["grants"], "GRANT_REQUIRED")
            require(len(self.active) < self.approval["limits"]["connections"], "CONCURRENCY_LIMIT")
            ticket = object()
            if self.block_started is None:
                self.block_started = now
            self.active[ticket] = (now, kind)
            return ticket

    def finish(self, ticket, *, physical_release_confirmed, timeout=False, error=False):
        with self.lock:
            require(ticket in self.active, "UNKNOWN_WORKLOAD")
            for flag in (physical_release_confirmed, timeout, error):
                exact_bool(flag)
            del self.active[ticket]
            if timeout or error or not physical_release_confirmed:
                self.stop("TIMEOUT" if timeout else "WORKLOAD_ERROR", uncertain=not physical_release_confirmed)


def performance_result(rule, samples, *, environmental_abort=False):
    fields(rule, "metric samples target maximum")
    require(rule["metric"] in ("median", "empirical_p95") and type(rule["samples"]) is int and rule["samples"] > 0, "INVALID_PERFORMANCE")
    for name in ("target", "maximum"):
        if rule[name] is not None:
            number(rule[name])
    require(rule["target"] is not None or rule["maximum"] is not None, "INVALID_PERFORMANCE")
    require(type(samples) in (list, tuple), "INVALID_SAMPLES")
    for sample in samples:
        number(sample, positive=False)
    if environmental_abort is True:
        return "ABORTED"
    require(environmental_abort is False, "INVALID_BOOLEAN")
    if len(samples) < rule["samples"]:
        return "NOT RUN"
    ordered = sorted(number(x, positive=False) for x in samples)
    n = len(ordered)
    if rule["metric"] == "median":
        observed = (ordered[(n-1)//2]+ordered[n//2])/2
    elif rule["metric"] == "empirical_p95":
        observed = ordered[math.ceil(.95*n)-1]
    else:
        raise SafetyError("INVALID_PERFORMANCE")
    return "FAIL" if (rule["target"] is not None and observed > rule["target"] or
                      rule["maximum"] is not None and ordered[-1] > rule["maximum"]) else "PASS"


def evidence_record(scenario, tier, status, metrics):
    """Allowlist numeric/boolean measurements, no free text or raw exception export."""
    require(type(scenario) is str and scenario in SCENARIOS and type(tier) is str and tier in TIERS and
            type(status) is str and status in {"PASS", "FAIL", "ABORTED", "NOT RUN"}, "INVALID_EVIDENCE")
    require(type(metrics) is dict and set(metrics) <= {"sample_count", "elapsed_seconds", "maximum_total_binds", "statement_count", "owned_session_count", "checkout_seconds", "release_seconds", "physical_coverage_equal", "cleanup_verified"}, "UNSAFE_EVIDENCE")
    for key, val in metrics.items():
        if key in {"physical_coverage_equal", "cleanup_verified"}:
            exact_bool(val)
        else:
            number(val, positive=False)
            if key in {"sample_count", "maximum_total_binds", "statement_count", "owned_session_count"}:
                require(type(val) is int, "INVALID_EVIDENCE")
    return dict(schema_version=1, execution_mode="OFFLINE_ONLY", scenario_id=scenario, fixture_tier=tier, status=status, metrics=dict(metrics))


def performance_evidence(rule, samples, *, environmental_abort=False):
    """Numeric offline evaluation, not a live scenario result or tail certification."""
    result = performance_result(rule, samples, environmental_abort=environmental_abort)
    clean = [number(x, positive=False) for x in samples]
    ordered = sorted(clean)
    n = len(clean)
    observed = None
    if n:
        observed = ((ordered[(n-1)//2]+ordered[n//2])/2 if rule["metric"] == "median"
                    else ordered[math.ceil(.95*n)-1])
    return dict(schema_version=1, execution_mode="OFFLINE_ONLY", status=result,
                sample_count=n, samples_seconds=clean, metric=rule["metric"],
                minimum_samples=rule["samples"], target_seconds=rule["target"],
                maximum_seconds=rule["maximum"], observed_metric_seconds=observed,
                observed_maximum_seconds=max(clean, default=None))


def run_record(a, plan):
    """Only digests of private approval/target, never raw approval or URL."""
    a = validate_approval(a)
    validate_plan(a, plan, bound=a["manifest_digest"] is not None)
    return dict(schema_version=1, execution_mode="OFFLINE_ONLY", status="NOT RUN",
                source_head=a["source_head"], source_tree=a["source_tree"],
                run_id=plan["run_id"], approval_digest=digest(a), target_digest=digest(a["target"]),
                plan_digest=digest(plan))


def sql_metric_record(family, transaction_id, binds, seconds):
    """Schema rehearsal only: SQL statement text and parameter values cannot be exported."""
    require(family in ("CAN", "UOW", "GEN", "ID", "ERP", "NOM", "PRES", "POP", "ASSIGN", "ASSET", "CONTACT", "QUAL", "AVAIL", "JSON"), "INVALID_STATEMENT_FAMILY")
    uuid_text(transaction_id)
    require(type(binds) is int and binds >= 0, "INVALID_BINDS")
    number(seconds, positive=False)
    return dict(schema_version=1, execution_mode="OFFLINE_ONLY", statement_family=family,
                transaction_id=transaction_id, total_binds=binds, elapsed_seconds=seconds)
