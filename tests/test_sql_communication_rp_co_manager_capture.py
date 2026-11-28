"""Phase 2A.2c.1 persisted independent RP nominations, not operational integration."""
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event, select

from app.application.errors import ApplicationUnavailableError
from app.application.project_communications import ProjectCommunicationService
from app.application.project_managers import ProjectManagerResolutionService
from app.application.security import AuthPrincipal
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import MessageRecipientInputs, RecipientPolicyContext
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
)
from app.domain.superintendent_population import SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure
from app.infrastructure.sql import (
    AppUser, Base, BusinessContact, Project, ProjectCoManager, Resource,
    create_session_factory, create_sql_engine,
)
from app.infrastructure.sql.communication_erp_manager_capture import capture_erp_manager_authorities
from app.infrastructure.sql.communication_rp_co_manager_capture import capture_rp_co_manager_authorities
from app.infrastructure.sql.project_manager_resolution_repository import SqlProjectManagerResolutionRepository
from tests import test_project_communication_v5 as fixtures


mailbox = fixtures.mailbox


class SqlRPCoManagerCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{directory.name}/nomination.db")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all([
                BusinessContact(id="C-PM", display_name="Demo manager", email=mailbox("manager")),
                BusinessContact(id="C-CO", display_name="Demo second", email=mailbox("second")),
                BusinessContact(id="C-MANUAL", display_name="Demo manual", email=mailbox("manual")),
                BusinessContact(id="C-LEGACY", display_name="Demo legacy", email=mailbox("legacy")),
                Resource(id="R-PM", name="Demo manager", external_id="E-PM", email=mailbox("resource")),
            ])
            session.flush()
            session.add_all([
                AppUser(id="U-PM", display_name="Demo manager", employee_external_id="E-PM",
                        business_contact_id="C-PM", email=mailbox("user"), roles_json="[]"),
                AppUser(id="U-CO", display_name="Demo second", employee_external_id="E-CO",
                        business_contact_id="C-CO", roles_json="[]"),
            ])
            session.add_all(Project(id=f"P{n}", number=f"100{n}", name="Demo project",
                                    project_manager_external_id="E-PM",
                                    project_manager_contact_id="C-LEGACY") for n in range(1, 5))

    def change(self, model, key, **values):
        with self.factory.begin() as session:
            row = session.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)

    def nominate(self, *pairs):
        with self.factory.begin() as session:
            session.add_all(ProjectCoManager(project_id=project_id, business_contact_id=contact_id,
                                             created_by_user_id="U-PM") for project_id, contact_id in pairs)

    def capture(self, keys=("P1",)):
        with self.factory() as session:
            result = capture_rp_co_manager_authorities(session, keys)
            self.assertFalse(session.new or session.dirty or session.deleted)
            return result

    def effective(self, project_id="P1"):
        with self.factory() as session:
            return ProjectManagerResolutionService(
                SqlProjectManagerResolutionRepository(session),
            ).resolve_project(project_id)

    def context(self, rows, evidence):
        return RecipientPolicyContext.capture(
            RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP-869")),
            evidence, rows,
        )

    @staticmethod
    def pairs(rows):
        return {row.project_id: tuple(candidate.primary.identity.id for candidate in row.candidates)
                for row in rows}

    def test_complete_requested_coverage_multiple_shared_and_empty(self):
        self.nominate(("P1", "C-PM"), ("P1", "C-CO"), ("P2", "C-PM"), ("P4", "C-LEGACY"))
        rows, evidence = self.capture(("P3", "P2", "P1", "P1"))
        self.assertEqual([row.project_id for row in rows], ["P1", "P2", "P3"])
        self.assertEqual(set(self.pairs(rows)["P1"]), {"C-PM", "C-CO"})
        self.assertEqual(self.pairs(rows)["P2"], ("C-PM",))
        self.assertEqual(self.pairs(rows)["P3"], ())
        self.assertEqual({fact.contact_id for fact in evidence.contacts}, {"C-PM", "C-CO"})
        self.assertEqual([row.project_id for row in self.context(rows, evidence).messages], ["P1", "P2", "P3"])

    def test_empty_and_legacy_fk_are_not_nominations(self):
        rows, evidence = self.capture(("P1", "P2"))
        self.assertEqual(self.pairs(rows), {"P1": (), "P2": ()})
        self.assertEqual((evidence.resources, evidence.contacts, evidence.users), ((), (), ()))
        empty_rows, empty_evidence = self.capture(())
        self.assertEqual(empty_rows, ())
        self.assertEqual(empty_evidence, evidence)
        self.assertEqual(self.effective().primary.business_contact_id, "C-PM")
        self.assertEqual(self.effective().co_managers, ())

    def test_same_person_effective_suppression_preserves_both_roles(self):
        self.nominate(("P1", "C-PM"))
        before = self.effective()
        self.assertEqual(before.primary.sources, ("ERP", "RP"))
        self.assertEqual(before.co_managers, ())
        rows, evidence = self.capture()
        self.assertEqual(self.pairs(rows), {"P1": ("C-PM",)})
        with self.factory() as session:
            managers, manager_evidence = capture_erp_manager_authorities(session, ("P1",))
            self.assertEqual(manager_evidence, evidence)
            self.assertIsNotNone(session.get(ProjectCoManager, ("P1", "C-PM")))
        combined = (MessageRecipientInputs("P1", (*managers, *rows[0].candidates)),)
        claims = combined[0].to_payload()["role_evidence"]
        self.assertEqual([row["relationship"] for row in claims],
                         ["ERP_PROJECT_PRINCIPAL", "RP_PROJECT_NOMINATION"])
        context = self.context(combined, evidence)
        recipient, = context.messages[0].recipients
        self.assertEqual(recipient.address, mailbox("manager"))
        self.assertEqual([row.role for row in recipient.contributions], ["MANAGER", "CO_MANAGER"])
        self.assertEqual(self.effective(), before)

    def test_erp_change_keeps_only_explicit_nominations(self):
        self.nominate(("P1", "C-PM"), ("P1", "C-CO"))
        before = self.capture(("P1", "P2"))
        self.change(Project, "P1", project_manager_external_id="E-CO")
        self.change(Project, "P2", project_manager_external_id="E-CO")
        effective = self.effective()
        self.assertEqual(effective.primary.business_contact_id, "C-CO")
        self.assertEqual(effective.primary.sources, ("ERP", "RP"))
        self.assertEqual([row.business_contact_id for row in effective.co_managers], ["C-PM"])
        self.assertEqual(self.capture(("P1", "P2")), before)
        self.assertEqual(self.effective("P2").co_managers, ())
        self.change(Project, "P1", project_manager_external_id=None)
        self.assertEqual(self.capture(("P1", "P2")), before)

    def test_raw_contact_authority_and_linked_identity(self):
        self.nominate(("P1", "C-PM"))
        raw = " " + mailbox("manager") + " "
        self.change(BusinessContact, "C-PM", email=raw)
        rows, evidence = self.capture()
        candidate, = rows[0].candidates
        self.assertEqual(candidate.primary.email, raw)
        self.assertTrue(candidate.primary.active)
        self.assertEqual(candidate.primary.role, "CO_MANAGER")
        self.assertEqual(candidate.primary.channel, "TO")
        self.assertEqual(candidate.resource_fallback.email, mailbox("resource"))
        self.assertEqual([row.raw_address for row in candidate.observations], [raw, mailbox("resource")])
        index = evidence.index()
        self.assertTrue(index.equivalent(RecipientIdentity("CONTACT", "C-PM"), RecipientIdentity("USER", "U-PM")))
        self.assertTrue(index.equivalent(RecipientIdentity("CONTACT", "C-PM"), RecipientIdentity("RESOURCE", "R-PM")))
        self.assertEqual(self.context(rows, evidence).messages[0].recipients[0].address, mailbox("manager"))

    def test_missing_contact_email_fallback_and_invalid_contact_no_fallback(self):
        self.nominate(("P1", "C-PM"))
        for raw in (None, "", "  "):
            with self.subTest(raw=raw):
                self.change(BusinessContact, "C-PM", email=raw)
                rows, evidence = self.capture()
                candidate, = rows[0].candidates
                self.assertEqual(candidate.primary.email, raw)
                recipient, = self.context(rows, evidence).messages[0].recipients
                self.assertEqual(recipient.address, mailbox("resource"))
                self.assertEqual(recipient.selected.address_source, "RESOURCE_EMAIL")
        self.change(BusinessContact, "C-PM", email="invalid")
        rows, evidence = self.capture()
        message = self.context(rows, evidence).messages[0]
        self.assertEqual(message.recipients, ())
        self.assertEqual(message.unusable_existing[0].resolution.failure, AddressFailure.INVALID)
        self.change(BusinessContact, "C-PM", email=None)
        self.change(Resource, "R-PM", email=None)
        rows, evidence = self.capture()
        message = self.context(rows, evidence).messages[0]
        self.assertEqual(message.recipients, ())
        self.assertEqual(message.unusable_existing[0].resolution.failure, AddressFailure.MISSING)

    def test_inactive_contact_and_linked_user_retain_roles_but_block_delivery(self):
        self.nominate(("P1", "C-PM"))
        for user_active, contact_active in ((False, True), (True, False), (False, False)):
            with self.subTest(user_active=user_active, contact_active=contact_active):
                self.change(AppUser, "U-PM", active=user_active)
                self.change(BusinessContact, "C-PM", active=contact_active, email=None)
                rows, evidence = self.capture()
                self.assertEqual(self.pairs(rows), {"P1": ("C-PM",)})
                self.assertFalse(rows[0].candidates[0].primary.active)
                self.assertEqual(self.context(rows, evidence).messages[0].recipients, ())

    def test_contact_without_login_and_resource_activity_separate_from_delivery(self):
        self.nominate(("P1", "C-MANUAL"))
        rows, evidence = self.capture()
        self.assertTrue(rows[0].candidates[0].primary.active)
        self.assertEqual(evidence.users, ())
        self.assertEqual(self.context(rows, evidence).messages[0].recipients[0].address, mailbox("manual"))
        self.nominate(("P2", "C-PM"))
        self.change(BusinessContact, "C-PM", email=None)
        for active, erp_active in ((False, True), (True, False), (False, False)):
            with self.subTest(active=active, erp_active=erp_active):
                self.change(Resource, "R-PM", active=active, erp_active=erp_active)
                rows, evidence = self.capture(("P2",))
                candidate, = rows[0].candidates
                self.assertFalse(candidate.resource_fallback.active)
                self.assertTrue(candidate.primary.active)
                self.assertEqual(self.context(rows, evidence).messages[0].recipients[0].address, mailbox("resource"))

    def test_conflicting_identity_and_noneligible_competitor_prevent_fallback(self):
        self.nominate(("P1", "C-PM"))
        with self.factory.begin() as session:
            session.add(Resource(id="R-OTHER", name="Demo other", external_id="E-OTHER",
                                 active=False, erp_active=False, email=mailbox("other")))
        self.change(BusinessContact, "C-PM", external_system="RESOURCEPLANNER",
                    external_entity="EMPLOYEE", external_id="E-OTHER")
        rows, evidence = self.capture()
        self.assertEqual({row.resource_id for row in evidence.resources}, {"R-PM", "R-OTHER"})
        self.assertIn(RecipientIdentity("CONTACT", "C-PM"), evidence.index().ambiguous)
        self.assertIn(RecipientIdentity("RESOURCE", "R-OTHER"), evidence.index().ambiguous)
        self.assertIsNone(rows[0].candidates[0].resource_fallback)
        self.assertEqual(self.context(rows, evidence).messages[0].recipients[0].address, mailbox("manager"))
        self.change(BusinessContact, "C-PM", email=None)
        rows, evidence = self.capture()
        self.assertEqual(self.context(rows, evidence).messages[0].recipients, ())
        self.assertEqual(self.pairs(rows), {"P1": ("C-PM",)})

    def test_no_inference_from_names_addresses_or_arbitrary_namespace(self):
        self.nominate(("P1", "C-MANUAL"), ("P1", "C-PM"))
        self.change(BusinessContact, "C-MANUAL", display_name="Demo manager", email=mailbox("manager"),
                    external_system="OTHER", external_entity="EMPLOYEE", external_id="E-PM")
        rows, evidence = self.capture()
        self.assertFalse(evidence.index().equivalent(RecipientIdentity("CONTACT", "C-MANUAL"),
                                                     RecipientIdentity("CONTACT", "C-PM")))
        manual = next(row for row in rows[0].candidates if row.primary.identity.id == "C-MANUAL")
        self.assertIsNone(manual.resource_fallback)
        recipient, = self.context(rows, evidence).messages[0].recipients
        self.assertEqual({row.identity.id for row in recipient.contributions}, {"C-MANUAL", "C-PM"})

    def test_exact_project_and_nomination_keys_under_sqlite_nocase(self):
        self.nominate(("P1", "C-PM"))
        with self.assertRaisesRegex(ValueError, "exact project identity"):
            self.capture((" P1 ",))
        with self.assertRaisesRegex(ValueError, "Missing or inexact persisted project"):
            self.capture(("P-UNKNOWN",))
        # Exercise actual SQL comparison with case-insensitive semantics, not a
        # fabricated result or a disabled FK. This is not SQL Server evidence.
        def nocase(conn, cursor, statement, parameters, context, executemany):
            return (statement.replace("projects.id IN", "projects.id COLLATE NOCASE IN")
                    .replace("project_co_managers.project_id IN",
                             "project_co_managers.project_id COLLATE NOCASE IN"), parameters)
        event.listen(self.engine, "before_cursor_execute", nocase, retval=True)
        try:
            with self.factory() as session:
                self.assertEqual(session.scalar(select(Project.id).where(Project.id.collate("NOCASE") == "p1")), "P1")
            with self.assertRaisesRegex(ValueError, "Missing or inexact persisted project"):
                self.capture(("p1",))
            # Binary SQLite permits two distinct PKs; a collation-equivalent
            # nomination returned by the SQL read must never be silently lost.
            with self.factory.begin() as session:
                session.add(Project(id="p1", number="lower", name="Demo other"))
            self.nominate(("p1", "C-CO"))
            with self.assertRaisesRegex(ValueError, "Inexact persisted RP nomination project key"):
                self.capture(("P1",))
        finally:
            event.remove(self.engine, "before_cursor_execute", nocase)

    def test_exact_contact_key_filters_collation_equivalent_contact(self):
        with self.factory.begin() as session:
            session.add(BusinessContact(id="c-pm", display_name="Demo other", email=mailbox("lower")))
        self.nominate(("P1", "c-pm"))
        def nocase(conn, cursor, statement, parameters, context, executemany):
            return statement.replace("business_contacts.id IN", "business_contacts.id COLLATE NOCASE IN"), parameters
        event.listen(self.engine, "before_cursor_execute", nocase, retval=True)
        try:
            with self.factory() as session:
                self.assertEqual(set(session.scalars(select(BusinessContact.id).where(
                    BusinessContact.id.collate("NOCASE") == "c-pm"))), {"C-PM", "c-pm"})
            rows, evidence = self.capture()
        finally:
            event.remove(self.engine, "before_cursor_execute", nocase)
        self.assertEqual(self.pairs(rows), {"P1": ("c-pm",)})
        self.assertEqual({row.contact_id for row in evidence.contacts}, {"c-pm"})
        self.assertEqual(self.context(rows, evidence).messages[0].recipients[0].address, mailbox("lower"))

    def test_batched_project_nomination_contact_identity_and_address_reads(self):
        self.nominate(("P1", "C-PM"), ("P2", "C-PM"))
        with self.factory.begin() as session:
            session.add_all(Project(id=f"PX-{n}", number=f"X-{n}", name="Demo batch") for n in range(901))
            session.add_all(BusinessContact(id=f"CX-{n}", display_name="Demo contact",
                external_system="RESOURCEPLANNER", external_entity="EMPLOYEE", external_id=f"EX-{n}")
                for n in range(901))
            session.add_all(Resource(id=f"RX-{n}", name="Demo resource", external_id=f"EX-{n}",
                                     email=mailbox("batch")) for n in range(901))
        self.nominate(*((f"PX-{n}", f"CX-{n}") for n in range(901)))

        def measured(keys):
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append((statement.lower().replace("\n", " "), len(parameters)))
            event.listen(self.engine, "before_cursor_execute", record)
            try:
                rows, evidence = self.capture(keys)
            finally:
                event.remove(self.engine, "before_cursor_execute", record)
            self.assertEqual(len(rows), len(keys))
            self.assertTrue(all(len(row.candidates) == 1 for row in rows))
            self.assertTrue(all(row.candidates[0].resource_fallback is not None for row in rows))
            self.assertTrue(all(" where " in sql and size <= 900 for sql, size in statements))
            self.assertTrue(all("app_users.email" not in sql for sql, _ in statements))
            return statements

        self.assertEqual(len(measured(("P1",))), len(measured(("P1", "P2"))))
        many = measured(("P1", *(f"PX-{n}" for n in range(901))))
        self.assertLess(len(many), 40)
        self.assertTrue(any(size == 900 for _, size in many))
        self.assertGreaterEqual(sum("project_co_managers.project_id in" in sql for sql, _ in many), 2)
        self.assertGreaterEqual(sum("business_contacts.id in" in sql for sql, _ in many), 4)
        self.assertGreaterEqual(sum("resources.external_id in" in sql for sql, _ in many), 2)
        self.assertGreaterEqual(sum("resources.id in" in sql for sql, _ in many), 2)

    def test_fresh_nomination_and_address_capture_ignores_cached_objects(self):
        self.nominate(("P1", "C-PM"))
        with self.factory() as session:
            cached = session.get(BusinessContact, "C-CO")
            old_nomination = session.get(ProjectCoManager, ("P1", "C-PM"))
            before, _ = capture_rp_co_manager_authorities(session, ("P1",))
            session.commit()
            with self.factory.begin() as writer:
                writer.delete(writer.get(ProjectCoManager, ("P1", "C-PM")))
                writer.add(ProjectCoManager(project_id="P1", business_contact_id="C-CO",
                                            created_by_user_id="U-PM"))
                writer.get(BusinessContact, "C-CO").email = mailbox("fresh")
            self.assertEqual(cached.email, mailbox("second"))
            self.assertEqual(old_nomination.business_contact_id, "C-PM")
            after, evidence = capture_rp_co_manager_authorities(session, ("P1",))
            self.assertEqual(self.pairs(before), {"P1": ("C-PM",)})
            self.assertEqual(self.pairs(after), {"P1": ("C-CO",)})
            self.assertEqual(self.context(after, evidence).messages[0].recipients[0].address, mailbox("fresh"))

    def test_inactive_operational_guards_remain(self):
        self.nominate(("P1", "C-PM"))
        self.assertEqual(self.pairs(self.capture()[0]), {"P1": ("C-PM",)})
        f = fixtures.ProjectCommunicationV5Tests()
        f.setUp()
        with self.assertRaisesRegex(ValueError, "v5/P5"):
            build_project_confirmation_batch(f.v5)
        with self.assertRaisesRegex(ValueError, "v5/P5"):
            build_project_delta_batch(f.v4, f.v5)
        principal = AuthPrincipal.from_roles(
            local_user_id=None, issuer="urn:resourceplanner:local", subject="local-user",
            display_name="Demo person", email=mailbox("generator"), roles=(), auth_mode="local",
        )
        with self.assertRaises(ApplicationUnavailableError) as exc:
            ProjectCommunicationService(None).capture_v5_projection(
                week_start=fixtures.WEEK, add_generator_cc=True, generator_principal=principal,
            )
        self.assertEqual(exc.exception.code, "project_communication_v5_generator_authority_unavailable")
