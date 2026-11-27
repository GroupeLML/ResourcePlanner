"""Persisted I.2a acquisition coverage; no operational activation."""
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.application.errors import ApplicationUnavailableError
from app.application.operational_contacts import OperationalContactService
from app.application.project_communications import ProjectCommunicationService
from app.application.security import AuthPrincipal
from app.domain.communication_recipient_authority import ERPManagerAuthority
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    deserialize_project_projection, project_projection_fingerprint, serialize_project_projection,
)
from app.infrastructure.sql import (
    AppUser, BusinessContact, Project, ProjectCoManager, Resource, ResourceRequirement, Shift,
    WorkforceRequest,
    create_session_factory, create_sql_engine,
)
from app.infrastructure.sql import communication_canonical_acquisition as acquisition
from app.infrastructure.sql.communication_read_uow import CommunicationReadError
from app.infrastructure.sql.models import Competency, ResourceCompetency
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from tests import test_sql_project_communication_projection as persisted


WEEK = persisted.WEEK
COMPETENCY = "COMP-869"


def mailbox(name):
    return name + chr(64) + persisted.TEST_DOMAIN


class SqlCanonicalCommunicationAcquisitionTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixture = persisted.SqlProjectCommunicationProjectionTests()
        url = fixture._database(directory.name)
        fixture._add_second_project(url)
        self.engine = create_sql_engine(url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
        with self.factory.begin() as session:
            session.get(Project, "P2").project_manager_external_id = None
            session.get(WorkforceRequest, "D1").requester_user_id = "U-RESP"
            session.get(AppUser, "U-RESP").email = " " + mailbox("requester") + " "
            session.get(Resource, "R1").email = " " + mailbox("assigned") + " "
            session.add_all([
                Competency(id=COMPETENCY, name="Surintendant"),
                Resource(id="R-GLOBAL", name="Demo global", email=mailbox("global")),
                Resource(id="R-INACTIVE", name="Demo inactive", active=False,
                         email=mailbox("inactive")),
                Resource(id="R-ERP-OFF", name="Demo ERP inactive", erp_active=False,
                         email=mailbox("erp-off")),
                Resource(id="R-PM", name="Demo manager Resource", external_id="PM-1",
                         email=mailbox("manager-resource")),
            ])
            session.flush()
            session.add_all(ResourceCompetency(resource_id=key, competency_id=COMPETENCY)
                            for key in ("R-GLOBAL", "R-INACTIVE", "R-ERP-OFF"))
            session.add(ProjectCoManager(project_id="P1", business_contact_id="C-PM",
                                         created_by_user_id="U-PM"))

    def acquire(self, week=WEEK, competency=COMPETENCY):
        return acquisition.acquire_canonical_current_projection(
            self.factory, week_start=week, superintendent_competency_id=competency,
        )

    def service(self, session):
        return ProjectCommunicationService(SqlProjectCommunicationRepository(
            session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
            superintendent_competency_id=COMPETENCY,
        ))

    def change(self, model, key, **values):
        with self.factory.begin() as session:
            row = session.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)

    @staticmethod
    def inputs(projection, project_id):
        return next(row for row in projection.recipient_policy_context.message_inputs
                    if row.project_id == project_id)

    @classmethod
    def direct(cls, projection, project_id, role, key):
        return next(row.primary for row in cls.inputs(projection, project_id).candidates
                    if type(row) is not ERPManagerAuthority
                    and row.primary.role == role and row.primary.identity.id == key)

    def test_complete_multi_project_raw_authority_and_frozen_round_trip(self):
        with self.factory() as session:
            v4 = self.service(session).project_projection(week_start=WEEK + timedelta(days=2))
        result = self.acquire(WEEK + timedelta(days=2))
        self.assertEqual((result.week_start, result.week_end), (WEEK, WEEK + timedelta(days=6)))
        self.assertEqual(result.request_provenance_version, 5)
        self.assertTrue(result.request_provenance_complete)
        self.assertEqual(replace(result, request_provenance_version=4, recipient_policy_context=None), v4)
        self.assertEqual({project.project_id for project in result.projects}, {"P1", "P2"})
        shifts = {row.shift_id for project in result.projects for day in project.days
                  for task in day.tasks for row in task.request_assignments}
        self.assertEqual(shifts, {"S1", "S2", "S-P2"})
        policy = result.recipient_policy_context
        self.assertEqual(policy.presentation_mode, "CANONICAL_R2B")
        self.assertIsNone(policy.generator)
        self.assertIsNone(policy.composition_identities)
        self.assertEqual(policy.manager_presentation.project_ids, ("P1", "P2"))
        self.assertEqual([row.employee.resource_id for row in policy.population.employees], ["R-GLOBAL"])
        first, second = self.inputs(result, "P1"), self.inputs(result, "P2")
        self.assertEqual(first.nomination_source.contact_ids, ("C-PM",))
        self.assertEqual(second.nomination_source.contact_ids, ())
        self.assertFalse(any(type(row) is ERPManagerAuthority for row in second.candidates))
        self.assertEqual(next(row for row in first.candidates if type(row) is ERPManagerAuthority)
                         .principal.employee_external_id, "PM-1")
        requester = self.direct(result, "P1", "REQUESTER", "U-RESP")
        assigned = self.direct(result, "P1", "ASSIGNED", "R1")
        self.assertEqual((requester.email, requester.address_source),
                         (" " + mailbox("requester") + " ", "APP_USER_EMAIL"))
        self.assertEqual((assigned.email, assigned.address_source),
                         (" " + mailbox("assigned") + " ", "RESOURCE_EMAIL"))
        self.assertEqual(result.projects[0].requesters[0].email, mailbox("requester"))
        self.assertEqual(sum(type(row) is not ERPManagerAuthority and row.primary.role == "REQUESTER"
                             for row in first.candidates), 1)
        for message in policy.current_resolution:
            self.assertIn(mailbox("global"), [row.address for row in message.recipients])
            self.assertTrue(message.mandatory_complete)
        frozen = serialize_project_projection(result)
        decoded = deserialize_project_projection(frozen)
        self.assertEqual(serialize_project_projection(decoded), frozen)
        self.assertEqual(project_projection_fingerprint(decoded), project_projection_fingerprint(result))

    def test_hidden_nomination_preserves_independent_role_and_deduplicates(self):
        result = self.acquire()
        first = self.inputs(result, "P1")
        self.assertEqual(result.projects[0].project_co_managers, ())
        self.assertEqual(first.nomination_source.contact_ids, ("C-PM",))
        self.assertEqual(self.direct(result, "P1", "CO_MANAGER", "C-PM").email, mailbox("pm"))
        resolution = next(row for row in result.recipient_policy_context.current_resolution
                          if row.project_id == "P1")
        manager = next(row for row in resolution.recipients if row.address == mailbox("pm"))
        self.assertEqual([row.role for row in manager.contributions], ["MANAGER", "CO_MANAGER"])

    def test_ambiguous_exact_namespace_competitor_prevents_unproven_fallback(self):
        with self.factory.begin() as session:
            session.add(Resource(id="R-COMPETE", name="Demo competitor", external_id="E-COMPETE",
                                 active=False, erp_active=False, email=mailbox("competitor")))
            contact = session.get(BusinessContact, "C-PM")
            contact.external_system = "RESOURCEPLANNER"
            contact.external_entity = "EMPLOYEE"
            contact.external_id = "E-COMPETE"
        direct = self.acquire()
        policy = direct.recipient_policy_context
        self.assertIn("R-COMPETE", {row.resource_id for row in policy.identities.resources})
        self.assertIn(RecipientIdentity("ERP_EMPLOYEE", "PM-1"), policy.current_index().ambiguous)
        first = next(row for row in policy.current_resolution if row.project_id == "P1")
        self.assertIn(mailbox("pm"), [row.address for row in first.recipients])
        self.change(BusinessContact, "C-PM", email=None)
        missing = self.acquire()
        self.assertEqual(missing.projects[0].project_manager.email, mailbox("manager-resource"))
        first = next(row for row in missing.recipient_policy_context.current_resolution
                     if row.project_id == "P1")
        self.assertNotIn(mailbox("manager-resource"), [row.address for row in first.recipients])
        self.change(BusinessContact, "C-PM", external_system="OTHER")
        unrelated = self.acquire().recipient_policy_context
        self.assertNotIn("R-COMPETE", {row.resource_id for row in unrelated.identities.resources})
        self.assertNotIn(RecipientIdentity("ERP_EMPLOYEE", "PM-1"), unrelated.current_index().ambiguous)

    def test_padded_presentation_only_resource_never_becomes_delivery_root(self):
        self.change(Resource, "R-PM", external_id="PM-1  ")
        self.change(BusinessContact, "C-PM", email=None)

        def collation(conn, cursor, statement, parameters, context, executemany):
            return statement.replace("resources.external_id IN",
                                     "resources.external_id COLLATE RTRIM IN"), parameters

        event.listen(self.engine, "before_cursor_execute", collation, retval=True)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", collation)
        result = self.acquire()
        policy = result.recipient_policy_context
        self.assertEqual(result.projects[0].project_manager.email, mailbox("manager-resource"))
        self.assertEqual(policy.manager_presentation.resources[0].employee_external_id, "PM-1  ")
        self.assertNotIn("R-PM", {row.resource_id for row in policy.identities.resources})
        first = next(row for row in policy.current_resolution if row.project_id == "P1")
        self.assertNotIn(mailbox("manager-resource"), [row.address for row in first.recipients])

    def test_missing_or_contradictory_independent_partial_closure_fails_closed(self):
        original = acquisition.capture_erp_manager_authorities
        for missing in (True, False):
            with self.subTest(missing=missing):
                def altered(session, project_ids):
                    managers, evidence = original(session, project_ids)
                    self.assertTrue(evidence.resources)
                    resource = evidence.resources[0]
                    bad = () if missing else (replace(resource, employee_external_id="DIFFERENT"),)
                    return managers, replace(evidence, resources=bad)

                with patch.object(acquisition, "capture_erp_manager_authorities", side_effect=altered):
                    with self.assertRaisesRegex(ValueError, "independent identity closure"):
                        self.acquire()
                self.assertEqual(self.engine.pool.checkedout(), 0)

        original_rp = acquisition.capture_rp_co_manager_authorities
        def incomplete(session, project_ids):
            nominations, evidence = original_rp(session, project_ids)
            self.assertTrue(evidence.contacts)
            return nominations, replace(evidence, contacts=())
        with patch.object(acquisition, "capture_rp_co_manager_authorities", side_effect=incomplete):
            with self.assertRaisesRegex(ValueError, "independent identity closure"):
                self.acquire()

    def test_inexact_assignment_relationship_and_missing_raw_address_row_fail_closed(self):
        original = acquisition._read_matches
        for missing in (False, True):
            with self.subTest(missing=missing):
                def altered(session, columns, match_column, values):
                    for row in original(session, columns, match_column, values):
                        if match_column is acquisition.Shift.id and row[0] == "S1" and not missing:
                            yield row[0], row[1], row[2] + " "
                        elif match_column is acquisition.AppUser.id and row[0] == "U-RESP" and missing:
                            continue
                        else:
                            yield row

                with patch.object(acquisition, "_read_matches", side_effect=altered):
                    with self.assertRaisesRegex(ValueError, "persisted assignment"):
                        self.acquire()
                self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_single_owned_session_and_transaction_materialization_before_release(self):
        sessions, connections, roots, calls, trace = [], [], [], [], []
        real_make = sessionmaker.__call__
        def make(factory, **options):
            session = real_make(factory, **options)
            sessions.append(session)
            event.listen(session, "before_flush", lambda *_: self.fail("flush"))
            event.listen(session, "before_commit", lambda *_: self.fail("commit"))
            return session

        def checked(session, stage):
            calls.append(stage)
            self.assertIs(session, sessions[0])
            connection = session.connection()
            connections.append(connection)
            roots.append(connection.get_transaction())
            self.assertTrue(roots[-1].is_active)
            self.assertTrue(connection.connection.driver_connection.in_transaction)

        from contextlib import ExitStack
        def checkout(dbapi, *_):
            trace.append(("checkout", dbapi))
        def checkin(dbapi, *_):
            trace.append(("checkin", dbapi))
        def rollback(connection):
            trace.append(("rollback", connection))
        def sql(connection, cursor, statement, parameters, context, many):
            if statement == "BEGIN":
                return
            self.assertIs(connection, connections[0] if connections else session_connection[0])
            self.assertTrue(context.compiled is not None and context.compiled.statement.is_select)
            trace.append(("read", connection))
        session_connection = []
        def begin(connection):
            session_connection.append(connection)
        def repository_stage(method, stage):
            def call(repository, *args, **kwargs):
                checked(repository._session, stage)
                return method(repository, *args, **kwargs)
            return call
        def adapter_stage(method, stage):
            def call(session, *args, **kwargs):
                checked(session, stage)
                return method(session, *args, **kwargs)
            return call

        with ExitStack() as stack:
            for target, name, fn in ((self.engine.pool, "checkout", checkout),
                                     (self.engine.pool, "checkin", checkin),
                                     (self.engine, "rollback", rollback),
                                     (self.engine, "begin", begin),
                                     (self.engine, "before_cursor_execute", sql)):
                event.listen(target, name, fn)
                stack.callback(event.remove, target, name, fn)
            stack.enter_context(patch.object(sessionmaker, "__call__", make))
            for name in ("list_assignments", "read_superintendent_population"):
                method = getattr(SqlProjectCommunicationRepository, name)
                stack.enter_context(patch.object(SqlProjectCommunicationRepository, name,
                                                 repository_stage(method, name)))
            for name in ("capture_erp_manager_authorities", "capture_rp_co_manager_authorities",
                         "capture_manager_presentation", "_capture_assignment_inputs",
                         "read_recipient_identity_evidence"):
                method = getattr(acquisition, name)
                stack.enter_context(patch.object(acquisition, name, adapter_stage(method, name)))
            result = self.acquire()
            before = tuple(trace)
            self.assertEqual(deserialize_project_projection(serialize_project_projection(result)),
                             result)
            project_projection_fingerprint(result)
            self.assertEqual(tuple(trace), before)
            with self.assertRaises(FrozenInstanceError):
                result.week_start = WEEK + timedelta(days=1)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(calls, ["list_assignments", "capture_erp_manager_authorities",
                                 "capture_rp_co_manager_authorities", "capture_manager_presentation",
                                 "read_superintendent_population", "_capture_assignment_inputs",
                                 "read_recipient_identity_evidence"])
        self.assertTrue(all(connection is connections[0] for connection in connections))
        self.assertTrue(all(root is roots[0] for root in roots))
        self.assertTrue(connections[0].closed)
        self.assertFalse(sessions[0].in_transaction())
        self.assertEqual([name for name, _ in trace if name != "read"],
                         ["checkout", "rollback", "checkin"])
        self.assertIs(trace[0][1], trace[-1][1])
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_writer_commit_between_business_and_source_reads_is_snapshot_coherent(self):
        before = self.acquire()
        original = acquisition.capture_erp_manager_authorities
        commits = []

        def change_then_capture(session, project_ids):
            # WAL permits a second connection to commit while the original
            # owned read transaction remains pinned to its earlier snapshot.
            driver = session.connection().connection.driver_connection
            with self.factory.begin() as writer:
                self.assertIsNot(writer.connection().connection.driver_connection, driver)
                writer.get(AppUser, "U-RESP").email = mailbox("requester-new")
                writer.get(Resource, "R1").email = mailbox("assigned-new")
                writer.get(Resource, "R-GLOBAL").email = mailbox("global-new")
                writer.get(BusinessContact, "C-PM").email = mailbox("manager-new")
                writer.add(ProjectCoManager(project_id="P2", business_contact_id="C-RESP",
                                            created_by_user_id="U-PM"))
            commits.append(True)
            return original(session, project_ids)

        with patch.object(acquisition, "capture_erp_manager_authorities", side_effect=change_then_capture):
            during = self.acquire()
        self.assertEqual(commits, [True])
        self.assertEqual(serialize_project_projection(during), serialize_project_projection(before))
        after = self.acquire()
        self.assertEqual(self.direct(after, "P1", "REQUESTER", "U-RESP").email, mailbox("requester-new"))
        self.assertEqual(self.direct(after, "P1", "ASSIGNED", "R1").email, mailbox("assigned-new"))
        self.assertEqual(after.projects[0].project_manager.email, mailbox("manager-new"))
        self.assertEqual(after.recipient_policy_context.population.employees[0].employee.email,
                         mailbox("global-new"))
        self.assertEqual(self.inputs(after, "P2").nomination_source.contact_ids, ("C-RESP",))
        self.assertNotEqual(project_projection_fingerprint(after), project_projection_fingerprint(before))

    def test_stage_failure_does_not_retry_or_touch_unrelated_caller_transaction(self):
        origin = RuntimeError("Acquisition stage failure")
        with self.factory() as unrelated:
            unrelated.begin()
            connection = unrelated.connection()
            root = connection.get_transaction()
            with patch.object(acquisition, "capture_rp_co_manager_authorities", side_effect=origin) as stage:
                with self.assertRaises(RuntimeError) as caught:
                    self.acquire()
                self.assertIs(caught.exception, origin)
                stage.assert_called_once()
            self.assertTrue(root.is_active)
            self.assertIs(connection.get_transaction(), root)
            self.assertFalse(connection.closed)
            self.assertEqual(self.engine.pool.checkedout(), 1)

    def test_cleanup_failure_after_materialization_never_returns_result(self):
        origin = RuntimeError("Acquisition cleanup failure")
        original = acquisition.replace
        constructed = []
        checkouts = []
        def record(dbapi, *_):
            checkouts.append(dbapi)
        def materialize(projection, **changes):
            result = original(projection, **changes)
            constructed.append(result)
            return result
        def fail(_session, _transaction):
            raise origin
        event.listen(self.engine.pool, "checkout", record)
        self.addCleanup(event.remove, self.engine.pool, "checkout", record)
        event.listen(Session, "after_transaction_end", fail)
        try:
            with patch.object(acquisition, "replace", side_effect=materialize):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    self.acquire()
        finally:
            event.remove(Session, "after_transaction_end", fail)
        self.assertEqual(len(constructed), 1)
        self.assertEqual(constructed[0].request_provenance_version, 5)
        self.assertIn(origin, caught.exception.exceptions)
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_empty_scope_preserves_global_population_and_bounded_exact_key_reads(self):
        # Only canonical identity acquisition has a large IN-list here;
        # inherited v4 assignment queries are deliberately not exercised.
        keys = [f"R-G-{i:04d}" for i in range(901)]
        with self.factory.begin() as session:
            session.add_all(Resource(id=key, name=key, email=mailbox(key)) for key in keys)
            session.flush()
            session.add_all(ResourceCompetency(resource_id=key, competency_id=COMPETENCY)
                            for key in keys)
        lengths = []
        def instrument(conn, cursor, statement, parameters, context, executemany):
            if "resources.id IN" in statement:
                lengths.append(len(parameters))
        event.listen(self.engine, "before_cursor_execute", instrument)
        try:
            result = self.acquire(WEEK + timedelta(days=28))
        finally:
            event.remove(self.engine, "before_cursor_execute", instrument)
        policy = result.recipient_policy_context
        self.assertEqual(result.projects, ())
        self.assertEqual(policy.message_inputs, ())
        self.assertEqual(policy.manager_presentation.project_ids, ())
        self.assertEqual(policy.presentation_mode, "CANONICAL_R2B")
        self.assertEqual({row.employee.resource_id for row in policy.population.employees},
                         {*keys, "R-GLOBAL"})
        self.assertEqual({row.resource_id for row in policy.identities.resources},
                         {*keys, "R-GLOBAL"})
        self.assertEqual(lengths, [900, 2])
        self.assertEqual(deserialize_project_projection(serialize_project_projection(result)), result)

    def test_inactive_boundary_preserves_v4_and_provisional_entry_point(self):
        with patch.object(acquisition, "acquire_canonical_current_projection",
                          side_effect=AssertionError("Inactive acquisition invoked")) as unused:
            with self.factory() as session:
                service = self.service(session)
                v4 = service.project_projection(week_start=WEEK)
                provisional = service.capture_v5_projection(week_start=WEEK)
        unused.assert_not_called()
        self.assertEqual(v4.request_provenance_version, 4)
        self.assertEqual(provisional.recipient_policy_context.presentation_mode, "UNAVAILABLE_PRE_R2B")
        roles = [role for row in provisional.recipient_policy_context.message_inputs
                 for role in row.to_payload()["role_evidence"]]
        self.assertIn("PROVISIONAL_CONTACT_CAPTURE", {row["relationship"] for row in roles})

    def test_requester_duplicate_role_retains_originating_fallback_label(self):
        with self.factory.begin() as session:
            session.get(AppUser, "U-RESP").display_name = ""
            session.get(WorkforceRequest, "D1").requester_name = "Demo original request"
            session.add(WorkforceRequest(id="D2", project_id="P1", requester_user_id="U-RESP",
                                         requester_name="Demo second request"))
            session.flush()
            session.add(ResourceRequirement(
                id="REQ-D2", project_id="P1", workforce_request_id="D2",
                assigned_resource_id="R2", start_date=WEEK, end_date=WEEK,
                planned_hours=Decimal("1"), description="Demo second task",
                origin="REQUEST", status="Planifié", approved_contact_context_status="LEGACY_UNKNOWN",
            ))
            session.flush()
            session.add(Shift(id="S-D2", resource_requirement_id="REQ-D2", resource_id="R2",
                              work_date=WEEK, hours=Decimal("1"), allocation_type="Flexible"))
        with self.factory() as session:
            business = self.service(session).project_projection(week_start=WEEK)
        result = self.acquire()
        participant, = business.projects[0].requesters
        self.assertEqual(result.projects[0].requesters, (participant,))
        candidates = [row.primary for row in self.inputs(result, "P1").candidates
                      if type(row) is not ERPManagerAuthority and row.primary.role == "REQUESTER"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].display_name, participant.display_name)
        self.assertEqual(candidates[0].email, " " + mailbox("requester") + " ")

    def test_assignment_scalar_reads_use_bounded_batches_in_actual_acquisition(self):
        # Distinct persisted requirements, Resources and shifts: exercise the
        # new exact relationship/address reads beyond 900 without claiming
        # inherited v4 SQL Server parameter safety.
        with self.factory.begin() as session:
            session.add_all(Resource(id=f"R-B-{n:04d}", name=f"Demo {n}",
                                     email=mailbox("batch")) for n in range(901))
            session.flush()
            session.add_all(ResourceRequirement(
                id=f"REQ-B-{n:04d}", project_id="P2", assigned_resource_id=f"R-B-{n:04d}",
                start_date=WEEK, end_date=WEEK, planned_hours=Decimal("1"),
                description="Demo batch task", status="Planifié", origin="AD_HOC",
                approved_contact_context_status="NOT_APPLICABLE",
            ) for n in range(901))
            session.flush()
            session.add_all(Shift(id=f"S-B-{n:04d}", resource_requirement_id=f"REQ-B-{n:04d}",
                                  resource_id=f"R-B-{n:04d}", work_date=WEEK,
                                  hours=Decimal("1"), allocation_type="Flexible") for n in range(901))
        original = acquisition._capture_assignment_inputs
        statements = []
        def instrument(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement.lower().replace("\n", " "), len(parameters)))

        def measured(session, assignments, project_ids):
            connection = session.connection()
            event.listen(connection, "before_cursor_execute", instrument)
            try:
                return original(session, assignments, project_ids)
            finally:
                event.remove(connection, "before_cursor_execute", instrument)

        with patch.object(acquisition, "_capture_assignment_inputs", side_effect=measured):
            result = self.acquire()
        self.assertTrue(statements)
        self.assertTrue(all(" where " in sql and 0 < size <= 900 for sql, size in statements))
        for table, expected in (("shifts", [900, 4]), ("resource_requirements", [900, 4]),
                                ("resources", [900, 3])):
            self.assertEqual([size for sql, size in statements if table + ".id in" in sql], expected)
        assigned = [row.primary.identity.id for row in self.inputs(result, "P2").candidates
                    if type(row) is not ERPManagerAuthority and row.primary.role == "ASSIGNED"]
        self.assertEqual(set(assigned), {"R1", *(f"R-B-{n:04d}" for n in range(901))})

    def test_collation_variant_resource_not_added_to_exact_assigned_authority(self):
        with self.factory.begin() as session:
            session.add(Resource(id="r1", name="Demo collation variant", email=mailbox("variant")))

        def collation(conn, cursor, statement, parameters, context, executemany):
            return statement.replace("resources.id IN", "resources.id COLLATE NOCASE IN"), parameters

        event.listen(self.engine, "before_cursor_execute", collation, retval=True)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", collation)
        result = self.acquire()
        self.assertEqual(self.direct(result, "P1", "ASSIGNED", "R1").email,
                         " " + mailbox("assigned") + " ")
        self.assertNotIn("r1", {row.resource_id for row in result.recipient_policy_context.identities.resources})

    def test_nested_acquisition_is_rejected_before_second_checkout(self):
        original = acquisition.capture_erp_manager_authorities
        checkouts = []
        def record(dbapi, *_):
            checkouts.append(dbapi)
        def nested(session, project_ids):
            with self.assertRaisesRegex(CommunicationReadError, "nested Session"):
                self.acquire()
            self.assertEqual(len(checkouts), 1)
            return original(session, project_ids)

        event.listen(self.engine.pool, "checkout", record)
        self.addCleanup(event.remove, self.engine.pool, "checkout", record)
        with patch.object(acquisition, "capture_erp_manager_authorities", side_effect=nested):
            result = self.acquire()
        self.assertEqual(result.request_provenance_version, 5)
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_canonical_result_still_rejected_by_operational_v5_guards(self):
        canonical = self.acquire()
        with self.factory() as session:
            service = self.service(session)
            v4 = service.project_projection(week_start=WEEK)
            with self.assertRaisesRegex(ValueError, "v5/P5"):
                build_project_confirmation_batch(canonical)
            with self.assertRaisesRegex(ValueError, "v5/P5"):
                build_project_delta_batch(v4, canonical)
            principal = AuthPrincipal.from_roles(
                local_user_id=None, issuer="urn:resourceplanner:local", subject="local-user",
                display_name="Demo generator", email=mailbox("generator"), roles=(), auth_mode="local",
            )
            with self.assertRaises(ApplicationUnavailableError) as caught:
                service.capture_v5_projection(week_start=WEEK, add_generator_cc=True,
                                              generator_principal=principal)
            self.assertEqual(caught.exception.code, "project_communication_v5_generator_authority_unavailable")
