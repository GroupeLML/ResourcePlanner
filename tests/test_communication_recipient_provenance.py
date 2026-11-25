from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from app.domain.communication_recipient_authority import AuthenticatedGeneratorAuthority, RecipientAddressObservation
from app.domain.communication_recipient_composition import RecipientCandidate
from app.domain.communication_recipient_identity import (
    AuthenticatedPrincipalIdentityFact, ContactIdentityFact, RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
)
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence, RecipientPolicyContext,
)
from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure


def mailbox(local):
    return local + chr(64) + "invalid.test"


def candidate(kind, key, email, role="MANAGER", channel="TO"):
    source = {"RESOURCE": "RESOURCE_EMAIL", "CONTACT": "BUSINESS_CONTACT_EMAIL", "USER": "APP_USER_EMAIL"}[kind]
    return RecipientCandidate(RecipientIdentity(kind, key), "Demo " + key, role, channel, email, source)


class RecipientPolicyContextTests(unittest.TestCase):
    def setUp(self):
        self.population = RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP-1", (
            SuperintendentEmployee("R-3", "Demo 3", "bad-address"),
            SuperintendentEmployee("R-2", "Demo 2", mailbox("shared")),
            SuperintendentEmployee("R-1", "Demo 1", None),
        )))
        self.identities = RecipientIdentityEvidence.capture((
            ResourceIdentityFact("R-3", "E-3"), ResourceIdentityFact("R-1", "E-1"),
            ResourceIdentityFact("R-2", "E-2"),
        ), (ContactIdentityFact("C-1", "RESOURCEPLANNER", "EMPLOYEE", "E-1"),), (
            UserIdentityFact("U-1", "E-1", "C-1"),
            UserIdentityFact("U-2", None, None),
        ))
        self.inputs = MessageRecipientInputs("P-1", (
            RecipientAuthorityInput(candidate("CONTACT", "C-1", mailbox("SHARED"))),
            RecipientAuthorityInput(candidate("USER", "U-1", mailbox("other"), "REQUESTER", "TO")),
            RecipientAuthorityInput(candidate("RESOURCE", "R-3", "bad-address", "ASSIGNED", "CC")),
        ))
        principal = AuthenticatedPrincipalIdentityFact("urn:test", "generator", "U-2")
        self.generator = AuthenticatedGeneratorAuthority(principal, "Demo generator",
            RecipientAddressObservation("AUTHENTICATED_EMAIL", principal.identity, "AUTHENTICATED", mailbox("shared")))
        self.context = RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,), generator=self.generator)

    def test_effective_authority_roles_and_distinct_shared_address(self):
        message = self.context.messages[0]
        self.assertEqual(len(message.recipients), 1)
        recipient = message.recipients[0]
        self.assertEqual((recipient.channel, recipient.address), ("TO", mailbox("SHARED")))
        self.assertEqual(recipient.selected.address_source, "BUSINESS_CONTACT_EMAIL")
        self.assertEqual({row.role for row in recipient.contributions}, {"MANAGER", "REQUESTER", "GENERATOR", "SUPERINTENDENT"})
        self.assertEqual({row.identity.id for row in recipient.contributions}, {"C-1", "U-1", self.generator.principal.identity.id, "R-1", "R-2"})
        stored = self.context.to_payload()["messages"][0]["recipients"][0]
        self.assertEqual(stored["selected"]["canonical_identity"], {"kind": "RESOURCE", "id": "R-1"})
        self.assertEqual({row["canonical_identity"]["id"] for row in stored["contributions"]}, {"R-1", "R-2", "U-2"})
        self.assertTrue(message.mandatory[0].fulfilled_by_existing)
        self.assertIsNone(message.mandatory[0].blocker_reason)
        self.assertFalse(message.mandatory[1].fulfilled_by_existing)
        self.assertEqual(message.mandatory[1].effective_address, mailbox("SHARED"))
        self.assertIsNone(message.mandatory[2].effective_address)
        self.assertEqual(message.mandatory[2].blocker_reason, AddressFailure.INVALID)
        self.assertFalse(message.mandatory_complete)
        with self.assertRaises(FrozenInstanceError):
            message.mandatory_complete = True

    def test_global_context_once_round_trip_and_order(self):
        other = replace(self.inputs, project_id="P-2")
        context = RecipientPolicyContext.capture(self.population, self.identities, (other, self.inputs), generator=self.generator)
        payload = context.to_payload()
        self.assertEqual([row["project_id"] for row in payload["messages"]], ["P-1", "P-2"])
        self.assertNotIn("population", payload["messages"][0])
        self.assertNotIn("population", payload["message_inputs"][0])
        self.assertEqual(RecipientPolicyContext.from_payload(json.loads(json.dumps(payload))), context)
        reversed_evidence = RecipientIdentityEvidence.capture(reversed(self.identities.resources), self.identities.contacts, reversed(self.identities.users))
        self.assertEqual(RecipientPolicyContext.capture(self.population, reversed_evidence, (self.inputs, other), generator=self.generator).to_payload(), payload)
        candidates = self.inputs.candidates
        reordered = replace(self.inputs, candidates=(candidates[2], candidates[0], candidates[1]))
        self.assertEqual(reordered, self.inputs)

    def test_ambiguous_or_unlinked_identity_cannot_fulfill_missing_email(self):
        for evidence in (
            replace(self.identities, contacts=(ContactIdentityFact("C-1", "ACUMATICA", "EMPLOYEE", "E-1"),), users=(UserIdentityFact("U-1", None, None), UserIdentityFact("U-2", None, None))),
            RecipientIdentityEvidence.capture(self.identities.resources, self.identities.contacts, self.identities.users + (UserIdentityFact("U-3", "E-1", "C-1"),)),
        ):
            with self.subTest(evidence=evidence):
                result = RecipientPolicyContext.capture(self.population, evidence, (self.inputs,)).messages[0]
                self.assertIsNone(result.mandatory[0].effective_address)
                self.assertEqual(result.mandatory[0].blocker_reason, AddressFailure.MISSING)

    def test_empty_and_failed_binding_are_distinct(self):
        for population, complete in ((SuperintendentPopulation("COMP-1"), True),
                                     (SuperintendentPopulation(None, diagnostic="SUPERINTENDENT_BINDING_MISSING"), False)):
            context = RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), self.identities, (self.inputs,))
            self.assertEqual(context.messages[0].mandatory_complete, complete)
            self.assertEqual(context.messages[0].mandatory, ())
            self.assertEqual(RecipientPolicyContext.from_payload(context.to_payload()), context)

    def test_rejects_contradictory_message_claims(self):
        mutations = (
            lambda data: data.pop("messages"),
            lambda data: data["messages"].clear(),
            lambda data: data["messages"][0]["mandatory"].pop(),
            lambda data: data["messages"][0]["mandatory"][0].update(fulfilled_by_existing=False),
            lambda data: data["messages"][0]["mandatory"][2].update(effective_address=mailbox("invented")),
            lambda data: data["messages"][0].update(mandatory_complete=0),
            lambda data: data["messages"][0]["recipients"][0]["selected"].update(address_source="RESOURCE_EMAIL"),
            lambda data: data["messages"][0]["recipients"][0]["contributions"].pop(),
            lambda data: data["identities"]["resources"].pop(0),
            lambda data: data["message_inputs"].append(data["message_inputs"][0]),
            lambda data: data["message_inputs"][0]["candidates"][0]["primary"]["identity"].update(kind=[]),
            lambda data: data["identities"]["users"].append(data["identities"]["users"][0]),
        )
        for mutate in mutations:
            data = deepcopy(self.context.to_payload())
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                RecipientPolicyContext.from_payload(data)

    def test_contact_fallback_requires_proven_authority_and_original_resource_email(self):
        contact = candidate("CONTACT", "C-1", None)
        fallback = candidate("RESOURCE", "R-1", None)
        inputs = MessageRecipientInputs("P-1", (RecipientAuthorityInput(contact, fallback),))
        result = RecipientPolicyContext.capture(self.population, self.identities, (inputs,)).messages[0]
        self.assertIsNone(result.mandatory[0].effective_address)
        with self.assertRaises(ValueError):
            RecipientPolicyContext.capture(self.population, self.identities, (replace(inputs, candidates=(RecipientAuthorityInput(contact, replace(fallback, email=mailbox("invented"))),)),))
        with self.assertRaises(ValueError):
            RecipientPolicyContext.capture(self.population, self.identities, (replace(inputs, candidates=(RecipientAuthorityInput(contact, candidate("RESOURCE", "R-2", mailbox("shared"))),)),))

    def test_duplicate_roles_and_conflicting_raw_authority_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.inputs, candidates=self.inputs.candidates + (self.inputs.candidates[0],))
        changed = replace(self.inputs.candidates[0], primary=replace(self.inputs.candidates[0].primary, email=mailbox("changed")))
        other = replace(self.inputs, project_id="P-2", candidates=(changed,))
        with self.assertRaises(ValueError):
            RecipientPolicyContext.capture(self.population, self.identities, (self.inputs, other))
