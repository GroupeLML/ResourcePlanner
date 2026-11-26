"""R1: independent persisted nomination coverage and inactive v5 binding."""
from dataclasses import replace
import json
import unittest

from app.application.operational_contacts import OperationalContactService
from app.application.project_managers import ProjectManagerResolutionService
from app.application.security import AuthPrincipal
from app.domain.communication_actual_message_recipients import ActualMessageComposition
from app.domain.communication_artifact_integrity import PreparedArtifactIntegrity
from app.domain.communication_message_context import MessageContextEvidence
from app.domain.communication_recipient_composition import RecipientCandidate
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence,
    RecipientPolicyContext, RPNominationSource,
)
from app.domain.project_communication import build_project_communication_projection
from app.domain.project_communication_messages import (
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_population import SuperintendentPopulation
from app.infrastructure.sql import AppUser, BusinessContact, ProjectCoManager, Resource
from app.infrastructure.sql.communication_erp_manager_capture import capture_erp_manager_authorities
from app.infrastructure.sql.communication_generator_capture import capture_generator_context
from app.infrastructure.sql.communication_recipient_identity import read_recipient_identity_evidence
from app.infrastructure.sql.communication_rp_co_manager_capture import capture_rp_co_manager_authorities
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.infrastructure.sql.project_manager_resolution_repository import SqlProjectManagerResolutionRepository
from tests import test_communication_artifact_integrity as artifacts
from tests import test_project_communication_v5 as v5
from tests import test_sql_communication_rp_co_manager_capture as persisted


mailbox = v5.mailbox


class SqlNominationConformanceTests(unittest.TestCase):
    def setUp(self):
        self.db = persisted.SqlRPCoManagerCaptureTests()
        self.db.setUp()
        self.addCleanup(self.db.doCleanups)
        self.f = v5.ProjectCommunicationV5Tests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.population = RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP-869"))

    def capture(self):
        # Only a regression fixture combines the independent persisted adapters.
        # No integrated production boundary or operational generator is called.
        with self.db.factory() as session:
            managers, manager_evidence = capture_erp_manager_authorities(session, ("P1",))
            (nomination,), nomination_evidence = capture_rp_co_manager_authorities(session, ("P1",))
            effective = ProjectManagerResolutionService(
                SqlProjectManagerResolutionRepository(session),
            ).resolve_project("P1")
            repository = SqlProjectCommunicationRepository(
                session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
            )
            contacts = {key: session.get(BusinessContact, key) for key in
                        ("C-PM", "C-CO", "C-MANUAL", "C-LEGACY")}
            users = {key: session.get(AppUser, key) for key in ("U-PM", "U-CO")}
            users_by_contact = {user.business_contact_id: user for user in users.values()
                                if user.business_contact_id is not None}
            resource = session.get(Resource, "R-PM")
            resources = {resource.external_id: resource}

            def participant(manager, primary=False):
                return repository._project_manager(
                    manager=manager, contacts=contacts, users_by_contact=users_by_contact,
                    resources_by_employee=resources, prefer_user_display_name=primary,
                )

            assigned = RecipientAuthorityInput(RecipientCandidate(
                RecipientIdentity("RESOURCE", resource.id), resource.name, "ASSIGNED", "CC",
                resource.email, "RESOURCE_EMAIL", bool(resource.active and resource.erp_active),
            ))
            inputs = MessageRecipientInputs(
                "P1", (*managers, *nomination.candidates, assigned), nomination.nomination_source,
            )
            roots = {assigned.primary.identity}
            roots.update(row.primary.identity for row in nomination.candidates)
            evidence = read_recipient_identity_evidence(
                session, roots, erp_principals=tuple(row.principal for row in managers),
            )
            # Corroborate partial facts for this fixture without assuming their union is complete.
            for partial in (manager_evidence, nomination_evidence):
                for name in ("resources", "contacts", "users"):
                    self.assertTrue(set(getattr(partial, name)) <= set(getattr(evidence, name)))
            assignment = replace(
                self.f.assignment, project_id="P1", project_manager=participant(effective.primary, True),
                project_co_managers=tuple(participant(row) for row in effective.co_managers),
                requester=None, resource_id=resource.id, resource_name=resource.name,
                resource_contact=repository._resource_contact(resource=resource),
            )
        business = build_project_communication_projection(
            week_start=v5.WEEK, week_end=v5.END, assignments=(assignment,), request_provenance_version=4,
        )
        policy = RecipientPolicyContext.capture(self.population, evidence, (inputs,))
        projection = replace(business, request_provenance_version=5, recipient_policy_context=policy)
        return projection

    def inputs(self, projection):
        return projection.recipient_policy_context.message_inputs[0]

    def bind(self, projection, inputs, evidence=None):
        policy = projection.recipient_policy_context
        return replace(projection, recipient_policy_context=RecipientPolicyContext.capture(
            policy.population, policy.identities if evidence is None else evidence, (inputs,),
        ))

    def test_original_effective_only_rule_rejects_hidden_and_accepts_omission(self):
        self.db.nominate(("P1", "C-PM"))
        canonical = self.capture()
        inputs = self.inputs(canonical)
        self.assertEqual(canonical.projects[0].project_co_managers, ())
        # Unavailable/provisional validation deliberately retains the original
        # equality rule. The original code lacked the independent source record.
        with self.assertRaisesRegex(ValueError, "Provenance des rôles destinataires"):
            self.bind(canonical, replace(inputs, nomination_source=None))
        omitted = tuple(row for row in inputs.candidates
                        if not (type(row) is RecipientAuthorityInput and row.primary.role == "CO_MANAGER"))
        provisional = self.bind(canonical, replace(inputs, candidates=omitted, nomination_source=None))
        self.assertIsNone(self.inputs(provisional).nomination_source)
        with self.assertRaisesRegex(ValueError, "candidate coverage"):
            replace(inputs, candidates=omitted)

    def test_persisted_hidden_nomination_changes_p5_but_not_business_delta(self):
        before = self.capture()
        self.assertEqual(self.inputs(before).nomination_source.contact_ids, ())
        self.db.nominate(("P1", "C-PM"))
        after = self.capture()
        self.assertEqual(before.projects, after.projects)
        self.assertEqual(before.diagnostics, after.diagnostics)
        self.assertEqual(compare_project_communication_projections(before, after), ())
        self.assertNotEqual(project_projection_fingerprint(before), project_projection_fingerprint(after))
        self.assertEqual(self.inputs(after).nomination_source.contact_ids, ("C-PM",))
        roles = self.inputs(after).to_payload()["role_evidence"]
        self.assertEqual([row["relationship"] for row in roles],
                         ["ERP_PROJECT_PRINCIPAL", "RP_PROJECT_NOMINATION", "PROJECT_ASSIGNMENT"])
        recipient, = after.recipient_policy_context.messages[0].recipients
        self.assertEqual(recipient.address, mailbox("manager"))
        self.assertEqual({row.role for row in recipient.contributions}, {"MANAGER", "CO_MANAGER", "ASSIGNED"})
        with self.db.factory.begin() as session:
            session.delete(session.get(ProjectCoManager, ("P1", "C-PM")))
        removed = self.capture()
        self.assertEqual(compare_project_communication_projections(after, removed), ())
        self.assertEqual(project_projection_fingerprint(removed), project_projection_fingerprint(before))

    def test_visible_nomination_preserves_genuine_business_delta(self):
        before = self.capture()
        self.db.nominate(("P1", "C-CO"))
        after = self.capture()
        self.assertEqual(self.inputs(after).nomination_source.contact_ids, ("C-CO",))
        self.assertEqual([row.contact_id for row in after.projects[0].project_co_managers], ["C-CO"])
        change, = compare_project_communication_projections(before, after)
        self.assertEqual((change.project_id, change.kind), ("P1", "modified"))
        with self.assertRaisesRegex(ValueError, "Effective RP nomination coverage"):
            replace(after, projects=(replace(after.projects[0], project_co_managers=()),))
        with self.assertRaisesRegex(ValueError, "candidate coverage"):
            replace(self.inputs(after), candidates=tuple(row for row in self.inputs(after).candidates
                    if not (type(row) is RecipientAuthorityInput and row.primary.role == "CO_MANAGER")))

    def test_extra_equivalent_or_unrelated_candidate_without_nomination_rejected(self):
        before = self.capture()
        self.db.nominate(("P1", "C-PM"), ("P1", "C-CO"))
        after = self.capture()
        inputs = self.inputs(before)
        for row in self.inputs(after).candidates:
            if type(row) is RecipientAuthorityInput and row.primary.role == "CO_MANAGER":
                with self.subTest(contact=row.primary.identity), self.assertRaisesRegex(ValueError, "candidate coverage"):
                    replace(inputs, candidates=(*inputs.candidates, row))
        with self.assertRaisesRegex(ValueError, "Duplicate message recipient role"):
            replace(self.inputs(after), candidates=(*self.inputs(after).candidates,
                                                   self.inputs(after).candidates[1]))
        with self.assertRaisesRegex(ValueError, "cross-project"):
            replace(self.inputs(after), nomination_source=RPNominationSource.capture("P2", ("C-CO", "C-PM")))

    def test_source_record_is_independent_and_strict(self):
        before = self.capture()
        empty = self.inputs(before)
        provisional = replace(empty, nomination_source=None)
        self.assertEqual(empty.to_payload()["nomination_source"]["contact_ids"], [])
        self.assertEqual(provisional.to_payload()["nomination_source"], {
            "project_id": "P1", "source_kind": "UNAVAILABLE_PROVISIONAL", "contact_ids": None,
        })
        self.assertNotEqual(project_projection_fingerprint(before),
                            project_projection_fingerprint(self.bind(before, provisional)))
        self.assertEqual(MessageRecipientInputs.from_payload(provisional.to_payload()), provisional)
        self.assertEqual(MessageRecipientInputs.from_payload(empty.to_payload()), empty)
        for ids in (("C-PM", "C-PM"), ("C-PM", "C-CO"), (" C-PM ",), (None,)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                RPNominationSource("P1", "CANONICAL_PERSISTED", ids)
        with self.assertRaises(ValueError):
            RPNominationSource("P1", "UNAVAILABLE_PROVISIONAL", ())
        with self.assertRaisesRegex(ValueError, "provisional manager"):
            replace(self.f.inputs, nomination_source=RPNominationSource.capture("P-1", ("C-CO",)))

    def test_canonical_suppression_and_contradictory_direct_bindings(self):
        self.db.nominate(("P1", "C-PM"), ("P1", "C-CO"))
        effective = self.db.effective().primary
        same = ProjectManagerResolutionService._same_stable_identity
        self.assertTrue(same(effective, replace(effective, business_contact_id="C-CO")))
        self.assertTrue(same(effective, replace(effective, app_user_id=None)))
        self.assertFalse(same(effective, replace(effective, app_user_id="U-CO", business_contact_id="C-CO")))
        projection = self.capture()
        visible, = projection.projects[0].project_co_managers
        self.assertEqual((visible.contact_id, visible.user_id), ("C-CO", "U-CO"))
        with self.assertRaisesRegex(ValueError, "Contradictory effective RP nomination"):
            replace(projection, projects=(replace(projection.projects[0],
                    project_co_managers=(replace(visible, user_id="U-PM"),)),))
        original = projection.recipient_policy_context.identities
        contradictory = RecipientIdentityEvidence.capture(original.resources, original.contacts,
            tuple(replace(row, business_contact_id="C-CO") if row.user_id == "U-PM" else row
                  for row in original.users))
        with self.assertRaises(ValueError):
            self.bind(projection, self.inputs(projection), contradictory)

    def test_ambiguity_and_inactive_hidden_roles_retain_nomination(self):
        self.db.nominate(("P1", "C-PM"))
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-COMPETING", name="Demo competing", external_id="E-COMPETING",
                                 active=False, erp_active=False))
        self.db.change(BusinessContact, "C-PM", external_system="RESOURCEPLANNER",
                       external_entity="EMPLOYEE", external_id="E-COMPETING")
        ambiguous = self.capture()
        self.assertIn(RecipientIdentity("CONTACT", "C-PM"), ambiguous.recipient_policy_context.current_index().ambiguous)
        self.assertEqual(self.inputs(ambiguous).nomination_source.contact_ids, ("C-PM",))
        self.assertEqual(ambiguous.projects[0].project_co_managers, ())
        self.assertIn(mailbox("manager"), [row.address for row in ambiguous.recipient_policy_context.messages[0].recipients])
        self.db.change(AppUser, "U-PM", active=False)
        inactive = self.capture()
        inputs = self.inputs(inactive)
        self.assertEqual(inputs.nomination_source.contact_ids, ("C-PM",))
        self.assertFalse(inputs.candidates[1].primary.active)
        self.assertNotIn(mailbox("manager"), [row.address for row in inactive.recipient_policy_context.messages[0].recipients])
        forged = replace(inputs.candidates[1], primary=replace(inputs.candidates[1].primary, active=True))
        with self.assertRaisesRegex(ValueError, "Contradictory canonical manager/nomination activity"):
            self.bind(inactive, replace(inputs, candidates=(inputs.candidates[0], forged, inputs.candidates[2])))

    def test_contactless_erp_manager_does_not_suppress_manual_nomination(self):
        self.db.nominate(("P1", "C-PM"))
        self.db.change(AppUser, "U-PM", business_contact_id=None)
        projection = self.capture()
        inputs = self.inputs(projection)
        self.assertEqual(inputs.candidates[0].contact_status, "ABSENT")
        self.assertEqual(inputs.nomination_source.contact_ids, ("C-PM",))
        visible, = projection.projects[0].project_co_managers
        self.assertEqual((visible.contact_id, visible.user_id), ("C-PM", None))
        with self.assertRaisesRegex(ValueError, "Effective RP nomination coverage"):
            replace(projection, projects=(replace(projection.projects[0], project_co_managers=()),))

    def test_strict_frozen_round_trip_rejects_missing_and_contradictory_source(self):
        self.db.nominate(("P1", "C-PM"))
        projection = self.capture()
        encoded = serialize_project_projection(projection)
        self.assertEqual(deserialize_project_projection(encoded), projection)
        self.assertEqual(serialize_project_projection(deserialize_project_projection(encoded)), encoded)
        def record(payload):
            return payload["recipient_policy_context"]["message_inputs"][0]
        for mutate in (
            lambda row: row.pop("nomination_source"),
            lambda row: row["nomination_source"].update(contact_ids=[]),
            lambda row: row["nomination_source"].update(contact_ids=["C-PM", "C-PM"]),
            lambda row: row["nomination_source"].update(project_id="P2"),
            lambda row: row["nomination_source"].update(source_kind="UNAVAILABLE_PROVISIONAL"),
            lambda row: row["role_evidence"][1].update(project_id="P2"),
            lambda row: row["role_evidence"][1].update(relationship="ERP_PROJECT_PRINCIPAL"),
        ):
            data = json.loads(encoded)
            mutate(record(data))
            with self.subTest(data=record(data)), self.assertRaises(ValueError):
                deserialize_project_projection(json.dumps(data))

    def test_frozen_history_and_artifact_integrity_bind_both_roles(self):
        before = self.capture()
        self.db.nominate(("P1", "C-PM"))
        captured = self.capture()
        full = MessageContextEvidence(artifacts.FULL, captured, None)
        original = artifacts.issue(full)
        replacement_context = MessageContextEvidence(artifacts.FULL, before, None)
        replacement = artifacts.issue(replacement_context)
        self.assertNotEqual(original.fingerprint, replacement.fingerprint)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=replacement_context,
                                                  expected_fingerprint=original.fingerprint)
        frozen = serialize_project_projection(captured)
        with self.db.factory.begin() as session:
            session.delete(session.get(ProjectCoManager, ("P1", "C-PM")))
        restored = deserialize_project_projection(frozen)
        self.assertEqual(restored, captured)
        self.assertEqual(artifacts.restore(original, MessageContextEvidence(artifacts.FULL, restored, None)).fingerprint,
                         original.fingerprint)
        empty_policy = RecipientPolicyContext.capture(self.population, RecipientIdentityEvidence((), (), ()), ())
        empty = build_project_communication_projection(
            week_start=v5.WEEK, week_end=v5.END, assignments=(), request_provenance_version=5,
            recipient_policy_context=empty_policy,
        )
        history = MessageContextEvidence(artifacts.DELTA, empty, artifacts.baseline(restored))
        source = history.messages[0].before
        self.assertEqual(source.source_inputs.nomination_source.contact_ids, ("C-PM",))
        actual, = ActualMessageComposition(history).messages
        self.assertEqual(actual.to_addresses, (mailbox("manager"),))
        self.assertEqual({row.role for row in actual.observations if row.scope == "HISTORICAL"},
                         {"MANAGER", "CO_MANAGER", "ASSIGNED"})

    def test_generator_only_does_not_change_nomination_p5(self):
        self.db.nominate(("P1", "C-PM"))
        original = self.capture()
        principal = AuthPrincipal.from_roles(local_user_id=None, issuer="urn:resourceplanner:local",
                    subject="local-user", display_name="Demo generator", email=mailbox("generator"),
                    roles=(), auth_mode="local")
        with self.db.factory() as session:
            extended = capture_generator_context(session, original.recipient_policy_context,
                                                 add_generator_cc=True, principal=principal)
        changed = replace(original, recipient_policy_context=extended)
        self.assertEqual(project_projection_fingerprint(changed), project_projection_fingerprint(original))
        self.assertEqual(self.inputs(changed).nomination_source, self.inputs(original).nomination_source)
        self.assertNotEqual(artifacts.issue(MessageContextEvidence(artifacts.FULL, changed, None)).fingerprint,
                            artifacts.issue(MessageContextEvidence(artifacts.FULL, original, None)).fingerprint)
