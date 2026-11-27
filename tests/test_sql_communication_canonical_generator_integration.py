"""Persisted I.2b integration; no operational v5 activation."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.application.errors import ApplicationUnavailableError, ApplicationValidationError
from app.application.project_communications import ProjectCommunicationService
from app.application.security import AuthPrincipal
from app.domain.communication_recipient_identity import RecipientIdentity
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.infrastructure.sql import AppUser, BusinessContact, Resource, Shift, SqlAuthSessionRepository
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql import communication_canonical_acquisition as acquisition
from app.infrastructure.sql import communication_generator_capture as generator
from tests import test_sql_communication_canonical_acquisition as accepted

WEEK = accepted.WEEK
COMPETENCY = accepted.COMPETENCY
mailbox = accepted.mailbox


class SqlCanonicalGeneratorIntegrationTests(TestCase):
    def setUp(self):
        # Reuse the accepted persisted fixture, without inheriting its tests.
        accepted.SqlCanonicalCommunicationAcquisitionTests.setUp(self)
        with self.factory.begin() as session:
            session.add(BusinessContact(id="C-G", display_name="Demo generator",
                                        email=mailbox("contact-generator")))
            session.add(Resource(id="R-G", name="Demo generator", external_id="E-G",
                                 email=mailbox("resource-generator"), active=False))
            session.flush()
            session.add(AppUser(id="U-G", issuer="urn:test", subject="generator",
                display_name="Demo generator", email=mailbox("persisted-generator"),
                employee_external_id="E-G", business_contact_id="C-G", roles_json="[]"))

    def change(self, model, key, **values):
        accepted.SqlCanonicalCommunicationAcquisitionTests.change(self,model,key,**values)

    def acquire(self, **options):
        return acquisition.acquire_canonical_current_projection(
            self.factory, week_start=WEEK, superintendent_competency_id=COMPETENCY, **options)

    def enabled(self, principal):
        return self.acquire(add_generator_cc=True,generator_principal=principal)

    @staticmethod
    def principal(key="U-G", **changes):
        data=dict(local_user_id=key,issuer="urn:test",subject="generator" if key else "unlinked",
                  display_name="Demo generator",email=mailbox("authenticated"),roles=(),
                  auth_mode="oidc")
        data.update(changes)
        return AuthPrincipal.from_roles(**data)

    def session_principal(self, mode):
        with self.factory.begin() as session:
            repository=SqlAuthSessionRepository(session)
            value="synthetic-i2b-session-"+mode
            repository.create_session(raw_token=value,user_id="U-G",
                expires_at=utc_now()+timedelta(hours=1),auth_mode=mode)
            result=repository.resolve_principal(value)
        self.assertIsNotNone(result)
        return result

    @staticmethod
    def message(policy, project_id="P1"):
        return next(row for row in policy.messages if row.project_id==project_id)

    def assert_current_equal(self, before, after):
        a,b=before.recipient_policy_context,after.recipient_policy_context
        self.assertEqual(a.current_state_payload(),b.current_state_payload())
        self.assertEqual(a.current_index().components,b.current_index().components)
        self.assertEqual(a.current_resolution,b.current_resolution)
        self.assertEqual(before.projects,after.projects)
        self.assertEqual(project_projection_fingerprint(before),project_projection_fingerprint(after))
        self.assertEqual(compare_project_communication_projections(before,after),())

    def test_i2b_current_only_default_disabled_and_existing_paths(self):
        statements=[]
        def record(conn,cur,sql,params,ctx,many):
            statements.append((sql,tuple(params)))
        event.listen(self.engine,"before_cursor_execute",record)
        try:
            with patch.object(acquisition,"capture_generator_context",
                              side_effect=AssertionError("disabled adapter invoked")) as adapter:
                default=self.acquire()
                original=tuple(statements)
                statements.clear()
                explicit=self.acquire(add_generator_cc=False,generator_principal=object())
                adapter.assert_not_called()
            self.assertEqual(tuple(statements),original)
        finally:
            event.remove(self.engine,"before_cursor_execute",record)
        self.assertEqual(default,explicit)
        self.assertIsNone(default.recipient_policy_context.generator)
        self.assertIsNone(default.recipient_policy_context.composition_identities)
        with patch.object(acquisition,"acquire_canonical_current_projection",
                          side_effect=AssertionError("inactive integration invoked")) as inactive:
            with self.factory() as session:
                service=accepted.SqlCanonicalCommunicationAcquisitionTests.service(self,session)
                v4=service.project_projection(week_start=WEEK)
                provisional=service.capture_v5_projection(week_start=WEEK)
                with self.assertRaises(ApplicationUnavailableError) as caught:
                    service.capture_v5_projection(week_start=WEEK,add_generator_cc=True,
                                                   generator_principal=self.principal())
                self.assertEqual(caught.exception.code,
                                 "project_communication_v5_generator_authority_unavailable")
            inactive.assert_not_called()
        self.assertEqual(replace(default,request_provenance_version=4,recipient_policy_context=None),v4)
        self.assertEqual(provisional.recipient_policy_context.presentation_mode,"UNAVAILABLE_PRE_R2B")
        self.assertIsNone(provisional.recipient_policy_context.generator)

    def test_i2b_trusted_unlinked_oidc_and_break_glass_authority(self):
        off=self.acquire()
        unlinked=self.principal(None,issuer="urn:resourceplanner:local",subject="local-user",auth_mode="local")
        result=self.enabled(unlinked)
        policy=result.recipient_policy_context
        self.assert_current_equal(off,result)
        self.assertEqual((policy.generator.principal.issuer,policy.generator.principal.subject),
                         (unlinked.issuer,unlinked.subject))
        self.assertIsNone(policy.generator.principal.local_user_id)
        self.assertIsNone(policy.composition_identities)
        self.assertIn(mailbox("authenticated"),[r.address for r in self.message(policy).recipients])
        resolved=self.session_principal("oidc")
        first=self.enabled(resolved)
        self.assertEqual(first.recipient_policy_context.generator.principal.local_user_id,"U-G")
        self.assertEqual(first.recipient_policy_context.generator.address.raw_address,resolved.email)
        trusted=replace(resolved,email=mailbox("authenticated"))
        result=self.enabled(trusted)
        policy=result.recipient_policy_context
        self.assert_current_equal(off,result)
        self.assertEqual(policy.generator.address.raw_address,trusted.email)
        self.assertEqual(policy.generator.identity_fingerprint,
                         ProjectCommunicationService._generator_context(trusted)[1])
        addresses=[r.address for r in self.message(policy).recipients]
        self.assertIn(trusted.email,addresses)
        for name in ("persisted-generator","resource-generator","contact-generator"):
            self.assertNotIn(mailbox(name),addresses)
        self.change(AppUser,"U-G",issuer=None,subject=None)
        emergency=self.session_principal("break_glass")
        self.assertEqual((emergency.issuer,emergency.subject),
                         ("urn:resourceplanner:break-glass","app-user:U-G"))
        result=self.enabled(emergency)
        self.assert_current_equal(off,result)
        self.assertEqual(result.recipient_policy_context.generator.principal.local_user_id,"U-G")
        with self.assertRaisesRegex(ValueError,"Contradictory authenticated local association"):
            self.enabled(replace(emergency,auth_mode="oidc"))

    def test_i2b_invalid_options_addresses_and_associations_fail_closed(self):
        for value in (None,0,1,"false",(),object()):
            with self.subTest(option=type(value).__name__), self.assertRaisesRegex(
                    ValueError,"Expected explicit generator option"):
                self.acquire(add_generator_cc=value,generator_principal=self.principal())
        for principal in (None,object(),{"email":mailbox("injected")},
                          self.principal(email=None),self.principal(email="invalid"),
                          self.principal(email=mailbox("a")+","+mailbox("b")),
                          replace(self.principal(),email=mailbox("a")+"\n")):
            with self.subTest(kind=type(principal).__name__), self.assertRaises(ApplicationValidationError) as caught:
                self.enabled(principal)
            self.assertEqual(caught.exception.code,"project_communication_generator_email_invalid")
            self.assertEqual(self.engine.pool.checkedout(),0)
        for principal in (self.principal("U-ABSENT"),self.principal(subject="wrong"),
                          self.principal(issuer="URN:TEST"),replace(self.principal(),subject="generator ")):
            with self.assertRaises(ValueError):
                self.enabled(principal)
            self.assertEqual(self.engine.pool.checkedout(),0)
        self.change(AppUser,"U-G",active=False)
        with self.assertRaisesRegex(ValueError,"Missing or inactive authenticated local association"):
            self.enabled(self.principal())
        self.assertEqual(self.engine.pool.checkedout(),0)

    def test_i2b_current_overlap_to_priority_and_distinct_shared_address(self):
        off=self.acquire()
        principal=self.principal("U-RESP",subject="resp")
        result=self.enabled(principal)
        self.assert_current_equal(off,result)
        policy=result.recipient_policy_context
        self.assertIsNone(policy.composition_identities)
        self.assertTrue(policy.index().equivalent(policy.generator.principal.identity,
                                                  RecipientIdentity("USER","U-RESP")))
        message=self.message(policy)
        envelope=next(row for row in message.recipients if row.address==mailbox("requester"))
        self.assertEqual(envelope.channel,"TO")
        self.assertTrue({"REQUESTER","GENERATOR"}<={row.role for row in envelope.contributions})
        self.assertNotIn(mailbox("authenticated"),[r.address for r in message.recipients])
        distinct=self.enabled(self.principal(None,email=mailbox("requester").upper()))
        self.assert_current_equal(off,distinct)
        policy=distinct.recipient_policy_context
        self.assertFalse(policy.index().equivalent(policy.generator.principal.identity,
                                                   RecipientIdentity("USER","U-RESP")))
        envelope=next(row for row in self.message(policy).recipients if row.address==mailbox("requester"))
        self.assertEqual(envelope.channel,"TO")
        self.assertTrue({"REQUESTER","GENERATOR"}<={row.role for row in envelope.contributions})

    def test_i2b_ambiguous_current_and_disjoint_competitors_remain_separate(self):
        with self.factory.begin() as session:
            session.add_all([
                Resource(id="R-CURRENT-CONFLICT",name="Demo competitor",external_id="E-CURRENT-CONFLICT",
                         active=False,erp_active=False),
                Resource(id="R-CURRENT-LINK",name="Demo current link",external_id="RESP-1",
                         active=False,erp_active=False),
                Resource(id="R-G-CONFLICT",name="Demo generator competitor",external_id="E-G-CONFLICT",
                         active=False,erp_active=False),
            ])
            for key,employee in (("C-RESP","E-CURRENT-CONFLICT"),("C-G","E-G-CONFLICT")):
                row=session.get(BusinessContact,key)
                row.external_system="RESOURCEPLANNER"
                row.external_entity="EMPLOYEE"
                row.external_id=employee
        off=self.acquire()
        overlap=self.enabled(self.principal("U-RESP",subject="resp"))
        self.assert_current_equal(off,overlap)
        policy=overlap.recipient_policy_context
        self.assertIn(RecipientIdentity("USER","U-RESP"),policy.current_index().ambiguous)
        self.assertIn(policy.generator.principal.identity,policy.index().ambiguous)
        self.assertIsNone(policy.composition_identities)
        self.assertTrue({"R-CURRENT-LINK","R-CURRENT-CONFLICT"}<=
                        {row.resource_id for row in policy.identities.resources})
        disjoint=self.enabled(self.principal())
        self.assert_current_equal(off,disjoint)
        policy=disjoint.recipient_policy_context
        extension=policy.composition_identities
        self.assertEqual({r.resource_id for r in extension.resources},{"R-G","R-G-CONFLICT"})
        self.assertEqual({r.contact_id for r in extension.contacts},{"C-G"})
        self.assertEqual({r.user_id for r in extension.users},{"U-G"})
        self.assertIn(policy.generator.principal.identity,policy.index().ambiguous)
        for kind,key in (("RESOURCE","R-G"),("RESOURCE","R-G-CONFLICT"),
                         ("CONTACT","C-G"),("USER","U-G")):
            self.assertNotIn(RecipientIdentity(kind,key),policy.current_index().components)
        self.assertIn(mailbox("authenticated"),[r.address for r in self.message(policy).recipients])

    def test_i2b_contradictory_overlapping_observation_is_not_repaired(self):
        original=generator.read_recipient_identity_evidence
        seen=[]
        def contradictory(session,roots):
            observed=original(session,roots)
            seen.append(observed)
            self.assertTrue(observed.users)
            # Simulate a contradictory second source observation, not a
            # mutated current policy or a persisted invalid FK.
            user=next(row for row in observed.users if row.user_id=="U-RESP")
            return replace(observed,users=tuple(
                replace(row,business_contact_id=None) if row==user else row
                for row in observed.users))
        with patch.object(generator,"read_recipient_identity_evidence",side_effect=contradictory):
            with self.assertRaisesRegex(ValueError,"Authenticated local association changed during capture"):
                self.enabled(self.principal("U-RESP",subject="resp"))
        self.assertEqual(len(seen),1)
        self.assertEqual(self.engine.pool.checkedout(),0)
        # Keep the directly corroborated user unchanged but drop another
        # physical member of the already complete component.
        original=generator.read_recipient_identity_evidence
        def incomplete(session,roots):
            observed=original(session,roots)
            self.assertTrue(observed.contacts)
            return replace(observed,contacts=())
        with patch.object(generator,"read_recipient_identity_evidence",side_effect=incomplete):
            with self.assertRaisesRegex(ValueError,"Generator contradicts complete current identity closure"):
                self.enabled(self.principal("U-RESP",subject="resp"))
        self.assertEqual(self.engine.pool.checkedout(),0)
        # A disjoint local root must not bridge into current authority either.
        def bridge(session,roots):
            observed=original(session,roots)
            current=original(session,(RecipientIdentity("USER","U-RESP"),))
            return type(observed).capture(observed.resources+current.resources,
                                          observed.contacts+current.contacts,
                                          observed.users+current.users)
        with patch.object(generator,"read_recipient_identity_evidence",side_effect=bridge):
            with self.assertRaisesRegex(ValueError,"Identity evidence must be unique and ordered"):
                self.enabled(self.principal())
        self.assertEqual(self.engine.pool.checkedout(),0)

    def test_i2b_p5_independence_strict_round_trip_and_real_current_change(self):
        off=self.acquire()
        variants=[self.enabled(p) for p in (
            self.principal(),self.principal(email=mailbox("other")),
            self.principal(None,subject="other"))]
        digests={off.recipient_policy_context.provisional_composition_fingerprint}
        identities=set()
        for result in variants:
            self.assert_current_equal(off,result)
            policy=result.recipient_policy_context
            digests.add(policy.provisional_composition_fingerprint)
            identities.add(policy.generator.identity_fingerprint)
            payload=serialize_project_projection(result)
            restored=deserialize_project_projection(payload)
            self.assertEqual(restored,result)
            self.assertEqual(serialize_project_projection(restored),payload)
            self.assertEqual(project_projection_fingerprint(restored),project_projection_fingerprint(off))
            invalid=policy.to_payload()
            invalid["provisional_composition_fingerprint"]="0"*64
            with self.assertRaises(ValueError):
                type(policy).from_payload(invalid)
        self.assertEqual(len(digests),4)
        self.assertEqual(len(identities),3)
        self.change(Resource,"R-G",external_id="E-G-NEW")
        changed_composition=self.enabled(self.principal())
        self.assert_current_equal(off,changed_composition)
        self.assertNotEqual(changed_composition.recipient_policy_context.provisional_composition_fingerprint,
                            variants[0].recipient_policy_context.provisional_composition_fingerprint)
        self.change(Resource,"R-GLOBAL",email=mailbox("global-new"))
        current=self.enabled(self.principal())
        self.assertNotEqual(project_projection_fingerprint(current),project_projection_fingerprint(off))
        self.assertEqual(compare_project_communication_projections(off,current),())
        self.change(Shift,"S1",hours=7)
        changed=self.enabled(self.principal())
        self.assertEqual([row.project_id for row in compare_project_communication_projections(current,changed)],["P1"])
        for call in (lambda:build_project_confirmation_batch(changed),
                     lambda:build_project_delta_batch(current,changed)):
            with self.assertRaisesRegex(ValueError,"v5/P5"):
                call()

    def test_i2b_one_owned_session_root_and_no_sql_after_release(self):
        off=self.acquire()
        sessions,owners,trace,stages,generator_sql=[],[],[],[],[]
        make=sessionmaker.__call__
        def factory_call(factory,**options):
            session=make(factory,**options)
            sessions.append(session)
            event.listen(session,"before_flush",lambda *_:self.fail("flush"))
            event.listen(session,"before_commit",lambda *_:self.fail("commit"))
            return session
        def sql(conn,cur,statement,params,ctx,many):
            if statement=="BEGIN":
                return
            self.assertTrue(ctx.compiled is not None and ctx.compiled.statement.is_select)
            owners.append((conn,conn.get_transaction()))
            trace.append("read")
            if stages and "materialize" not in trace:
                generator_sql.append((statement,len(params)))
        def checkout(*_): trace.append("checkout")
        def checkin(*_): trace.append("checkin")
        def rollback(*_): trace.append("rollback")
        original=acquisition.capture_generator_context
        def stage(session,current,**options):
            self.assertIs(session,sessions[0])
            self.assertEqual(current,off.recipient_policy_context)
            self.assertTrue(session.connection().connection.driver_connection.in_transaction)
            owners.append((session.connection(),session.connection().get_transaction()))
            stages.append("generator")
            trace.append("generator")
            return original(session,current,**options)
        materialize=acquisition.replace
        def final(projection,**options):
            self.assertIsNotNone(options["recipient_policy_context"].generator)
            self.assertTrue(owners[0][1].is_active)
            trace.append("materialize")
            return materialize(projection,**options)
        with ExitStack() as stack:
            for target,name,fn in ((self.engine.pool,"checkout",checkout),(self.engine.pool,"checkin",checkin),
                                   (self.engine,"rollback",rollback),(self.engine,"before_cursor_execute",sql)):
                event.listen(target,name,fn)
                stack.callback(event.remove,target,name,fn)
            stack.enter_context(patch.object(sessionmaker,"__call__",factory_call))
            stack.enter_context(patch.object(acquisition,"capture_generator_context",side_effect=stage))
            stack.enter_context(patch.object(acquisition,"replace",side_effect=final))
            result=self.enabled(self.principal())
            frozen=tuple(trace)
            self.assertEqual(deserialize_project_projection(serialize_project_projection(result)),result)
            project_projection_fingerprint(result)
            self.assertEqual(tuple(trace),frozen)
            with self.assertRaises(FrozenInstanceError):
                result.week_start=WEEK+timedelta(days=1)
        self.assertEqual(len(sessions),1)
        self.assertEqual(stages,["generator"])
        self.assertTrue(generator_sql)
        self.assertTrue(all("where" in statement.lower() and 0<n<=900
                            and "app_users.email" not in statement
                            for statement,n in generator_sql))
        self.assertTrue(all(c is owners[0][0] and t is owners[0][1] for c,t in owners))
        self.assertTrue(owners[0][0].closed)
        self.assertFalse(sessions[0].in_transaction())
        self.assertEqual([s for s in trace if s!="read"],
                         ["checkout","generator","materialize","rollback","checkin"])
        self.assertEqual(self.engine.pool.checkedout(),0)

    def test_i2b_writer_between_current_and_generator_stages_wal_snapshot(self):
        principal=self.principal("U-RESP",subject="resp")
        before=self.enabled(principal)
        original=acquisition.capture_generator_context
        commits=[]
        def writer_then_generator(session,current,**options):
            self.assertEqual(current.current_state_payload(),
                             before.recipient_policy_context.current_state_payload())
            driver=session.connection().connection.driver_connection
            with self.factory.begin() as writer:
                self.assertIsNot(writer.connection().connection.driver_connection,driver)
                writer.get(AppUser,"U-RESP").email=mailbox("requester-new")
                writer.get(AppUser,"U-RESP").employee_external_id="E-G-NEW"
                writer.add(Resource(id="R-WRITER",name="Demo new linked Resource",
                                    external_id="E-G-NEW",active=False))
            commits.append(True)
            return original(session,current,**options)
        with patch.object(acquisition,"capture_generator_context",side_effect=writer_then_generator):
            during=self.enabled(principal)
        self.assertEqual(commits,[True])
        self.assertEqual(serialize_project_projection(during),serialize_project_projection(before))
        after=self.enabled(principal)
        self.assertNotEqual(project_projection_fingerprint(after),project_projection_fingerprint(before))
        self.assertIn("R-WRITER",{row.resource_id for row in after.recipient_policy_context.identities.resources})
        self.assertEqual(after.recipient_policy_context.generator.address.raw_address,principal.email)
        self.assertEqual(self.engine.pool.checkedout(),0)

    def test_i2b_generator_and_cleanup_failures_no_result_or_retry(self):
        origin=RuntimeError("synthetic generator stage failure")
        with self.factory() as unrelated:
            unrelated.begin()
            connection=unrelated.connection()
            root=connection.get_transaction()
            with patch.object(acquisition,"capture_generator_context",side_effect=origin) as stage:
                with self.assertRaises(RuntimeError) as caught:
                    self.enabled(self.principal())
                self.assertIs(caught.exception,origin)
                stage.assert_called_once()
            self.assertTrue(root.is_active)
            self.assertIs(connection.get_transaction(),root)
            self.assertFalse(connection.closed)
            self.assertEqual(self.engine.pool.checkedout(),1)
        original=acquisition.replace
        constructed=[]
        cleanup=RuntimeError("synthetic generator inclusive cleanup failure")
        def materialize(projection,**changes):
            result=original(projection,**changes)
            constructed.append(result)
            return result
        def fail(*_):
            raise cleanup
        event.listen(Session,"after_transaction_end",fail)
        try:
            with patch.object(acquisition,"replace",side_effect=materialize):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    self.enabled(self.principal())
        finally:
            event.remove(Session,"after_transaction_end",fail)
        self.assertEqual(len(constructed),1)
        self.assertIsNotNone(constructed[0].recipient_policy_context.generator)
        self.assertIn(cleanup,caught.exception.exceptions)
        self.assertEqual(self.engine.pool.checkedout(),0)
