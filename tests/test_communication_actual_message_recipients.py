"""Actual included-message recipients; synthetic, inactive ADR-039 evidence."""
from dataclasses import replace
import json
import unittest

from app.domain.communication_actual_message_recipients import ActualMessageComposition
from app.domain.communication_message_context import MessageContextEvidence
from app.domain.communication_recipient_identity import RecipientIdentity, ResourceIdentityFact, UserIdentityFact
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import RecipientIdentityEvidence, RecipientPolicyContext
from app.domain.project_communication_messages import (
    project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure
from tests import test_communication_message_context as context_fixtures
from tests import test_project_communication_v5 as fixtures
from tests.test_project_communication_historical_golden import historical_fixture


FULL, DELTA = context_fixtures.FULL, context_fixtures.DELTA
baseline = context_fixtures.baseline
mailbox = fixtures.mailbox


class ActualMessageCompositionTests(unittest.TestCase):
    def setUp(self):
        self.helper = context_fixtures.CommunicationMessageContextTests()
        self.helper.setUp()
        self.f = self.helper.f

    def compose(self, current, previous=None, *, excluded=()):
        context = MessageContextEvidence(FULL if previous is None else DELTA, current,
                                         None if previous is None else baseline(previous), excluded)
        return ActualMessageComposition(context)

    def with_policy(self, current, *, population=None, identities=None, generator=None):
        old = current.recipient_policy_context
        context = RecipientPolicyContext.capture(
            old.population if population is None else population,
            old.identities if identities is None else identities,
            old.message_inputs, generator=generator,
        )
        return replace(current, recipient_policy_context=context)

    def complete_population(self):
        old = self.f.population.to_population()
        return RecipientPolicyPopulation.capture(replace(old, employees=(
            old.employees[0], replace(old.employees[1], email=mailbox("r-2")),
        )))

    def test_subset_coverage_and_all_four_message_kinds_apply_current_population(self):
        previous, current = self.helper.two_projects()
        population = self.complete_population()
        current = self.with_policy(current, population=population)
        empty = self.with_policy(self.helper.empty(), population=population)
        full = self.compose(current)
        delta = self.compose(current, previous)
        removal = self.compose(empty, previous)
        added = self.compose(current, self.helper.build((), version=4))
        self.assertEqual(full.included_message_keys, ("project:P-1", "project:P-2"))
        self.assertEqual(delta.included_message_keys, ("project:P-2",))
        self.assertEqual(len(delta.context.current.projects), 2)
        self.assertEqual(removal.included_message_keys, ("project:P-1", "project:P-2"))
        self.assertEqual(removal.context.current.projects, ())
        self.assertEqual(added.included_message_keys, full.included_message_keys)
        self.assertEqual([row.change_kind for row in added.context.included_messages], ["added", "added"])
        self.assertEqual([row.change_kind for row in removal.context.included_messages], ["removed", "removed"])
        for composition in (full, delta, removal, added):
            for message in composition.messages:
                self.assertEqual([row.resolution.employee.employee.resource_id for row in message.mandatory], ["R-1", "R-2"])
                self.assertIn(mailbox("r-2"), message.cc_addresses)
                self.assertTrue(message.mandatory_complete)

    def test_current_to_precedence_and_old_new_assigned_recipients(self):
        old = replace(
            self.f.assignment, resource_id="R-OLD", resource_name="Demo old resource",
            resource_contact=fixtures.participant("OLD-RESOURCE", contact=False, user=False),
            project_manager=fixtures.participant("OLD-PM"),
            project_co_managers=(fixtures.participant("OLD-CO"),),
            requester=fixtures.participant("OLD-RQ"),
        )
        previous = self.helper.build((old,), version=4)
        message, = self.compose(self.f.v5, previous).messages
        self.assertEqual(message.to_addresses, tuple(mailbox(key) for key in
                         ("pm", "co", "rq", "old-pm", "old-co", "old-rq")))
        self.assertEqual(set(message.cc_addresses), {mailbox("r-1"), mailbox("old-resource")})
        self.assertEqual([row.scope for row in message.observations if row.requested_channel == "TO"],
                         ["CURRENT"] * 3 + ["HISTORICAL"] * 3)

    def test_frozen_historical_typed_addresses_retained_current_selection_wins(self):
        address = mailbox("new-assigned")
        manager_address = mailbox("new-manager")
        population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            replace(self.f.population.to_population().employees[0], email=address),
            self.f.population.to_population().employees[1],
        )))
        candidates = self.f.inputs.candidates
        inputs = replace(self.f.inputs, candidates=(
            replace(candidates[0], primary=replace(candidates[0].primary, email=manager_address)),
            *candidates[1:-1],
            replace(candidates[-1], primary=replace(candidates[-1].primary, email=address)),
        ))
        policy = RecipientPolicyContext.capture(population, self.f.identities, (inputs,))
        current = self.helper.build((replace(self.f.assignment,
            project_manager=replace(self.f.assignment.project_manager, email=manager_address),
            resource_contact=replace(self.f.assignment.resource_contact, email=address)),), policy)
        message, = self.compose(current, self.f.v5).messages
        self.assertEqual(message.to_addresses[0], manager_address)
        self.assertNotIn(mailbox("pm"), message.to_addresses)
        self.assertIn(address, message.cc_addresses)
        self.assertNotIn(mailbox("r-1"), message.cc_addresses)
        old = next(row for row in message.observations if row.scope == "HISTORICAL" and row.role == "ASSIGNED")
        self.assertTrue(old.superseded)
        self.assertEqual(old.raw_address, mailbox("r-1"))
        manager = message.recipients[0].identity_deliveries[0]
        self.assertEqual([message.observations[i].raw_address for i in manager.contributing_observations],
                         [manager_address, mailbox("pm")])

    def test_exact_record_precedence_without_historical_alias_enrichment(self):
        # Old requester has a captured USER root. Current requester resolves to
        # CONTACT via its own graph. Same recorded user, not a historical alias.
        previous = self.helper.build((replace(self.f.assignment,
            requester=replace(self.f.assignment.requester, email=mailbox("old-requester"))),), version=4)
        message, = self.compose(self.f.v5, previous).messages
        self.assertIn(mailbox("rq"), message.to_addresses)
        self.assertNotIn(mailbox("old-requester"), message.to_addresses)
        old = next(row for row in message.observations if row.scope == "HISTORICAL" and row.role == "REQUESTER")
        self.assertEqual(old.canonical_identity, RecipientIdentity("USER", "U-RQ"))
        delivery = next(group for recipient in message.recipients for group in recipient.identity_deliveries
                        if group.canonical_identity == RecipientIdentity("CONTACT", "C-RQ"))
        self.assertTrue(any(message.observations[i] == old for i in delivery.contributing_observations))

    def test_frozen_historical_resource_link_fulfills_missing_address(self):
        identities = RecipientIdentityEvidence.capture(
            self.f.identities.resources, self.f.identities.contacts,
            tuple(UserIdentityFact(row.user_id, "E-2", row.business_contact_id)
                  if row.user_id == "U-PM" else row for row in self.f.identities.users),
        )
        previous = self.with_policy(self.f.v5, identities=identities)
        current = self.helper.empty()
        message, = self.compose(current, previous).messages
        mandatory = message.mandatory[1]
        self.assertTrue(mandatory.resolution.fulfilled_by_existing)
        self.assertEqual(mandatory.resolution.effective_address, mailbox("pm"))
        self.assertEqual(message.observations[mandatory.selected_observation].scope, "HISTORICAL")
        self.assertEqual(message.observations[mandatory.selected_observation].canonical_identity,
                         RecipientIdentity("RESOURCE", "R-2"))
        self.assertTrue(message.mandatory_complete)

    def test_unknown_or_unproven_historical_identity_cannot_discharge_blocker(self):
        identities = RecipientIdentityEvidence.capture(
            self.f.identities.resources, self.f.identities.contacts,
            tuple(UserIdentityFact(row.user_id, "E-2", row.business_contact_id)
                  if row.user_id == "U-PM" else row for row in self.f.identities.users),
        )
        current = self.with_policy(self.helper.empty(), identities=identities)
        for contact_id in (None, "C-PM"):
            with self.subTest(contact_id=contact_id):
                manager = replace(self.f.assignment.project_manager, contact_id=contact_id, user_id=None,
                                  display_name="Demo R-2", email=mailbox("r-2"))
                previous = self.helper.build((replace(self.f.assignment, project_manager=manager),), version=4)
                message, = self.compose(current, previous).messages
                observation = message.observations[0]
                self.assertEqual(observation.canonical_identity,
                                 None if contact_id is None else RecipientIdentity("CONTACT", contact_id))
                self.assertIn(mailbox("r-2"), message.to_addresses)
                self.assertIsNone(message.mandatory[1].selected_observation)
                self.assertFalse(message.mandatory[1].resolution.fulfilled_by_existing)
                self.assertEqual(message.blockers[0].resource_id, "R-2")
                self.assertEqual(message.blockers[0].reason, AddressFailure.MISSING)

    def test_superseded_assignment_cannot_rescue_current_missing_mandatory_address(self):
        old = self.f.v4
        population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            replace(self.f.population.to_population().employees[0], email=None),
            self.f.population.to_population().employees[1],
        )))
        inputs = replace(self.f.inputs, candidates=(
            *self.f.inputs.candidates[:-1],
            replace(self.f.inputs.candidates[-1], primary=replace(self.f.inputs.candidates[-1].primary, email=None)),
        ))
        policy = RecipientPolicyContext.capture(population, self.f.identities, (inputs,))
        current = self.helper.build((replace(self.f.assignment,
            resource_contact=replace(self.f.assignment.resource_contact, email=None)),), policy)
        message, = self.compose(current, old).messages
        self.assertNotIn(mailbox("r-1"), message.cc_addresses)
        self.assertIsNone(message.mandatory[0].selected_observation)
        self.assertEqual(message.mandatory[0].resolution.blocker.reason, AddressFailure.MISSING)
        self.assertTrue(any(row.superseded and row.raw_address == mailbox("r-1") for row in message.observations))

    def test_address_dedup_to_priority_and_distinct_selected_identity_proofs(self):
        shared = mailbox("shared")
        population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            replace(self.f.population.to_population().employees[0], email=shared),
            self.f.population.to_population().employees[1],
        )))
        rows = self.f.inputs.candidates
        inputs = replace(self.f.inputs, candidates=(
            replace(rows[0], primary=replace(rows[0].primary, email=shared.upper())),
            replace(rows[1], primary=replace(rows[1].primary, email=shared)),
            rows[2],
            replace(rows[3], primary=replace(rows[3].primary, email=shared)),
        ))
        generator = self.f._generator("linked", mailbox("other-generator"), "U-RQ")
        policy = RecipientPolicyContext.capture(population, self.f.identities, (inputs,), generator=generator)
        current = self.helper.build((replace(self.f.assignment,
            project_manager=replace(self.f.assignment.project_manager, email=shared.upper()),
            project_co_managers=(replace(self.f.assignment.project_co_managers[0], email=shared),),
            resource_contact=replace(self.f.assignment.resource_contact, email=shared)),), policy)
        message, = self.compose(current).messages
        self.assertEqual(message.to_addresses, (shared.upper(), mailbox("rq")))
        self.assertEqual(message.cc_addresses, ())
        envelope = message.recipients[0]
        self.assertEqual({row.canonical_identity for row in envelope.identity_deliveries}, {
            RecipientIdentity("CONTACT", "C-PM"), RecipientIdentity("CONTACT", "C-CO"),
            RecipientIdentity("RESOURCE", "R-1"),
        })
        mandatory = message.mandatory[0]
        self.assertEqual(mandatory.resolution.effective_address, shared.upper())
        self.assertTrue(mandatory.resolution.fulfilled_by_existing)
        self.assertEqual(message.observations[mandatory.selected_observation].identity, RecipientIdentity("RESOURCE", "R-1"))
        self.assertNotEqual(mandatory.selected_observation, envelope.selected_observation)
        self.assertTrue(any(message.observations[i].role == "GENERATOR"
                            for i in message.recipients[1].identity_deliveries[0].contributing_observations))

    def test_current_generator_and_population_only_in_removed_message(self):
        old_generator = self.f._generator("old", mailbox("old-generator"))
        old_population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            *self.f.population.to_population().employees,
            SuperintendentEmployee("R-HIST", "Demo historical", mailbox("historical")),
        )))
        old_identities = RecipientIdentityEvidence.capture(
            (*self.f.identities.resources, ResourceIdentityFact("R-HIST", "E-HIST")),
            self.f.identities.contacts, self.f.identities.users,
        )
        previous = self.with_policy(self.f.v5, population=old_population,
                                    identities=old_identities, generator=old_generator)
        identities = RecipientIdentityEvidence.capture(self.f.identities.resources, self.f.identities.contacts,
            (*self.f.identities.users, UserIdentityFact("U-R2", "E-2", None)))
        generator = self.f._generator("current", mailbox("current-generator"), "U-R2")
        current = self.with_policy(self.helper.empty(), identities=identities, generator=generator)
        message, = self.compose(current, previous).messages
        self.assertIn(mailbox("current-generator"), message.cc_addresses)
        self.assertNotIn(mailbox("old-generator"), message.cc_addresses)
        self.assertNotIn(mailbox("historical"), message.cc_addresses)
        self.assertEqual([row.resolution.employee.employee.resource_id for row in message.mandatory], ["R-1", "R-2"])
        self.assertTrue(message.mandatory_complete)
        self.assertEqual(message.observations[message.mandatory[1].selected_observation].role, "GENERATOR")

    def test_exclusions_no_message_and_all_excluded_do_not_approve(self):
        previous, current = self.helper.two_projects()
        composition = self.compose(current, excluded=("project:P-1",))
        self.assertEqual(composition.eligible_message_keys, ("project:P-1", "project:P-2"))
        self.assertEqual(composition.included_message_keys, ("project:P-2",))
        self.assertEqual(len(composition.messages[0].mandatory), 2)
        self.assertFalse(composition.messages[0].mandatory_complete)
        for result in (self.compose(self.f.v5, self.f.v4),
                       self.compose(current, excluded=("project:P-1", "project:P-2"))):
            self.assertEqual(result.messages, ())
            self.assertEqual(result.included_message_keys, ())
            self.assertNotIn("approved", result.to_payload())
            self.assertEqual(ActualMessageComposition.deserialize(result.serialize(), context=result.context), result)

    def test_strict_actual_coverage_envelope_and_false_fulfillment_rejection(self):
        composition = self.compose(self.f.v5)
        original = composition.to_payload()
        for mutate in (
            lambda p: p.pop("messages"),
            lambda p: p["messages"].clear(),
            lambda p: p["messages"].append(p["messages"][0]),
            lambda p: p["messages"][0].update(message_key="project:invented"),
            lambda p: p["included_message_keys"].clear(),
            lambda p: p["messages"][0]["recipients"][0].update(address=mailbox("forged")),
            lambda p: p["messages"][0]["recipients"][0].update(channel="CC"),
            lambda p: p["messages"][0]["recipients"][0]["identity_deliveries"][0].update(selected_observation=99),
            lambda p: p["messages"][0]["observations"][0].update(scope="HISTORICAL"),
            lambda p: p["messages"][0]["mandatory"].pop(),
            lambda p: p["messages"][0]["mandatory"][1]["resolution"].update(fulfilled_by_existing=True),
            lambda p: p["messages"][0]["mandatory"][1].update(selected_observation=0),
            lambda p: p["messages"][0].update(mandatory_complete=0),
            lambda p: p["context"].update(excluded_message_keys=["project:P-1"]),
        ):
            payload = json.loads(json.dumps(original))
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ActualMessageComposition.from_payload(payload, context=composition.context)
        encoded = composition.serialize()
        self.assertEqual(ActualMessageComposition.deserialize(encoded, context=composition.context), composition)
        duplicate = encoded.replace('"mandatory_complete":false', '"mandatory_complete":false,"mandatory_complete":false')
        self.assertNotEqual(duplicate, encoded)
        with self.assertRaises(ValueError):
            ActualMessageComposition.deserialize(duplicate, context=composition.context)

    def test_binding_failure_and_invalid_existing_recipient_diagnostics(self):
        population = RecipientPolicyPopulation.capture(SuperintendentPopulation(
            None, (), "SUPERINTENDENT_BINDING_MISSING",
        ))
        message, = self.compose(self.with_policy(self.f.v5, population=population)).messages
        self.assertFalse(message.mandatory_complete)
        self.assertEqual(message.binding_diagnostic, "SUPERINTENDENT_BINDING_MISSING")
        self.assertEqual(message.mandatory, ())
        old = self.helper.build((replace(self.f.assignment,
            project_manager=replace(self.f.assignment.project_manager, email="bad-address"),
            resource_contact=replace(self.f.assignment.resource_contact, email="bad-address")),), version=4)
        removed, = self.compose(self.helper.empty(), old).messages
        self.assertTrue(any(row.severity == "BLOCKING" and row.reason == "INVALID" for row in removed.existing_diagnostics))
        self.assertTrue(any(row.severity == "WARNING" and row.reason == "INVALID" for row in removed.existing_diagnostics))
        self.assertNotIn("bad-address", removed.to_addresses + removed.cc_addresses)

    def test_p5_independence_and_all_frozen_historical_serializations(self):
        current = self.helper.empty()
        original = project_projection_fingerprint(current), serialize_project_projection(current)
        for version in (None, 1, 2, 3, 4):
            previous = historical_fixture(version)
            frozen = project_projection_fingerprint(previous), serialize_project_projection(previous)
            composition = self.compose(current, previous)
            self.assertEqual(len(composition.messages), 1)
            self.assertEqual(ActualMessageComposition.deserialize(composition.serialize(), context=composition.context), composition)
            self.assertEqual((project_projection_fingerprint(previous), serialize_project_projection(previous)), frozen)
            self.assertEqual((project_projection_fingerprint(current), serialize_project_projection(current)), original)

    def test_invalid_current_link_does_not_transfer_proof_to_historical_contact(self):
        identities = RecipientIdentityEvidence.capture(
            self.f.identities.resources, self.f.identities.contacts,
            tuple(UserIdentityFact(row.user_id, "E-2", row.business_contact_id)
                  if row.user_id == "U-PM" else row for row in self.f.identities.users),
        )
        inputs = replace(self.f.inputs, candidates=(
            replace(self.f.inputs.candidates[0], primary=replace(self.f.inputs.candidates[0].primary, email="bad-address")),
            *self.f.inputs.candidates[1:],
        ))
        policy = RecipientPolicyContext.capture(self.f.population, identities, (inputs,))
        current = self.helper.build((replace(self.f.assignment,
            project_manager=replace(self.f.assignment.project_manager, email="bad-address")),), policy)
        message, = self.compose(current, self.f.v4).messages
        self.assertIn(mailbox("pm"), message.to_addresses)
        self.assertTrue(any(row.severity == "BLOCKING" and row.reason == "INVALID" for row in message.existing_diagnostics))
        self.assertIsNone(message.mandatory[1].selected_observation)
        self.assertEqual(message.mandatory[1].resolution.blocker.reason, AddressFailure.MISSING)
        # The first valid To may be a current co-manager. Find the historical
        # manager delivery explicitly rather than assuming its position.
        selected = next(message.observations[row.selected_observation] for row in message.recipients
                        if row.address == mailbox("pm"))
        self.assertEqual(selected.scope, "HISTORICAL")
        self.assertEqual(selected.canonical_identity, RecipientIdentity("CONTACT", "C-PM"))

    def test_disjoint_generator_only_identity_evidence_applies_to_actual_messages(self):
        extension = RecipientIdentityEvidence.capture(
            (ResourceIdentityFact("R-G", "E-G"),), (), (UserIdentityFact("U-G", "E-G", None),),
        )
        generator = self.f._generator("disjoint", mailbox("disjoint-generator"), "U-G")
        policy = RecipientPolicyContext.capture(self.f.population, self.f.identities,
            (self.f.inputs,), generator=generator, composition_identities=extension)
        current = self.f.build(5, policy)
        message, = self.compose(current).messages
        group = next(group for recipient in message.recipients for group in recipient.identity_deliveries
                     if group.canonical_identity == RecipientIdentity("RESOURCE", "R-G"))
        selected = message.observations[group.selected_observation]
        self.assertEqual(selected.scope, "AUTHENTICATED")
        self.assertEqual(selected.raw_address, mailbox("disjoint-generator"))
        self.assertEqual(project_projection_fingerprint(current), project_projection_fingerprint(self.f.v5))
