"""Phase 2A.1a: trusted source facts and current-source composition only."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import unittest

from app.domain.communication_recipient_authority import (
    AuthenticatedGeneratorAuthority, ERPManagerAuthority, RecipientAddressObservation,
)
from app.domain.communication_recipient_composition import RecipientCandidate
from app.domain.communication_recipient_identity import (
    AuthenticatedPrincipalIdentityFact, ContactIdentityFact, ERPPrincipalIdentityFact,
    RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
)
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence, RecipientPolicyContext,
)
from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure


def mailbox(local):
    return local + chr(64) + "invalid.test"


def observation(kind, key, raw):
    return RecipientAddressObservation(
        {"RESOURCE": "RESOURCE_EMAIL", "CONTACT": "BUSINESS_CONTACT_EMAIL", "USER": "APP_USER_EMAIL"}[kind],
        RecipientIdentity(kind, key), "CURRENT", raw,
    )


def direct(kind, key, role, channel, raw, active=True):
    address = observation(kind, key, raw)
    return RecipientAuthorityInput(RecipientCandidate(
        address.record, "Demo same name", role, channel, raw, address.source, active,
    ))


def generator(local_user=None, *, issuer="urn:test", subject="generator", email=None):
    principal = AuthenticatedPrincipalIdentityFact(issuer, subject, local_user)
    return AuthenticatedGeneratorAuthority(
        principal, "Demo same name", RecipientAddressObservation(
            "AUTHENTICATED_EMAIL", principal.identity, "AUTHENTICATED",
            mailbox("auth") if email is None else email,
        ),
    )


def manager(**changes):
    # ERP-only principal, unique Resource address, no user or contact.
    fields = dict(principal=ERPPrincipalIdentityFact("P", "E"), display_name="Demo same name",
                  linked_user_id=None, linked_user_active=None, contact_id=None,
                  contact_status="ABSENT", contact_active=None, contact_address=None,
                  resource_address=observation("RESOURCE", "R", mailbox("fallback")),
                  resource_active=True, resource_erp_active=True)
    fields.update(changes)
    return ERPManagerAuthority(**fields)


def context(rows, *, resources=(), contacts=(), users=(), employees=(), generator_authority=None):
    evidence = RecipientIdentityEvidence.capture(resources, contacts, users)
    population = RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP", employees))
    return RecipientPolicyContext.capture(population, evidence,
        (MessageRecipientInputs("P", tuple(rows)),), generator=generator_authority)


class RecipientSourceAuthorityTests(unittest.TestCase):
    def test_unlinked_and_linked_authenticated_principal(self):
        requester = direct("USER", "U", "REQUESTER", "TO", mailbox("persisted"))
        users = (UserIdentityFact("U", "E", None),)
        for link in (None, "U"):
            with self.subTest(link=link):
                authority = generator(link)
                result = context((requester,), users=users, generator_authority=authority)
                recipients = result.messages[0].recipients
                self.assertEqual(recipients[0].address, mailbox("persisted"))
                if link:
                    self.assertEqual(len(recipients), 1)
                    self.assertEqual([row.role for row in recipients[0].contributions], ["REQUESTER", "GENERATOR"])
                    self.assertEqual(recipients[0].contributions[1].resolution.address, mailbox("auth"))
                else:
                    self.assertEqual(len(recipients), 2)
                    self.assertEqual(recipients[1].selected.identity, authority.principal.identity)
                expected = hashlib.sha256("\0".join((
                    "project-communication-generator-cc-v1", "urn:test", "generator",
                    link or "", mailbox("auth"),
                )).encode("utf-8")).hexdigest()
                self.assertEqual(result.to_payload()["generator_identity_fingerprint"], expected)
                self.assertEqual(RecipientPolicyContext.from_payload(result.to_payload()), result)

    def test_generator_off_and_invalid_or_fabricated_sources(self):
        off = context(())
        self.assertIsNone(off.to_payload()["generator"])
        self.assertIsNone(off.to_payload()["generator_identity_fingerprint"])
        self.assertEqual(off.messages[0].recipients, ())
        for raw in ("", "bad-address", mailbox("a") + "," + mailbox("b"), mailbox("a") + "\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                generator(email=raw)
        with self.assertRaises(ValueError):
            context((), generator_authority=generator("U"))
        with self.assertRaises(ValueError):
            direct("USER", "U", "GENERATOR", "CC", mailbox("fabricated"))
        with self.assertRaises(ValueError):
            direct("USER", "U", "MANAGER", "TO", mailbox("not-manager-authority"))
        with self.assertRaises(ValueError):
            RecipientAddressObservation("AUTHENTICATED_EMAIL", RecipientIdentity("USER", "U"),
                                        "AUTHENTICATED", mailbox("fabricated"))
        with self.assertRaises(ValueError):
            RecipientAddressObservation("RESOURCE_EMAIL", RecipientIdentity("RESOURCE", "R"),
                                        "AUTHENTICATED", mailbox("fabricated"))
        with self.assertRaises(ValueError):
            context((), generator_authority={"email": mailbox("client")})

    def test_erp_only_and_contactless_user_fallback_including_inactive_resource(self):
        resources = (ResourceIdentityFact("R", "E"),)
        for user in (None, UserIdentityFact("U", "E", None), UserIdentityFact("U", "E", "C-missing")):
            for active, erp_active in ((True, True), (False, True), (True, False)):
                with self.subTest(user=user, active=active, erp_active=erp_active):
                    row = manager(linked_user_id=user.user_id if user else None,
                                  linked_user_active=True if user else None,
                                  contact_id=user.business_contact_id if user else None,
                                  contact_status="UNRESOLVED" if user and user.business_contact_id else "ABSENT",
                                  resource_active=active, resource_erp_active=erp_active)
                    result = context((row,), resources=resources, users=(user,) if user else ())
                    recipient = result.messages[0].recipients[0]
                    self.assertEqual((recipient.channel, recipient.address), ("TO", mailbox("fallback")))
                    self.assertEqual(recipient.selected.identity, row.principal.identity)
                    self.assertEqual(recipient.selected.address_source, "RESOURCE_EMAIL")
                    self.assertEqual(result.messages[0].mandatory, ())
                    self.assertEqual(RecipientPolicyContext.from_payload(result.to_payload()), result)
                    if not (active and erp_active):
                        with self.assertRaises(ValueError):
                            context((row,), resources=resources, users=(user,) if user else (),
                                employees=(SuperintendentEmployee("R", "Demo same name", mailbox("fallback")),))

    def test_contact_precedence_and_inactive_manager_blocks(self):
        resources = (ResourceIdentityFact("R", "E"),)
        users = (UserIdentityFact("U", "E", "C"),)
        contacts = (ContactIdentityFact("C"),)
        for raw, user_active, contact_active, expected in (
            (mailbox("contact"), True, True, mailbox("contact")),
            (None, True, True, mailbox("fallback")),
            ("bad-address", True, True, None),
            (mailbox("a") + "," + mailbox("b"), True, True, None),
            (mailbox("contact") + "\n", True, True, None),
            (None, False, True, None),
            (None, True, False, None),
            (mailbox("contact"), False, True, None),
            (mailbox("contact"), True, False, None),
        ):
            with self.subTest(raw=raw, user_active=user_active, contact_active=contact_active):
                row = manager(linked_user_id="U", linked_user_active=user_active, contact_id="C",
                              contact_status="RESOLVED", contact_active=contact_active,
                              contact_address=observation("CONTACT", "C", raw))
                result = context((row,), resources=resources, users=users, contacts=contacts)
                actual = result.messages[0].recipients
                self.assertEqual([recipient.address for recipient in actual], [] if expected is None else [expected])
                self.assertEqual(row.candidate(result.identities, result.index()).active, user_active and contact_active)
                if expected == mailbox("contact"):
                    self.assertEqual(actual[0].selected.address_source, "BUSINESS_CONTACT_EMAIL")
                elif expected == mailbox("fallback"):
                    self.assertEqual(actual[0].selected.address_source, "RESOURCE_EMAIL")
        inactive = manager(linked_user_id="U", linked_user_active=False)
        self.assertEqual(context((inactive,), resources=resources,
            users=(UserIdentityFact("U", "E", None),)).messages[0].recipients, ())

    def test_no_fabricated_erp_and_unproven_fallback(self):
        with self.assertRaises(ValueError):
            ERPPrincipalIdentityFact("P", "")
        row = manager()
        for resources in ((), (ResourceIdentityFact("R", "OTHER"),),
                          (ResourceIdentityFact("R", "E"), ResourceIdentityFact("R2", "E"))):
            with self.subTest(resources=resources), self.assertRaises(ValueError):
                context((row,), resources=resources)
        with self.assertRaises(ValueError):
            context((replace(row, principal=ERPPrincipalIdentityFact("OTHER-P", "E")),),
                    resources=(ResourceIdentityFact("R", "E"),))
        # A legacy ERP-labelled contact is not independent ERP identity authority.
        co = direct("CONTACT", "C", "CO_MANAGER", "TO", mailbox("co"))
        result = context((co,), contacts=(ContactIdentityFact("C", "ACUMATICA", "EMPLOYEE", "E"),),
                         resources=(ResourceIdentityFact("R", "E"),),
                         employees=(SuperintendentEmployee("R", "Demo same name", None),))
        self.assertEqual(result.messages[0].recipients[0].address, mailbox("co"))
        self.assertEqual(result.messages[0].mandatory[0].blocker_reason, AddressFailure.MISSING)
        self.assertNotIn(RecipientIdentity("ERP_EMPLOYEE", "E"), result.index().aliases)

    def test_combined_roles_nomination_and_address_deduplication(self):
        row = manager(linked_user_id="U", linked_user_active=True, contact_id="C",
                      contact_status="RESOLVED", contact_active=True,
                      contact_address=observation("CONTACT", "C", mailbox("contact")),
                      resource_address=observation("RESOURCE", "R", None))
        contacts = (ContactIdentityFact("C"), ContactIdentityFact("C2"))
        users = (UserIdentityFact("U", "E", "C"),)
        resources = (ResourceIdentityFact("R", "E"), ResourceIdentityFact("R2", "E2"))
        co = direct("CONTACT", "C", "CO_MANAGER", "TO", mailbox("contact"))
        independent = direct("CONTACT", "C2", "CO_MANAGER", "TO", mailbox("contact"))
        requester = direct("USER", "U", "REQUESTER", "TO", mailbox("requester"))
        assigned = direct("RESOURCE", "R", "ASSIGNED", "CC", None)
        employees = (SuperintendentEmployee("R", "Demo same name", None),
                     SuperintendentEmployee("R2", "Demo same name", mailbox("CONTACT")))
        result = context((assigned, co, requester, row, independent), resources=resources,
                         users=users, contacts=contacts, employees=employees,
                         generator_authority=generator("U"))
        recipient, = result.messages[0].recipients
        self.assertEqual((recipient.channel, recipient.address), ("TO", mailbox("contact")))
        self.assertEqual(recipient.selected.identity, row.principal.identity)
        self.assertEqual({c.role for c in recipient.contributions},
                         {"MANAGER", "CO_MANAGER", "REQUESTER", "GENERATOR", "SUPERINTENDENT"})
        self.assertEqual(result.messages[0].unusable_existing[0].role, "ASSIGNED")
        self.assertTrue(result.messages[0].mandatory[0].fulfilled_by_existing)
        self.assertFalse(result.messages[0].mandatory[1].fulfilled_by_existing)
        roles = result.to_payload()["message_inputs"][0]["role_evidence"]
        self.assertEqual([r["relationship"] for r in roles],
                         ["ERP_PROJECT_PRINCIPAL", "RP_PROJECT_NOMINATION", "RP_PROJECT_NOMINATION",
                          "PROJECT_REQUESTER", "PROJECT_ASSIGNMENT"])
        self.assertEqual(RecipientPolicyContext.from_payload(json.loads(json.dumps(result.to_payload()))), result)

    def test_combined_valid_assignment_retains_every_role(self):
        raw = mailbox("fallback")
        row = manager(linked_user_id="U", linked_user_active=True, contact_id="C",
                      contact_status="RESOLVED", contact_active=True,
                      contact_address=observation("CONTACT", "C", mailbox("contact")))
        result = context((
            row, direct("CONTACT", "C", "CO_MANAGER", "TO", mailbox("contact")),
            direct("USER", "U", "REQUESTER", "TO", mailbox("requester")),
            direct("RESOURCE", "R", "ASSIGNED", "CC", raw),
        ), resources=(ResourceIdentityFact("R", "E"),), contacts=(ContactIdentityFact("C"),),
            users=(UserIdentityFact("U", "E", "C"),),
            employees=(SuperintendentEmployee("R", "Demo same name", raw),),
            generator_authority=generator("U"))
        recipient, = result.messages[0].recipients
        self.assertEqual(recipient.address, mailbox("contact"))
        self.assertEqual({c.role for c in recipient.contributions},
                         {"MANAGER", "CO_MANAGER", "REQUESTER", "ASSIGNED", "GENERATOR", "SUPERINTENDENT"})
        self.assertEqual(result.messages[0].unusable_existing, ())
        self.assertTrue(result.messages[0].mandatory[0].fulfilled_by_existing)

    def test_conflicting_bindings_preserve_direct_delivery_not_mandatory_fulfillment(self):
        resources = (ResourceIdentityFact("R", "E"), ResourceIdentityFact("R2", "OTHER"))
        users = (UserIdentityFact("U", "E", "C"),)
        contacts = (ContactIdentityFact("C", "RESOURCEPLANNER", "EMPLOYEE", "OTHER"),)
        row = manager(linked_user_id="U", linked_user_active=True, contact_id="C",
                      contact_status="RESOLVED", contact_active=True,
                      contact_address=observation("CONTACT", "C", mailbox("contact")),
                      resource_address=None, resource_active=None, resource_erp_active=None)
        result = context((row,), resources=resources, users=users, contacts=contacts,
                         employees=(SuperintendentEmployee("R", "Demo same name", None),),
                         generator_authority=generator("U"))
        self.assertIn(row.principal.identity, result.index().ambiguous)
        self.assertEqual(result.messages[0].recipients[0].address, mailbox("contact"))
        self.assertEqual(result.messages[0].mandatory[0].blocker_reason, AddressFailure.MISSING)
        with self.assertRaises(ValueError):
            context((replace(row, resource_address=observation("RESOURCE", "R", mailbox("fallback")),
                             resource_active=True, resource_erp_active=True),),
                    resources=resources, users=users, contacts=contacts)
        unlinked = generator(issuer="urn:a", subject="same")
        other = generator(issuer="urn:b", subject="same")
        self.assertNotEqual(unlinked.principal.identity, other.principal.identity)
        self.assertNotEqual(RecipientIdentity("ERP_EMPLOYEE", "E"), RecipientIdentity("USER", "E"))
        evidence = RecipientIdentityEvidence.capture((), (), (UserIdentityFact("U", None, None),))
        with self.assertRaises(ValueError):
            evidence.index(authenticated_principals=(
                generator("U").principal, generator().principal,
            ))

    def test_missing_fallback_observation_and_conflicting_source_activity(self):
        resources = (ResourceIdentityFact("R", "E"),)
        row = manager()
        with self.assertRaisesRegex(ValueError, "Missing proven ERP manager Resource address"):
            context((replace(row, resource_address=None, resource_active=None,
                             resource_erp_active=None),), resources=resources)
        with self.assertRaisesRegex(ValueError, "Contradictory Resource activity"):
            context((row, direct("RESOURCE", "R", "ASSIGNED", "CC", mailbox("fallback"), False)),
                    resources=resources)
        linked = manager(linked_user_id="U", linked_user_active=False, contact_id="C",
                         contact_status="RESOLVED", contact_active=True,
                         contact_address=observation("CONTACT", "C", mailbox("contact")))
        with self.assertRaisesRegex(ValueError, "Contradictory requester/manager user activity"):
            context((linked, direct("USER", "U", "REQUESTER", "TO", mailbox("requester"))),
                    resources=resources, users=(UserIdentityFact("U", "E", "C"),),
                    contacts=(ContactIdentityFact("C"),))
        inactive_contact = replace(linked, linked_user_active=True, contact_active=False)
        with self.assertRaisesRegex(ValueError, "Contradictory resolved contact activity"):
            context((inactive_contact, direct("CONTACT", "C", "CO_MANAGER", "TO", mailbox("contact"))),
                    resources=resources, users=(UserIdentityFact("U", "E", "C"),),
                    contacts=(ContactIdentityFact("C"),))

    def test_scoped_consistency_round_trip_and_tampering(self):
        row = manager()
        resources = (ResourceIdentityFact("R", "E"),)
        assigned = direct("RESOURCE", "R", "ASSIGNED", "CC", mailbox("different"))
        with self.assertRaises(ValueError):
            context((row, assigned), resources=resources)
        result = context((row,), resources=resources, generator_authority=generator())
        payload = result.to_payload()
        self.assertEqual(RecipientPolicyContext.from_payload(payload), result)
        for mutate in (
            lambda p: p.pop("generator_identity_fingerprint"),
            lambda p: p.update(generator_identity_fingerprint="0" * 64),
            lambda p: p["generator"]["address"]["resolution"].update(address=mailbox("tampered")),
            lambda p: p["message_inputs"][0]["role_evidence"][0].update(relationship="RP_PROJECT_NOMINATION"),
            lambda p: p["message_inputs"][0]["candidates"][0]["erp_manager"].update(resource_active=1),
            lambda p: p["message_inputs"][0]["candidates"][0]["erp_manager"]["resource_address"].update(scope="AUTHENTICATED"),
            lambda p: p["message_inputs"][0]["candidates"][0]["erp_manager"]["principal"].update(project_id="OTHER-P"),
            lambda p: p["messages"][0]["recipients"][0]["contributions"].clear(),
        ):
            tampered = deepcopy(payload)
            mutate(tampered)
            with self.subTest(tampered=tampered), self.assertRaises(ValueError):
                RecipientPolicyContext.from_payload(tampered)
