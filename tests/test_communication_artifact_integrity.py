"""Inactive final artifact integrity using actual domain sources."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.domain.communication_artifact_integrity import (
    ActualCompositionIntegrity, GeneratedMessageContent, MessageTextReview,
    PreparedArtifactIntegrity, _digest,
)
from app.domain.communication_message_context import MessageContextEvidence
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.project_communication_messages import (
    deserialize_project_projection, project_projection_fingerprint,
    render_project_review_html, serialize_project_projection,
)
from app.domain.superintendent_population import SuperintendentPopulation
from tests import test_communication_actual_message_recipients as fixtures
from tests.test_project_communication_historical_golden import historical_fixture


FULL, DELTA = fixtures.FULL, fixtures.DELTA
baseline, mailbox = fixtures.baseline, fixtures.mailbox


def generated_for(context):
    # Synthetic trusted server output. No operational v5 generator is called.
    return tuple(GeneratedMessageContent(
        row.message_key, row.fingerprint, "Generated " + row.message_key,
        "Demo schedule for " + row.message_key,
        render_project_review_html("Demo schedule for " + row.message_key),
    ) for row in ActualCompositionIntegrity(context).envelopes)


def issue(context, reviews=()):
    return PreparedArtifactIntegrity.capture(context=context, generated=generated_for(context), reviews=reviews)


def restore(artifact, context=None):
    return PreparedArtifactIntegrity.deserialize(
        artifact.serialize(), context=artifact.integrity.context if context is None else context,
        expected_fingerprint=artifact.fingerprint,
    )


def reseal(payload):
    # Simulate an independently supplied hash of malformed structural claims.
    # This is not how a trusted expected fingerprint is acquired in production.
    payload["fingerprint"] = _digest("prepared_artifact",
        {key: value for key, value in payload.items() if key != "fingerprint"})
    return payload["fingerprint"]


class ArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ActualMessageCompositionTests()
        self.helper.setUp()
        self.f = self.helper.f
        self.population = self.helper.complete_population()
        self.current = self.helper.with_policy(self.f.v5, population=self.population)
        self.full = MessageContextEvidence(FULL, self.current, None)

    def two_projects(self):
        previous, current = self.helper.helper.two_projects()
        return previous, self.helper.with_policy(current, population=self.population)

    def empty(self):
        return self.helper.with_policy(self.helper.helper.empty(), population=self.population)

    def test_canonical_round_trip_and_strict_independent_trust_root(self):
        artifact = issue(self.full)
        same = issue(self.full)
        self.assertEqual(artifact.serialize(), same.serialize())
        current = deserialize_project_projection(serialize_project_projection(self.current))
        independent = MessageContextEvidence(FULL, current, None)
        restored = restore(artifact, independent)
        self.assertEqual(restored.serialize(), artifact.serialize())
        self.assertEqual(restored.fingerprint, artifact.fingerprint)
        for expected in (None, "", "wrong", "0" * 64):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                PreparedArtifactIntegrity.deserialize(artifact.serialize(), context=independent,
                                                      expected_fingerprint=expected)
        encoded = artifact.serialize()
        duplicate = encoded.replace('"review_explicit":false', '"review_explicit":false,"review_explicit":false')
        self.assertNotEqual(encoded, duplicate)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(duplicate, context=independent,
                                                  expected_fingerprint=artifact.fingerprint)
        with self.assertRaises(TypeError):
            PreparedArtifactIntegrity()

    def test_distinct_fingerprint_responsibilities_and_review_only_change(self):
        artifact = issue(self.full)
        revised = issue(self.full, (MessageTextReview("project:P-1", subject="New subject"),))
        p5 = project_projection_fingerprint(self.current)
        hashes = (p5, artifact.integrity.fingerprint, artifact.integrity.envelopes[0].fingerprint,
                  artifact.contents[0].fingerprint, artifact.fingerprint)
        self.assertEqual(len(set(hashes)), len(hashes))
        self.assertEqual(artifact.integrity, revised.integrity)
        self.assertNotEqual(artifact.contents[0].fingerprint, revised.contents[0].fingerprint)
        self.assertNotEqual(artifact.fingerprint, revised.fingerprint)
        self.assertEqual(project_projection_fingerprint(self.current), p5)

    def test_generator_selection_and_identity_change_composition_not_p5(self):
        original = issue(self.full)
        for subject in ("first", "second"):
            generator = self.f._generator(subject, mailbox("authenticated"), "U-RQ")
            current = self.helper.with_policy(self.current, generator=generator)
            context = MessageContextEvidence(FULL, current, None)
            artifact = issue(context)
            # Generator linked to existing To; transport is unchanged, provenance is not.
            self.assertEqual(artifact.integrity.envelopes[0].to_addresses, original.integrity.envelopes[0].to_addresses)
            self.assertEqual(artifact.integrity.envelopes[0].cc_addresses, original.integrity.envelopes[0].cc_addresses)
            self.assertEqual(project_projection_fingerprint(current), project_projection_fingerprint(self.current))
            self.assertNotEqual(artifact.integrity.fingerprint, original.integrity.fingerprint)
            self.assertNotEqual(artifact.integrity.envelopes[0].fingerprint, original.integrity.envelopes[0].fingerprint)
            with self.assertRaises(ValueError):
                restore(original, context)
            if subject == "first":
                first = artifact.integrity.fingerprint
            else:
                self.assertNotEqual(first, artifact.integrity.fingerprint)

    def test_baseline_row_substitution_even_with_equal_snapshot_fingerprint(self):
        current = self.empty()
        first = MessageContextEvidence(DELTA, current, baseline(self.f.v4, "B-1"))
        second = MessageContextEvidence(DELTA, current, baseline(self.f.v4, "B-2"))
        a, b = issue(first), issue(second)
        self.assertEqual(first.baseline.reference.snapshot_fingerprint, second.baseline.reference.snapshot_fingerprint)
        self.assertEqual(a.integrity.baseline_snapshot_integrity, b.integrity.baseline_snapshot_integrity)
        self.assertNotEqual(a.integrity.fingerprint, b.integrity.fingerprint)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        with self.assertRaises(ValueError):
            restore(a, second)

    def test_changed_baseline_facts_reject_original_artifact(self):
        current = self.empty()
        old = self.f.v4
        changed = self.helper.helper.build((replace(self.f.assignment, hours=7.0),), version=4)
        a = issue(MessageContextEvidence(DELTA, current, baseline(old)))
        b = issue(MessageContextEvidence(DELTA, current, baseline(changed)))
        self.assertEqual(a.integrity.composition.included_message_keys, b.integrity.composition.included_message_keys)
        self.assertNotEqual(a.integrity.baseline_snapshot_integrity, b.integrity.baseline_snapshot_integrity)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        with self.assertRaises(ValueError):
            restore(a, b.integrity.context)

    def test_coordinated_legitimate_inclusion_change_rejected_by_original_trust(self):
        _, current = self.two_projects()
        full = MessageContextEvidence(FULL, current, None)
        subset = MessageContextEvidence(FULL, current, None, ("project:P-1",))
        original, replacement = issue(full), issue(subset)
        self.assertEqual(original.integrity.composition.eligible_message_keys,
                         replacement.integrity.composition.eligible_message_keys)
        self.assertEqual(project_projection_fingerprint(full.current), project_projection_fingerprint(subset.current))
        self.assertNotEqual(original.fingerprint, replacement.fingerprint)
        self.assertEqual(restore(replacement), replacement)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=subset,
                                                  expected_fingerprint=original.fingerprint)

    def test_generated_and_review_coverage_rejects_missing_duplicate_or_substituted_keys(self):
        _, current = self.two_projects()
        context = MessageContextEvidence(FULL, current, None)
        generated = generated_for(context)
        for rows in (generated[:-1], generated + generated[:1],
                     (replace(generated[0], message_key="project:invented"), generated[1]),
                     (replace(generated[0], envelope_fingerprint=generated[1].envelope_fingerprint), generated[1])):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                PreparedArtifactIntegrity.capture(context=context, generated=rows)
        for reviews in ((MessageTextReview("project:invented"),),
                        (MessageTextReview(generated[0].message_key), MessageTextReview(generated[0].message_key))):
            with self.assertRaises(ValueError):
                PreparedArtifactIntegrity.capture(context=context, generated=generated, reviews=reviews)
        # Input order does not dictate canonical output order.
        self.assertEqual(PreparedArtifactIntegrity.capture(context=context, generated=generated[::-1]), issue(context))

    def test_forged_envelope_and_recipient_claims_rejected_even_with_resealed_hash(self):
        artifact = issue(self.full)
        for mutate in (
            lambda row: row["recipients"].append(row["recipients"][0]),
            lambda row: row["recipients"].pop(),
            lambda row: row["recipients"][0].update(address=mailbox("substituted")),
            lambda row: row["recipients"][0].update(channel="CC"),
            lambda row: row["recipients"][0]["identity_deliveries"][0].update(selected_observation=99),
            lambda row: row["recipients"][0]["identity_deliveries"][0]["contributing_observations"].clear(),
            lambda row: row["mandatory"][0]["resolution"].update(fulfilled_by_existing=False),
            lambda row: row["mandatory"][1].update(selected_observation=0),
        ):
            payload = artifact.to_payload()
            mutate(payload["actual_integrity"]["composition"]["messages"][0])
            expected = reseal(payload)
            with self.assertRaises(ValueError):
                PreparedArtifactIntegrity.from_payload(payload, context=self.full, expected_fingerprint=expected)

    def test_forged_actual_message_and_envelope_coverage_rejected(self):
        artifact = issue(self.full)
        for mutate in (
            lambda p: p["actual_integrity"]["envelopes"].clear(),
            lambda p: p["actual_integrity"]["envelopes"].append(p["actual_integrity"]["envelopes"][0]),
            lambda p: p["actual_integrity"]["envelopes"][0].update(message_key="project:invented"),
            lambda p: p["actual_integrity"]["envelopes"][0]["to_addresses"].append(mailbox("extra")),
            lambda p: p["actual_integrity"]["composition"]["messages"].clear(),
            lambda p: p["actual_integrity"]["composition"]["messages"].append(p["actual_integrity"]["composition"]["messages"][0]),
            lambda p: p["actual_integrity"]["composition"]["included_message_keys"].clear(),
            lambda p: p["contents"].clear(),
            lambda p: p["contents"].append(p["contents"][0]),
            lambda p: p["contents"][0]["generated"].update(message_key="project:invented"),
        ):
            payload = artifact.to_payload()
            mutate(payload)
            expected = reseal(payload)
            with self.assertRaises(ValueError):
                PreparedArtifactIntegrity.from_payload(payload, context=self.full, expected_fingerprint=expected)

    def test_independent_subject_text_and_html_tampering(self):
        artifact = issue(self.full, (MessageTextReview("project:P-1", body="Free edited text"),))
        for key in ("subject", "body", "body_html"):
            payload = artifact.to_payload()
            payload["contents"][0][key] += " altered"
            with self.subTest(key=key), self.assertRaises(ValueError):
                PreparedArtifactIntegrity.from_payload(payload, context=self.full, expected_fingerprint=artifact.fingerprint)
            expected = reseal(payload)
            # Even a rehashed top-level claim cannot bypass the content digest.
            with self.assertRaises(ValueError):
                PreparedArtifactIntegrity.from_payload(payload, context=self.full, expected_fingerprint=expected)

    def test_free_review_uses_server_html_and_restoration_never_renders(self):
        generated = generated_for(self.full)
        subject = MessageTextReview("project:P-1", subject="  New subject  ")
        subject_only = PreparedArtifactIntegrity.capture(context=self.full, generated=generated, reviews=(subject,))
        self.assertEqual(subject_only.contents[0].subject, "New subject")
        self.assertEqual(subject_only.contents[0].html_origin, "GENERATED")
        self.assertEqual(subject_only.contents[0].body_html, generated[0].body_html)
        text = "Completely different <script>unsafe</script> & text"
        reviewed = issue(self.full, (MessageTextReview("project:P-1", subject="  New  ", body=" " + text + " "),))
        content = reviewed.contents[0]
        self.assertEqual(content.body, text)
        self.assertEqual(content.html_origin, "SERVER_REVIEW_RENDERED")
        self.assertTrue(content.review_explicit)
        self.assertEqual(content.body_html, render_project_review_html(text))
        self.assertNotIn("<script>", content.body_html)
        self.assertIn("&lt;script&gt;", content.body_html)
        with patch("app.domain.communication_artifact_integrity.render_project_review_html",
                   side_effect=AssertionError("Frozen content must not be rendered")):
            self.assertEqual(restore(reviewed), reviewed)
        with self.assertRaises(TypeError):
            MessageTextReview("project:P-1", body_html="<p>client</p>")
        for review in (MessageTextReview("project:P-1", subject=" "), MessageTextReview("project:P-1", body=" ")):
            with self.assertRaises(ValueError):
                issue(self.full, (review,))

    def test_reviewed_message_swap_between_projects(self):
        _, current = self.two_projects()
        context = MessageContextEvidence(FULL, current, None)
        artifact = issue(context, (MessageTextReview("project:P-1", body="Review one"),
                                   MessageTextReview("project:P-2", body="Review two")))
        payload = artifact.to_payload()
        payload["contents"].reverse()
        expected = reseal(payload)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.from_payload(payload, context=context, expected_fingerprint=expected)
        other = issue(context, (MessageTextReview("project:P-1", body="Review two"),
                                MessageTextReview("project:P-2", body="Review one")))
        self.assertNotEqual(other.fingerprint, artifact.fingerprint)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(other.serialize(), context=context,
                                                  expected_fingerprint=artifact.fingerprint)

    def test_full_delta_subset_and_removed_artifacts(self):
        previous, current = self.two_projects()
        full = MessageContextEvidence(FULL, current, None)
        delta = MessageContextEvidence(DELTA, current, baseline(previous))
        removed = MessageContextEvidence(DELTA, self.empty(), baseline(previous))
        for context, keys in ((full, ("project:P-1", "project:P-2")),
                              (delta, ("project:P-2",)),
                              (removed, ("project:P-1", "project:P-2"))):
            artifact = issue(context)
            self.assertEqual(tuple(row.generated.message_key for row in artifact.contents), keys)
            self.assertEqual(restore(artifact), artifact)
        self.assertEqual(removed.current.projects, ())

    def test_empty_no_change_and_all_excluded_not_preparation_candidates(self):
        for context in (MessageContextEvidence(FULL, self.empty(), None),
                        MessageContextEvidence(DELTA, self.current, baseline(self.f.v4)),
                        MessageContextEvidence(FULL, self.current, None, ("project:P-1",))):
            composition = ActualCompositionIntegrity(context)
            self.assertEqual(composition.envelopes, ())
            self.assertEqual(composition.composition.included_message_keys, ())
            with self.assertRaises(ValueError):
                issue(context)

    def test_missing_mandatory_binding_and_blocking_recipient_diagnostics(self):
        missing = MessageContextEvidence(FULL, self.f.v5, None)
        invalid_population = RecipientPolicyPopulation.capture(SuperintendentPopulation(
            None, (), "SUPERINTENDENT_BINDING_MISSING",
        ))
        invalid = MessageContextEvidence(FULL, self.helper.with_policy(self.current, population=invalid_population), None)
        old = self.helper.helper.build((replace(self.f.assignment,
            project_manager=replace(self.f.assignment.project_manager, email="bad-address")),), version=4)
        blocking = MessageContextEvidence(DELTA, self.empty(), baseline(old))
        for context in (missing, invalid, blocking):
            with self.subTest(context=context), self.assertRaises(ValueError):
                issue(context)
        self.assertTrue(ActualCompositionIntegrity(blocking).composition.messages[0].mandatory_complete)
        self.assertTrue(any(d.severity == "BLOCKING" for d in
                            ActualCompositionIntegrity(blocking).composition.messages[0].existing_diagnostics))

    def test_replacement_payload_and_replacement_digest_not_trusted(self):
        artifact = issue(self.full, (MessageTextReview("project:P-1", body="Original review"),))
        replacement = issue(self.full, (MessageTextReview("project:P-1", body="Different valid review"),))
        self.assertEqual(restore(replacement), replacement)
        self.assertNotEqual(replacement.fingerprint, artifact.fingerprint)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=self.full,
                                                  expected_fingerprint=artifact.fingerprint)

    def test_frozen_p0_p4_and_current_p5_unchanged(self):
        current = self.empty()
        frozen = project_projection_fingerprint(current), serialize_project_projection(current)
        for version in (None, 1, 2, 3, 4):
            previous = historical_fixture(version)
            before = project_projection_fingerprint(previous), serialize_project_projection(previous)
            context = MessageContextEvidence(DELTA, current, baseline(previous))
            artifact = issue(context)
            self.assertEqual(restore(artifact), artifact)
            self.assertEqual((project_projection_fingerprint(previous), serialize_project_projection(previous)), before)
            self.assertEqual((project_projection_fingerprint(current), serialize_project_projection(current)), frozen)
