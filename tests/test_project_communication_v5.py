from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import patch

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
from app.domain.operational_contacts import ContactResolution
from app.domain.project_communication import (
    ProjectCommunicationAsset, ProjectCommunicationAssignment, ProjectCommunicationParticipant,
    build_project_communication_projection,
)
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection, _project_shift_deltas,
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

    def test_operational_v5_remains_unsupported_after_p5_and_pure_comparison(self):
        self.assertNotEqual(project_projection_fingerprint(self.v5), project_projection_fingerprint(self.v4))
        self.assertEqual(compare_project_communication_projections(self.v4, self.v5), ())
        self.assertEqual(compare_project_communication_projections(self.v5, self.v5), ())
        with self.assertRaisesRegex(ValueError, "Régression"):
            compare_project_communication_projections(self.v5, self.v4)
        with patch("app.domain.project_communication_messages._build_draft") as draft:
            for previous, current in ((self.v4, self.v5), (self.v5, self.v4), (self.v5, self.v5)):
                with self.assertRaisesRegex(ValueError, "v5/P5"):
                    build_project_delta_batch(previous, current)
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


    def _generator(self, subject, email, local_user=None):
        principal = AuthenticatedPrincipalIdentityFact("urn:test", subject, local_user)
        return AuthenticatedGeneratorAuthority(principal, "Demo generator",
            RecipientAddressObservation("AUTHENTICATED_EMAIL", principal.identity,
                                        "AUTHENTICATED", email))

    def test_p5_is_independent_of_generator_option_and_authenticated_identity(self):
        current = self.context.current_state_payload()
        original = project_projection_fingerprint(self.v5)
        contexts = [self.context]
        for generator in (
            self._generator("first", mailbox("first")),
            self._generator("second", mailbox("second")),
            self._generator("first", mailbox("different")),
            self._generator("linked", mailbox("authenticated"), "U-RQ"),
        ):
            context = RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,),
                                                     generator=generator)
            contexts.append(context)
            projection = self.build(5, context)
            self.assertEqual(project_projection_fingerprint(projection), original)
            self.assertEqual(context.current_state_payload(), current)
            self.assertEqual(context.current_resolution, self.context.current_resolution)
            self.assertEqual(deserialize_project_projection(serialize_project_projection(projection)), projection)
        self.assertEqual(len({c.provisional_composition_fingerprint for c in contexts}), len(contexts))

    def test_p5_is_independent_of_disjoint_generator_identity_closure(self):
        extension = RecipientIdentityEvidence.capture(
            (ResourceIdentityFact("R-G", "E-G"),),
            (ContactIdentityFact("C-G", "RESOURCEPLANNER", "EMPLOYEE", "E-G"),),
            (UserIdentityFact("U-G", "E-G", "C-G"),),
        )
        generator = self._generator("linked", mailbox("authenticated"), "U-G")
        context = RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,),
                                                 generator=generator, composition_identities=extension)
        self.assertEqual(context.current_state_payload(), self.context.current_state_payload())
        self.assertEqual(project_projection_fingerprint(self.build(5, context)), project_projection_fingerprint(self.v5))
        self.assertEqual(context.index().canonical(generator.principal.identity), RecipientIdentity("RESOURCE", "R-G"))
        self.assertNotIn(RecipientIdentity("RESOURCE", "R-G"), context.current_index().components)
        self.assertNotEqual(context.provisional_composition_fingerprint, self.context.provisional_composition_fingerprint)
        self.assertEqual(RecipientPolicyContext.from_payload(context.to_payload()), context)

        for invalid in (
            RecipientIdentityEvidence.capture((), (), (UserIdentityFact("U-G", "E-1", None),)),
            RecipientIdentityEvidence.capture((), (), (UserIdentityFact("U-G", None, "C-PM"),)),
            RecipientIdentityEvidence.capture((), (), (UserIdentityFact("U-G", None, None),
                                                       UserIdentityFact("U-UNRELATED", None, None))),
            RecipientIdentityEvidence.capture((), (), (UserIdentityFact("U-PM", None, "C-PM"),)),
            RecipientIdentityEvidence((), (), ()),
        ):
            with self.subTest(extension=invalid), self.assertRaises(ValueError):
                RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,),
                                               generator=generator, composition_identities=invalid)
        with self.assertRaises(ValueError):
            RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,),
                                           composition_identities=extension)

    def test_generator_fulfillment_does_not_change_current_policy_resolution(self):
        identities = RecipientIdentityEvidence.capture(
            self.identities.resources, self.identities.contacts,
            self.identities.users + (UserIdentityFact("U-R2", "E-2", None),),
        )
        off = RecipientPolicyContext.capture(self.population, identities, (self.inputs,))
        on = RecipientPolicyContext.capture(self.population, identities, (self.inputs,),
            generator=self._generator("superintendent", mailbox("authenticated"), "U-R2"))
        self.assertFalse(off.current_resolution[0].mandatory_complete)
        self.assertEqual(on.current_resolution, off.current_resolution)
        self.assertTrue(on.messages[0].mandatory_complete)
        self.assertTrue(on.messages[0].mandatory[1].fulfilled_by_existing)
        self.assertEqual(project_projection_fingerprint(self.build(5, off)),
                         project_projection_fingerprint(self.build(5, on)))
        self.assertNotEqual(off.provisional_composition_fingerprint, on.provisional_composition_fingerprint)

    def test_p5_separate_evidence_claims_and_composition_digest_fail_closed(self):
        generator = self._generator("first", mailbox("first"))
        context = RecipientPolicyContext.capture(self.population, self.identities, (self.inputs,), generator=generator)
        payload = serialize_project_projection(self.build(5, context))
        for mutate in (
            lambda row: row.pop("current_resolution"),
            lambda row: row["current_resolution"][0].update(mandatory_complete=True),
            lambda row: row.pop("composition_identities"),
            lambda row: row.pop("provisional_composition_fingerprint"),
            lambda row: row.update(provisional_composition_fingerprint="0" * 64),
            lambda row: row["generator"].update(display_name="Changed"),
            lambda row: row["messages"][0]["recipients"].pop(),
        ):
            data = json.loads(payload)
            mutate(data["recipient_policy_context"])
            with self.subTest(data=data), self.assertRaises(ValueError):
                deserialize_project_projection(json.dumps(data))

    def test_p5_canonical_erp_principal_change_without_display_change(self):
        participant = replace(self.assignment.project_manager, contact_id=None, user_id=None,
                              email=None, active=False)
        fingerprints = []
        for employee_id in ("E-MANAGER-1", "E-MANAGER-2"):
            manager = ERPManagerAuthority(
                ERPPrincipalIdentityFact("P-1", employee_id), participant.display_name,
                None, None, None, "ABSENT", None, None, None,
            )
            inputs = replace(self.inputs, candidates=(manager, *self.inputs.candidates[1:]))
            context = RecipientPolicyContext.capture(self.population, self.identities, (inputs,))
            projection = build_project_communication_projection(
                week_start=WEEK, week_end=END,
                assignments=(replace(self.assignment, project_manager=participant),),
                request_provenance_version=5, recipient_policy_context=context,
            )
            fingerprints.append(project_projection_fingerprint(projection))
        self.assertNotEqual(*fingerprints)

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

    def test_v5_policy_only_changes_do_not_select_business_deltas(self):
        source = self.population.to_population()
        for population in (
            replace(source, competency_id="COMP-2"),
            replace(source, employees=source.employees[:1]),
            replace(source, employees=(source.employees[0], replace(source.employees[1], email=mailbox("new")))),
            SuperintendentPopulation(None, diagnostic="SUPERINTENDENT_BINDING_MISSING"),
        ):
            context = RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), self.identities, (self.inputs,))
            changed = self.build(5, context)
            self.assertNotEqual(project_projection_fingerprint(self.v5), project_projection_fingerprint(changed))
            self.assertEqual(compare_project_communication_projections(self.v5, changed), ())

    def test_v5_preserves_historical_refresh_and_week_boundaries(self):
        p0 = deserialize_project_projection(serialize_project_projection(replace(
            self.build(1), request_provenance_version=None, request_provenance_complete=False,
        )))
        for baseline in (p0, self.build(1), self.build(2), self.build(3)):
            changes = compare_project_communication_projections(baseline, self.v5)
            self.assertEqual([(row.project_id, row.kind) for row in changes], [("P-1", "modified")])
            deltas = _project_shift_deltas(changes[0].before, changes[0].after,
                previous_version=baseline.request_provenance_version, current_version=5)
            self.assertEqual([row.kind for row in deltas], ["refresh"])
            self.assertTrue(deltas[0].historical_refresh)
        self.assertEqual(_project_shift_deltas(self.v4.projects[0], self.v5.projects[0],
            previous_version=4, current_version=5), ())
        with self.assertRaises(ValueError):
            compare_project_communication_projections(self.v5, replace(self.v5, week_start=date(2026, 9, 14)))

    def test_v5_genuine_shift_and_header_changes(self):
        for changes, expected in (
            ({"hours": 7.0}, ["modified"]),
            ({"day": date(2026, 9, 22)}, ["moved"]),
            ({"shift_id": "S-2"}, ["removed", "added"]),
            ({"project_name": "Demo changed"}, []),
        ):
            current = build_project_communication_projection(week_start=WEEK, week_end=END,
                assignments=(replace(self.assignment, **changes),), request_provenance_version=5,
                recipient_policy_context=self.context)
            result = compare_project_communication_projections(self.v5, current)
            self.assertEqual([(row.project_id, row.kind) for row in result], [("P-1", "modified")])
            deltas = _project_shift_deltas(result[0].before, result[0].after, previous_version=5, current_version=5)
            self.assertEqual([row.kind for row in deltas], expected)

    def test_v5_assigned_superintendent_address_change_is_business_change(self):
        address = mailbox("changed-assigned")
        source = self.population.to_population()
        population = RecipientPolicyPopulation.capture(replace(source, employees=(
            replace(source.employees[0], email=address), source.employees[1],
        )))
        inputs = replace(self.inputs, candidates=(
            *self.inputs.candidates[:-1],
            replace(self.inputs.candidates[-1], primary=replace(self.inputs.candidates[-1].primary, email=address)),
        ))
        context = RecipientPolicyContext.capture(population, self.identities, (inputs,))
        current = build_project_communication_projection(week_start=WEEK, week_end=END,
            assignments=(replace(self.assignment, resource_contact=replace(self.assignment.resource_contact, email=address)),),
            request_provenance_version=5, recipient_policy_context=context)
        result = compare_project_communication_projections(self.v5, current)
        self.assertEqual([row.kind for row in result], ["modified"])
        deltas = _project_shift_deltas(result[0].before, result[0].after, previous_version=5, current_version=5)
        self.assertEqual([row.kind for row in deltas], ["modified"])
        self.assertIn("resource_contact", deltas[0].changed_fields)

    def test_typed_current_source_context_round_trips_v5(self):
        manager_participant = replace(self.assignment.project_manager, contact_id=None, user_id=None,
                                      email=mailbox("r-1"))
        manager = ERPManagerAuthority(
            ERPPrincipalIdentityFact("P-1", "E-1"), manager_participant.display_name,
            None, None, None, "ABSENT", None, None,
            RecipientAddressObservation("RESOURCE_EMAIL", RecipientIdentity("RESOURCE", "R-1"),
                                        "CURRENT", mailbox("r-1")), True, True,
        )
        principal = AuthenticatedPrincipalIdentityFact("urn:test", "generator")
        generator = AuthenticatedGeneratorAuthority(principal, "Demo generator",
            RecipientAddressObservation("AUTHENTICATED_EMAIL", principal.identity,
                                        "AUTHENTICATED", mailbox("generator")))
        inputs = replace(self.inputs, candidates=(manager, *self.inputs.candidates[1:]))
        context = RecipientPolicyContext.capture(self.population, self.identities, (inputs,), generator=generator)
        projection = build_project_communication_projection(
            week_start=WEEK, week_end=END,
            assignments=(replace(self.assignment, project_manager=manager_participant),),
            request_provenance_version=5, recipient_policy_context=context,
        )
        message = projection.recipient_policy_context.messages[0]
        self.assertEqual(message.recipients[0].selected.identity, manager.principal.identity)
        self.assertEqual(message.recipients[0].address, mailbox("r-1"))
        self.assertTrue(message.mandatory[0].fulfilled_by_existing)
        self.assertTrue(any(recipient.selected.identity == principal.identity
                            for recipient in message.recipients))
        payload = serialize_project_projection(projection)
        restored = deserialize_project_projection(payload)
        self.assertEqual(restored, projection)
        self.assertEqual(serialize_project_projection(restored), payload)
        self.assertEqual(project_projection_fingerprint(restored), project_projection_fingerprint(projection))
        with self.assertRaises(ValueError):
            build_project_communication_projection(
                week_start=WEEK, week_end=END, assignments=(self.assignment,),
                request_provenance_version=5, recipient_policy_context=context,
            )

    def test_v5_replacement_and_project_addition_removal(self):
        inputs = replace(self.inputs, candidates=(
            *self.inputs.candidates[:-1],
            authority("RESOURCE", "R-2", "ASSIGNED", "CC", None),
        ))
        context = RecipientPolicyContext.capture(self.population, self.identities, (inputs,))
        replacement = build_project_communication_projection(week_start=WEEK, week_end=END,
            assignments=(replace(self.assignment, resource_id="R-2", resource_name="Demo R-2",
                resource_contact=replace(participant("R-2", contact=False, user=False), email=None)),),
            request_provenance_version=5, recipient_policy_context=context)
        changes = compare_project_communication_projections(self.v5, replacement)
        self.assertEqual([row.kind for row in changes], ["modified"])
        self.assertEqual([row.kind for row in _project_shift_deltas(changes[0].before, changes[0].after,
            previous_version=5, current_version=5)], ["replaced"])
        empty_context = RecipientPolicyContext.capture(self.population, self.identities, ())
        empty = build_project_communication_projection(week_start=WEEK, week_end=END, assignments=(),
            request_provenance_version=5, recipient_policy_context=empty_context)
        self.assertEqual([row.kind for row in compare_project_communication_projections(empty, self.v5)], ["added"])
        self.assertEqual([row.kind for row in compare_project_communication_projections(self.v5, empty)], ["removed"])
