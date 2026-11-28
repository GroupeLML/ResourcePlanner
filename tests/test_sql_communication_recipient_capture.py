"""Actual SQL capture regression cases; no live v5 generation."""
from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import event

from app.application.operational_contacts import OperationalContactService
from app.application.project_communications import ProjectCommunicationService
from app.application.errors import ApplicationUnavailableError, ApplicationValidationError
from app.application.security import AuthPrincipal
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.project_communication import build_project_communication_projection
from app.domain.project_communication_messages import (
    deserialize_project_projection, project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_recipient_resolution import AddressFailure
from app.infrastructure.sql import (
    AppUser, BusinessContact, ProjectCoManager, Resource, WorkforceRequest,
    create_session_factory, create_sql_engine,
)
from app.infrastructure.sql.models import Competency, ResourceCompetency
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from tests import test_sql_project_communication_projection as fixtures


WEEK = fixtures.WEEK
COMP = "COMP-869"


def mailbox(local):
    return local + chr(64) + "example.test"


class SqlRecipientCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.url = fixtures.SqlProjectCommunicationProjectionTests()._database(directory.name)
        self.engine = create_sql_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(Competency(id=COMP, name="Surintendant"))
            session.add_all([
                Resource(id="R-PM", name="Demo manager", external_id="PM-1"),
                Resource(id="R-GLOBAL", name="Demo global", email=mailbox("global")),
                Resource(id="R-MISSING", name="Demo missing"),
                Resource(id="R-INVALID", name="Demo invalid", email="bad-address"),
                Resource(id="R-AMBIG", name="Demo ambiguous", email=mailbox("one") + "," + mailbox("two")),
                Resource(id="R-OFF", name="Demo inactive", active=False),
                Resource(id="R-ERP-OFF", name="Demo ERP inactive", erp_active=False),
                Resource(id="R-NONMEMBER", name="Surintendant"),
                BusinessContact(id="C-CO", display_name="Demo co-manager", email=mailbox("co")),
                AppUser(id="U-REQUESTER", display_name="Demo requester", email=mailbox("requester"), roles_json="[]"),
            ])
            session.flush()
            session.add_all(ResourceCompetency(resource_id=key, competency_id=COMP) for key in (
                "R-PM", "R-GLOBAL", "R-MISSING", "R-INVALID", "R-AMBIG", "R-OFF", "R-ERP-OFF",
            ))
            session.add(ProjectCoManager(project_id="P1", business_contact_id="C-CO", created_by_user_id="U-REQUESTER"))
            session.get(WorkforceRequest, "D1").requester_user_id = "U-REQUESTER"

    def repository(self, session, binding=COMP):
        return SqlProjectCommunicationRepository(
            session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
            superintendent_competency_id=binding,
        )

    def capture(self, binding=COMP):
        with self.factory() as session:
            repository = self.repository(session, binding)
            projection = build_project_communication_projection(
                week_start=WEEK, week_end=WEEK.replace(day=27),
                assignments=repository.list_assignments(week_start=WEEK, week_end=WEEK.replace(day=27)),
                request_provenance_version=4,
            )
            with patch.object(repository, "read_superintendent_population", wraps=repository.read_superintendent_population) as read:
                context = repository.capture_recipient_context(projection)
                read.assert_called_once_with()
            return replace(projection, request_provenance_version=5, recipient_policy_context=context)

    def test_multi_project_population_roles_and_canonical_round_trip(self):
        fixtures.SqlProjectCommunicationProjectionTests._add_second_project(self.url)
        projection = self.capture()
        context = projection.recipient_policy_context
        self.assertEqual([row.project_id for row in context.messages], ["P1", "P2"])
        self.assertEqual([row.employee.resource_id for row in context.population.employees],
                         ["R-AMBIG", "R-GLOBAL", "R-INVALID", "R-MISSING", "R-PM"])
        first = context.message_inputs[0]
        candidates = {row.primary.role: row.primary for row in first.candidates if row.primary.role != "ASSIGNED"}
        self.assertEqual(candidates["MANAGER"].identity, RecipientIdentity("CONTACT", "C-PM"))
        self.assertEqual(candidates["MANAGER"].email, mailbox("pm"))
        self.assertEqual(candidates["CO_MANAGER"].email, mailbox("co"))
        self.assertEqual(candidates["REQUESTER"].identity, RecipientIdentity("USER", "U-REQUESTER"))
        self.assertEqual(candidates["REQUESTER"].email, mailbox("requester"))
        self.assertEqual({row.primary.identity.id: row.primary.email for row in first.candidates if row.primary.role == "ASSIGNED"},
                         {"R1": mailbox("resource-one"), "R2": mailbox("resource-two")})
        for message in context.messages:
            mandatory = {row.resource_id: row for row in message.mandatory}
            self.assertEqual(mandatory["R-GLOBAL"].effective_address, mailbox("global"))
            self.assertTrue(mandatory["R-PM"].fulfilled_by_existing)
            self.assertEqual(mandatory["R-AMBIG"].blocker_reason, AddressFailure.AMBIGUOUS)
            self.assertEqual(mandatory["R-INVALID"].blocker_reason, AddressFailure.INVALID)
            self.assertEqual(mandatory["R-MISSING"].blocker_reason, AddressFailure.MISSING)
        restored = deserialize_project_projection(serialize_project_projection(projection))
        self.assertEqual(restored, projection)
        self.assertEqual(project_projection_fingerprint(restored), project_projection_fingerprint(projection))

    def test_contact_precedence_and_proven_resource_fallback(self):
        with self.factory.begin() as session:
            session.get(Resource, "R-PM").email = mailbox("fallback")
        before = self.capture().recipient_policy_context
        manager = before.message_inputs[0].candidates[0]
        self.assertEqual(manager.primary.email, mailbox("pm"))
        self.assertEqual(manager.resource_fallback.email, mailbox("fallback"))
        self.assertEqual(before.messages[0].recipients[0].address, mailbox("pm"))
        with self.factory.begin() as session:
            session.get(BusinessContact, "C-PM").email = None
        after = self.capture().recipient_policy_context
        self.assertIsNone(after.message_inputs[0].candidates[0].primary.email)
        self.assertEqual(after.messages[0].recipients[0].address, mailbox("fallback"))
        self.assertEqual(after.messages[0].recipients[0].selected.address_source, "RESOURCE_EMAIL")

    def test_competing_inactive_identity_and_fresh_email_correction(self):
        before = self.capture()
        with self.factory.begin() as session:
            session.add(Resource(id="R-CONFLICT", name="Demo conflict", external_id="E-CONFLICT", active=False))
            contact = session.get(BusinessContact, "C-PM")
            contact.external_system = "RESOURCEPLANNER"
            contact.external_entity = "EMPLOYEE"
            contact.external_id = "E-CONFLICT"
            session.get(Resource, "R-MISSING").email = mailbox("corrected")
        after = self.capture()
        context = after.recipient_policy_context
        self.assertIn(RecipientIdentity("RESOURCE", "R-CONFLICT"), context.identities.index().ambiguous)
        mandatory = {row.resource_id: row for row in context.messages[0].mandatory}
        self.assertFalse(mandatory["R-PM"].fulfilled_by_existing)
        self.assertEqual(mandatory["R-PM"].blocker_reason, AddressFailure.MISSING)
        self.assertEqual(mandatory["R-MISSING"].effective_address, mailbox("corrected"))
        old = {row.resource_id: row for row in before.recipient_policy_context.messages[0].mandatory}
        self.assertEqual(old["R-MISSING"].blocker_reason, AddressFailure.MISSING)
        self.assertNotEqual(project_projection_fingerprint(before), project_projection_fingerprint(after))

    def test_empty_binding_distinction(self):
        with self.factory.begin() as session:
            session.add(Competency(id="COMP-EMPTY", name="Demo empty"))
            session.add(Competency(id="COMP-OFF", name="Demo inactive", active=False))
        empty = self.capture("COMP-EMPTY").recipient_policy_context
        self.assertTrue(empty.population.population_complete)
        self.assertEqual(empty.population.employees, ())
        for binding, diagnostic in ((None, "SUPERINTENDENT_BINDING_MISSING"),
                                    ("COMP-UNKNOWN", "SUPERINTENDENT_BINDING_INVALID"),
                                    ("COMP-OFF", "SUPERINTENDENT_BINDING_INACTIVE")):
            context = self.capture(binding).recipient_policy_context
            self.assertFalse(context.population.population_complete)
            self.assertEqual(context.population.binding_diagnostic, diagnostic)
            self.assertFalse(context.messages[0].mandatory_complete)

    def test_capture_reads_are_batched_across_projects(self):
        def counts():
            with self.factory() as session:
                repository = self.repository(session)
                projection = build_project_communication_projection(
                    week_start=WEEK, week_end=WEEK.replace(day=27),
                    assignments=repository.list_assignments(week_start=WEEK, week_end=WEEK.replace(day=27)),
                    request_provenance_version=4,
                )
                statements = []
                def record(conn, cursor, statement, parameters, context, executemany):
                    statements.append((statement, len(parameters)))
                event.listen(self.engine, "before_cursor_execute", record)
                try:
                    repository.capture_recipient_context(projection)
                finally:
                    event.remove(self.engine, "before_cursor_execute", record)
                return statements
        first = counts()
        fixtures.SqlProjectCommunicationProjectionTests._add_second_project(self.url)
        second = counts()
        self.assertEqual(len(first), len(second))
        self.assertEqual(sum("resource_competencies" in sql.lower() for sql, _ in second), 1)
        self.assertTrue(all(size <= 900 for _, size in second))

    def test_unrooted_erp_manager_fallback_is_not_silently_lost(self):
        with self.factory.begin() as session:
            session.get(AppUser, "U-PM").employee_external_id = "OTHER-PM"
            session.get(Resource, "R-PM").email = mailbox("fallback")
        with self.assertRaisesRegex(ValueError, "no representable project participant root"):
            self.capture()

    def test_application_capture_keeps_live_v4_and_normalizes_week(self):
        with self.factory() as session:
            repository = self.repository(session)
            service = ProjectCommunicationService(repository)
            with patch.object(repository, "read_superintendent_population", wraps=repository.read_superintendent_population) as read:
                current = service.capture_v5_projection(week_start=WEEK.replace(day=23))
                read.assert_called_once_with()
            live = service.project_projection(week_start=WEEK)
            self.assertEqual(current.week_start, WEEK)
            self.assertEqual(current.projects, live.projects)
            self.assertEqual(current.request_provenance_version, 5)
            self.assertEqual(live.request_provenance_version, 4)
            self.assertIsNone(live.recipient_policy_context)
            self.assertFalse(session.new)
            self.assertFalse(session.dirty)
            self.assertFalse(session.deleted)

    def test_generator_authority_is_not_fabricated(self):
        with self.factory() as session:
            repository = self.repository(session)
            service = ProjectCommunicationService(repository)
            principal = AuthPrincipal.from_roles(
                local_user_id=None, issuer="urn:test", subject="generator",
                display_name="Demo generator", email=mailbox("generator"),
                roles=(), auth_mode="test",
            )
            with patch.object(repository, "capture_recipient_context", wraps=repository.capture_recipient_context) as capture:
                with self.assertRaises(ApplicationValidationError):
                    service.capture_v5_projection(week_start=WEEK, add_generator_cc=True)
                with self.assertRaises(ApplicationUnavailableError) as exc:
                    service.capture_v5_projection(week_start=WEEK, add_generator_cc=True, generator_principal=principal)
                self.assertEqual(exc.exception.code, "project_communication_v5_generator_authority_unavailable")
                capture.assert_not_called()
            projection = service.capture_v5_projection(week_start=WEEK, generator_principal=principal)
            self.assertFalse(any(row.primary.role == "GENERATOR"
                                 for message in projection.recipient_policy_context.message_inputs
                                 for row in message.candidates))

    def test_large_population_identity_queries_respect_batch_limit(self):
        with self.factory.begin() as session:
            keys = ["R-BULK-" + str(i) for i in range(905)]
            session.add_all(Resource(id=key, name="Demo bulk", external_id="E-" + key,
                                     email=mailbox(key.lower())) for key in keys)
            session.flush()
            session.add_all(ResourceCompetency(resource_id=key, competency_id=COMP) for key in keys)
        with self.factory() as session:
            repository = self.repository(session)
            projection = ProjectCommunicationService(repository).project_projection(week_start=WEEK)
            sizes = []
            def record(conn, cursor, statement, parameters, context, executemany):
                sizes.append(len(parameters))
            event.listen(self.engine, "before_cursor_execute", record)
            try:
                context = repository.capture_recipient_context(projection)
            finally:
                event.remove(self.engine, "before_cursor_execute", record)
            self.assertEqual(len(context.population.employees), 910)
            self.assertEqual(len(context.messages[0].mandatory), 910)
            self.assertIn(900, sizes)
            self.assertTrue(all(size <= 900 for size in sizes))
