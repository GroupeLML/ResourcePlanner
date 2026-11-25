from __future__ import annotations

import unittest

from app.application.operational_contacts import OperationalContactService
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.communication_recipient_composition import RecipientCandidate, compose_mandatory_recipients
from app.domain.superintendent_recipient_resolution import AddressFailure
from app.infrastructure.sql import Base, create_session_factory, create_sql_engine
from app.infrastructure.sql.business_contact_models import BusinessContact
from app.infrastructure.sql.communication_recipient_identity import read_recipient_identity_index
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.models import Competency, Resource, ResourceCompetency
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository


class SuperintendentRecipientSqlTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all([
                Competency(id="COMP-869", name="Surintendant"),
                Resource(id="R1", name="Same name", external_id="E1"),
                Resource(id="R2", name="Same name", external_id="E2", email="two@example.ca"),
                BusinessContact(id="C1", display_name="Same name", email="contact@example.ca"),
                BusinessContact(id="C2", display_name="Same name", external_system="RESOURCEPLANNER",
                                external_entity="EMPLOYEE", external_id="E2"),
                BusinessContact(id="C3", display_name="Same name", external_system="OTHER",
                                external_entity="EMPLOYEE", external_id="E1"),
            ])
            session.flush()
            session.add_all([
                ResourceCompetency(resource_id="R1", competency_id="COMP-869"),
                ResourceCompetency(resource_id="R2", competency_id="COMP-869"),
                AppUser(id="U1", display_name="Same name", roles_json="[]",
                        employee_external_id="E1", business_contact_id="C1"),
            ])

    def read(self, existing=()):
        with self.factory() as session:
            repository = SqlProjectCommunicationRepository(
                session,
                operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
                superintendent_competency_id="COMP-869",
            )
            population = repository.read_superintendent_population()
            roots = tuple(RecipientIdentity("RESOURCE", row.resource_id) for row in population.employees)
            index = read_recipient_identity_index(session, roots + tuple(row.identity for row in existing))
            return compose_mandatory_recipients(population, existing, index), index

    def test_committed_resource_email_correction_is_fresh_and_old_facts_unchanged(self):
        before, _ = self.read()
        self.assertTrue(before.population.complete)
        self.assertFalse(before.mandatory_complete)
        self.assertEqual([(row.resource_id, row.reason) for row in before.blockers], [("R1", AddressFailure.MISSING)])
        with self.factory.begin() as session:
            session.get(Resource, "R1").email = " corrected@example.ca "
        after, _ = self.read()
        self.assertTrue(after.mandatory_complete)
        self.assertEqual([row.address for row in after.recipients], ["corrected@example.ca", "two@example.ca"])
        self.assertEqual(before.blockers[0].resource_id, "R1")
        self.assertIsNone(before.population.employees[0].email)

    def test_identity_read_follows_stable_links_and_includes_inactive_competitors(self):
        _, index = self.read()
        resource = RecipientIdentity("RESOURCE", "R1")
        contact = RecipientIdentity("CONTACT", "C1")
        self.assertTrue(index.equivalent(resource, contact))
        self.assertTrue(index.equivalent(resource, RecipientIdentity("USER", "U1")))
        self.assertTrue(index.equivalent(RecipientIdentity("RESOURCE", "R2"), RecipientIdentity("CONTACT", "C2")))
        self.assertFalse(index.equivalent(resource, RecipientIdentity("CONTACT", "C3")))
        existing = (RecipientCandidate(contact, "Same name", "MANAGER", "TO",
                                       "contact@example.ca", "BUSINESS_CONTACT_EMAIL"),)
        before, _ = self.read(existing)
        self.assertTrue(before.mandatory_complete)
        self.assertTrue(before.mandatory[0].fulfilled_by_existing)
        # Distinct external IDs obey the unique filtered Resource index. The
        # explicit U1 -> C1 link now conflicts with C1's canonical employee
        # namespace, which points to an inactive, noneligible Resource.
        with self.factory.begin() as session:
            session.add(Resource(id="R-INACTIVE", name="Other", external_id="E3", active=False))
            linked_contact = session.get(BusinessContact, "C1")
            linked_contact.external_system = "RESOURCEPLANNER"
            linked_contact.external_entity = "EMPLOYEE"
            linked_contact.external_id = "E3"
        result, index = self.read(existing)
        self.assertEqual(tuple(row.resource_id for row in result.population.employees), ("R1", "R2"))
        self.assertIn(resource, index.ambiguous)
        self.assertIn(RecipientIdentity("RESOURCE", "R-INACTIVE"), index.ambiguous)
        self.assertIn(contact, index.ambiguous)
        self.assertIn(RecipientIdentity("USER", "U1"), index.ambiguous)
        self.assertFalse(index.equivalent(resource, contact))
        self.assertTrue(index.equivalent(RecipientIdentity("RESOURCE", "R2"), RecipientIdentity("CONTACT", "C2")))
        self.assertFalse(result.mandatory_complete)
        self.assertFalse(result.mandatory[0].fulfilled_by_existing)
        self.assertEqual([(row.resource_id, row.reason) for row in result.blockers], [("R1", AddressFailure.MISSING)])
        self.assertEqual([(row.channel, row.address) for row in result.recipients],
                         [("TO", "contact@example.ca"), ("CC", "two@example.ca")])

    def test_unlinked_acumatica_contact_does_not_fulfill_mandatory_delivery(self):
        with self.factory.begin() as session:
            session.add(BusinessContact(
                id="C-ERP", display_name="Same name", email="contact@example.ca",
                external_system="ACUMATICA", external_entity="EMPLOYEE", external_id="E1",
            ))
        contact = RecipientIdentity("CONTACT", "C-ERP")
        existing = (RecipientCandidate(contact, "Same name", "MANAGER", "TO",
                                       "contact@example.ca", "BUSINESS_CONTACT_EMAIL"),)
        result, index = self.read(existing)
        self.assertFalse(index.equivalent(RecipientIdentity("RESOURCE", "R1"), contact))
        self.assertFalse(result.mandatory[0].fulfilled_by_existing)
        self.assertFalse(result.mandatory_complete)
        self.assertEqual([(row.resource_id, row.reason) for row in result.blockers], [("R1", AddressFailure.MISSING)])
        self.assertEqual([(row.channel, row.address) for row in result.recipients],
                         [("TO", "contact@example.ca"), ("CC", "two@example.ca")])


if __name__ == "__main__":
    unittest.main()
