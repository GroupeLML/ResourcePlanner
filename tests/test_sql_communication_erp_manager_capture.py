"""Phase 2A.2a: persisted ERP manager source, not operational v5 generation."""
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event, select

from app.application.operational_contacts import OperationalContactService
from app.application.project_managers import ProjectManagerResolutionService
from app.domain.communication_recipient_identity import ERPPrincipalIdentityFact, RecipientIdentity
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import MessageRecipientInputs, RecipientPolicyContext
from app.domain.superintendent_population import SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure, resolve_address_v1
from app.infrastructure.sql import (
    AppUser, Base, BusinessContact, Project, ProjectCoManager, Resource,
    create_session_factory, create_sql_engine,
)
from app.infrastructure.sql.communication_erp_manager_capture import capture_erp_manager_authorities
from app.infrastructure.sql.communication_recipient_identity import (
    _read_matches, read_recipient_identity_evidence, read_recipient_identity_index,
)
from app.infrastructure.sql.models import Competency, ResourceCompetency
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.infrastructure.sql.project_manager_resolution_repository import SqlProjectManagerResolutionRepository


def mailbox(local):
    return local + chr(64) + "example.test"


class SqlERPManagerCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{directory.name}/manager.db")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all([
                BusinessContact(id="C-PM", display_name="Demo manager", email=mailbox("contact")),
                BusinessContact(id="C-LEGACY", display_name="Demo legacy", email=mailbox("legacy")),
                Resource(id="R-PM", name="Demo manager", external_id="PM-1", email=mailbox("resource")),
                Competency(id="COMP-869", name="Surintendant"),
            ])
            session.flush()
            session.add(AppUser(id="U-PM", display_name="Demo manager", employee_external_id="PM-1",
                                business_contact_id="C-PM", email=mailbox("user"), roles_json="[]"))
            session.add(Project(id="P1", number="1000", name="Demo project",
                                project_manager_external_id="PM-1", project_manager_name="Demo ERP",
                                project_manager_contact_id="C-LEGACY"))
            session.add(ResourceCompetency(resource_id="R-PM", competency_id="COMP-869"))

    def change(self, model, key, **values):
        with self.factory.begin() as session:
            row = session.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)

    def capture(self, project_ids=("P1",)):
        with self.factory() as session:
            rows, evidence = capture_erp_manager_authorities(session, project_ids)
            self.assertFalse(session.new or session.dirty or session.deleted)
            return rows, evidence

    def single(self):
        rows, evidence = self.capture()
        row, = rows
        return row, evidence

    def message(self, row, evidence, population=None):
        if population is None:
            population = SuperintendentPopulation("COMP-869", ())
        return RecipientPolicyContext.capture(
            RecipientPolicyPopulation.capture(population), evidence,
            (MessageRecipientInputs(row.principal.project_id, (row,)),),
        ).messages[0]

    def test_resolved_canonical_principal_and_raw_contact(self):
        raw = " " + mailbox("contact") + " "
        self.change(BusinessContact, "C-PM", email=raw)
        row, evidence = self.single()
        self.assertEqual(row.principal, ERPPrincipalIdentityFact("P1", "PM-1"))
        self.assertEqual((row.linked_user_id, row.linked_user_active), ("U-PM", True))
        self.assertEqual((row.contact_id, row.contact_status, row.contact_active),
                         ("C-PM", "RESOLVED", True))
        self.assertEqual(row.contact_address.raw_address, raw)
        self.assertEqual(row.contact_address.record, RecipientIdentity("CONTACT", "C-PM"))
        recipient, = self.message(row, evidence).recipients
        self.assertEqual((recipient.channel, recipient.address), ("TO", mailbox("contact")))
        self.assertEqual(recipient.selected.identity, row.principal.identity)
        self.assertEqual(recipient.selected.role, "MANAGER")

    def test_contact_precedence_retains_different_resource_observation(self):
        row, evidence = self.single()
        self.assertEqual(row.resource_address.raw_address, mailbox("resource"))
        self.assertEqual(row.resource_address.record, RecipientIdentity("RESOURCE", "R-PM"))
        recipient, = self.message(row, evidence).recipients
        self.assertEqual(recipient.selected.address_source, "BUSINESS_CONTACT_EMAIL")
        self.assertEqual(recipient.address, mailbox("contact"))

    def test_missing_contact_email_falls_back_to_unique_resource(self):
        for raw in (None, "", "  "):
            with self.subTest(raw=raw):
                self.change(BusinessContact, "C-PM", email=raw)
                row, evidence = self.single()
                self.assertEqual(row.contact_address.raw_address, raw)
                recipient, = self.message(row, evidence).recipients
                self.assertEqual(recipient.selected.address_source, "RESOURCE_EMAIL")
                self.assertEqual(recipient.address, mailbox("resource"))

    def test_absent_contact_with_local_user_and_resource(self):
        self.change(AppUser, "U-PM", business_contact_id=None)
        row, evidence = self.single()
        self.assertEqual((row.linked_user_id, row.linked_user_active), ("U-PM", True))
        self.assertEqual((row.contact_status, row.contact_id, row.contact_address), ("ABSENT", None, None))
        self.assertEqual(self.message(row, evidence).recipients[0].address, mailbox("resource"))

    def test_unresolved_contact_in_explicit_legacy_corruption_fixture(self):
        # A dangling FK is not legal with normal SQLite FK enforcement. Model
        # pre-existing corruption explicitly, restore enforcement before capture,
        # and assert it really exists; all other fixtures enforce foreign keys.
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(
                "UPDATE app_users SET business_contact_id = ? WHERE id = ?", ("C-MISSING", "U-PM"),
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertTrue(connection.exec_driver_sql("PRAGMA foreign_key_check(app_users)").all())
            connection.commit()
        row, evidence = self.single()
        self.assertEqual((row.contact_id, row.contact_status, row.contact_active),
                         ("C-MISSING", "UNRESOLVED", None))
        self.assertIsNone(row.contact_address)
        self.assertEqual(self.message(row, evidence).recipients[0].address, mailbox("resource"))

    def test_no_local_app_user_preserves_erp_principal_and_fallback(self):
        self.change(AppUser, "U-PM", employee_external_id=None)
        row, evidence = self.single()
        self.assertEqual(row.principal.employee_external_id, "PM-1")
        self.assertIsNone(row.linked_user_id)
        self.assertEqual(row.contact_status, "ABSENT")
        self.assertEqual(evidence.users, ())
        self.assertEqual(self.message(row, evidence).recipients[0].address, mailbox("resource"))

    def test_erp_only_without_any_local_records(self):
        self.change(Project, "P1", project_manager_external_id="PM-NOT-LINKED")
        row, evidence = self.single()
        self.assertEqual(row.principal.employee_external_id, "PM-NOT-LINKED")
        self.assertEqual((evidence.users, evidence.contacts, evidence.resources), ((), (), ()))
        self.assertIsNone(row.linked_user_id)
        self.assertIsNone(row.contact_address)
        self.assertIsNone(row.resource_address)
        self.assertEqual(self.message(row, evidence).recipients, ())

    def test_nonblank_invalid_ambiguous_and_injected_contact_never_fall_back(self):
        for raw, failure in (
            ("bad-address", AddressFailure.INVALID),
            (mailbox("one") + "," + mailbox("two"), AddressFailure.AMBIGUOUS),
            (mailbox("contact") + "\n", AddressFailure.HEADER_INJECTION),
        ):
            with self.subTest(failure=failure):
                self.change(BusinessContact, "C-PM", email=raw)
                row, evidence = self.single()
                candidate = row.candidate(evidence, evidence.index(erp_principals=(row.principal,)))
                self.assertEqual(candidate.address_source, "BUSINESS_CONTACT_EMAIL")
                self.assertEqual(resolve_address_v1(candidate.email).failure, failure)
                self.assertEqual(self.message(row, evidence).recipients, ())

    def test_inactive_manager_user_or_contact_blocks_contact_and_fallback(self):
        for user_active, contact_active in ((False, True), (True, False), (False, False)):
            for raw in (mailbox("contact"), None):
                with self.subTest(user=user_active, contact=contact_active, raw=raw):
                    self.change(AppUser, "U-PM", active=user_active)
                    self.change(BusinessContact, "C-PM", active=contact_active, email=raw)
                    row, evidence = self.single()
                    self.assertEqual((row.linked_user_active, row.contact_active), (user_active, contact_active))
                    self.assertFalse(row.candidate(evidence, evidence.index(erp_principals=(row.principal,))).active)
                    self.assertEqual(self.message(row, evidence).recipients, ())

    def test_resource_activity_flags_separate_delivery_from_roster_eligibility(self):
        self.change(BusinessContact, "C-PM", email=None)
        for active, erp_active in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(active=active, erp_active=erp_active):
                self.change(Resource, "R-PM", active=active, erp_active=erp_active)
                with self.factory() as session:
                    (row,), evidence = capture_erp_manager_authorities(session, ("P1",))
                    repository = SqlProjectCommunicationRepository(
                        session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
                        superintendent_competency_id="COMP-869",
                    )
                    population = repository.read_superintendent_population()
                self.assertEqual((row.resource_active, row.resource_erp_active), (active, erp_active))
                self.assertEqual([item.resource_id for item in population.employees],
                                 ["R-PM"] if active and erp_active else [])
                message = self.message(row, evidence, population)
                self.assertEqual(message.recipients[0].address, mailbox("resource"))
                self.assertEqual(len(message.mandatory), int(active and erp_active))

    def test_app_user_email_is_not_manager_delivery_authority(self):
        self.change(BusinessContact, "C-PM", email=None)
        self.change(Resource, "R-PM", email=None)
        row, evidence = self.single()
        self.assertEqual(row.linked_user_id, "U-PM")
        self.assertIsNone(row.contact_address.raw_address)
        self.assertIsNone(row.resource_address.raw_address)
        self.assertEqual(self.message(row, evidence).recipients, ())
        self.change(AppUser, "U-PM", business_contact_id=None)
        row, evidence = self.single()
        self.assertEqual(self.message(row, evidence).recipients, ())

    def test_contradictory_identity_retains_competitors_but_disallows_fallback(self):
        with self.factory.begin() as session:
            session.add(Resource(id="R-CONFLICT", name="Demo conflict", external_id="PM-OTHER", active=False))
            contact = session.get(BusinessContact, "C-PM")
            contact.external_system = "RESOURCEPLANNER"
            contact.external_entity = "EMPLOYEE"
            contact.external_id = "PM-OTHER"
        row, evidence = self.single()
        index = evidence.index(erp_principals=(row.principal,))
        self.assertIn(row.principal.identity, index.ambiguous)
        self.assertIn(RecipientIdentity("RESOURCE", "R-CONFLICT"), index.ambiguous)
        self.assertIsNone(row.resource_address)
        # Ambiguity blocks equivalence, not the independently resolved contact.
        self.assertEqual(self.message(row, evidence).recipients[0].address, mailbox("contact"))
        self.change(BusinessContact, "C-PM", email=None)
        row, evidence = self.single()
        self.assertEqual(self.message(row, evidence).recipients, ())

    def test_names_addresses_and_arbitrary_contact_namespaces_are_not_identity(self):
        self.change(AppUser, "U-PM", employee_external_id=None, email=mailbox("resource"))
        with self.factory.begin() as session:
            session.add(BusinessContact(id="C-ARBITRARY", display_name="Demo manager",
                                        email=mailbox("resource"), external_system="ACUMATICA",
                                        external_entity="EMPLOYEE", external_id="PM-1"))
            session.add(Resource(id="R-UNRELATED", name="Demo manager", email=mailbox("resource"),
                                 external_id="PM-OTHER"))
        row, evidence = self.single()
        self.assertEqual(evidence.contacts, ())
        self.assertEqual(evidence.users, ())
        self.assertEqual([fact.resource_id for fact in evidence.resources], ["R-PM"])
        self.assertIsNone(row.contact_id)
        self.assertEqual(self.message(row, evidence).recipients[0].address, mailbox("resource"))

    def test_legacy_contact_cannot_override_canonical_or_missing_principal(self):
        row, evidence = self.single()
        self.assertEqual(row.contact_id, "C-PM")
        self.assertNotIn("C-LEGACY", {fact.contact_id for fact in evidence.contacts})
        self.change(Project, "P1", project_manager_external_id=None)
        rows, evidence = self.capture()
        self.assertEqual(rows, ())
        self.assertEqual(evidence.contacts, ())

    def test_same_person_rp_nomination_remains_separate_persisted_relationship(self):
        before = self.capture()
        with self.factory.begin() as session:
            session.add(ProjectCoManager(project_id="P1", business_contact_id="C-PM", created_by_user_id="U-PM"))
        with self.factory() as session:
            resolved = ProjectManagerResolutionService(SqlProjectManagerResolutionRepository(session)).resolve_project("P1")
            self.assertEqual(resolved.primary.sources, ("ERP", "RP"))
            self.assertEqual(resolved.co_managers, ())
            self.assertIsNotNone(session.get(ProjectCoManager, ("P1", "C-PM")))
            after = capture_erp_manager_authorities(session, ("P1",))
            self.assertEqual(after, before)
            self.assertIsNotNone(session.get(ProjectCoManager, ("P1", "C-PM")))

    def test_normalized_or_case_variant_keys_do_not_create_authority(self):
        self.change(Project, "P1", project_manager_external_id=" PM-1 ")
        with self.assertRaisesRegex(ValueError, "inexact ERP principal"):
            self.capture()
        self.change(Project, "P1", project_manager_external_id="PM-1")
        for key in ("pm-1", "PM-1 "):
            with self.subTest(key=key):
                self.change(AppUser, "U-PM", employee_external_id=key)
                self.change(Resource, "R-PM", external_id=key)
                row, evidence = self.single()
                self.assertEqual(row.principal.employee_external_id, "PM-1")
                self.assertIsNone(row.linked_user_id)
                self.assertIsNone(row.resource_address)
                self.assertEqual(evidence.users, ())
                self.assertEqual(evidence.resources, ())
        with self.assertRaisesRegex(ValueError, "inexact persisted project"):
            self.capture(("p1",))
        with self.assertRaisesRegex(ValueError, "exact project identity"):
            self.capture((" P1 ",))

    def test_exact_key_filter_rejects_sqlite_collation_equivalent_match(self):
        # Exercise the same SQL result filter with a real case-insensitive
        # SQLite expression; actual SQL Server collation remains deferred.
        with self.factory() as session:
            key = Resource.external_id.collate("NOCASE")
            self.assertEqual(session.scalar(select(Resource.id).where(key.in_(("pm-1",)))), "R-PM")
            self.assertEqual(list(_read_matches(session, (Resource.id, key), key, {"pm-1"})), [])

    def test_shared_identity_reader_without_erp_seeds_preserves_closure(self):
        root = RecipientIdentity("CONTACT", "C-PM")
        with self.factory() as session:
            evidence = read_recipient_identity_evidence(session, (root,))
            self.assertEqual(evidence, read_recipient_identity_evidence(session, (root,), erp_principals=()))
            self.assertEqual(evidence, capture_erp_manager_authorities(session, ("P1",))[1])
            index = read_recipient_identity_index(session, (root,))
            self.assertTrue(index.equivalent(root, RecipientIdentity("RESOURCE", "R-PM")))
            self.assertNotIn(RecipientIdentity("ERP_EMPLOYEE", "PM-1"), index.aliases)
            with self.assertRaisesRegex(ValueError, "typed ERP principal"):
                read_recipient_identity_evidence(session, (), erp_principals=("PM-1",))
        self.change(BusinessContact, "C-PM", external_system="RESOURCEPLANNER",
                    external_entity="EMPLOYEE", external_id="PM-OTHER")
        with self.factory() as session:
            index = read_recipient_identity_index(session, (root,))
            self.assertIn(root, index.ambiguous)
            self.assertFalse(index.equivalent(root, RecipientIdentity("RESOURCE", "R-PM")))

    def test_batched_exact_key_reads_across_projects_and_employee_ids(self):
        with self.factory.begin() as session:
            session.add(Project(id="P2", number="2000", name="Demo second", project_manager_external_id="PM-1"))
            session.add_all(Project(id=f"PX-{n}", number=f"X-{n}", name="Demo batch",
                                    project_manager_external_id=f"EMP-{n}") for n in range(900))

        def measured(keys):
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append((statement.lower(), len(parameters)))
            event.listen(self.engine, "before_cursor_execute", record)
            try:
                rows, _ = self.capture(keys)
            finally:
                event.remove(self.engine, "before_cursor_execute", record)
            self.assertEqual(len(rows), len(keys))
            self.assertTrue(all(" where " in sql.replace("\n", " ") and size <= 900
                                for sql, size in statements))
            return statements

        first = measured(("P1",))
        second = measured(("P1", "P2"))
        self.assertEqual(len(first), len(second))
        many = measured(("P1", "P2", *(f"PX-{n}" for n in range(900))))
        self.assertLess(len(many), 30)
        self.assertTrue(any(size == 900 for _, size in many))
        self.assertGreaterEqual(sum("resources.external_id in" in sql for sql, _ in many), 2)

    def test_fresh_capture_uses_persisted_values_not_cached_orm_instances(self):
        with self.factory() as session:
            cached = session.get(BusinessContact, "C-PM")
            before, _ = capture_erp_manager_authorities(session, ("P1",))
            session.commit()
            self.change(BusinessContact, "C-PM", email=None)
            self.change(Resource, "R-PM", email=mailbox("fresh"))
            # Factory uses expire_on_commit=False; cached ORM data is stale.
            self.assertEqual(cached.email, mailbox("contact"))
            (after,), evidence = capture_erp_manager_authorities(session, ("P1",))
            self.assertEqual(before[0].contact_address.raw_address, mailbox("contact"))
            self.assertIsNone(after.contact_address.raw_address)
            self.assertEqual(self.message(after, evidence).recipients[0].address, mailbox("fresh"))
