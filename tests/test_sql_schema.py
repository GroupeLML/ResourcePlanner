from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
import unittest

from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import mssql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.infrastructure.sql import Base


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "app" / "application"
DOMAIN_ENGINE = ROOT / "app" / "domain" / "planning_engine.py"
DAY = date(2026, 8, 26)

EXPECTED_TABLES = {
    "approval_decisions",
    "approval_requirement_approvers",
    "approval_requirements",
    "approval_scopes",
    "approval_scope_approvers",
    "asset_type_approval_scope_mappings",
    "resource_class_approval_scope_mappings",
    "task_approval_scope_mappings",
    "asset_types",
    "asset_type_competencies",
    "assets",
    "asset_unavailability",
    "asset_requirements",
    "asset_allocations",
    "asset_approvers",
    "app_users",
    "erp_user_directory",
    "identity_admin_audit",
    "business_contacts",
    "auth_login_transactions",
    "auth_security_audit",
    "auth_sessions",
    "break_glass_credentials",
    "command_idempotency_receipts",
    "communication_batches",
    "communication_contacts",
    "communication_messages",
    "communication_snapshot_lines",
    "communication_deliveries",
    "competencies",
    "delivery_plans",
    "delivery_items",
    "delivery_change_history",
    "planning_change_history",
    "planning_mutation_state",
    "projects",
    "request_approval_cycles",
    "request_approval_references",
    "request_approval_revisions",
    "request_operational_states",
    "request_lines",
    "request_line_competencies",
    "resource_requirement_competencies",
    "resource_class_configs",
    "project_task_class_overrides",
    "resources",
    "work_packages",
    "work_package_weekly_loads",
    "work_package_audit",
    "workforce_requests",
    "workforce_request_competencies",
    "workforce_request_history",
    "workforce_request_periods",
    "workforce_request_period_selections",
    "workforce_request_period_requirements",
    "resource_availability_rules",
    "resource_competencies",
    "resource_requirements",
    "smtp_configuration",
    "smtp_configuration_audit",
    "shifts",
    "task_catalog_items",
    "task_catalog_project_sync_state",
    "task_class_standards",
}


