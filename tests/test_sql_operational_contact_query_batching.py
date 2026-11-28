"""P01-B: actual materialized-contact bulk paths and communication preservation."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.dialects.mssql import pyodbc

from app.application.errors import ApplicationOperationError
from app.application.operational_contacts import OperationalContactService
from app.domain.operational_contacts import (
    DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
    DIAGNOSTIC_APPROVED_TASK_PROJECT_MISMATCH,
    DIAGNOSTIC_RESOURCE_INACTIVE, DIAGNOSTIC_TASK_INACTIVE,
    SOURCE_REQUIREMENT_OVERRIDE, SOURCE_RESOURCE_COORDINATOR, SOURCE_TASK_COORDINATOR,
    SOURCE_SHIFT_OVERRIDE, SOURCE_TASK_RESPONSIBLE,
    STATUS_INACTIVE, STATUS_INVALID_REFERENCE, STATUS_RESOLVED, STATUS_UNRESOLVED,
)
from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    deserialize_project_projection, project_projection_fingerprint,
    serialize_project_projection,
)
from app.infrastructure.sql import (
    BusinessContact, Project, Resource, ResourceRequirement, Shift,
    TaskCatalogEntry, WorkforceRequest, create_session_factory, create_sql_engine,
)
from app.infrastructure.sql import operational_contact_repository as repository
from app.infrastructure.sql.communication_canonical_acquisition import acquire_canonical_current_projection
from tests import test_sql_project_communication_projection as persisted
from tests.test_sql_communication_query_batching import add_project_diagnostics

WEEK = persisted.WEEK
END = WEEK + timedelta(days=6)


def original_read(session, model, keys):
    """Independent parent single-SELECT oracle, including original SQL matching."""
    wanted = tuple(keys)
    if not wanted:
        return ()
    return tuple(session.scalars(select(model).where(model.id.in_(wanted))).all())


class SqlOperationalContactQueryBatchingTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = persisted.SqlProjectCommunicationProjectionTests()
        url = self.fixture._database(directory.name)
        self.engine = create_sql_engine(url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)

    @contextmanager
    def trace(self, session, *, nocase=False):
        connection = session.connection()
        root = connection.get_transaction()
        executed, compiled = [], []
        def before(conn, clause, multiparams, params, options):
            self.assertIs(conn, connection)
            self.assertIs(conn.get_transaction(), root)
            statement = clause.compile(dialect=pyodbc.dialect(paramstyle="qmark"),
                                       compile_kwargs={"render_postcompile": True})
            compiled.append(len(statement.positiontup))
        def cursor(conn, cur, statement, params, ctx, many):
            self.assertIs(conn, connection)
            self.assertIs(conn.get_transaction(), root)
            self.assertTrue(ctx.compiled.statement.is_select)
            executed.append((statement, len(params)))
            if nocase:
                statement = statement.replace("business_contacts.id IN",
                                              "business_contacts.id COLLATE NOCASE IN")
            return statement, params
        event.listen(connection, "before_execute", before)
        event.listen(connection, "before_cursor_execute", cursor, retval=True)
        try:
            yield executed
            self.assertEqual(compiled, [count for _, count in executed])
            self.assertIs(session.connection(), connection)
            self.assertIs(connection.get_transaction(), root)
        finally:
            event.remove(connection, "before_execute", before)
            event.remove(connection, "before_cursor_execute", cursor)

    def seed_contacts(self, count=1802):
        keys = [f"B{i:04}" for i in range(count)]
        with self.factory.begin() as session:
            session.add_all(BusinessContact(id=key, display_name=key) for key in keys+["b0000"])
        return keys

    def test_p01b_contact_boundaries_physical_union_and_identity_map(self):
        keys = self.seed_contacts()
        with self.factory() as session:
            for size in (0, 1, 899, 900, 901, 1802):
                with self.subTest(size=size):
                    wanted = tuple(keys[:size])
                    if wanted:
                        wanted += (wanted[0],)
                    with self.trace(session, nocase=True) as original:
                        expected = original_read(session, BusinessContact, wanted)
                    with self.trace(session, nocase=True) as actual:
                        found = repository._read_materialized_rows(session, BusinessContact, wanted)
                        self.assertEqual({row.id for row in found}, {row.id for row in expected})
                        self.assertEqual(len(found), size + bool(size))
                        # Release the oracle references, then prove the returned
                        # tuple keeps every partition alive in the identity map.
                        del expected
                        for row in found:
                            self.assertIs(session.get(BusinessContact, row.id), row)
                    self.assertEqual([count for _, count in actual],
                                     [min(900,size-i) for i in range(0,size,900)])
                    self.assertEqual(len(original), bool(size))

    def test_p01b_contact_cache_matches_parent_across_partitions_and_repeated_calls(self):
        keys = self.seed_contacts()
        sequence = (
            ("B0000", "MISSING", None, " "),
            tuple(keys[1:]+["b0000","B0000","MISSING","MISSING2","missing2","B0001","b0001"]),
            tuple(keys[1:]+["b0000","B0000","MISSING","MISSING2","missing2","B0001","b0001"]),
            (),
            ("B0000","MISSING","b0000"),
        )
        def run(use_original):
            with self.factory() as session:
                store = repository.SqlOperationalContactRepository(session)
                results, counts = [], []
                reader = original_read if use_original else repository._read_materialized_rows
                with patch.object(repository, "_read_materialized_rows", side_effect=reader):
                    for wanted in sequence:
                        with self.trace(session, nocase=True) as queries:
                            found = store._contacts(wanted)
                        results.append((tuple(found), set(store._contact_cache),
                                        set(store._missing_contact_ids)))
                        counts.append([count for _,count in queries])
                return results, counts
        expected, old_counts = run(True)
        actual, new_counts = run(False)
        self.assertEqual(actual, expected)
        self.assertEqual(old_counts, [[2],[1804],[],[],[]])
        self.assertEqual(new_counts, [[2],[900,900,4],[],[],[]])
        self.assertEqual(actual[0][1], {"B0000","b0000"})
        self.assertEqual(actual[1][2], {"MISSING","MISSING2","missing2","b0001"})
        self.assertEqual(len(actual[1][1]), 1803)
        # No collation alias fabricated for b0001.
        self.assertNotIn("b0001", actual[1][0])

    def test_p01b_later_partition_failure_does_not_publish_partial_cache(self):
        keys = self.seed_contacts(901)
        with self.factory() as session:
            store = repository.SqlOperationalContactRepository(session)
            store._contacts(("C-PM","NOT-FOUND"))
            prior = dict(store._contact_cache), set(store._missing_contact_ids)
            connection = session.connection()
            root = connection.get_transaction()
            origin = RuntimeError("synthetic second contact partition failure")
            counts = []
            def fail(conn, cursor, stmt, params, ctx, many):
                counts.append(len(params))
                self.assertIs(conn, connection)
                self.assertIs(conn.get_transaction(), root)
                if len(counts) == 2:
                    raise origin
            event.listen(connection, "before_cursor_execute", fail)
            try:
                with self.assertRaises(RuntimeError) as caught:
                    store._contacts(tuple(keys))
                self.assertIs(caught.exception, origin)
            finally:
                event.remove(connection, "before_cursor_execute", fail)
            self.assertEqual(counts, [900,1])
            self.assertEqual((store._contact_cache,store._missing_contact_ids),prior)
            self.assertIs(connection.get_transaction(),root)
            self.assertTrue(root.is_active)

    def seed_materialized(self, count=901, *, special=True):
        keys = [f"B{i:04}" for i in range(count)]
        with self.factory.begin() as session:
            session.add_all(BusinessContact(id="C-"+k, display_name=k,
                                           email=k+chr(64)+persisted.TEST_DOMAIN) for k in keys)
            session.add_all([
                BusinessContact(id="B-SHIFT-OVERRIDE",display_name="Demo Shift override"),
                BusinessContact(id="B-INACTIVE",display_name="Demo inactive",active=False),
            ])
            session.flush()
            session.add_all(Project(id=k,number="N-"+k,name=k) for k in keys)
            session.add_all(Resource(id=k,name=k,email=k+chr(64)+persisted.TEST_DOMAIN,
                                     coordinator_contact_id="C-"+k) for k in keys)
            session.add_all(TaskCatalogEntry(id=k,project_number="N-"+k,task_code="210",label=k,
                                              coordinator_contact_id="C-"+k,
                                              operational_responsible_contact_id="C-CURRENT")
                            for k in keys)
            session.flush()
            session.add_all(WorkforceRequest(id=k,project_id=k,legacy_demand_number="D-"+k,
                                             aggregate_version=4,
                                             operational_responsible_override_contact_id="C-CURRENT")
                            for k in keys)
            session.flush()
            session.add_all(ResourceRequirement(
                id=k,project_id=k,workforce_request_id=k,approved_task_catalog_item_id=k,
                approved_request_version=3,approved_contact_context_status="CAPTURED",
                captured_operational_responsible_contact_id="C-"+k,
                captured_operational_responsible_source_type=SOURCE_TASK_RESPONSIBLE,
                captured_operational_responsible_source_entity_id=k,
                captured_operational_responsible_status=STATUS_RESOLVED,
                captured_operational_responsible_diagnostics='["DEMO_Z","DEMO_A","DEMO_Z"]',
                operational_responsibility_context_provenance="APPROVAL_CAPTURE",
                operational_responsibility_context_version=1,
                assigned_resource_id=k,start_date=WEEK,end_date=WEEK,planned_hours=Decimal("1"),
                origin="REQUEST",status="Planifié",description="Demo task") for k in keys)
            session.flush()
            session.add_all(Shift(id=k,resource_requirement_id=k,
                                  resource_id=keys[(i+1)%count],work_date=WEEK,
                                  hours=Decimal("1"),allocation_type="Flexible")
                            for i,k in enumerate(keys))
            session.flush()
            if special:
                session.get(Shift,keys[0]).operational_responsible_override_contact_id="B-SHIFT-OVERRIDE"
                session.get(ResourceRequirement,keys[0]).operational_responsible_override_contact_id="C-"+keys[0]
                session.get(ResourceRequirement,keys[1]).operational_responsible_override_contact_id="C-"+keys[1]
                session.get(Shift,keys[2]).operational_responsible_override_contact_id="B-INACTIVE"
                legacy = session.get(ResourceRequirement,keys[3])
                legacy.approved_contact_context_status="LEGACY_UNKNOWN"
                legacy.operational_responsibility_context_provenance="LEGACY_UNKNOWN"
                legacy.captured_operational_responsible_contact_id=None
                legacy.captured_operational_responsible_status=None
                legacy.operational_responsibility_context_version=None
                # Valid FK but wrong project: never disable referential integrity.
                mismatch=session.get(ResourceRequirement,keys[4])
                mismatch.approved_task_catalog_item_id="T-APPROVED"
                invalid=session.get(ResourceRequirement,keys[5])
                invalid.captured_operational_responsible_status=STATUS_INVALID_REFERENCE
                session.get(TaskCatalogEntry,keys[6]).active=False
                session.get(Resource,keys[7]).active=False
                session.get(Resource,keys[10]).coordinator_contact_id=None
        return keys

    def test_p01b_large_materialized_contexts_all_eight_calls_and_parent_equivalence(self):
        keys = self.seed_materialized()
        wanted = tuple(reversed(keys))+(" "+keys[0]+" ",None,"","ABSENT")
        for shift_path in (True,False):
            def run(reader):
                with self.factory() as session:
                    store = repository.SqlOperationalContactRepository(session)
                    service = OperationalContactService(store)
                    method = service.resolve_shifts if shift_path else service.resolve_resource_requirements
                    raw_method = (store.get_shift_contact_contexts if shift_path else
                                  store.get_resource_requirement_contact_contexts)
                    # Separate repository instances for raw context and resolution
                    # observations; fresh Session in each run keeps counts honest.
                    with patch.object(repository,"_read_materialized_rows",side_effect=reader):
                        with self.trace(session) as trace:
                            result = method(wanted)
                        contexts = raw_method(wanted)
                    return result, contexts, trace
            expected, expected_contexts, original = run(original_read)
            actual, actual_contexts, bounded = run(repository._read_materialized_rows)
            self.assertEqual(actual, expected)
            self.assertEqual(actual_contexts,expected_contexts)
            self.assertEqual(tuple(row.subject_id for row in actual),tuple(reversed(keys)))
            by_id = {row.subject_id:row for row in actual}
            contexts = {(row.shift_id if shift_path else row.requirement_id):row for row in actual_contexts}
            self.assertEqual(by_id[keys[0]].operational_responsible.source_type,
                             SOURCE_SHIFT_OVERRIDE if shift_path else SOURCE_REQUIREMENT_OVERRIDE)
            self.assertEqual(by_id[keys[1]].operational_responsible.source_type,SOURCE_REQUIREMENT_OVERRIDE)
            self.assertEqual(by_id[keys[2]].operational_responsible.status,
                             STATUS_INACTIVE if shift_path else STATUS_RESOLVED)
            self.assertEqual(by_id[keys[3]].operational_responsible.status,STATUS_UNRESOLVED)
            self.assertIn(DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,by_id[keys[3]].diagnostics)
            self.assertIn(DIAGNOSTIC_APPROVED_TASK_PROJECT_MISMATCH,by_id[keys[4]].diagnostics)
            self.assertEqual(by_id[keys[5]].operational_responsible.status,STATUS_INVALID_REFERENCE)
            self.assertIn(DIAGNOSTIC_TASK_INACTIVE,by_id[keys[6]].diagnostics)
            self.assertIn(DIAGNOSTIC_RESOURCE_INACTIVE,by_id[keys[6] if shift_path else keys[7]].diagnostics)
            fallback_key=keys[9] if shift_path else keys[10]
            self.assertEqual(by_id[fallback_key].coordinator.source_type,SOURCE_TASK_COORDINATOR)
            self.assertEqual(by_id[fallback_key].coordinator.contact_id,"C-"+fallback_key)
            row = by_id[keys[8]]
            resource_key=keys[9] if shift_path else keys[8]
            self.assertEqual(row.resource_id,resource_key)
            self.assertEqual(row.coordinator.contact_id,"C-"+resource_key)
            self.assertEqual(row.coordinator.source_type,SOURCE_RESOURCE_COORDINATOR)
            self.assertEqual(row.operational_responsible.source_type,SOURCE_TASK_RESPONSIBLE)
            self.assertEqual(row.operational_responsible.contact_id,"C-"+keys[8])
            self.assertEqual(row.task_id,keys[8])
            self.assertEqual(row.approved_request_version,3)
            self.assertEqual(contexts[keys[8]].operational_responsibility_context_provenance,"APPROVAL_CAPTURE")
            self.assertEqual(contexts[keys[8]].operational_responsibility_context_version,1)
            self.assertEqual(contexts[keys[8]].captured_inherited.diagnostics,("DEMO_Z","DEMO_A"))
            # Seven bulk families for Shift, six for requirement; the only
            # extra queries are inherited unprimed unique Shift overrides.
            bulk = 7 if shift_path else 6
            extra = 2 if shift_path else 0
            self.assertEqual(len(original),bulk+extra)
            self.assertEqual(len(bounded),bulk*2+extra)
            self.assertTrue(all(0<n<=900 for _,n in bounded))
            self.assertEqual(sum(n==900 for _,n in bounded),bulk)
            # Count every actual query, not just the changed helper calls:
            # any identity-map ref loss would add scalar Session.get SELECTs.
            for table in ("projects","workforce_requests","task_catalog_items","resources",
                          "resource_requirements","business_contacts"):
                counts=[n for sql,n in bounded if "FROM "+table+" " in sql.replace("\n"," ")]
                self.assertEqual(counts[:1],[900],table)
                self.assertEqual(len(counts),2+(extra if table=="business_contacts" else 0),table)

    def test_p01b_small_scope_query_counts_and_inherited_shift_override_growth(self):
        keys=self.seed_materialized(8,special=False)
        with self.factory.begin() as session:
            for k in keys:
                session.add(BusinessContact(id="O-"+k,display_name=k))
            session.flush()
            for k in keys:
                session.get(Shift,k).operational_responsible_override_contact_id="O-"+k
        for scope in ((),tuple(keys[:1]),tuple(keys)):
            outcomes=[]
            for reader in (original_read,repository._read_materialized_rows):
                with self.factory() as session:
                    service=OperationalContactService(repository.SqlOperationalContactRepository(session))
                    with patch.object(repository,"_read_materialized_rows",side_effect=reader):
                        with self.trace(session) as trace:
                            result=service.resolve_shifts(scope)
                    outcomes.append((result,len(trace)))
            self.assertEqual(outcomes[0],outcomes[1])
            self.assertEqual(outcomes[1][1],7+len(scope) if scope else 0)

    def test_p01b_large_v4_and_owned_v5_business_fingerprint_equivalence(self):
        keys=self.seed_materialized(special=False)
        with self.factory.begin() as session:
            add_project_diagnostics(session,"P1","P01-B")
        def v4():
            with self.factory() as session:
                return self.fixture._service(session).project_projection(week_start=WEEK)
        def v5():
            return acquire_canonical_current_projection(self.factory,week_start=WEEK,
                                                         superintendent_competency_id=None)
        with patch.object(repository,"_read_materialized_rows",side_effect=original_read):
            expected4,expected5=v4(),v5()
        actual_reader=repository._read_materialized_rows
        observations=[]
        def measured(session,model,wanted):
            with self.trace(session) as trace:
                result=actual_reader(session,model,wanted)
            self.assertTrue(all(0<n<=900 for _,n in trace))
            observations.append((session,model,len(wanted),tuple(n for _,n in trace),
                                 session.connection(),session.connection().get_transaction()))
            return result
        checkouts=[]
        def checkout(driver,*_):
            checkouts.append(driver)
        event.listen(self.engine.pool,"checkout",checkout)
        try:
            with patch.object(repository,"_read_materialized_rows",side_effect=measured):
                actual4=v4()
                v4_observations=tuple(observations)
                observations.clear()
                checkouts.clear()
                actual5=v5()
            self.assertEqual(len(checkouts),1)
            self.assertTrue(observations)
            self.assertTrue(all(row[0] is observations[0][0] for row in observations))
            self.assertTrue(all(row[4] is observations[0][4] and row[5] is observations[0][5]
                                for row in observations))
            self.assertTrue(observations[0][4].closed)
            self.assertFalse(observations[0][0].in_transaction())
            self.assertEqual(self.engine.pool.checkedout(),0)
        finally:
            event.remove(self.engine.pool,"checkout",checkout)
        for rows in (v4_observations,observations):
            self.assertTrue(all(row[0] is rows[0][0] and row[4] is rows[0][4]
                                and row[5] is rows[0][5] for row in rows))
            for model in (Shift,ResourceRequirement,Project,WorkforceRequest,TaskCatalogEntry,Resource,BusinessContact):
                self.assertTrue(any(row[1] is model and row[2]>900 and len(row[3])>=2 for row in rows),model)
        self.assertEqual(actual4,expected4)
        self.assertEqual(actual5,expected5)
        self.assertEqual(actual5.recipient_policy_context,expected5.recipient_policy_context)
        self.assertEqual(replace(actual5,request_provenance_version=4,recipient_policy_context=None),actual4)
        self.assertTrue(set(keys)<={project.project_id for project in actual4.projects})
        def unexpected_sql(*args):
            self.fail("Immutable projections must not execute SQL after acquisition")
        event.listen(self.engine,"before_cursor_execute",unexpected_sql)
        try:
            for actual,expected in ((actual4,expected4),(actual5,expected5)):
                self.assertEqual(project_projection_fingerprint(actual),project_projection_fingerprint(expected))
                payload=serialize_project_projection(actual)
                self.assertEqual(deserialize_project_projection(payload),actual)
        finally:
            event.remove(self.engine,"before_cursor_execute",unexpected_sql)
        first=next(project for project in actual4.projects if project.project_id=="P1")
        self.assertEqual(tuple(code for code in first.diagnostics if code.startswith("ASSET_QUALIFICATION_")),
                         ("ASSET_QUALIFICATION_MISSING_OPERATOR","ASSET_QUALIFICATION_SKILL_MISMATCH"))
        with self.assertRaisesRegex(ValueError,"v5/P5"):
            build_project_confirmation_batch(actual5)
        with self.assertRaisesRegex(ValueError,"v5/P5"):
            build_project_delta_batch(actual4,actual5)

    def test_p01b_provisional_compatibility_and_owned_scope_failure_no_retry(self):
        with self.factory() as session:
            service=self.fixture._service(session)
            before=service.capture_v5_projection(week_start=WEEK)
        with patch.object(repository,"_read_materialized_rows",side_effect=original_read):
            with self.factory() as session:
                prior=self.fixture._service(session).capture_v5_projection(week_start=WEEK)
        self.assertEqual(before,prior)
        self.assertEqual(before.recipient_policy_context.presentation_mode,"UNAVAILABLE_PRE_R2B")
        origin=RuntimeError("synthetic materialized bulk failure")
        reader=repository._read_materialized_rows
        failures=[]
        def fail(session,model,keys):
            if model is Shift:
                failures.append(session)
                raise origin
            return reader(session,model,keys)
        with patch.object(repository,"_read_materialized_rows",side_effect=fail):
            # The application port may translate a failure; it must never
            # return a successful partial projection or retry acquisition.
            with self.assertRaises(ApplicationOperationError) as caught:
                acquire_canonical_current_projection(self.factory,week_start=WEEK,
                                                       superintendent_competency_id=None)
            self.assertEqual(caught.exception.code,"operational_contact_read_failed")
            self.assertIs(caught.exception.__cause__,origin)
        self.assertEqual(len(failures),1)
        self.assertFalse(failures[0].in_transaction())
        self.assertEqual(self.engine.pool.checkedout(),0)
