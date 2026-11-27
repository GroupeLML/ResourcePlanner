"""P01-A: bounded SQL reads and forward-only qualification diagnostic order."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event, or_, select
from sqlalchemy.dialects.mssql import pyodbc

from app.domain.project_communication_messages import (
    _project_shift_deltas, build_project_confirmation_batch, build_project_delta_batch,
    compare_project_communication_projections, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.infrastructure.sql import (
    AppUser, Asset, AssetAllocation, AssetRequirement, AssetType, BusinessContact,
    Project, Shift, WorkforceRequest, create_session_factory, create_sql_engine,
)
from app.infrastructure.sql.asset_models import AssetTypeCompetency
from app.infrastructure.sql.models import Competency, ResourceCompetency, ResourceRequirement
from app.infrastructure.sql import project_communication_repository as repository
from app.infrastructure.sql.communication_canonical_acquisition import acquire_canonical_current_projection
from tests import test_sql_project_communication_projection as persisted

WEEK = persisted.WEEK
END = WEEK + timedelta(days=6)
MISSING = "ASSET_QUALIFICATION_MISSING_OPERATOR"
MISMATCH = "ASSET_QUALIFICATION_SKILL_MISMATCH"
NO_OVERLAP = "ASSET_QUALIFICATION_NO_OVERLAP"


def reverse_asset_codes(assignment):
    """Model the prior encounter order, without changing source facts or other groups."""
    codes = list(assignment.diagnostics)
    positions = [i for i, code in enumerate(codes) if code.startswith("ASSET_QUALIFICATION_")]
    old = [codes[i] for i in reversed(positions)]
    for i, code in zip(positions, old):
        codes[i] = code
    return replace(assignment, diagnostics=tuple(codes))


def add_project_diagnostics(session, project_id, prefix):
    """Two actual qualification failures restricted to this project's Shifts."""
    session.add(Competency(id=prefix+"-SKILL", name=prefix+" skill"))
    session.add(AssetType(id=prefix+"-TYPE", code=prefix+"-TYPE", label="Demo type", category="EQUIPMENT"))
    session.flush()
    session.add(AssetTypeCompetency(asset_type_id=prefix+"-TYPE", competency_id=prefix+"-SKILL"))
    for suffix, operator in (("MISSING", None), ("MISMATCH", "R1"), ("DUP", None)):
        key = prefix+"-"+suffix
        session.add(Asset(id=key, code=key, label=key, asset_type_id=prefix+"-TYPE"))
        session.add(AssetRequirement(id=key, origin="PROJECT_DIRECT", project_id=project_id,
                                     asset_type_id=prefix+"-TYPE", start_date=WEEK, end_date=END))
        session.flush()
        session.add(AssetAllocation(id=key, asset_requirement_id=key, asset_id=key,
                                    operator_resource_id=operator, start_date=WEEK, end_date=END))
    session.flush()


class SqlCommunicationQueryBatchingTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = persisted.SqlProjectCommunicationProjectionTests()
        self.url = self.fixture._database(directory.name)
        self.fixture._add_second_project(self.url)
        self.engine = create_sql_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)

    def projection(self):
        with self.factory() as session:
            return self.fixture._service(session).project_projection(week_start=WEEK)

    def canonical(self):
        return acquire_canonical_current_projection(self.factory, week_start=WEEK,
                                                    superintendent_competency_id=None)

    def add_allocations(self):
        with self.factory.begin() as session:
            session.add(AssetType(id="T-B", code="T-B", label="Demo", category="EQUIPMENT"))
            session.flush()
            session.add(Asset(id="A-B", code="A-B", label="Demo", asset_type_id="T-B"))
            session.flush()
            for key, origin, fields, operator, start, end, status in (
                ("REQUEST", "REQUEST", dict(project_id="P1", workforce_request_id="D1",
                    source_request_line_id="L1", approved_entry_key="B-REQ"), "R1", WEEK, END, "Affecté"),
                ("SHIFT", "SHIFT_AD_HOC", dict(project_id="P1", shift_id="S1"), "R1", WEEK, WEEK, "Affecté"),
                ("PROJECT", "PROJECT_DIRECT", dict(project_id="P1"), None, WEEK, END, "Affecté"),
                ("RESOURCE", "RESOURCE_PERIOD", dict(context_resource_id="R1",
                    resource_period_mode="CONTINUOUS"), "R1", WEEK, None, "Affecté"),
                ("SEGMENT", "SEGMENT", dict(project_id="P1", resource_requirement_id="REQ1"), "R1", WEEK, END, "Affecté"),
                ("CANCEL", "PROJECT_DIRECT", dict(project_id="P1"), None, WEEK, END, "Annulé"),
                ("BEFORE", "PROJECT_DIRECT", dict(project_id="P1"), None,
                    WEEK-timedelta(days=1), WEEK-timedelta(days=1), "Affecté"),
                ("AFTER", "PROJECT_DIRECT", dict(project_id="P1"), None,
                    END+timedelta(days=1), END+timedelta(days=1), "Affecté"),
                ("AT-END", "PROJECT_DIRECT", dict(project_id="P1"), None, END, END, "Affecté"),
                ("UNRELATED", "PROJECT_DIRECT", dict(project_id="P2"), None, WEEK, END, "Affecté"),
            ):
                session.add(AssetRequirement(id=key, origin=origin, asset_type_id="T-B",
                                             start_date=start, end_date=end, status=status, **fields))
                session.flush()
                session.add(AssetAllocation(id=key, asset_requirement_id=key, asset_id="A-B",
                                            operator_resource_id=operator, start_date=start, end_date=end))

    @staticmethod
    def oracle(session, groups):
        # The unchanged parent OR query is the independent membership oracle.
        scopes = [column.in_(keys) for column, keys in zip((
            AssetRequirement.workforce_request_id, AssetRequirement.shift_id,
            AssetRequirement.project_id, AssetRequirement.context_resource_id,
            AssetRequirement.resource_requirement_id,
        ), groups) if keys]
        if not scopes:
            return ()
        return tuple(session.execute(
            select(AssetAllocation, AssetRequirement, Asset, AssetType)
            .join(AssetRequirement, AssetAllocation.asset_requirement_id == AssetRequirement.id)
            .join(Asset, Asset.id == AssetAllocation.asset_id)
            .join(AssetType, AssetType.id == Asset.asset_type_id)
            .where(or_(*scopes), AssetRequirement.status != "Annulé",
                   AssetAllocation.start_date <= END,
                   or_(AssetAllocation.end_date.is_(None), AssetAllocation.end_date >= WEEK))
        ).all())

    @staticmethod
    def bounded(session, groups):
        return repository._read_scoped_assets(session, request_ids=groups[0], shift_ids=groups[1],
            project_ids=groups[2], resource_ids=groups[3], human_requirement_ids=groups[4],
            week_start=WEEK, week_end=END)

    @staticmethod
    def allocation_ids(rows):
        return {row[0].id for row in rows}

    def test_p01a_asset_union_all_branches_and_total_parameter_budget(self):
        self.add_allocations()
        expected = {"REQUEST", "SHIFT", "PROJECT", "RESOURCE", "SEGMENT", "AT-END"}
        single_groups = (("D1",), ("S1",), ("P1",), ("R1",), ("REQ1",))
        with self.factory() as session:
            for i, ids in enumerate(({"REQUEST"}, {"SHIFT"}, expected-{"RESOURCE"},
                                     {"RESOURCE"}, {"SEGMENT"})):
                scope = [() for _ in range(5)]
                scope[i] = single_groups[i]
                self.assertEqual(self.allocation_ids(self.bounded(session, scope)), ids)
            rows = self.bounded(session, single_groups)
            self.assertEqual(self.allocation_ids(rows), expected)
            self.assertEqual(len(rows), len(expected))
            self.assertEqual({row[2].id for row in rows}, {"A-B"})
            self.assertEqual(self.allocation_ids(self.oracle(session, single_groups)), expected)
            assignments = self.fixture._service(session)._repository.list_assignments(
                week_start=WEEK, week_end=END)
            by_shift = {row.shift_id: row for row in assignments}
            self.assertEqual(tuple((asset.allocation_id, asset.association_kind)
                                   for asset in by_shift["S1"].assets), (
                ("REQUEST", "INHERITED_REQUEST"), ("RESOURCE", "INHERITED_RESOURCE_PERIOD"),
                ("SEGMENT", "INHERITED_SEGMENT"), ("SHIFT", "OWNED_SHIFT"),
            ))
            self.assertEqual(by_shift["S2"].assets, ())
            self.assertEqual(tuple(asset.allocation_id for asset in by_shift["S-P2"].assets),
                             ("RESOURCE",))

            # Match the accepted parent fixture cardinalities; additional keys
            # intentionally exercise missing-key behavior, not phantom rows.
            groups = tuple(tuple(keys)+tuple(f"X-{i:04}" for i in range(size-len(keys)))
                           for keys, size in zip(single_groups, (1,904,2,903,904)))
            counts, compiled = [], []
            connection = session.connection()
            root = connection.get_transaction()
            def before(conn, clause, multiparams, params, options):
                if "FROM asset_allocations JOIN asset_requirements" in str(clause):
                    self.assertIs(conn, connection)
                    self.assertIs(conn.get_transaction(), root)
                    stmt = clause.compile(dialect=pyodbc.dialect(paramstyle="qmark"),
                                          compile_kwargs={"render_postcompile": True})
                    compiled.append(len(stmt.positiontup))
            def cursor(conn, cur, stmt, params, ctx, many):
                if "FROM asset_allocations JOIN asset_requirements" in stmt:
                    counts.append(len(params))
            event.listen(connection, "before_execute", before)
            event.listen(connection, "before_cursor_execute", cursor)
            try:
                self.assertEqual(self.allocation_ids(self.oracle(session, groups)), expected)
                self.assertEqual(counts, [2717])
                self.assertEqual(compiled, counts)
                counts.clear(); compiled.clear()
                rows = self.bounded(session, groups)
                self.assertEqual(self.allocation_ids(rows), expected)
                self.assertEqual(len(rows), len(expected))
                self.assertEqual(counts, [4,903,7,5,903,6,903,7])
                self.assertEqual(compiled, counts)
                counts.clear(); compiled.clear()
                self.assertEqual(self.bounded(session, [()]*5), ())
                self.assertEqual(counts, [])
            finally:
                event.remove(connection, "before_execute", before)
                event.remove(connection, "before_cursor_execute", cursor)

    def test_p01a_asset_cross_partition_collation_overlap_is_one_allocation(self):
        self.add_allocations()
        groups = ((), (), ("P1", *[f"X{i:04}" for i in range(900)], "p1"), (), ())
        def nocase(conn, cursor, statement, parameters, context, many):
            return statement.replace("asset_requirements.project_id IN",
                "asset_requirements.project_id COLLATE NOCASE IN"), parameters
        event.listen(self.engine, "before_cursor_execute", nocase, retval=True)
        try:
            with self.factory() as session:
                expected = self.allocation_ids(self.oracle(session, groups))
                actual = self.bounded(session, groups)
                self.assertEqual(expected, {"REQUEST","SHIFT","PROJECT","SEGMENT","AT-END"})
                self.assertEqual(self.allocation_ids(actual), expected)
                self.assertEqual(len(actual), len(expected))
        finally:
            event.remove(self.engine, "before_cursor_execute", nocase)

    def test_p01a_direct_lookup_families_bounded_and_legacy_collation(self):
        keys = [f"X{i:04}" for i in range(901)] + ["x0000"]
        with self.factory.begin() as session:
            session.add_all(BusinessContact(id=k, display_name=k) for k in keys)
            session.flush()
            session.add_all(AppUser(id=k, display_name=k, business_contact_id=k, roles_json="[]") for k in keys)
            session.add_all(WorkforceRequest(id=k, project_id="P1") for k in keys)
        families = ((WorkforceRequest, WorkforceRequest.id), (AppUser, AppUser.id),
                    (BusinessContact, BusinessContact.id), (AppUser, AppUser.business_contact_id))
        def nocase(conn, cursor, stmt, params, ctx, many):
            for model, column in families:
                token = model.__tablename__+"."+column.key+" IN"
                stmt = stmt.replace(token, token.replace(" IN", " COLLATE NOCASE IN"))
            return stmt, params
        event.listen(self.engine, "before_cursor_execute", nocase, retval=True)
        try:
            with self.factory() as session:
                connection = session.connection()
                root = connection.get_transaction()
                for model, column in families:
                    with self.subTest(column=str(column)):
                        wanted = {*keys, "MISSING"}
                        expected = {row.id for row in session.scalars(select(model).where(column.in_(wanted)))}
                        counts, compiled = [], []
                        def cursor(conn, cur, stmt, params, ctx, many):
                            counts.append(len(params))
                            self.assertIs(conn, connection)
                            self.assertIs(conn.get_transaction(), root)
                        def before(conn, clause, multiparams, params, options):
                            stmt = clause.compile(dialect=pyodbc.dialect(paramstyle="qmark"),
                                                  compile_kwargs={"render_postcompile": True})
                            compiled.append(len(stmt.positiontup))
                        event.listen(connection, "before_execute", before)
                        event.listen(connection, "before_cursor_execute", cursor)
                        try:
                            actual = repository._read_scoped_rows(session, model, column, wanted)
                            self.assertEqual({row.id for row in actual}, expected)
                            self.assertEqual(expected, set(keys))
                            self.assertEqual(len(actual), len(expected))
                            self.assertEqual(counts, [900,3])
                            self.assertEqual(compiled, counts)
                            self.assertEqual(repository._read_scoped_rows(session, model, column, []), [])
                            self.assertEqual(counts, [900,3])
                        finally:
                            event.remove(connection, "before_execute", before)
                            event.remove(connection, "before_cursor_execute", cursor)
        finally:
            event.remove(self.engine, "before_cursor_execute", nocase)

    def test_p01a_forward_diagnostics_independent_of_asset_order_v4_v5(self):
        with self.factory.begin() as session:
            add_project_diagnostics(session, "P1", "P01")
        canonical = self.projection()
        canonical5 = self.canonical()
        self.assertEqual(replace(canonical5, request_provenance_version=4, recipient_policy_context=None),
                         canonical)
        expected = (MISSING, MISMATCH)
        self.assertEqual(tuple(c for c in canonical.projects[0].diagnostics if c.startswith("ASSET_QUALIFICATION_")),
                         expected)
        original = repository._read_scoped_assets
        for permutation in (lambda rows: rows, lambda rows: tuple(reversed(rows)),
                            lambda rows: rows[1:]+rows[:1], lambda rows: rows[-1:]+rows[:-1]):
            def order(*args, **kwargs):
                return permutation(original(*args, **kwargs))
            with patch.object(repository, "_read_scoped_assets", side_effect=order):
                self.assertEqual(self.projection(), canonical)
                self.assertEqual(project_projection_fingerprint(self.canonical()),
                                 project_projection_fingerprint(canonical5))
        def pragma(conn, *_):
            conn.exec_driver_sql("PRAGMA reverse_unordered_selects=ON")
        event.listen(self.engine, "begin", pragma)
        try:
            self.assertEqual(self.projection(), canonical)
            self.assertEqual(self.canonical(), canonical5)
        finally:
            event.remove(self.engine, "begin", pragma)
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA reverse_unordered_selects=OFF")

    def test_p01a_historical_order_round_trip_delta_and_genuine_change(self):
        with self.factory.begin() as session:
            add_project_diagnostics(session, "P1", "P01")
        current, current5 = self.projection(), self.canonical()
        actual = repository.SqlProjectCommunicationRepository.list_assignments
        def historical(instance, **kwargs):
            return tuple(reverse_asset_codes(row) for row in actual(instance, **kwargs))
        with patch.object(repository.SqlProjectCommunicationRepository, "list_assignments", historical):
            old, old5 = self.projection(), self.canonical()
        self.assertNotEqual(project_projection_fingerprint(old), project_projection_fingerprint(current))
        self.assertNotEqual(project_projection_fingerprint(old5), project_projection_fingerprint(current5))
        self.assertEqual(old5.recipient_policy_context, current5.recipient_policy_context)
        def resource_codes(projection):
            resource = next(row for day in projection.projects[0].days for task in day.tasks
                            for row in task.resources if row.resource_id == "R1")
            return tuple(code for code in resource.diagnostics if code.startswith("ASSET_QUALIFICATION_"))
        self.assertEqual(resource_codes(old), (MISMATCH, MISSING))
        self.assertEqual(resource_codes(current), (MISSING, MISMATCH))
        for frozen in (old, current, old5, current5):
            payload = serialize_project_projection(frozen)
            decoded = deserialize_project_projection(payload)
            self.assertEqual(decoded, frozen)
            self.assertEqual(serialize_project_projection(decoded), payload)
            self.assertEqual(project_projection_fingerprint(decoded), project_projection_fingerprint(frozen))
        changes = compare_project_communication_projections(old, current)
        change, = changes
        self.assertEqual((change.project_id, change.kind), ("P1","modified"))
        self.assertEqual(_project_shift_deltas(change.before, change.after, previous_version=4, current_version=4), ())
        draft, = build_project_delta_batch(old, current).drafts
        self.assertIn("Informations du projet mises à jour", draft.body)
        self.assertFalse(draft.approvable)
        self.assertEqual({d.code for d in draft.diagnostics if d.code.startswith("ASSET_QUALIFICATION_")},
                         {MISSING,MISMATCH})
        self.assertEqual(build_project_delta_batch(current,current).drafts, ())
        self.assertEqual(compare_project_communication_projections(current,current5), ())
        for v4, v5 in zip(current.projects, current5.projects):
            self.assertEqual(_project_shift_deltas(v4, v5, previous_version=4, current_version=5), ())
        with self.assertRaisesRegex(ValueError, "v5/P5"):
            build_project_confirmation_batch(current5)
        with self.assertRaisesRegex(ValueError, "v5/P5"):
            build_project_delta_batch(current,current5)
        with self.factory.begin() as session:
            # Real qualification-state change, without replacing allocations
            # or modifying any Shift or its asset association.
            session.add(ResourceCompetency(resource_id="R1", competency_id="P01-SKILL"))
        changed = self.projection()
        self.assertNotEqual(project_projection_fingerprint(changed), project_projection_fingerprint(current))
        self.assertEqual({c for c in changed.projects[0].diagnostics if c.startswith("ASSET_QUALIFICATION_")},
                         {MISSING})
        self.assertEqual(_project_shift_deltas(current.projects[0], changed.projects[0],
                                               previous_version=4, current_version=4), ())
        changed_draft, = build_project_delta_batch(current,changed).drafts
        self.assertFalse(changed_draft.approvable)

    def test_p01a_unknown_and_all_qualification_codes_sorted_without_other_group_changes(self):
        with self.factory.begin() as session:
            add_project_diagnostics(session, "P1", "P01")
            session.get(ResourceRequirement, "REQ1").status = "Annulé"
            session.add(AssetType(id="PLAIN", code="PLAIN", label="Demo", category="EQUIPMENT"))
            session.flush()
            session.add(Asset(id="PLAIN", code="PLAIN", label="Demo", asset_type_id="PLAIN"))
            session.add(AssetRequirement(id="SEG-OVERLAP", origin="SEGMENT", project_id="P1",
                resource_requirement_id="REQ1", asset_type_id="PLAIN", start_date=WEEK, end_date=END))
            session.flush()
            session.add(AssetAllocation(id="SEG-OVERLAP", asset_requirement_id="SEG-OVERLAP",
                asset_id="PLAIN", operator_resource_id="R1", start_date=WEEK, end_date=END))
        def assignments():
            with self.factory() as session:
                service = self.fixture._service(session)
                return service._repository.list_assignments(week_start=WEEK, week_end=END)
        original = repository.evaluate_asset_qualifications
        normal = {row.shift_id: row for row in assignments()}["S1"]
        self.assertEqual(tuple(c for c in normal.diagnostics if c.startswith("ASSET_QUALIFICATION_")),
                         (MISSING, NO_OVERLAP, MISMATCH))
        def unknown(session, *, pairs):
            result = original(session, pairs=pairs)
            key = ("P01-DUP","P01-DUP")
            result[key] = replace(result[key], state="FUTURE_UNKNOWN")
            return result
        with patch.object(repository,"evaluate_asset_qualifications",side_effect=unknown):
            after = {row.shift_id: row for row in assignments()}["S1"]
            draft = build_project_confirmation_batch(self.projection()).drafts[0]
        self.assertEqual(tuple(c for c in after.diagnostics if c.startswith("ASSET_QUALIFICATION_")),
                         ("ASSET_QUALIFICATION_FUTURE_UNKNOWN", MISSING, NO_OVERLAP, MISMATCH))
        self.assertEqual(tuple(c for c in normal.diagnostics if not c.startswith("ASSET_QUALIFICATION_")),
                         tuple(c for c in after.diagnostics if not c.startswith("ASSET_QUALIFICATION_")))
        self.assertFalse(draft.approvable)
        self.assertIn("ASSET_QUALIFICATION_FUTURE_UNKNOWN", {d.code for d in draft.diagnostics})

    def test_p01a_all_four_direct_lookup_families_in_actual_large_assignment_read(self):
        keys = [f"B{i:04}" for i in range(901)]
        with self.factory.begin() as session:
            session.add_all(BusinessContact(id=k, display_name=k, email=k+chr(64)+persisted.TEST_DOMAIN)
                            for k in keys)
            session.flush()
            session.add_all(AppUser(id=k, display_name=k, email=k+chr(64)+persisted.TEST_DOMAIN,
                                    employee_external_id="E-"+k, business_contact_id=k, roles_json="[]")
                            for k in keys)
            session.add_all(Project(id=k, number="N-"+k, name=k, project_manager_external_id="E-"+k)
                            for k in keys)
            session.flush()
            session.add_all(WorkforceRequest(id=k, project_id=k, requester_user_id=k) for k in keys)
            session.flush()
            session.add_all(ResourceRequirement(id=k, project_id=k, workforce_request_id=k,
                assigned_resource_id="R1", start_date=WEEK, end_date=WEEK,
                planned_hours=Decimal("1"), description="Demo", status="Planifié", origin="REQUEST",
                approved_contact_context_status="LEGACY_UNKNOWN") for k in keys)
            session.flush()
            session.add_all(Shift(id=k, resource_requirement_id=k, resource_id="R1",
                                  work_date=WEEK, hours=Decimal("1"), allocation_type="Flexible")
                            for k in keys)
        direct_reads = {}
        original = repository._read_scoped_rows
        with self.factory() as session:
            root = session.connection().get_transaction()
            def measure(owner, model, column, wanted):
                self.assertIs(owner, session)
                connection = owner.connection()
                self.assertIs(connection.get_transaction(), root)
                counts, compiled = [], []
                def cursor(conn, cur, stmt, params, ctx, many):
                    counts.append(len(params))
                def before(conn, clause, multiparams, params, options):
                    compiled.append(len(clause.compile(dialect=pyodbc.dialect(paramstyle="qmark"),
                        compile_kwargs={"render_postcompile":True}).positiontup))
                event.listen(connection, "before_execute", before)
                event.listen(connection, "before_cursor_execute", cursor)
                try:
                    result = original(owner,model,column,wanted)
                finally:
                    event.remove(connection, "before_execute", before)
                    event.remove(connection, "before_cursor_execute", cursor)
                self.assertTrue(all(0 < n <= 900 for n in counts))
                self.assertEqual(compiled,counts)
                direct_reads[(model,column.key)] = (len(wanted),len(result),counts)
                return result
            with patch.object(repository,"_read_scoped_rows",side_effect=measure):
                assignments = self.fixture._service(session)._repository.list_assignments(
                    week_start=WEEK,week_end=END)
            self.assertIs(session.connection().get_transaction(),root)
        selected = {row.shift_id:row for row in assignments if row.shift_id in keys}
        self.assertEqual(set(selected),set(keys))
        for key,row in selected.items():
            self.assertEqual(row.workforce_request_id,key)
            self.assertEqual(row.requester.user_id,key)
            self.assertEqual(row.requester.email,key+chr(64)+persisted.TEST_DOMAIN)
            self.assertEqual(row.project_manager.contact_id,key)
            self.assertEqual(row.project_manager.user_id,key)
            self.assertEqual(row.project_manager.email,key+chr(64)+persisted.TEST_DOMAIN)
        self.assertEqual(set(direct_reads),{(WorkforceRequest,"id"),(AppUser,"id"),
                          (BusinessContact,"id"),(AppUser,"business_contact_id")})
        for wanted,received,counts in direct_reads.values():
            self.assertGreaterEqual(wanted,901)
            self.assertEqual(received,wanted)
            self.assertEqual(sum(counts),wanted)
            self.assertGreaterEqual(len(counts),2)
