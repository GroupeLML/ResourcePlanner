"""R2b persisted presentation versus delivery; inactive domain integration only."""
from dataclasses import replace
import hashlib
import json
import unittest

from sqlalchemy import select

from app.application.operational_contacts import OperationalContactService
from app.application.project_managers import ProjectManagerResolutionService
from app.application.security import AuthPrincipal
from app.domain.communication_actual_message_recipients import ActualMessageComposition
from app.domain.communication_artifact_integrity import (
    ActualCompositionIntegrity, MessageTextReview, PreparedArtifactIntegrity,
)
from app.domain.communication_message_context import MessageContextEvidence
from app.domain.communication_recipient_authority import ManagerPresentationLookup
from app.domain.communication_recipient_composition import RecipientCandidate
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientPolicyContext,
)
from app.domain.project_communication import build_project_communication_projection
from app.domain.project_communication_messages import (
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.domain.superintendent_recipient_resolution import AddressFailure
from app.infrastructure.sql import AppUser, BusinessContact, Project, ProjectCoManager, Resource
from app.infrastructure.sql.models import Competency, ResourceCompetency
from app.infrastructure.sql.communication_erp_manager_capture import capture_erp_manager_authorities
from app.infrastructure.sql.communication_generator_capture import capture_generator_context
from app.infrastructure.sql.communication_manager_presentation_capture import capture_manager_presentation
from app.infrastructure.sql.communication_recipient_identity import read_recipient_identity_evidence
from app.infrastructure.sql.communication_rp_co_manager_capture import capture_rp_co_manager_authorities
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.infrastructure.sql.project_manager_resolution_repository import SqlProjectManagerResolutionRepository
from tests import test_communication_artifact_integrity as artifacts
from tests import test_project_communication_historical_golden as golden
from tests import test_project_communication_v5 as v5
from tests import test_sql_communication_manager_presentation as r2a
from tests import test_sql_communication_rp_co_manager_capture as persisted


mailbox = v5.mailbox


class SqlManagerPresentationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = persisted.SqlRPCoManagerCaptureTests()
        self.db.setUp()
        self.addCleanup(self.db.doCleanups)
        self.f = v5.ProjectCommunicationV5Tests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        with self.db.factory.begin() as session:
            session.add_all([
                Competency(id="COMP-869", name="Surintendant"),
                Resource(id="R-WORK", name="Demo assigned", email=mailbox("assigned")),
            ])

    def collation(self, name):
        helper = r2a.SqlManagerPresentationTests()
        helper.db = self.db
        helper.collation(name)
        self.addCleanup(helper.doCleanups)

    def eligible(self, key):
        with self.db.factory.begin() as session:
            session.add(ResourceCompetency(resource_id=key, competency_id="COMP-869"))

    def conflict(self, contact="C-PM"):
        # Distinct raw Resource identifiers; no schema or uniqueness bypass.
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-COMPETING", external_id="E-COMPETING",
                                 name="Demo competing", active=False, erp_active=False))
        self.db.change(BusinessContact, contact, external_system="RESOURCEPLANNER",
                       external_entity="EMPLOYEE", external_id="E-COMPETING")

    def capture(self, projects=("P1",), *, hours=None):
        """Combine independent real adapters only inside this test fixture."""
        hours = hours or {}
        with self.db.factory() as session:
            managers, manager_evidence = capture_erp_manager_authorities(session, projects)
            nominations, nomination_evidence = capture_rp_co_manager_authorities(session, projects)
            lookup = capture_manager_presentation(session, projects)
            repository = SqlProjectCommunicationRepository(
                session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
                superintendent_competency_id="COMP-869",
            )
            population = RecipientPolicyPopulation.capture(repository.read_superintendent_population())
            assigned = session.get(Resource, "R-WORK")
            assigned_input = RecipientAuthorityInput(RecipientCandidate(
                RecipientIdentity("RESOURCE", assigned.id), assigned.name, "ASSIGNED", "CC",
                assigned.email, "RESOURCE_EMAIL", bool(assigned.active and assigned.erp_active),
            ))
            by_manager = {row.principal.project_id: row for row in managers}
            inputs = tuple(MessageRecipientInputs(
                row.project_id, ((by_manager[row.project_id],) if row.project_id in by_manager else ())
                + row.candidates + (assigned_input,), row.nomination_source,
            ) for row in nominations)
            roots = {assigned_input.primary.identity} if projects else set()
            roots.update(RecipientIdentity("RESOURCE", row.employee.resource_id)
                         for row in population.employees)
            roots.update(row.primary.identity for group in nominations for row in group.candidates)
            evidence = read_recipient_identity_evidence(
                session, roots, erp_principals=tuple(row.principal for row in managers),
            )
            for partial in (manager_evidence, nomination_evidence):
                for name in ("resources", "contacts", "users"):
                    self.assertTrue(set(getattr(partial, name)) <= set(getattr(evidence, name)))

            # Real v4 presenter, independently supplied with the same originating
            # shared query semantics as the accepted R2a legacy fixture. Do not
            # derive the business participants from the new frozen lookup.
            effective = ProjectManagerResolutionService(
                SqlProjectManagerResolutionRepository(session),
            ).resolve_projects(list(projects))
            roles = [row for value in effective.values()
                     for row in ((value.primary,) if value.primary is not None else ()) + value.co_managers]
            keys = sorted({(row.employee_external_id or "").strip() for row in roles
                           if (row.employee_external_id or "").strip()})
            all_keys = set(keys)
            occurrences = {}
            for offset in range(0, len(keys), 900):
                for resource in session.scalars(select(Resource).where(
                        Resource.external_id.in_(keys[offset:offset + 900]))).all():
                    key = (resource.external_id or "").strip()
                    if key in all_keys:
                        occurrences.setdefault(key, []).append(resource)
            resources = {key: rows[0] for key, rows in occurrences.items() if len(rows) == 1}
            contact_ids = sorted({row.business_contact_id for row in roles if row.business_contact_id})
            contacts, users = {}, {}
            for offset in range(0, len(contact_ids), 900):
                subset = contact_ids[offset:offset + 900]
                contacts.update((row.id, row) for row in session.scalars(
                    select(BusinessContact).where(BusinessContact.id.in_(subset))))
                users.update((row.business_contact_id, row) for row in session.scalars(
                    select(AppUser).where(AppUser.business_contact_id.in_(subset))))

            def participant(manager, primary=False):
                return repository._project_manager(
                    manager=manager, contacts=contacts, users_by_contact=users,
                    resources_by_employee=resources, prefer_user_display_name=primary,
                )

            # Keep the fixture's own SQL reads bounded too.
            project_rows = {}
            ordered = sorted(projects)
            for offset in range(0, len(ordered), 900):
                project_rows.update((row.id, row) for row in session.scalars(
                    select(Project).where(Project.id.in_(ordered[offset:offset + 900]))))
            assignments = tuple(replace(
                self.f.assignment, shift_id="SHIFT-" + key, requirement_id="REQ-" + key,
                project_id=key, project_number=project_rows[key].number,
                project_name=project_rows[key].name, hours=hours.get(key, 6.0),
                project_manager=participant(effective[key].primary, True),
                project_co_managers=tuple(participant(row) for row in effective[key].co_managers),
                requester=None, resource_id=assigned.id, resource_name=assigned.name,
                resource_contact=repository._resource_contact(resource=assigned),
            ) for key in projects)
            self.assertFalse(session.new or session.dirty or session.deleted)
        business = build_project_communication_projection(
            week_start=v5.WEEK, week_end=v5.END, assignments=assignments,
            request_provenance_version=4,
        )
        policy = RecipientPolicyContext.capture_canonical(
            population, evidence, inputs, manager_presentation=lookup,
        )
        return business, replace(business, request_provenance_version=5, recipient_policy_context=policy)

    def actual(self, projection, *, excluded=(), baseline=None):
        context = MessageContextEvidence(
            artifacts.FULL if baseline is None else artifacts.DELTA, projection,
            None if baseline is None else artifacts.baseline(baseline), excluded,
        )
        return ActualMessageComposition(context)

    def manager(self, projection, key="P1"):
        return next(row.project_manager for row in projection.projects if row.project_id == key)

    def typed(self, actual, role="MANAGER"):
        return tuple(row for row in actual.observations if row.role == role and row.scope == "CURRENT")

    def test_ambiguous_erp_direct_contact_remains_delivery_authority(self):
        self.conflict()
        _, projection = self.capture()
        index = projection.recipient_policy_context.current_index()
        self.assertIn(RecipientIdentity("ERP_EMPLOYEE", "E-PM"), index.ambiguous)
        self.assertFalse(index.equivalent(RecipientIdentity("ERP_EMPLOYEE", "E-PM"),
                                          RecipientIdentity("RESOURCE", "R-PM")))
        message, = self.actual(projection).messages
        self.assertEqual(message.to_addresses, (mailbox("manager"),))
        row, = self.typed(message)
        self.assertEqual(row.address_source, "BUSINESS_CONTACT_EMAIL")
        self.assertEqual(self.manager(projection).email, mailbox("manager"))
        artifacts.issue(self.actual(projection).context)

    def test_ambiguous_erp_missing_contact_legacy_fallback_not_delivery(self):
        self.conflict()
        self.db.change(BusinessContact, "C-PM", email=None)
        _, projection = self.capture()
        self.assertEqual(self.manager(projection).email, mailbox("resource"))
        message, = self.actual(projection).messages
        self.assertEqual(message.to_addresses, ())
        row, = self.typed(message)
        self.assertIsNone(row.raw_address)
        self.assertFalse(row.selectable)
        self.assertNotIn(mailbox("resource"), message.cc_addresses)
        self.assertTrue(any(row.severity == "BLOCKING" for row in message.existing_diagnostics))
        with self.assertRaisesRegex(ValueError, "Blocking recipient evidence"):
            artifacts.issue(self.actual(projection).context)

    def test_proven_erp_resource_fallback_still_selectable(self):
        self.db.change(BusinessContact, "C-PM", email=None)
        _, projection = self.capture()
        message, = self.actual(projection).messages
        self.assertEqual(message.to_addresses, (mailbox("resource"),))
        row, = self.typed(message)
        self.assertEqual(row.address_source, "RESOURCE_EMAIL")
        self.assertEqual(row.canonical_identity, RecipientIdentity("RESOURCE", "R-PM"))
        self.assertEqual(self.manager(projection).email, mailbox("resource"))
        artifacts.issue(self.actual(projection).context)

    def test_effective_rp_ambiguity_and_hidden_nomination(self):
        self.db.nominate(("P1", "C-PM"), ("P1", "C-CO"))
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-CO", external_id="E-CO", name="Demo second",
                                 email=mailbox("co-resource")))
        self.conflict("C-CO")
        _, direct = self.capture()
        self.assertEqual([row.contact_id for row in direct.projects[0].project_co_managers], ["C-CO"])
        message, = self.actual(direct).messages
        self.assertEqual(set(message.to_addresses), {mailbox("manager"), mailbox("second")})
        self.assertEqual({row.identity.id for row in self.typed(message, "CO_MANAGER")}, {"C-PM", "C-CO"})
        self.db.change(BusinessContact, "C-CO", email=None)
        _, missing = self.capture()
        self.assertEqual(missing.projects[0].project_co_managers[0].email, mailbox("co-resource"))
        message, = self.actual(missing).messages
        self.assertEqual(message.to_addresses, (mailbox("manager"),))
        self.assertFalse(next(row for row in self.typed(message, "CO_MANAGER")
                              if row.identity.id == "C-CO").selectable)
        self.assertNotIn(mailbox("co-resource"), message.cc_addresses)
        with self.assertRaisesRegex(ValueError, "Blocking recipient evidence"):
            artifacts.issue(self.actual(missing).context)
        self.db.change(BusinessContact, "C-CO", external_system=None, external_entity=None, external_id=None)
        _, proven = self.capture()
        message, = self.actual(proven).messages
        self.assertIn(mailbox("co-resource"), message.to_addresses)
        artifacts.issue(self.actual(proven).context)

    def test_padded_presentation_only_resource_is_not_canonical_authority(self):
        self.db.change(Resource, "R-PM", external_id="E-PM  ")
        self.db.change(BusinessContact, "C-PM", email=None)
        self.collation("RTRIM")
        _, projection = self.capture()
        policy = projection.recipient_policy_context
        self.assertEqual(policy.manager_presentation.resources[0].employee_external_id, "E-PM  ")
        self.assertNotIn("R-PM", {row.resource_id for row in policy.identities.resources})
        self.assertEqual(self.manager(projection).email, mailbox("resource"))
        message, = self.actual(projection).messages
        self.assertNotIn(mailbox("resource"), message.to_addresses + message.cc_addresses)
        self.eligible("R-PM")
        _, eligible = self.capture()
        message, = self.actual(eligible).messages
        self.assertIn(mailbox("resource"), message.cc_addresses)
        mandatory, = message.mandatory
        self.assertFalse(mandatory.resolution.fulfilled_by_existing)
        self.assertFalse(eligible.recipient_policy_context.current_index().equivalent(
            RecipientIdentity("ERP_EMPLOYEE", "E-PM"), RecipientIdentity("RESOURCE", "R-PM")))

    def test_unchanged_and_excluded_project_does_not_block_other_included_message(self):
        self.db.change(Project, "P2", project_manager_external_id="E-CO")
        self.conflict()
        self.db.change(BusinessContact, "C-PM", email=None)
        _, before = self.capture(("P1", "P2"))
        _, after = self.capture(("P1", "P2"), hours={"P2": 7.0})
        self.assertEqual(after.recipient_policy_context.manager_presentation.project_ids, ("P1", "P2"))
        delta = self.actual(after, baseline=before)
        self.assertEqual(delta.included_message_keys, ("project:P2",))
        artifacts.issue(delta.context)
        full = self.actual(after, excluded=("project:P1",))
        self.assertEqual(full.included_message_keys, ("project:P2",))
        artifacts.issue(full.context)
        with self.assertRaisesRegex(ValueError, "Blocking recipient evidence"):
            artifacts.issue(self.actual(after).context)

    def test_global_mandatory_blocker_remains_global(self):
        self.db.change(Project, "P2", project_manager_external_id="E-CO")
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-BLOCK", name="Demo missing address"))
        self.eligible("R-BLOCK")
        _, projection = self.capture(("P1", "P2"))
        for excluded in ((), ("project:P1",), ("project:P2",)):
            composition = self.actual(projection, excluded=excluded)
            for message in composition.messages:
                self.assertFalse(message.mandatory_complete)
                self.assertEqual([(row.resource_id, row.reason) for row in message.blockers],
                                 [("R-BLOCK", AddressFailure.MISSING)])
            with self.assertRaisesRegex(ValueError, "Blocking recipient evidence"):
                artifacts.issue(composition.context)

    def test_v4_shape_golden_and_business_delta_separation(self):
        business, before = self.capture()
        v4_payload = json.loads(serialize_project_projection(business))
        v5_payload = json.loads(serialize_project_projection(before))
        v5_payload.pop("recipient_policy_context")
        v5_payload["request_provenance_version"] = 4
        self.assertEqual(v5_payload, v4_payload)
        self.assertEqual(compare_project_communication_projections(business, before), ())
        for version, (serialized_hash, fingerprint) in golden.GOLDEN.items():
            historical = golden.historical_fixture(version)
            encoded = serialize_project_projection(historical)
            self.assertEqual(hashlib.sha256(encoded.encode()).hexdigest(), serialized_hash)
            self.assertEqual(project_projection_fingerprint(historical), fingerprint)
        self.conflict()
        _, delivery_only = self.capture()
        self.assertEqual(before.projects, delivery_only.projects)
        self.assertNotEqual(project_projection_fingerprint(before), project_projection_fingerprint(delivery_only))
        self.assertEqual(compare_project_communication_projections(before, delivery_only), ())
        self.db.nominate(("P1", "C-PM"))
        _, hidden = self.capture()
        self.assertEqual(hidden.projects, delivery_only.projects)
        self.assertEqual(compare_project_communication_projections(delivery_only, hidden), ())
        self.assertNotEqual(project_projection_fingerprint(delivery_only), project_projection_fingerprint(hidden))
        self.db.change(BusinessContact, "C-PM", email=mailbox("changed"))
        _, changed = self.capture()
        change, = compare_project_communication_projections(hidden, changed)
        self.assertEqual((change.project_id, change.kind), ("P1", "modified"))

    def test_presentation_only_raw_evidence_changes_p5_and_frozen_integrity(self):
        self.conflict()
        _, before = self.capture()
        self.db.change(Resource, "R-PM", email=mailbox("not-selected"))
        _, after = self.capture()
        self.assertEqual(before.projects, after.projects)
        old, new = before.recipient_policy_context, after.recipient_policy_context
        self.assertEqual(old.current_resolution, new.current_resolution)
        self.assertEqual(old.identities, new.identities)
        self.assertEqual(old.message_inputs, new.message_inputs)
        self.assertNotEqual(old.manager_presentation, new.manager_presentation)
        self.assertNotEqual(project_projection_fingerprint(before), project_projection_fingerprint(after))
        self.assertNotEqual(old.provisional_composition_fingerprint, new.provisional_composition_fingerprint)
        self.assertEqual(compare_project_communication_projections(before, after), ())
        self.assertNotEqual(ActualCompositionIntegrity(self.actual(before).context).fingerprint,
                            ActualCompositionIntegrity(self.actual(after).context).fingerprint)
        original = artifacts.issue(self.actual(before).context)
        replacement = artifacts.issue(self.actual(after).context)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=self.actual(after).context,
                                                  expected_fingerprint=original.fingerprint)

    def test_generator_forwarding_retains_exact_lookup_mode_and_p5(self):
        _, projection = self.capture()
        policy = projection.recipient_policy_context
        principal = AuthPrincipal.from_roles(local_user_id=None, issuer="urn:resourceplanner:local",
                    subject="local-user", display_name="Demo generator", email=mailbox("generator"),
                    roles=(), auth_mode="local")
        with self.db.factory() as session:
            self.assertIs(capture_generator_context(session, policy, add_generator_cc=False), policy)
            extended = capture_generator_context(session, policy, add_generator_cc=True, principal=principal)
        self.assertIs(extended.manager_presentation, policy.manager_presentation)
        self.assertEqual(extended.presentation_mode, "CANONICAL_R2B")
        self.assertEqual(extended.current_state_payload(), policy.current_state_payload())
        changed = replace(projection, recipient_policy_context=extended)
        self.assertEqual(project_projection_fingerprint(changed), project_projection_fingerprint(projection))
        self.assertNotEqual(artifacts.issue(self.actual(changed).context).fingerprint,
                            artifacts.issue(self.actual(projection).context).fingerprint)
        self.assertEqual(deserialize_project_projection(serialize_project_projection(changed)), changed)

    def test_canonical_mode_and_trusted_downgrade_rejection(self):
        _, original = self.capture()
        policy = original.recipient_policy_context
        with self.assertRaises(ValueError):
            RecipientPolicyContext.capture_canonical(policy.population, policy.identities,
                                                     policy.message_inputs, manager_presentation=None)
        compatible = RecipientPolicyContext.capture(policy.population, policy.identities, policy.message_inputs)
        self.assertEqual(compatible.presentation_mode, "UNAVAILABLE_PRE_R2B")
        self.assertIsNone(compatible.manager_presentation)
        self.assertEqual(RecipientPolicyContext.from_payload(compatible.to_payload()), compatible)
        # Preserve both earlier typed-manager/R1 inputs and the explicit
        # PROVISIONAL_CONTACT_CAPTURE path with no independent manager lookup.
        provisional = self.f.context
        self.assertIsNone(provisional.manager_presentation)
        self.assertEqual(provisional.presentation_mode, "UNAVAILABLE_PRE_R2B")
        self.assertEqual(RecipientPolicyContext.from_payload(provisional.to_payload()), provisional)
        self.assertEqual(deserialize_project_projection(serialize_project_projection(self.f.v5)), self.f.v5)
        principal = AuthPrincipal.from_roles(local_user_id=None, issuer="urn:resourceplanner:local",
                    subject="local-user", display_name="Demo generator", email=mailbox("generator"),
                    roles=(), auth_mode="local")
        with self.db.factory() as session:
            extended = capture_generator_context(session, compatible, add_generator_cc=True, principal=principal)
        self.assertEqual(extended.presentation_mode, compatible.presentation_mode)
        self.assertIsNone(extended.manager_presentation)
        self.assertEqual(extended.current_state_payload(), compatible.current_state_payload())
        with self.assertRaises(ValueError):
            replace(policy, manager_presentation=None)
        with self.assertRaises(ValueError):
            replace(policy, presentation_mode="UNAVAILABLE_PRE_R2B")
        for mutate in (
            lambda p: p.pop("manager_presentation"),
            lambda p: p.pop("presentation_mode"),
            lambda p: p.update(presentation_mode="UNKNOWN"),
            lambda p: p.update(manager_presentation=None),
            lambda p: p.update(presentation_mode="UNAVAILABLE_PRE_R2B"),
        ):
            raw = json.loads(serialize_project_projection(original))
            mutate(raw["recipient_policy_context"])
            with self.assertRaises(ValueError):
                deserialize_project_projection(json.dumps(raw))
        # This separately valid compatibility snapshot has fully recomputed
        # internal fingerprints; only the independently trusted original root
        # establishes that it is not the original canonical snapshot.
        downgraded = replace(original, recipient_policy_context=compatible)
        restored = deserialize_project_projection(serialize_project_projection(downgraded))
        self.assertEqual(restored, downgraded)
        self.assertNotEqual(project_projection_fingerprint(restored), project_projection_fingerprint(original))
        trusted = artifacts.baseline(original)
        with self.assertRaises(ValueError):
            replace(trusted, snapshot=restored)
        artifact = artifacts.issue(self.actual(original).context)
        replacement = artifacts.issue(self.actual(restored).context)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=self.actual(restored).context,
                                                  expected_fingerprint=artifact.fingerprint)

    def test_strict_scope_binding_and_cross_source_tampering(self):
        self.db.nominate(("P1", "C-CO"))
        _, projection = self.capture()
        policy = projection.recipient_policy_context
        lookup = policy.manager_presentation
        for mutate in (
            lambda p: p.pop("partition_results"),
            lambda p: p.update(project_ids=["P2"]),
            lambda p: p["roles"].append(p["roles"][0]),
            lambda p: p["roles"].pop(0),
            lambda p: p["roles"][0].update(role_id="C-FABRICATED", contact_id="C-FABRICATED"),
            lambda p: p["roles"][0].update(raw_contact_email=mailbox("forged")),
            lambda p: p["roles"][0].update(linked_user_active=False),
            lambda p: p["resources"][0].update(raw_email=mailbox("forged")),
            lambda p: p["resources"][0].update(erp_active=False),
            lambda p: p.update(resources=[], partition_results=[[] for _ in p["partition_results"]]),
        ):
            payload = lookup.to_payload()
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                replace(policy, manager_presentation=ManagerPresentationLookup.from_payload(payload))
            raw = json.loads(serialize_project_projection(projection))
            raw["recipient_policy_context"]["manager_presentation"] = payload
            with self.assertRaises(ValueError):
                deserialize_project_projection(json.dumps(raw))
        # A structurally valid effective role from another project cannot be
        # grafted onto this originating scope without its own nomination source.
        self.db.nominate(("P2", "C-MANUAL"))
        with self.db.factory() as session:
            other = capture_manager_presentation(session, ("P2",))
        manual = next(row for row in other.roles if row.role_id == "C-MANUAL")
        graft = replace(lookup, roles=tuple(sorted((*lookup.roles, replace(manual, project_id="P1")),
                                                   key=lambda row: row.binding)))
        with self.assertRaisesRegex(ValueError, "Extra or missing independent manager presentation binding"):
            replace(policy, manager_presentation=graft)
        encoded = serialize_project_projection(projection)
        duplicate = encoded.replace('"presentation_mode":"CANONICAL_R2B"',
                                    '"presentation_mode":"CANONICAL_R2B","presentation_mode":"CANONICAL_R2B"')
        self.assertNotEqual(encoded, duplicate)
        with self.assertRaises(ValueError):
            deserialize_project_projection(duplicate)
        project, = projection.projects
        for participant in (
            replace(project.project_manager, email=mailbox("forged")),
            replace(project.project_manager, active=False),
            replace(project.project_manager, display_name="Demo forged"),
        ):
            with self.assertRaises(ValueError):
                replace(projection, projects=(replace(project, project_manager=participant),))
        co, = project.project_co_managers
        with self.assertRaises(ValueError):
            replace(projection, projects=(replace(project, project_co_managers=(
                replace(co, email=mailbox("forged")),)),))

    def test_missing_erp_observation_and_hidden_nomination_name_rejected(self):
        self.db.nominate(("P1", "C-PM"))
        _, projection = self.capture()
        policy = projection.recipient_policy_context
        message, = policy.message_inputs
        manager = next(row for row in message.candidates if hasattr(row, "principal"))
        hidden = next(row for row in message.candidates if type(row) is RecipientAuthorityInput
                      and row.primary.role == "CO_MANAGER")
        self.assertIsNotNone(manager.resource_address)
        self.assertIsNotNone(manager.contact_address.raw_address)
        self.assertEqual(projection.projects[0].project_co_managers, ())
        for original, replacement, error in (
            (manager, replace(manager, resource_address=None, resource_active=None, resource_erp_active=None),
             "Missing or unproven canonical ERP Resource observation"),
            (hidden, replace(hidden, primary=replace(hidden.primary, display_name="Contradictory name")),
             "Contradictory hidden RP nomination display name"),
        ):
            changed = replace(message, candidates=tuple(replacement if row is original else row
                                                         for row in message.candidates))
            with self.assertRaisesRegex(ValueError, error):
                replace(policy, message_inputs=(changed,))

    def test_frozen_history_survives_live_change_and_reviewed_integrity(self):
        self.db.nominate(("P1", "C-PM"))
        _, original = self.capture()
        context = self.actual(original).context
        review = MessageTextReview("project:P1", subject="Reviewed schedule", body="Reviewed demo schedule")
        artifact = artifacts.issue(context, (review,))
        self.assertTrue(artifact.contents[0].review_explicit)
        self.assertNotEqual(artifact.contents[0].fingerprint, artifacts.issue(context).contents[0].fingerprint)
        encoded = serialize_project_projection(original)
        with self.db.factory.begin() as session:
            session.delete(session.get(ProjectCoManager, ("P1", "C-PM")))
            session.get(BusinessContact, "C-PM").email = mailbox("new-manager")
        _, fresh = self.capture()
        restored = deserialize_project_projection(encoded)
        self.assertEqual(restored, original)
        self.assertEqual(artifacts.restore(artifact, self.actual(restored).context).fingerprint, artifact.fingerprint)
        historical = self.actual(fresh, baseline=restored).context.messages[0].before
        self.assertEqual(historical.source_inputs.nomination_source.contact_ids, ("C-PM",))
        self.assertEqual({row.candidate.email for row in historical.typed_observations
                          if row.candidate.role in ("MANAGER", "CO_MANAGER")}, {mailbox("manager")})
        replacement = artifacts.issue(self.actual(fresh).context, (review,))
        self.assertNotEqual(artifact.contents[0].fingerprint, replacement.contents[0].fingerprint)
        with self.assertRaises(ValueError):
            PreparedArtifactIntegrity.deserialize(replacement.serialize(), context=self.actual(fresh).context,
                                                  expected_fingerprint=artifact.fingerprint)
        # Remove originating live facts too; frozen restoration cannot read
        # them or replace original source_inputs with the latest identities.
        with self.db.factory.begin() as session:
            session.get(AppUser, "U-PM").business_contact_id = None
            session.flush()
            session.delete(session.get(BusinessContact, "C-PM"))
            session.delete(session.get(Resource, "R-PM"))
        again = deserialize_project_projection(encoded)
        self.assertEqual(again, original)
        self.assertEqual(artifacts.restore(artifact, self.actual(again).context).fingerprint, artifact.fingerprint)

    def test_empty_scope_and_manual_nomination_do_not_expand_lookup(self):
        self.db.change(Project, "P1", project_manager_external_id=None)
        self.db.nominate(("P1", "C-MANUAL"))
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-MANUAL", external_id="E-MANUAL", name="Demo manual",
                                 email=mailbox("manual-resource")))
        self.db.change(BusinessContact, "C-MANUAL", external_system="RESOURCEPLANNER",
                       external_entity="EMPLOYEE", external_id="E-MANUAL")
        _, projection = self.capture(("P1",))
        policy = projection.recipient_policy_context
        role, = policy.manager_presentation.roles
        self.assertEqual(role.role, "CO_MANAGER")
        self.assertIsNone(role.employee_key)
        self.assertEqual(policy.manager_presentation.resources, ())
        self.assertEqual(policy.manager_presentation.partitions, ())
        self.assertIn("R-MANUAL", {row.resource_id for row in policy.identities.resources})
        self.assertEqual(deserialize_project_projection(serialize_project_projection(projection)), projection)
        _, empty = self.capture(())
        self.assertEqual(empty.recipient_policy_context.manager_presentation,
                         ManagerPresentationLookup((), (), (), ()))
        self.assertEqual(deserialize_project_projection(serialize_project_projection(empty)), empty)

    def test_complete_multi_partition_scope_and_exact_overlap_rejection(self):
        keys = ("A-KEY", *(f"B-{i:04d}" for i in range(898)), "a-key")
        projects = tuple(f"X-{i:04d}" for i in range(len(keys)))
        with self.db.factory.begin() as session:
            session.add_all(Project(id=project, number=f"X{i:04d}", name="Demo project",
                                    project_manager_external_id=key)
                            for i, (project, key) in enumerate(zip(projects, keys)))
            session.add(Resource(id="R-CROSS", external_id="a-key", name="Demo resource",
                                 email=mailbox("cross")))
        self.collation("NOCASE")
        _, projection = self.capture(("P1", *projects))
        policy = projection.recipient_policy_context
        lookup = policy.manager_presentation
        self.assertEqual(tuple(map(len, lookup.partitions)), (900, 1))
        self.assertEqual(lookup.partitions[1], ("a-key",))
        self.assertEqual(lookup.partition_results, (("R-CROSS", "R-PM"), ("R-CROSS",)))
        self.assertEqual(len(lookup.occurrences("a-key")), 2)
        self.assertEqual(sum(row.resource_id == "R-CROSS" for row in lookup.resources), 1)
        self.assertIsNone(self.manager(projection, projects[-1]).email)
        self.assertEqual(self.manager(projection, projects[-1]).active, False)
        selected = next(row for row in policy.current_resolution if row.project_id == projects[-1])
        self.assertIn(mailbox("cross"), [row.address for row in selected.recipients if row.channel == "TO"])
        self.assertEqual(lookup.project_ids, tuple(sorted(("P1", *projects))))
        restored = deserialize_project_projection(serialize_project_projection(projection))
        self.assertEqual(restored.recipient_policy_context.manager_presentation, lookup)
        self.assertEqual(restored, projection)
        full = self.actual(restored, excluded=tuple(sorted("project:" + key for key in projects)))
        self.assertEqual(full.included_message_keys, ("project:P1",))
        self.assertEqual(full.context.current_policy.manager_presentation, lookup)
        artifacts.issue(full.context)
        # The first partition's cross-collation occurrence is legitimate, but
        # it cannot stand in for the exact-key occurrence in partition two.
        tampered = replace(lookup, partition_results=(lookup.partition_results[0], ()))
        with self.assertRaisesRegex(ValueError, "Missing overlapping canonical Resource"):
            replace(policy, manager_presentation=tampered)
