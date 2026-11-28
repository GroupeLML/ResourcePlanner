"""Inactive historical context evidence, with no operational message generation."""
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from app.domain.communication_message_context import (
    CommunicatedBaselineEvidence, CommunicatedBaselineReference, MessageContextEvidence,
)
from app.domain.communication_recipient_identity import RecipientIdentity, ResourceIdentityFact, UserIdentityFact
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import RecipientIdentityEvidence, RecipientPolicyContext
from app.domain.project_communication import build_project_communication_projection
from app.domain.project_communication_messages import (
    MESSAGE_KIND_PLANNING_CHANGE as DELTA, MESSAGE_KIND_WEEKLY_CONFIRMATION as FULL,
    project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_population import SuperintendentEmployee
from tests import test_project_communication_v5 as fixtures
from tests.test_project_communication_historical_golden import historical_fixture


def baseline(snapshot, batch_id="B-1"):
    return CommunicatedBaselineEvidence(CommunicatedBaselineReference(
        batch_id, snapshot.week_start, snapshot.week_end, project_projection_fingerprint(snapshot),
    ), snapshot)


class CommunicationMessageContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectCommunicationV5Tests()
        self.fixture.setUp()
        self.f = self.fixture

    def build(self, assignments, context=None, *, version=5, week=fixtures.WEEK):
        return build_project_communication_projection(
            week_start=week, week_end=week + timedelta(days=6), assignments=tuple(assignments),
            request_provenance_version=version, recipient_policy_context=context,
        )

    def empty(self, *, generator=None):
        return self.build((), RecipientPolicyContext.capture(
            self.f.population, self.f.identities, (), generator=generator,
        ))

    def two_projects(self):
        second = replace(self.f.assignment, project_id="P-2", project_number="2000", shift_id="S-2")
        inputs = replace(self.f.inputs, project_id="P-2")
        context = RecipientPolicyContext.capture(self.f.population, self.f.identities, (self.f.inputs, inputs))
        previous = self.build((self.f.assignment, second), version=4)
        current = self.build((self.f.assignment, replace(second, hours=7.0)), context)
        return previous, current

    def test_subset_of_current_projects_requires_delta_context(self):
        previous, current = self.two_projects()
        original = serialize_project_projection(current)
        evidence = MessageContextEvidence(DELTA, current, baseline(previous))
        self.assertEqual([row.project_id for row in current.recipient_policy_context.message_inputs], ["P-1", "P-2"])
        self.assertEqual([row.project_id for row in evidence.messages], ["P-2"])
        self.assertEqual(evidence.messages[0].change_kind, "modified")
        self.assertEqual(serialize_project_projection(current), original)
        self.assertEqual(MessageContextEvidence(DELTA, self.f.v5, baseline(self.f.v4)).messages, ())

    def test_removed_project_context_with_current_project_and_empty_current(self):
        previous, _ = self.two_projects()
        for current, expected in ((self.f.v5, ["P-2"]), (self.empty(), ["P-1", "P-2"])):
            evidence = MessageContextEvidence(DELTA, current, baseline(previous))
            self.assertEqual([row.project_id for row in evidence.messages], expected)
            for row in evidence.messages:
                self.assertEqual(row.change_kind, "removed")
                self.assertIsNone(row.after)
                self.assertEqual(row.before.scope, "HISTORICAL")
                self.assertEqual(row.before.baseline, evidence.baseline.reference)
                self.assertEqual(row.before.project_id, row.project_id)
                self.assertTrue(row.before.participants)
            self.assertEqual(evidence.current_policy.population, self.f.population)

    def test_whole_project_exclusion_retains_eligible_coverage_and_current_policy(self):
        _, current = self.two_projects()
        full = MessageContextEvidence(FULL, current, None)
        excluded = MessageContextEvidence(FULL, current, None, ("project:P-1",))
        self.assertEqual([row.message_key for row in excluded.messages], [row.message_key for row in full.messages])
        self.assertEqual([row.message_key for row in excluded.included_messages], ["project:P-2"])
        self.assertEqual(excluded.current_policy.population, full.current_policy.population)
        self.assertFalse(excluded.current_policy.current_resolution[1].mandatory_complete)
        self.assertEqual(project_projection_fingerprint(excluded.current), project_projection_fingerprint(full.current))
        with self.assertRaises(ValueError):
            MessageContextEvidence(FULL, current, None, ("project:invented",))

    def test_baseline_reference_identity_week_status_and_fingerprint_validation(self):
        reference = baseline(self.f.v4)
        for changes in ({"snapshot_fingerprint": "0" * 64}, {"week_end": fixtures.END + timedelta(days=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CommunicatedBaselineEvidence(replace(reference.reference, **changes), self.f.v4)
        for changes in ({"status": "PREPARED"}, {"model_version": "legacy"}, {"batch_id": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(reference.reference, **changes)
        other_week = self.build((), version=4, week=fixtures.WEEK - timedelta(days=7))
        with self.assertRaises(ValueError):
            MessageContextEvidence(DELTA, self.empty(), baseline(other_week))
        with self.assertRaises(ValueError):
            MessageContextEvidence(DELTA, self.empty(), None)
        with self.assertRaises(ValueError):
            MessageContextEvidence(FULL, self.f.v5, reference)
        current = self.empty()
        payload = MessageContextEvidence(DELTA, current, reference).to_payload()
        with self.assertRaises(ValueError):
            MessageContextEvidence.from_payload(payload, current=current, baseline=baseline(self.f.v4, "B-2"))

    def test_historical_unknown_identity_and_legacy_contact_not_enriched(self):
        identities = RecipientIdentityEvidence.capture(self.f.identities.resources, self.f.identities.contacts,
            tuple(UserIdentityFact(row.user_id, "E-2", row.business_contact_id) if row.user_id == "U-PM" else row
                  for row in self.f.identities.users))
        current = self.build((), RecipientPolicyContext.capture(self.f.population, identities, ()))
        self.assertTrue(current.recipient_policy_context.current_index().equivalent(
            RecipientIdentity("CONTACT", "C-PM"), RecipientIdentity("RESOURCE", "R-2")))
        for contact_id in (None, "C-PM"):
            manager = replace(self.f.assignment.project_manager, contact_id=contact_id, user_id=None,
                              display_name="Demo R-2", email=fixtures.mailbox("r-2"))
            previous = self.build((replace(self.f.assignment, project_manager=manager),), version=4)
            evidence = MessageContextEvidence(DELTA, current, baseline(previous))
            before = evidence.messages[0].before
            observed = before.participants[0]
            self.assertEqual(observed.participant, manager)
            self.assertIsNone(observed.proven_resource_id)
            if contact_id is None:
                self.assertIsNone(observed.recorded_identity)
            else:
                self.assertEqual(observed.recorded_identity.id, contact_id)
            self.assertIsNone(before.source_inputs)
            self.assertFalse(before.supports_mandatory_resource("R-2"))
            self.assertTrue(before.supports_mandatory_resource("R-1"))
            self.assertIsNone(evidence.current_policy.population.employees[1].resolution.address)

    def test_typed_historical_address_is_distinct_and_policy_is_current_only(self):
        old_generator = self.f._generator("old", fixtures.mailbox("old-generator"))
        new_generator = self.f._generator("new", fixtures.mailbox("new-generator"))
        old_population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            *self.f.population.to_population().employees,
            SuperintendentEmployee("R-HIST", "Demo historical", fixtures.mailbox("historical")),
        )))
        old_identities = RecipientIdentityEvidence.capture(
            (*self.f.identities.resources, ResourceIdentityFact("R-HIST", "E-HIST")),
            self.f.identities.contacts, self.f.identities.users,
        )
        old_context = RecipientPolicyContext.capture(old_population, old_identities, (self.f.inputs,), generator=old_generator)
        previous = self.f.build(5, old_context)
        address = fixtures.mailbox("new-assigned")
        new_population = RecipientPolicyPopulation.capture(replace(self.f.population.to_population(), employees=(
            replace(self.f.population.to_population().employees[0], email=address),
            self.f.population.to_population().employees[1],
        )))
        inputs = replace(self.f.inputs, candidates=(
            *self.f.inputs.candidates[:-1],
            replace(self.f.inputs.candidates[-1], primary=replace(self.f.inputs.candidates[-1].primary, email=address)),
        ))
        new_context = RecipientPolicyContext.capture(new_population, self.f.identities, (inputs,), generator=new_generator)
        current = self.build((replace(self.f.assignment, resource_contact=replace(self.f.assignment.resource_contact, email=address)),), new_context)
        evidence = MessageContextEvidence(DELTA, current, baseline(previous))
        row = evidence.messages[0]
        self.assertEqual(row.before.typed_observations[-1].scope, "HISTORICAL")
        self.assertEqual(row.after.typed_observations[-1].scope, "CURRENT")
        self.assertEqual(row.before.typed_observations[-1].canonical_identity, row.after.typed_observations[-1].canonical_identity)
        self.assertEqual(row.before.typed_observations[-1].candidate.email, fixtures.mailbox("r-1"))
        self.assertEqual(row.after.typed_observations[-1].candidate.email, address)
        self.assertEqual(evidence.current_policy.population, new_population)
        self.assertEqual(evidence.current_policy.generator, new_generator)
        payload = evidence.to_payload()
        self.assertNotIn("R-HIST", json.dumps(payload))
        self.assertNotIn("old-generator", json.dumps(payload))
        self.assertEqual(MessageContextEvidence.from_payload(payload, current=current, baseline=baseline(previous)), evidence)
        removal = MessageContextEvidence(DELTA, self.empty(generator=new_generator), baseline(previous))
        self.assertEqual(removal.current_policy.generator, new_generator)
        self.assertEqual(removal.current_policy.population, self.f.population)

    def test_fabricated_removal_and_malformed_context_claims_rejected(self):
        previous, current = self.two_projects()
        reference = baseline(previous)
        evidence = MessageContextEvidence(DELTA, current, reference)
        original = evidence.to_payload()
        for mutate in (
            lambda p: p.pop("baseline"),
            lambda p: p["baseline"].update(batch_id="B-X"),
            lambda p: p["baseline"].update(snapshot_fingerprint="0" * 64),
            lambda p: p["messages"].clear(),
            lambda p: p["messages"][0].update(change_kind="removed", after=None),
            lambda p: p["messages"][0].update(project_id="P-X"),
            lambda p: p["messages"][0]["before"].update(project_id="P-X"),
            lambda p: p["messages"][0]["before"]["participants"][0].update(scope="CURRENT"),
            lambda p: p["messages"][0]["before"]["participants"][0]["participant"].update(email=fixtures.mailbox("invented")),
            lambda p: p["messages"][0].update(included=0),
            lambda p: p["messages"][0].update(included=False),
            lambda p: p.update(excluded_message_keys=["project:P-1"]),
            lambda p: p.update(current_generator={}),
        ):
            payload = json.loads(json.dumps(original))
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                MessageContextEvidence.from_payload(payload, current=current, baseline=reference)
        self.assertEqual(MessageContextEvidence.from_payload(original, current=current, baseline=reference).to_payload(), original)

    def test_historical_and_selection_evidence_do_not_change_current_p5(self):
        current = self.empty()
        original = project_projection_fingerprint(current)
        previous = self.f.v4
        changed = self.build((replace(self.f.assignment, hours=7.0),), version=4)
        first = MessageContextEvidence(DELTA, current, baseline(previous))
        second = MessageContextEvidence(DELTA, current, baseline(changed), ("project:P-1",))
        self.assertNotEqual(first.to_payload(), second.to_payload())
        self.assertEqual(project_projection_fingerprint(first.current), original)
        self.assertEqual(project_projection_fingerprint(second.current), original)
        self.assertEqual(second.included_messages, ())
        self.assertEqual(MessageContextEvidence(FULL, current, None).messages, ())

    def test_canonical_serialization_and_duplicate_keys_rejected(self):
        current = self.empty()
        reference = baseline(self.f.v4)
        evidence = MessageContextEvidence(DELTA, current, reference)
        encoded = evidence.serialize()
        restored = MessageContextEvidence.deserialize(encoded, current=current, baseline=reference)
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.serialize(), encoded)
        duplicate = encoded.replace('"included":true', '"included":true,"included":true')
        self.assertNotEqual(duplicate, encoded)
        with self.assertRaises(ValueError):
            MessageContextEvidence.deserialize(duplicate, current=current, baseline=reference)

    def test_all_historical_capabilities_retain_only_captured_participant_facts(self):
        current = self.empty()
        for version in (None, 1, 2, 3, 4):
            previous = historical_fixture(version)
            reference = baseline(previous)
            evidence = MessageContextEvidence(DELTA, current, reference)
            with self.subTest(version=version):
                row, = evidence.messages
                self.assertEqual(row.change_kind, "removed")
                self.assertIsNone(row.before.source_inputs)
                self.assertEqual(row.before.typed_observations, ())
                self.assertEqual(row.before.participants[0].participant, previous.projects[0].project_manager)
                self.assertEqual(MessageContextEvidence.deserialize(evidence.serialize(), current=current,
                                                                    baseline=reference), evidence)