class SqlSchemaTests(unittest.TestCase):
    def test_expected_core_tables_and_nullability_contracts(self) -> None:
        self.assertEqual(set(Base.metadata.tables), EXPECTED_TABLES)

        requirements = Base.metadata.tables["resource_requirements"].c
        requests = Base.metadata.tables["workforce_requests"].c
        request_history = Base.metadata.tables["workforce_request_history"].c
        projects = Base.metadata.tables["projects"].c
        resources = Base.metadata.tables["resources"].c
        business_contacts = Base.metadata.tables["business_contacts"].c
        periods = Base.metadata.tables["workforce_request_periods"].c
        asset_types = Base.metadata.tables["asset_types"].c
        asset_type_competencies = Base.metadata.tables["asset_type_competencies"].c
        asset_requirements = Base.metadata.tables["asset_requirements"].c
        asset_allocations = Base.metadata.tables["asset_allocations"].c
        period_requirements = Base.metadata.tables["workforce_request_period_requirements"].c
        availability = Base.metadata.tables["resource_availability_rules"].c
        idempotency = Base.metadata.tables["command_idempotency_receipts"].c
        users = Base.metadata.tables["app_users"].c
        erp_users = Base.metadata.tables["erp_user_directory"].c
        identity_audit = Base.metadata.tables["identity_admin_audit"].c
        login_transactions = Base.metadata.tables["auth_login_transactions"].c
        auth_security_audit = Base.metadata.tables["auth_security_audit"].c
        auth_sessions = Base.metadata.tables["auth_sessions"].c
        break_glass = Base.metadata.tables["break_glass_credentials"].c
        communication_contacts = Base.metadata.tables["communication_contacts"].c
        communication_batches = Base.metadata.tables["communication_batches"].c
        communication_messages = Base.metadata.tables["communication_messages"].c
        communication_snapshots = Base.metadata.tables["communication_snapshot_lines"].c
        planning_history = Base.metadata.tables["planning_change_history"].c
        planning_state = Base.metadata.tables["planning_mutation_state"].c
        task_catalog = Base.metadata.tables["task_catalog_items"].c
        work_packages = Base.metadata.tables["work_packages"].c
        work_package_audit = Base.metadata.tables["work_package_audit"].c
        task_sync_state = Base.metadata.tables["task_catalog_project_sync_state"].c
        competencies = Base.metadata.tables["competencies"].c
        resource_competencies = Base.metadata.tables["resource_competencies"].c
        request_competencies = Base.metadata.tables["workforce_request_competencies"].c
        request_lines = Base.metadata.tables["request_lines"].c
        line_competencies = Base.metadata.tables["request_line_competencies"].c
        requirement_competencies = Base.metadata.tables[
            "resource_requirement_competencies"
        ].c
        selections = Base.metadata.tables["workforce_request_period_selections"].c
        approval_cycles = Base.metadata.tables["request_approval_cycles"].c
        approval_requirements = Base.metadata.tables["approval_requirements"].c
        approval_requirement_approvers = Base.metadata.tables["approval_requirement_approvers"].c
        approval_decisions = Base.metadata.tables["approval_decisions"].c
        approval_revisions = Base.metadata.tables["request_approval_revisions"].c
        approval_references = Base.metadata.tables["request_approval_references"].c
        operational_states = Base.metadata.tables["request_operational_states"].c
        resource_classes = Base.metadata.tables["resource_class_configs"].c
        task_class_standards = Base.metadata.tables["task_class_standards"].c
        project_task_overrides = Base.metadata.tables["project_task_class_overrides"].c

        self.assertFalse(operational_states.budget_overrides_text.nullable)
        self.assertTrue(work_packages.task_catalog_item_id.nullable)
        self.assertTrue(work_packages.resource_class_code.nullable)
        self.assertEqual(
            next(iter(work_packages.resource_class_code.foreign_keys)).target_fullname,
            "resource_class_configs.code",
        )
        self.assertFalse(work_packages.version.nullable)
        self.assertFalse(work_package_audit.work_package_id.nullable)
        self.assertFalse(work_package_audit.actor_user_id.nullable)
        self.assertFalse(work_package_audit.resulting_version.nullable)
        self.assertFalse(resource_classes.code.nullable)
        self.assertTrue(resource_classes.average_hourly_cost_cad.nullable)
        self.assertFalse(resource_classes.version.nullable)
        self.assertFalse(task_class_standards.task_code.nullable)
        self.assertFalse(task_class_standards.resource_class_code.nullable)
        self.assertFalse(project_task_overrides.project_id.nullable)
        self.assertFalse(project_task_overrides.task_code.nullable)
        self.assertTrue(project_task_overrides.resource_class_code.nullable)
        self.assertFalse(project_task_overrides.excluded.nullable)
        self.assertFalse(planning_state.id.nullable)
        self.assertFalse(planning_state.version.nullable)

        self.assertFalse(requirements.project_id.nullable)
        self.assertTrue(requirements.workforce_request_id.nullable)
        self.assertTrue(requirements.source_request_line_id.nullable)
        self.assertTrue(requirements.approved_task_catalog_item_id.nullable)
        self.assertTrue(
            requirements.approved_operational_responsible_override_contact_id.nullable
        )
        self.assertTrue(requirements.approved_request_version.nullable)
        self.assertFalse(requirements.approved_contact_context_status.nullable)
        self.assertTrue(requirements.approval_revision_id.nullable)
        self.assertTrue(requirements.approved_entry_key.nullable)
        self.assertFalse(requirements.approval_reference_status.nullable)
        self.assertFalse(approval_cycles.workforce_request_id.nullable)
        self.assertFalse(approval_cycles.submitted_request_version.nullable)
        self.assertFalse(approval_cycles.subject_fingerprint.nullable)
        self.assertTrue(approval_cycles.approved_revision_id.nullable)
        self.assertFalse(approval_requirements.approval_cycle_id.nullable)
        self.assertFalse(approval_requirements.request_line_id.nullable)
        self.assertTrue(approval_requirements.asset_type_id.nullable)
        self.assertTrue(approval_requirements.proposed_asset_id.nullable)
        self.assertFalse(approval_requirement_approvers.requirement_id.nullable)
        self.assertFalse(approval_requirement_approvers.app_user_id.nullable)
        self.assertFalse(approval_decisions.requirement_id.nullable)
        self.assertFalse(approval_decisions.app_user_id.nullable)
        self.assertFalse(approval_decisions.action_id.nullable)
        self.assertFalse(approval_revisions.workforce_request_id.nullable)
        self.assertTrue(approval_revisions.previous_revision_id.nullable)
        self.assertFalse(approval_revisions.payload_text.nullable)
        self.assertFalse(approval_references.workforce_request_id.nullable)
        self.assertTrue(approval_references.active_revision_id.nullable)
        self.assertFalse(approval_references.status.nullable)
        self.assertFalse(operational_states.workforce_request_id.nullable)
        self.assertFalse(operational_states.approval_revision_id.nullable)
        self.assertFalse(operational_states.version.nullable)
        self.assertFalse(operational_states.selections_text.nullable)
        self.assertFalse(operational_states.confirmations_text.nullable)
        self.assertTrue(requirements.required_resource_class.nullable)
        self.assertTrue(projects.project_manager_contact_id.nullable)
        self.assertTrue(resources.coordinator_contact_id.nullable)
        self.assertFalse(business_contacts.display_name.nullable)
        self.assertTrue(business_contacts.email.nullable)
        self.assertTrue(business_contacts.phone.nullable)
        self.assertFalse(business_contacts.active.nullable)
        self.assertFalse(business_contacts.source.nullable)
        self.assertTrue(business_contacts.external_system.nullable)
        self.assertTrue(business_contacts.external_entity.nullable)
        self.assertTrue(business_contacts.external_id.nullable)
        self.assertFalse(business_contacts.version.nullable)
        self.assertTrue(requests.work_package_id.nullable)
        self.assertTrue(requests.requester_user_id.nullable)
        self.assertTrue(request_history.actor_user_id.nullable)
        self.assertTrue(requests.erp_task_code.nullable)
        self.assertTrue(requests.erp_task_label.nullable)
        self.assertFalse(requests.project_id.nullable)
        self.assertFalse(requests.aggregate_version.nullable)
        self.assertTrue(requests.cancellation_request_id.nullable)
        self.assertTrue(requests.cancellation_state.nullable)
        self.assertTrue(requests.cancellation_requested_by_user_id.nullable)
        self.assertTrue(requests.cancellation_requested_at.nullable)
        self.assertTrue(requests.cancellation_reason.nullable)
        self.assertTrue(requests.cancellation_resolved_by_user_id.nullable)
        self.assertTrue(requests.cancellation_resolved_at.nullable)
        self.assertTrue(requests.cancellation_resolution_comment.nullable)
        self.assertFalse(requests.line_mode.nullable)
        self.assertFalse(periods.workforce_request_id.nullable)
        self.assertTrue(periods.request_line_id.nullable)
        self.assertFalse(selections.request_line_id.nullable)
        self.assertFalse(periods.start_date.nullable)
        self.assertFalse(periods.end_date.nullable)
        self.assertTrue(periods.hours.nullable)
        self.assertFalse(asset_types.qualification_policy.nullable)
        self.assertFalse(asset_type_competencies.asset_type_id.nullable)
        self.assertFalse(asset_type_competencies.competency_id.nullable)
        self.assertEqual(
            [column.name for column in Base.metadata.tables["asset_type_competencies"].primary_key.columns],
            ["asset_type_id", "competency_id"],
        )
        self.assertTrue(asset_requirements.usage_hours.nullable)
        self.assertFalse(asset_requirements.asset_type_id.nullable)
        self.assertFalse(asset_allocations.asset_id.nullable)
        self.assertTrue(asset_allocations.operator_resource_id.nullable)
        self.assertEqual(
            [column.name for column in Base.metadata.tables["workforce_request_period_selections"].primary_key.columns],
            ["request_line_id", "alternative_group"],
        )
        self.assertFalse(period_requirements.resource_requirement_id.nullable)
        self.assertFalse(period_requirements.period_id.nullable)
        self.assertTrue(availability.resource_id.nullable)
        self.assertFalse(idempotency.actor_name.nullable)
        self.assertFalse(idempotency.command_scope.nullable)
        self.assertFalse(idempotency.idempotency_key.nullable)
        self.assertFalse(idempotency.request_fingerprint.nullable)
        self.assertFalse(idempotency.response_json.nullable)
        self.assertFalse(erp_users.user_id.nullable)
        self.assertFalse(erp_users.employee_external_id.nullable)
        self.assertFalse(erp_users.display_name.nullable)
        self.assertFalse(erp_users.erp_user_active.nullable)
        self.assertFalse(erp_users.local_active.nullable)
        self.assertFalse(erp_users.roles_json.nullable)
        self.assertTrue(users.issuer.nullable)
        self.assertTrue(users.subject.nullable)
        self.assertTrue(users.erp_user_id.nullable)
        self.assertFalse(users.display_name.nullable)
        self.assertTrue(users.email.nullable)
        self.assertFalse(users.roles_json.nullable)
        self.assertFalse(users.active.nullable)
        self.assertFalse(identity_audit.actor_user_id.nullable)
        self.assertFalse(identity_audit.target_user_id.nullable)
        self.assertTrue(identity_audit.erp_user_id.nullable)
        self.assertFalse(identity_audit.action.nullable)
        self.assertFalse(identity_audit.old_state_json.nullable)
        self.assertFalse(identity_audit.new_state_json.nullable)
        self.assertFalse(identity_audit.created_at.nullable)
        self.assertFalse(login_transactions.state_hash.nullable)
        self.assertFalse(login_transactions.nonce.nullable)
        self.assertFalse(login_transactions.code_verifier.nullable)
        self.assertFalse(auth_sessions.auth_mode.nullable)
        self.assertFalse(break_glass.user_id.nullable)
        self.assertFalse(break_glass.login_name.nullable)
        self.assertFalse(break_glass.secret_hash.nullable)
        self.assertFalse(break_glass.credential_version.nullable)
        self.assertFalse(break_glass.active.nullable)
        self.assertFalse(break_glass.failed_attempt_count.nullable)
        self.assertTrue(break_glass.locked_until.nullable)
        self.assertFalse(auth_security_audit.event_type.nullable)
        self.assertFalse(auth_security_audit.success.nullable)
        self.assertTrue(auth_security_audit.credential_id.nullable)
        self.assertTrue(auth_security_audit.target_user_id.nullable)
        self.assertFalse(auth_security_audit.login_name_hash.nullable)
        self.assertFalse(auth_security_audit.reason_code.nullable)
        self.assertFalse(login_transactions.expires_at.nullable)
        self.assertTrue(login_transactions.consumed_at.nullable)
        self.assertFalse(auth_sessions.token_hash.nullable)
        self.assertFalse(auth_sessions.user_id.nullable)
        self.assertFalse(auth_sessions.expires_at.nullable)
        self.assertTrue(auth_sessions.revoked_at.nullable)
        self.assertFalse(communication_contacts.recipient_id.nullable)
        self.assertTrue(communication_contacts.email.nullable)
        self.assertFalse(communication_batches.snapshot_fingerprint.nullable)
        self.assertFalse(communication_batches.status.nullable)
        self.assertFalse(communication_messages.batch_id.nullable)
        self.assertFalse(communication_messages.included.nullable)
        self.assertFalse(communication_snapshots.batch_id.nullable)
        self.assertFalse(communication_snapshots.resource_id.nullable)
        self.assertFalse(planning_history.entity_type.nullable)
        self.assertFalse(planning_history.entity_id.nullable)
        self.assertFalse(planning_history.entity_reference.nullable)
        self.assertFalse(planning_history.action.nullable)
        self.assertTrue(planning_history.parent_reference.nullable)
        self.assertTrue(planning_history.details.nullable)
        self.assertTrue(planning_history.actor_name.nullable)
        self.assertFalse(planning_history.occurred_at.nullable)
        self.assertFalse(task_catalog.project_number.nullable)
        self.assertFalse(task_catalog.task_code.nullable)
        self.assertFalse(task_catalog.label.nullable)
        self.assertFalse(task_catalog.status.nullable)
        self.assertFalse(task_catalog.active.nullable)
        self.assertTrue(task_catalog.operational_responsible_contact_id.nullable)
        self.assertTrue(task_catalog.coordinator_contact_id.nullable)
        self.assertTrue(task_catalog.erp_task_id.nullable)
        self.assertTrue(task_catalog.account_group.nullable)
        self.assertTrue(task_catalog.budget_amount_cad.nullable)
        self.assertTrue(task_catalog.budget_actual_cad.nullable)
        self.assertTrue(task_catalog.budget_diagnostic.nullable)
        self.assertTrue(task_catalog.workforce_eligible.nullable)
        self.assertTrue(task_catalog.resource_class_code.nullable)
        self.assertTrue(task_catalog.average_hourly_cost_cad.nullable)
        self.assertTrue(task_catalog.budget_hours.nullable)
        self.assertTrue(task_catalog.workforce_diagnostics.nullable)
        self.assertFalse(task_sync_state.project_number.nullable)
        self.assertFalse(task_sync_state.last_attempt_at.nullable)
        self.assertFalse(task_sync_state.last_success_at.nullable)
        self.assertFalse(task_sync_state.source_rows.nullable)
        self.assertFalse(task_sync_state.task_count.nullable)
        self.assertFalse(task_sync_state.rejected_rows.nullable)
        self.assertTrue(task_sync_state.duration_ms.nullable)
        self.assertTrue(task_sync_state.last_error_code.nullable)
        self.assertTrue(requests.operational_responsible_override_contact_id.nullable)
        self.assertFalse(competencies.name.nullable)
        self.assertFalse(competencies.active.nullable)
        self.assertFalse(competencies.sort_order.nullable)
        self.assertFalse(resource_competencies.resource_id.nullable)
        self.assertFalse(resource_competencies.competency_id.nullable)
        self.assertFalse(request_competencies.workforce_request_id.nullable)
        self.assertFalse(request_competencies.competency_id.nullable)
        self.assertFalse(request_lines.workforce_request_id.nullable)
        self.assertFalse(request_lines.position.nullable)
        self.assertFalse(request_lines.kind.nullable)
        self.assertFalse(request_lines.slot_count.nullable)
        self.assertTrue(request_lines.required_resource_class.nullable)
        self.assertTrue(request_lines.required_competencies_snapshot.nullable)
        self.assertTrue(request_lines.desired_active_days.nullable)
        self.assertTrue(request_lines.estimated_hours.nullable)
        self.assertTrue(request_lines.estimated_hours_source.nullable)
        self.assertTrue(request_lines.default_hours_per_day.nullable)
        self.assertFalse(request_lines.confirmation.nullable)
        self.assertFalse(request_lines.active.nullable)
        self.assertFalse(line_competencies.request_line_id.nullable)
        self.assertFalse(line_competencies.competency_id.nullable)
        self.assertFalse(requirement_competencies.resource_requirement_id.nullable)
        self.assertFalse(requirement_competencies.competency_id.nullable)
        self.assertTrue(requirements.required_competency_id.nullable)

    def test_business_contact_external_identity_is_complete_and_unique(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        contacts = Base.metadata.tables["business_contacts"]

        with engine.begin() as connection:
            connection.execute(
                contacts.insert().values(
                    id="C1",
                    display_name="Jean",
                    source="LOCAL",
                    external_system="RESOURCEPLANNER",
                    external_entity="EMPLOYEE",
                    external_id="EMP-1",
                )
            )
            connection.execute(
                contacts.insert().values(
                    id="C2",
                    display_name="Contact local sans identité externe",
                    source="LOCAL",
                )
            )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    contacts.insert().values(
                        id="C3",
                        display_name="Identité incomplète",
                        source="LOCAL",
                        external_id="EMP-2",
                    )
                )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    contacts.insert().values(
                        id="C4",
                        display_name="Doublon",
                        source="LOCAL",
                        external_system="RESOURCEPLANNER",
                        external_entity="EMPLOYEE",
                        external_id="EMP-1",
                    )
                )

    def test_metadata_creates_all_tables_on_sqlite_memory(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)

        self.assertEqual(set(inspect(engine).get_table_names()), EXPECTED_TABLES)

    def test_quick_shift_without_request_is_valid_but_request_origin_without_request_is_not(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        projects = Base.metadata.tables["projects"]
        resources = Base.metadata.tables["resources"]
        requirements = Base.metadata.tables["resource_requirements"]
        shifts = Base.metadata.tables["shifts"]

        with engine.begin() as connection:
            connection.execute(
                projects.insert().values(id="P1", number="P-1", name="Projet test")
            )
            connection.execute(resources.insert().values(id="R1", name="Alice"))
            connection.execute(
                requirements.insert().values(
                    id="REQ-QS",
                    project_id="P1",
                    workforce_request_id=None,
                    assigned_resource_id="R1",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=4,
                    origin="QUICK_SHIFT",
                )
            )
            connection.execute(
                shifts.insert().values(
                    id="SHIFT-QS",
                    resource_requirement_id="REQ-QS",
                    resource_id="R1",
                    work_date=DAY,
                    hours=4,
                    source="MANUAL",
                    locked=True,
                )
            )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    requirements.insert().values(
                        id="REQ-INVALID",
                        project_id="P1",
                        workforce_request_id=None,
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=2,
                        origin="REQUEST",
                    )
                )

    def test_idempotency_key_is_unique_per_actor_and_command_scope(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        receipts = Base.metadata.tables["command_idempotency_receipts"]
        base = {
            "id": "I1",
            "actor_name": "Jean",
            "command_scope": "demand.create",
            "idempotency_key": "same-key",
            "request_fingerprint": "a" * 64,
            "response_json": "{}",
        }
        with engine.begin() as connection:
            connection.execute(receipts.insert().values(**base))
            connection.execute(
                receipts.insert().values(
                    **{**base, "id": "I2", "command_scope": "segment.create"}
                )
            )
            connection.execute(
                receipts.insert().values(
                    **{**base, "id": "I3", "actor_name": "Other"}
                )
            )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(receipts.insert().values(**{**base, "id": "I4"}))

    def test_only_holiday_rule_may_be_global(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        availability = Base.metadata.tables["resource_availability_rules"]

        with engine.begin() as connection:
            connection.execute(
                availability.insert().values(
                    id="HOLIDAY",
                    resource_id=None,
                    availability_type="Jour férié",
                    start_date=DAY,
                    end_date=DAY,
                )
            )

        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    availability.insert().values(
                        id="VACATION-GLOBAL",
                        resource_id=None,
                        availability_type="Vacances",
                        start_date=DAY,
                        end_date=DAY,
                    )
                )

    def test_schema_and_indexes_compile_for_supported_dialects(self) -> None:
        dialects = {
            "sqlite": sqlite.dialect(),
            "postgresql": postgresql.dialect(),
            "mssql": mssql.dialect(),
        }

        for dialect_name, dialect in dialects.items():
            for table in Base.metadata.sorted_tables:
                ddl = str(CreateTable(table).compile(dialect=dialect))
                self.assertIn("CREATE TABLE", ddl.upper(), dialect_name)
                for index in table.indexes:
                    index_ddl = str(CreateIndex(index).compile(dialect=dialect))
                    self.assertIn("CREATE", index_ddl.upper(), dialect_name)
                    self.assertIn("INDEX", index_ddl.upper(), dialect_name)

    def test_application_and_pure_engine_do_not_depend_on_sqlalchemy(self) -> None:
        files = [*APPLICATION.rglob("*.py"), DOMAIN_ENGINE]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            self.assertFalse(
                any(module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in imports),
                f"SQLAlchemy leaked into portable core: {path.relative_to(ROOT)}",
            )


if __name__ == "__main__":
    unittest.main()
