from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import patch

from app.domain.communication_recipient_composition import RecipientCandidate
from app.domain.communication_recipient_identity import (
    ContactIdentityFact, RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
)
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence, RecipientPolicyContext,
)
from app.domain.operational_contacts import ContactResolution
from app.domain.project_communication import (
    ProjectCommunicationAsset, ProjectCommunicationAssignment, ProjectCommunicationParticipant,
    build_project_communication_projection,
)
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure


WEEK = date(2026, 9, 21)
END = date(2026, 9, 27)


def mailbox(local):
    return local + chr(64) + "invalid.test"


def participant(key, *, contact=True, user=True):
    return ProjectCommunicationParticipant(
        "C-" + key if contact else None, "U-" + key if user else None,
        "Demo " + key, mailbox(key.lower()), None, True,
    )


def authority(kind, key, role, channel, email):
    source = {"RESOURCE": "RESOURCE_EMAIL", "CONTACT": "BUSINESS_CONTACT_EMAIL", "USER": "APP_USER_EMAIL"}[kind]
    return RecipientAuthorityInput(RecipientCandidate(
        RecipientIdentity(kind, key), "Demo " + key, role, channel, email, source,
    ))


class ProjectCommunicationV5Tests(unittest.TestCase):
    def setUp(self):
        self.assignment = ProjectCommunicationAssignment(
            shift_id="S-1", requirement_id="REQ-1", project_id="P-1",
            project_number="1000", project_name="Demo project", day=WEEK,
            hours=6.0, allocation_type="Flexible", outside_schedule=False,
            confirmation="Confirm\u00e9e", task_id="TASK-1", task_code="210",
            task_description="Demo task", resource_id="R-1", resource_name="Demo R-1",
            resource_contact=participant("R-1", contact=False, user=False),
            project_manager=participant("PM"), project_co_managers=(participant("CO"),),
            requester=participant("RQ"),
            operational_responsible=ContactResolution("RESOLVED", "C-RESP", "Demo responsible", None, None, "TASK_RESPONSIBLE", "TASK-1"),
            workforce_request_id="D-1", request_number="DMO-1", site_contact_name="Demo site",
            include_project_manager_contact_phone=True,
            project_manager_contact_phone="514" + "-" + "555" + "-" + "0100",
            assets=(ProjectCommunicationAsset("ALLOC-1", "ASSET-1", "UNIT-1", "Demo asset", "Demo type", "Demo category", "OWNED_SHIFT"),),
            standard_hours=8.0,
        )
        self.population = RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP-1", (
            SuperintendentEmployee("R-1", "Demo R-1", mailbox("r-1")),
            SuperintendentEmployee("R-2", "Demo R-2", None),
        )))
        self.identities = RecipientIdentityEvidence.capture((ResourceIdentityFact("R-1", "E-1"), ResourceIdentityFact("R-2", "E-2")),
            (ContactIdentityFact("C-PM"), ContactIdentityFact("C-CO"), ContactIdentityFact("C-RQ")),
            (UserIdentityFact("U-PM", None, "C-PM"), UserIdentityFact("U-CO", None, "C-CO"), UserIdentityFact("U-RQ", None, "C-RQ")))
        self.inputs = MessageRecipientInputs("P-1", (
            authority("CONTACT", "C-PM", "MANAGER", "TO", mailbox("pm")),
            authority("CONTACT", "C-CO", "CO_MANAGER", "TO", mailbox("co")),
            authority("USER", "U-RQ", "REQUESTER", "TO", mailbox("rq")),
            authority("RESOURCE", "R-1", "ASSIGNED", "CC", mailbox("r-1")),
        ))
        self.context = RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,))
        self.v4 = self.build(4)
        self.v5 = self.build(5, self.context)

    def build(self, version, context=None):
        return build_project_communication_projection(week_start=WEEK, week_end=END,
            assignments=(self.assignment,), request_provenance_version=version,
            recipient_policy_context=context)

    def test_cumulative_v5_canonical_round_trip_preserves_all_v4_facts(self):
        self.assertEqual(self.v4.projects, self.v5.projects)
        self.assertEqual(self.v4.diagnostics, self.v5.diagnostics)
        self.assertTrue(self.v5.request_provenance_complete)
        self.assertFalse(self.v5.recipient_policy_context.messages[0].mandatory_complete)
        self.assertEqual(self.v5.recipient_policy_context.messages[0].mandatory[1].blocker_reason, AddressFailure.MISSING)
        serialized = serialize_project_projection(self.v5)
        restored = deserialize_project_projection(serialized)
        self.assertEqual(restored, self.v5)
        self.assertEqual(serialize_project_projection(restored), serialized)
        data = json.loads(serialized)
        self.assertEqual(data.pop("recipient_policy_context"), self.context.to_payload())
        data["request_provenance_version"] = 4
        self.assertEqual(data, json.loads(serialize_project_projection(self.v4)))
        self.assertNotIn("recipient_policy_context", serialize_project_projection(self.v4))

    def test_empty_and_failed_binding_remain_distinct_even_without_projects(self):
        for population, complete in ((SuperintendentPopulation("COMP-1"), True),
                                     (SuperintendentPopulation(None, diagnostic="SUPERINTENDENT_BINDING_MISSING"), False)):
            context = RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), self.identities, (self.inputs,))
            restored = deserialize_project_projection(serialize_project_projection(self.build(5, context)))
            self.assertEqual(restored.recipient_policy_context.population.population_complete, complete)
            self.assertEqual(restored.recipient_policy_context.messages[0].mandatory_complete, complete)
            empty_context = RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), RecipientIdentityEvidence((), (), ()), ())
            empty = build_project_communication_projection(week_start=WEEK, week_end=END, assignments=(), request_provenance_version=5, recipient_policy_context=empty_context)
            self.assertEqual(deserialize_project_projection(serialize_project_projection(empty)), empty)

    def test_context_required_and_downgraded_claims_fail_at_construction(self):
        for version, context in ((5, None), (4, self.context), (1, self.context), (6, None), (True, None)):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.build(version, context)
        for change in ({"recipient_policy_context": None}, {"request_provenance_complete": False},
                       {"request_provenance_version": None}, {"request_provenance_version": 4},
                       {"request_provenance_version": 6}, {"request_provenance_version": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(self.v5, **change)
        with self.assertRaises(ValueError):
            replace(self.v5, projects=self.v5.projects * 2)

    def test_project_and_source_role_coverage_is_required(self):
        for inputs in ((), (replace(self.inputs, project_id="P-2"),),
                       (replace(self.inputs, candidates=self.inputs.candidates[:-1]),),
                       (replace(self.inputs, candidates=()),),
                       (replace(self.inputs, candidates=self.inputs.candidates + (authority("RESOURCE", "R-2", "ASSIGNED", "CC", None),)),)):
            context = RecipientPolicyContext.capture(self.population, self.identities, inputs)
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.build(5, context)

    def test_raw_capability_and_message_claims_fail_closed(self):
        original = serialize_project_projection(self.v5)
        def assignment(data):
            return data["projects"][0]["days"][0]["tasks"][0]["request_assignments"][0]
        mutations = (
            lambda data: data.pop("recipient_policy_context"),
            lambda data: data.update(recipient_policy_context=None),
            lambda data: data.update(request_provenance_version=4),
            lambda data: data.update(request_provenance_version=3),
            lambda data: data.update(request_provenance_version=None),
            lambda data: data.update(request_provenance_version=6),
            lambda data: data.update(request_provenance_version=True),
            lambda data: data.update(request_provenance_complete=False),
            lambda data: data["recipient_policy_context"]["messages"][0]["mandatory"].pop(),
            lambda data: data["recipient_policy_context"]["messages"][0].update(mandatory_complete=True),
            lambda data: assignment(data).pop("confirmation"),
            lambda data: assignment(data).update(outside_schedule=1),
            lambda data: data["projects"][0]["project_manager"].update(active=1),
            lambda data: data["projects"][0].update(unknown_field=True),
        )
        for mutate in mutations:
            data = json.loads(original)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                deserialize_project_projection(json.dumps(data))
        with self.assertRaises(ValueError):
            deserialize_project_projection(original.replace('"request_provenance_version":5', '"request_provenance_version":5,"request_provenance_version":4'))
        older = json.loads(serialize_project_projection(self.v4))
        older["projects"][0]["recipient_policy_context"] = self.context.to_payload()
        with self.assertRaises(ValueError):
            deserialize_project_projection(json.dumps(older))

    def test_operational_v5_unsupported_until_p5_and_delta_integration(self):
        self.assertNotEqual(project_projection_fingerprint(self.v5), project_projection_fingerprint(self.v4))
        for previous, current in ((self.v4, self.v5), (self.v5, self.v4), (self.v5, self.v5)):
            with self.assertRaisesRegex(ValueError, "v5/P5"):
                compare_project_communication_projections(previous, current)
            with self.assertRaisesRegex(ValueError, "v5/P5"):
                build_project_delta_batch(previous, current)
        with patch("app.domain.project_communication_messages._build_draft") as draft:
            with self.assertRaisesRegex(ValueError, "v5/P5"):
                build_project_confirmation_batch(self.v5)
            draft.assert_not_called()

    def test_p5_round_trip_and_canonical_context_order(self):
        fingerprint = project_projection_fingerprint(self.v5)
        restored = deserialize_project_projection(serialize_project_projection(self.v5))
        self.assertEqual(project_projection_fingerprint(restored), fingerprint)
        population = RecipientPolicyPopulation.capture(replace(
            self.population.to_population(), employees=tuple(reversed(self.population.to_population().employees)),
        ))
        identities = RecipientIdentityEvidence.capture(
            reversed(self.identities.resources), reversed(self.identities.contacts), reversed(self.identities.users),
        )
        inputs = replace(self.inputs, candidates=(self.inputs.candidates[-1], *self.inputs.candidates[:-1]))
        context = RecipientPolicyContext.capture(population, identities, (inputs,))
        self.assertEqual(context, self.context)
        self.assertEqual(project_projection_fingerprint(self.build(5, context)), fingerprint)

    def test_p5_policy_changes_are_fingerprint_sensitive(self):
        original = project_projection_fingerprint(self.v5)
        source = self.population.to_population()
        populations = (
            replace(source, competency_id="COMP-2"),
            replace(source, employees=source.employees[:1]),
            replace(source, employees=(source.employees[0], replace(source.employees[1], email=mailbox("new")))),
            SuperintendentPopulation("COMP-1"),
            SuperintendentPopulation(None, diagnostic="SUPERINTENDENT_BINDING_MISSING"),
        )
        fingerprints = {original}
        for population in populations:
            context = RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), self.identities, (self.inputs,))
            fingerprints.add(project_projection_fingerprint(self.build(5, context)))
        self.assertEqual(len(fingerprints), len(populations) + 1)
        changed_identity = replace(self.identities, resources=(
            self.identities.resources[0], replace(self.identities.resources[1], employee_external_id="E-NEW"),
        ))
        changed_inputs = replace(self.inputs, candidates=(
            replace(self.inputs.candidates[0], primary=replace(self.inputs.candidates[0].primary, email=mailbox("other"))),
            *self.inputs.candidates[1:],
        ))
        for identities, inputs in ((changed_identity, self.inputs), (self.identities, changed_inputs)):
            context = RecipientPolicyContext.capture(self.population, identities, (inputs,))
            self.assertNotEqual(project_projection_fingerprint(self.build(5, context)), original)

    def test_p5_preserves_business_fingerprint_sensitivity(self):
        original = project_projection_fingerprint(self.v5)
        for changes in (
            {"shift_id": "S-NEW"}, {"hours": 7.0}, {"confirmation": "Tentative"},
            {"project_name": "Demo changed"}, {"standard_hours": 9.0},
        ):
            projection = build_project_communication_projection(
                week_start=WEEK, week_end=END, assignments=(replace(self.assignment, **changes),),
                request_provenance_version=5, recipient_policy_context=self.context,
            )
            self.assertNotEqual(project_projection_fingerprint(projection), original)
        trace_only = build_project_communication_projection(
            week_start=WEEK, week_end=END, assignments=(replace(self.assignment, requirement_id="REQ-NEW"),),
            request_provenance_version=5, recipient_policy_context=self.context,
        )
        self.assertEqual(project_projection_fingerprint(trace_only), original)

    def test_p5_rejects_invalid_capability_and_context(self):
        # Exercise the fingerprint boundary, not just construction.
        for field, value in (("request_provenance_complete", False), ("recipient_policy_context", None),
                             ("request_provenance_version", 6), ("request_provenance_version", True)):
            malformed = replace(self.v5)
            object.__setattr__(malformed, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                project_projection_fingerprint(malformed)
