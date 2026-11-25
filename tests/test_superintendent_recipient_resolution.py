from __future__ import annotations

import unittest

from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import (
    AddressFailure,
    AddressResolution,
    resolve_address,
    resolve_superintendent_addresses,
)

from app.domain.communication_recipient_identity import (
    ContactIdentityFact, RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
    build_recipient_identity_index,
)
from app.domain.communication_recipient_composition import (
    RecipientCandidate, compose_mandatory_recipients, contact_rooted_candidate,
)


class SuperintendentAddressTests(unittest.TestCase):
    def test_valid_bare_address_retains_case_and_trims_spaces(self):
        for raw, expected in (
            ("  First.Last+tag@Example.ca  ", "First.Last+tag@Example.ca"),
            ("o'neill@example.co.uk", "o'neill@example.co.uk"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_address(raw), AddressResolution(expected))

    def test_missing_invalid_ambiguous_and_injection_are_distinct(self):
        cases = {
            AddressFailure.MISSING: (None, "", "  "),
            AddressFailure.INVALID: (
                "invalid", "a@b", "a..b@example.ca", "a@-example.ca",
                "Name <a@example.ca>", "a@example..ca", "a b@example.ca",
                "a" * 65 + "@example.ca", "a@example.ca (comment)",
            ),
            AddressFailure.AMBIGUOUS: (
                "a@example.ca,b@example.ca", "a@example.ca;b@example.ca",
                "a@example.ca b@example.ca",
            ),
            AddressFailure.HEADER_INJECTION: (
                "a@example.ca\r\nBcc: b@example.ca", "a@example.ca\n",
                "a@example.ca\x00", "a@example.ca\u2028", "a\t@example.ca",
            ),
        }
        for reason, values in cases.items():
            for raw in values:
                with self.subTest(raw=raw):
                    self.assertEqual(resolve_address(raw), AddressResolution(None, reason))

    def test_complete_population_retains_every_employee_and_specific_diagnostic(self):
        employees = (
            SuperintendentEmployee("R3", "Same name", "a@example.ca"),
            SuperintendentEmployee("R2", "Same name", "bad"),
            SuperintendentEmployee("R1", "Same name", None),
        )
        population = SuperintendentPopulation("C1", employees)
        result = resolve_superintendent_addresses(population)
        self.assertTrue(population.complete)
        self.assertEqual(tuple(row.employee for row in result), employees[::-1])
        self.assertEqual(result[0].diagnostic.reason, AddressFailure.MISSING)
        self.assertEqual(result[1].diagnostic.reason, AddressFailure.INVALID)
        self.assertEqual(result[0].diagnostic.resource_id, "R1")
        self.assertEqual(result[1].diagnostic.display_name, "Same name")
        self.assertIn("R1", result[0].diagnostic.message)
        self.assertIn("fiche employ", result[0].diagnostic.message)
        self.assertIsNone(result[2].diagnostic)

    def test_fresh_population_is_resolved_without_cache(self):
        old = SuperintendentPopulation("C1", (SuperintendentEmployee("R1", "A", None),))
        new = SuperintendentPopulation("C1", (SuperintendentEmployee("R1", "A", "a@example.ca"),))
        before = resolve_superintendent_addresses(old)
        after = resolve_superintendent_addresses(new)
        self.assertEqual(before[0].diagnostic.reason, AddressFailure.MISSING)
        self.assertEqual(after[0].resolution.address, "a@example.ca")
        self.assertIsNone(after[0].diagnostic)
        self.assertIsNotNone(before[0].diagnostic)




def identity(kind, value):
    return RecipientIdentity(kind, value)


def candidate(kind, value, email, role="MANAGER", channel="TO", active=True):
    source = {"RESOURCE": "RESOURCE_EMAIL", "CONTACT": "BUSINESS_CONTACT_EMAIL", "USER": "APP_USER_EMAIL"}[kind]
    return RecipientCandidate(identity(kind, value), "Same name", role, channel, email, source, active)


class RecipientIdentityTests(unittest.TestCase):
    def test_stable_links_and_namespaced_contact_identity(self):
        index = build_recipient_identity_index(
            (ResourceIdentityFact("R1", "E1"), ResourceIdentityFact("R2", "E2")),
            (ContactIdentityFact("C1"), ContactIdentityFact("C2", "RESOURCEPLANNER", "EMPLOYEE", "E2"),
             ContactIdentityFact("C3", "OTHER", "EMPLOYEE", "E1")),
            (UserIdentityFact("U1", "E1", "C1"),),
        )
        self.assertTrue(index.equivalent(identity("RESOURCE", "R1"), identity("CONTACT", "C1")))
        self.assertTrue(index.equivalent(identity("RESOURCE", "R1"), identity("USER", "U1")))
        self.assertTrue(index.equivalent(identity("RESOURCE", "R2"), identity("CONTACT", "C2")))
        self.assertFalse(index.equivalent(identity("RESOURCE", "R1"), identity("CONTACT", "C3")))
        self.assertFalse(index.equivalent(identity("RESOURCE", "R1"), identity("RESOURCE", "R2")))
        self.assertFalse(index.equivalent(identity("RESOURCE", "R1"), identity("CONTACT", "R1")))

    def test_duplicate_resources_and_conflicting_contact_link_fail_closed(self):
        for resources, contacts, users in (
            ((ResourceIdentityFact("R1", "E1"), ResourceIdentityFact("R2", "E1")),
             (ContactIdentityFact("C1"),), (UserIdentityFact("U1", "E1", "C1"),)),
            ((ResourceIdentityFact("R1", "E1"), ResourceIdentityFact("R2", "E2")),
             (ContactIdentityFact("C1", "ACUMATICA", "EMPLOYEE", "E2"),), (UserIdentityFact("U1", "E1", "C1"),)),
        ):
            with self.subTest(resources=resources, contacts=contacts):
                index = build_recipient_identity_index(resources, contacts, users)
                self.assertEqual(index.ambiguous, frozenset((identity("RESOURCE", "R1"), identity("RESOURCE", "R2"), identity("CONTACT", "C1"), identity("USER", "U1"))))
                self.assertFalse(index.equivalent(identity("RESOURCE", "R1"), identity("CONTACT", "C1")))
                self.assertTrue(index.equivalent(identity("RESOURCE", "R1"), identity("RESOURCE", "R1")))

    def test_exact_external_id_and_namespace_required(self):
        index = build_recipient_identity_index(
            (ResourceIdentityFact("R", "Employee"),),
            (ContactIdentityFact("C", "RESOURCEPLANNER", "EMPLOYEE", "employee"),
             ContactIdentityFact("D", "RESOURCEPLANNER", "CUSTOMER", "Employee")),
            (UserIdentityFact("U", " Employee", None),),
        )
        self.assertFalse(index.ambiguous)
        for kind, value in (("CONTACT", "C"), ("CONTACT", "D"), ("USER", "U")):
            self.assertFalse(index.equivalent(identity("RESOURCE", "R"), identity(kind, value)))


class RecipientCompositionTests(unittest.TestCase):
    def setUp(self):
        self.index = build_recipient_identity_index(
            (ResourceIdentityFact("R1", "E1"), ResourceIdentityFact("R2", "E2"), ResourceIdentityFact("R3", "E3")),
            (ContactIdentityFact("C1"), ContactIdentityFact("C2")),
            (UserIdentityFact("U1", "E1", "C1"),),
        )

    def test_contact_address_authority_and_proven_resource_fallback(self):
        contact = candidate("CONTACT", "C1", "contact@example.ca")
        resource = candidate("RESOURCE", "R1", "resource@example.ca")
        self.assertEqual(contact_rooted_candidate(contact, resource, self.index), contact)
        missing = candidate("CONTACT", "C1", None)
        resolved = contact_rooted_candidate(missing, resource, self.index)
        self.assertEqual((resolved.email, resolved.address_source), ("resource@example.ca", "RESOURCE_EMAIL"))
        self.assertEqual(contact_rooted_candidate(candidate("CONTACT", "C2", None), resource, self.index).email, None)
        invalid = candidate("CONTACT", "C1", "bad")
        self.assertEqual(contact_rooted_candidate(invalid, resource, self.index), invalid)

    def test_to_priority_shared_address_and_all_roles(self):
        population = SuperintendentPopulation("C", (
            SuperintendentEmployee("R1", "Same name", None),
            SuperintendentEmployee("R2", "Same name", " SHARED@example.ca "),
            SuperintendentEmployee("R3", "Same name", "other@example.ca"),
        ))
        result = compose_mandatory_recipients(population, (
            candidate("RESOURCE", "R1", "resource@example.ca", "ASSIGNED", "CC"),
            candidate("CONTACT", "C1", "shared@EXAMPLE.ca"),
            candidate("USER", "U1", "requester@example.ca", "REQUESTER"),
            candidate("CONTACT", "C2", " shared@example.ca ", "GENERATOR", "CC"),
        ), self.index)
        self.assertTrue(result.mandatory_complete)
        self.assertEqual([(r.channel, r.address) for r in result.recipients], [("TO", "shared@EXAMPLE.ca"), ("CC", "other@example.ca")])
        self.assertEqual({(c.identity.id, c.role) for c in result.recipients[0].contributions}, {("R1", "ASSIGNED"), ("C1", "MANAGER"), ("U1", "REQUESTER"), ("C2", "GENERATOR"), ("R1", "SUPERINTENDENT"), ("R2", "SUPERINTENDENT")})
        self.assertTrue(result.mandatory[0].fulfilled_by_existing)
        self.assertIsNotNone(result.mandatory[0].employee.diagnostic)
        self.assertFalse(result.mandatory[1].fulfilled_by_existing)
        self.assertEqual(result.mandatory[1].effective_address, "shared@EXAMPLE.ca")
        self.assertEqual(result.blockers, ())

    def test_equal_names_do_not_fulfill_missing_or_invalid_mandatory_addresses(self):
        population = SuperintendentPopulation("C", (
            SuperintendentEmployee("R1", "Same name", "bad"),
            SuperintendentEmployee("R2", "Same name", None),
        ))
        result = compose_mandatory_recipients(population, (
            candidate("CONTACT", "C2", "valid@example.ca"),
            candidate("CONTACT", "C1", "bad"),
            candidate("USER", "U1", "valid@example.ca", active=False),
        ), self.index)
        self.assertFalse(result.mandatory_complete)
        self.assertEqual(tuple(row.resource_id for row in result.blockers), ("R1", "R2"))
        self.assertEqual(len(result.unusable_existing), 1)
        self.assertEqual(len(result.recipients), 1)

    def test_ambiguous_link_does_not_fulfill_mandatory_recipient(self):
        index = build_recipient_identity_index(
            (ResourceIdentityFact("R1", "E1"), ResourceIdentityFact("R2", "E1")),
            (ContactIdentityFact("C1", "RESOURCEPLANNER", "EMPLOYEE", "E1"),), (),
        )
        population = SuperintendentPopulation("C", (SuperintendentEmployee("R1", "Same name", None),))
        result = compose_mandatory_recipients(population, (candidate("CONTACT", "C1", "valid@example.ca"),), index)
        self.assertEqual(result.blockers[0].resource_id, "R1")
        self.assertFalse(result.mandatory[0].fulfilled_by_existing)

    def test_binding_failure_is_not_complete_empty_population(self):
        invalid = SuperintendentPopulation(None, diagnostic="SUPERINTENDENT_BINDING_MISSING")
        self.assertFalse(compose_mandatory_recipients(invalid, (), self.index).mandatory_complete)
        self.assertTrue(compose_mandatory_recipients(SuperintendentPopulation("C"), (), self.index).mandatory_complete)


if __name__ == "__main__":
    unittest.main()
