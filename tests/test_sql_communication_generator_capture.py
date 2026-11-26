"""Persisted authenticated generator capture; no operational v5 integration."""
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event

from app.application.errors import ApplicationUnavailableError, ApplicationValidationError
from app.application.project_communications import ProjectCommunicationService
from app.application.security import AuthPrincipal
from app.domain.communication_actual_message_recipients import ActualMessageComposition
from app.domain.communication_artifact_integrity import ActualCompositionIntegrity
from app.domain.communication_message_context import MessageContextEvidence
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import RecipientIdentityEvidence, RecipientPolicyContext
from app.domain.project_communication_messages import project_projection_fingerprint
from app.domain.superintendent_recipient_resolution import AddressFailure
from app.infrastructure.sql import (
    AppUser, Base, BusinessContact, Resource, SqlAuthSessionRepository,
    create_session_factory, create_sql_engine,
)
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.communication_generator_capture import capture_generator_context
from app.infrastructure.sql.communication_recipient_identity import read_recipient_identity_evidence
from app.server.security import static_auth_resolver
from tests import test_communication_artifact_integrity as artifacts
from tests import test_project_communication_v5 as fixtures


mailbox = fixtures.mailbox
FULL = artifacts.FULL


class SqlGeneratorCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{directory.name}/generator.db")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        self.f = fixtures.ProjectCommunicationV5Tests()
        self.f.setUp()
        with self.factory.begin() as session:
            session.add_all(BusinessContact(id=key, display_name="Demo person")
                            for key in ("C-PM", "C-CO", "C-RQ", "C-G"))
            session.add_all([
                Resource(id="R-1", name="Demo person", external_id="E-1", email=mailbox("r-1")),
                Resource(id="R-2", name="Demo person", external_id="E-2"),
                Resource(id="R-G", name="Demo person", external_id="E-G", active=False),
            ])
            session.flush()
            for key, contact, employee in (
                ("U-PM", "C-PM", None), ("U-CO", "C-CO", None),
                ("U-RQ", "C-RQ", None), ("U-G", "C-G", "E-G"),
            ):
                session.add(AppUser(id=key, issuer="urn:test", subject=key,
                                    display_name="Demo person", email=mailbox("persisted"),
                                    business_contact_id=contact, employee_external_id=employee,
                                    roles_json="[]"))
        self.current = self.current_policy()

    def change(self, model, key, **values):
        with self.factory.begin() as session:
            row = session.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)

    def current_policy(self, *, complete=False):
        roots = (
            RecipientIdentity("CONTACT", "C-PM"), RecipientIdentity("CONTACT", "C-CO"),
            RecipientIdentity("USER", "U-RQ"), RecipientIdentity("RESOURCE", "R-1"),
            RecipientIdentity("RESOURCE", "R-2"),
        )
        with self.factory() as session:
            evidence = read_recipient_identity_evidence(session, roots)
        population = self.f.population
        if complete:
            original = population.to_population()
            population = RecipientPolicyPopulation.capture(replace(original, employees=(
                original.employees[0], replace(original.employees[1], email=mailbox("r-2")),
            )))
        return RecipientPolicyContext.capture(population, evidence, (self.f.inputs,))

    def principal(self, key="U-G", **changes):
        values = dict(local_user_id=key, issuer="urn:test", subject=key or "unlinked",
                      display_name="Demo person", email=mailbox("authenticated"),
                      roles=(), auth_mode="oidc")
        values.update(changes)
        return AuthPrincipal.from_roles(**values)

    def session_principal(self, key, mode):
        value = "synthetic-session-value"
        with self.factory.begin() as session:
            repository = SqlAuthSessionRepository(session)
            repository.create_session(raw_token=value, user_id=key,
                                      expires_at=utc_now() + timedelta(hours=1), auth_mode=mode)
            principal = repository.resolve_principal(value)
        self.assertIsNotNone(principal)
        return principal

    def capture(self, principal=None, *, current=None, enabled=True):
        with self.factory() as session:
            result = capture_generator_context(
                session, self.current if current is None else current,
                add_generator_cc=enabled, principal=principal,
            )
            self.assertFalse(session.new or session.dirty or session.deleted)
            return result

    def actual(self, policy, previous=None):
        current = self.f.build(5, policy)
        context = MessageContextEvidence(FULL if previous is None else artifacts.DELTA,
                                         current, None if previous is None else artifacts.baseline(previous))
        return ActualMessageComposition(context)

    def test_actual_sql_session_resolved_principal_and_fingerprint(self):
        principal = self.session_principal("U-G", "oidc")
        policy = self.capture(principal)
        authority = policy.generator
        self.assertEqual((authority.principal.issuer, authority.principal.subject,
                          authority.principal.local_user_id), ("urn:test", "U-G", "U-G"))
        self.assertEqual(authority.display_name, principal.display_name)
        self.assertEqual(authority.address.raw_address, principal.email)
        self.assertEqual(authority.identity_fingerprint, ProjectCommunicationService._generator_context(principal)[1])
        self.assertEqual(RecipientPolicyContext.from_payload(policy.to_payload()), policy)

    def test_issuer_subject_only_local_server_principal(self):
        principal = self.principal(None, issuer="urn:resourceplanner:local",
                                   subject="local-user", auth_mode="local")
        resolved = static_auth_resolver(principal)(None)
        policy = self.capture(resolved)
        self.assertIsNone(policy.generator.principal.local_user_id)
        self.assertIsNone(policy.composition_identities)
        self.assertIn(mailbox("authenticated"), self.actual(policy).messages[0].cc_addresses)

    def test_disabled_and_unlinked_do_not_read_sql(self):
        statements = []
        def record(*args):
            statements.append(args[2])
        event.listen(self.engine, "before_cursor_execute", record)
        try:
            self.assertIs(self.capture(object(), enabled=False), self.current)
            self.assertIsNone(self.capture(self.principal(None)).composition_identities)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertEqual(statements, [])

    def test_requested_missing_principal_or_invalid_address(self):
        for principal in (None, self.principal(email=None), self.principal(email="bad"),
                          self.principal(email=mailbox("a") + "," + mailbox("b")),
                          replace(self.principal(), email=mailbox("a") + "\n")):
            with self.subTest(principal=principal), self.assertRaises(ApplicationValidationError) as exc:
                self.capture(principal)
            self.assertEqual(exc.exception.code, "project_communication_generator_email_invalid")

    def test_authenticated_address_not_app_user_email(self):
        policy = self.capture(self.principal())
        self.assertEqual(policy.generator.address.raw_address, mailbox("authenticated"))
        self.assertNotIn(mailbox("persisted"), self.actual(policy).messages[0].cc_addresses)

    def test_local_and_break_glass_existing_session_semantics(self):
        local = self.session_principal("U-G", "local")
        self.assertEqual(self.capture(local).generator.principal.local_user_id, "U-G")
        self.change(AppUser, "U-G", issuer=None, subject=None)
        emergency = self.session_principal("U-G", "break_glass")
        self.assertEqual(emergency.issuer, "urn:resourceplanner:break-glass")
        self.assertEqual(emergency.subject, "app-user:U-G")
        self.assertEqual(self.capture(emergency).generator.principal.local_user_id, "U-G")
        with self.assertRaises(ValueError):
            self.capture(replace(emergency, auth_mode="oidc"))

    def test_missing_inactive_wrong_and_stale_local_link(self):
        for principal in (self.principal("U-MISSING"), self.principal(subject="U-RQ"),
                          self.principal(issuer="URN:TEST"), self.principal(subject="U-G ")):
            # from_roles trims the last variant; test the exact raw coordinates below.
            if principal.subject == "U-G" and principal.issuer == "urn:test" and principal.local_user_id == "U-G":
                continue
            with self.subTest(principal=principal), self.assertRaises(ValueError):
                self.capture(principal)
        with self.assertRaises(ValueError):
            self.capture(replace(self.principal(), subject="U-G "))
        stale = self.principal()
        self.change(AppUser, "U-G", subject="new-subject")
        with self.assertRaises(ValueError):
            self.capture(stale)
        self.change(AppUser, "U-G", active=False)
        with self.assertRaises(ValueError):
            self.capture(self.principal(subject="new-subject"))

    def test_current_component_reused_and_to_priority(self):
        policy = self.capture(self.principal("U-RQ"))
        self.assertIs(policy.identities, self.current.identities)
        self.assertIsNone(policy.composition_identities)
        message = self.actual(policy).messages[0]
        self.assertIn(mailbox("rq"), message.to_addresses)
        self.assertNotIn(mailbox("authenticated"), message.cc_addresses)
        self.assertTrue(any(row.role == "GENERATOR" for row in message.observations))

    def test_disjoint_component_and_competitors(self):
        self.change(BusinessContact, "C-G", external_system="RESOURCEPLANNER",
                    external_entity="EMPLOYEE", external_id="E-CONFLICT")
        with self.factory.begin() as session:
            session.add(Resource(id="R-CONFLICT", name="Demo person",
                                 external_id="E-CONFLICT", erp_active=False))
        policy = self.capture(self.principal())
        extension = policy.composition_identities
        self.assertEqual({row.resource_id for row in extension.resources}, {"R-G", "R-CONFLICT"})
        self.assertEqual({row.user_id for row in extension.users}, {"U-G"})
        self.assertEqual({row.contact_id for row in extension.contacts}, {"C-G"})
        self.assertIn(policy.generator.principal.identity, policy.index().ambiguous)
        self.assertIn(mailbox("authenticated"), self.actual(policy).messages[0].cc_addresses)

    def test_incomplete_existing_component_rejected_not_repaired(self):
        self.change(AppUser, "U-RQ", employee_external_id="E-G")
        with self.assertRaisesRegex(ValueError, "complete current identity closure"):
            self.capture(self.principal("U-RQ"))
        # A generator outside the captured closure may also bridge into it.
        self.change(AppUser, "U-RQ", employee_external_id=None)
        self.change(AppUser, "U-G", employee_external_id="E-2")
        with self.assertRaises(ValueError):
            self.capture(self.principal())
        refreshed = self.current_policy()
        self.assertIsNone(self.capture(self.principal(), current=refreshed).composition_identities)

    def test_address_dedup_without_identity_equivalence(self):
        policy = self.capture(self.principal(None, email=mailbox("rq").upper()))
        message = self.actual(policy).messages[0]
        envelope = next(row for row in message.recipients if row.address == mailbox("rq"))
        self.assertEqual(envelope.channel, "TO")
        self.assertEqual(len(envelope.identity_deliveries), 2)
        self.assertFalse(policy.index().equivalent(policy.generator.principal.identity,
                                                   RecipientIdentity("USER", "U-RQ")))

    def test_proven_mandatory_delivery_and_unrelated_address_coincidence(self):
        unrelated = self.capture(self.principal(None, email=mailbox("rq")))
        blocked = self.actual(unrelated).messages[0].mandatory[1]
        self.assertEqual(blocked.resolution.blocker.reason, AddressFailure.MISSING)
        self.assertIsNone(blocked.selected_observation)
        self.change(AppUser, "U-G", employee_external_id="E-2")
        current = self.current_policy()
        linked = self.capture(self.principal(email=mailbox("rq")), current=current)
        message = self.actual(linked).messages[0]
        mandatory = message.mandatory[1]
        self.assertTrue(mandatory.resolution.fulfilled_by_existing)
        self.assertEqual(mandatory.resolution.effective_address, mailbox("rq"))
        self.assertEqual(message.observations[mandatory.selected_observation].role, "GENERATOR")
        self.assertEqual(message.observations[mandatory.selected_observation].canonical_identity,
                         RecipientIdentity("RESOURCE", "R-2"))

    def test_unknown_historical_generator_cannot_discharge_current_blocker(self):
        previous = self.f.build(5, self.capture(self.principal(None, email=mailbox("rq"))))
        changed = replace(self.f.assignment, hours=7.0)
        from app.domain.project_communication import build_project_communication_projection
        current = build_project_communication_projection(
            week_start=fixtures.WEEK, week_end=fixtures.END, assignments=(changed,),
            request_provenance_version=5, recipient_policy_context=self.current,
        )
        context = MessageContextEvidence(artifacts.DELTA, current, artifacts.baseline(previous))
        message = ActualMessageComposition(context).messages[0]
        self.assertEqual(message.mandatory[1].resolution.blocker.reason, AddressFailure.MISSING)
        self.assertIsNone(message.mandatory[1].selected_observation)

    def test_generator_only_changes_p5_independent_and_integrity_sensitive(self):
        current = self.current_policy(complete=True)
        off = self.f.build(5, current)
        original = artifacts.issue(MessageContextEvidence(FULL, off, None))
        p5 = project_projection_fingerprint(off)
        results = []
        for principal in (self.principal(), self.principal(email=mailbox("other")),
                          self.principal(None, subject="other")):
            policy = self.capture(principal, current=current)
            projection = self.f.build(5, policy)
            self.assertEqual(project_projection_fingerprint(projection), p5)
            artifact = artifacts.issue(MessageContextEvidence(FULL, projection, None))
            self.assertNotEqual(artifact.fingerprint, original.fingerprint)
            results.append(artifact.integrity.fingerprint)
        self.assertEqual(len(set(results)), 3)
        self.change(Resource, "R-G", external_id="E-NEW")
        policy = self.capture(self.principal(), current=current)
        projection = self.f.build(5, policy)
        self.assertEqual(project_projection_fingerprint(projection), p5)
        self.assertNotEqual(ActualCompositionIntegrity(MessageContextEvidence(FULL, projection, None)).fingerprint,
                            results[0])

    def test_bounded_reads_independent_of_message_count(self):
        def measured(current):
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append((statement.lower(), len(parameters)))
            event.listen(self.engine, "before_cursor_execute", record)
            try:
                self.capture(self.principal(), current=current)
            finally:
                event.remove(self.engine, "before_cursor_execute", record)
            self.assertTrue(all("where" in sql and size <= 900 for sql, size in statements))
            self.assertTrue(all("app_users.email" not in sql for sql, _ in statements))
            return len(statements)
        multi = RecipientPolicyContext.capture(self.current.population, self.current.identities,
            tuple(replace(self.f.inputs, project_id=f"P-{n}") for n in range(100)))
        self.assertEqual(measured(self.current), measured(multi))
        self.assertLess(measured(multi), 15)

    def test_fresh_identity_capture_and_no_generator_replacement(self):
        first = self.capture(self.principal())
        self.change(Resource, "R-G", external_id="E-NEW")
        second = self.capture(self.principal())
        self.assertNotEqual(first.composition_identities, second.composition_identities)
        self.assertEqual(second.identities, first.identities)
        with self.assertRaises(ValueError):
            self.capture(self.principal(), current=first)

    def test_provisional_capture_still_fails_closed(self):
        service = ProjectCommunicationService(None)
        with self.assertRaises(ApplicationUnavailableError) as exc:
            service.capture_v5_projection(week_start=fixtures.WEEK, add_generator_cc=True,
                                          generator_principal=self.principal())
        self.assertEqual(exc.exception.code, "project_communication_v5_generator_authority_unavailable")
