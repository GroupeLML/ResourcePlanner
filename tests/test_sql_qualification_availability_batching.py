"""P01-C: bounded qualification and class-scope acquisition against parent SQL oracles."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import time, timedelta
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.dialects.mssql import pyodbc

from app.domain.project_communication_messages import (
    build_project_confirmation_batch, build_project_delta_batch,
    deserialize_project_projection, project_projection_fingerprint,
    serialize_project_projection,
)
from app.infrastructure.sql import (
    Asset, AssetAllocation, AssetRequirement, AssetType, AssetTypeCompetency,
    AvailabilityRuleResourceClass, Project, Resource, ResourceAvailabilityRule,
    RequestLine, ResourceClassConfig, ResourceCompetency, ResourceRequirement, Shift,
    WorkforceRequest,
    create_sql_engine, create_session_factory,
)
from app.infrastructure.sql.models import Competency
from app.infrastructure.sql import asset_qualification as qualification
from app.infrastructure.sql import project_communication_repository as communication
from app.infrastructure.sql.availability_class_scope import availability_class_codes_by_rule
from app.infrastructure.sql.asset_query import SqlAssetPlanningQuery
from app.infrastructure.sql.planning_repository import SqlPlanningReadRepository
from app.infrastructure.sql import planning_repository
from app.infrastructure.sql.communication_canonical_acquisition import acquire_canonical_current_projection
from tests import test_sql_project_communication_projection as persisted
from tests import test_sql_operational_contact_query_batching as contacts
from tests.test_sql_communication_query_batching import add_project_diagnostics

WEEK = persisted.WEEK
END = WEEK + timedelta(days=6)
SKILLS = ("Q-FIRST", "Q-ALPHA", "Q-ZETA")
CLASSES = ("Q-A", "Q-b", "Q-Z")


def original_required(session, keys):
    if not keys:
        return {}
    grouped = {}
    for type_id, competency in session.execute(
        select(AssetTypeCompetency.asset_type_id, Competency)
        .join(Competency, AssetTypeCompetency.competency_id == Competency.id)
        .where(AssetTypeCompetency.asset_type_id.in_(keys))
        .order_by(AssetTypeCompetency.asset_type_id, Competency.sort_order,
                  Competency.name, Competency.id)
    ).all():
        grouped.setdefault(type_id, []).append(competency)
    return grouped


def original_entities(session, model, keys):
    if not keys:
        return {}
    return {row.id: row for row in session.scalars(
        select(model).where(model.id.in_(keys))).all()}


def original_held(session, keys):
    grouped = {}
    if keys:
        for key, skill in session.execute(
            select(ResourceCompetency.resource_id, ResourceCompetency.competency_id)
            .where(ResourceCompetency.resource_id.in_(keys))
        ).all():
            grouped.setdefault(key, set()).add(skill)
    return grouped


def original_human(session, operators, projects, start, end):
    if not operators:
        return ()
    statement = (
        select(Shift, ResourceRequirement)
        .join(ResourceRequirement, Shift.resource_requirement_id == ResourceRequirement.id)
        .where(Shift.resource_id.in_(operators), Shift.work_date >= start,
               Shift.work_date <= end, ResourceRequirement.status != "Annulé")
    )
    if projects:
        statement = statement.where(ResourceRequirement.project_id.in_(projects))
    return tuple(session.execute(statement).all())


def original_availability(session, rule_ids):
    identifiers = tuple(dict.fromkeys(str(value) for value in rule_ids if str(value)))
    if not identifiers:
        return {}
    grouped = {identifier: [] for identifier in identifiers}
    for rule, code in session.execute(
        select(AvailabilityRuleResourceClass.availability_rule_id,
               AvailabilityRuleResourceClass.resource_class_code)
        .where(AvailabilityRuleResourceClass.availability_rule_id.in_(identifiers))
        .order_by(AvailabilityRuleResourceClass.availability_rule_id,
                  AvailabilityRuleResourceClass.resource_class_code)
    ).all():
        grouped.setdefault(str(rule), []).append(str(code))
    return {rule: tuple(codes) for rule, codes in grouped.items()}


@contextmanager
def parent_queries():
    # Replace only the new acquisition seams with the original single SELECTs.
    # The unchanged actual production evaluator remains the semantic authority.
    with ExitStack() as stack:
        for name, reader in (("_required_by_type", original_required),
                             ("_qualification_entities", original_entities),
                             ("_held_by_resource", original_held),
                             ("_human_assignment_rows", original_human)):
            stack.enter_context(patch.object(qualification, name, reader))
        for module in (communication, planning_repository):
            stack.enter_context(patch.object(module, "availability_class_codes_by_rule",
                                             original_availability))
        yield


class SqlQualificationAvailabilityBatchingTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = persisted.SqlProjectCommunicationProjectionTests()
        self.url = self.fixture._database(directory.name)
        self.engine = create_sql_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)

    @contextmanager
    def trace(self, session, *, collation=False):
        connection = session.connection()
        root = connection.get_transaction()
        counts, compiled = [], []
        def before(conn, clause, mp, params, options):
            self.assertIs(conn, connection)
            self.assertIs(conn.get_transaction(), root)
            compiled.append(len(clause.compile(
                dialect=pyodbc.dialect(paramstyle="qmark"),
                compile_kwargs={"render_postcompile": True}).positiontup))
        def cursor(conn, cur, statement, params, ctx, many):
            self.assertIs(conn, connection)
            self.assertIs(conn.get_transaction(), root)
            self.assertTrue(ctx.compiled.statement.is_select)
            counts.append((statement, len(params)))
            if collation:
                # Simulate WHERE collation overlap independently of ordering.
                # Both parent and candidate use the same actual SQL ordering,
                # intentionally different from Python ordinal order.
                for column in ("asset_type_competencies.asset_type_id", "resources.id",
                               "resource_competencies.resource_id", "resource_requirements.id",
                               "shifts.resource_id", "resource_requirements.project_id",
                               "availability_rule_resource_classes.availability_rule_id"):
                    statement = statement.replace(column+" IN", column+" COLLATE NOCASE IN")
                statement = statement.replace("competencies.name, competencies.id",
                    "competencies.name COLLATE NOCASE DESC, competencies.id")
                statement = statement.replace(
                    "ORDER BY availability_rule_resource_classes.availability_rule_id, "
                    "availability_rule_resource_classes.resource_class_code",
                    "ORDER BY availability_rule_resource_classes.availability_rule_id, "
                    "availability_rule_resource_classes.resource_class_code COLLATE NOCASE DESC")
            return statement, params
        event.listen(connection, "before_execute", before)
        event.listen(connection, "before_cursor_execute", cursor, retval=True)
        try:
            yield counts
            self.assertEqual(compiled, [n for _, n in counts])
        finally:
            event.remove(connection, "before_execute", before)
            event.remove(connection, "before_cursor_execute", cursor)

    def seed_assets(self, count, *, variants=False):
        keys = contacts.SqlOperationalContactQueryBatchingTests.seed_materialized(
            self, count, special=False)
        with self.factory.begin() as session:
            session.add_all([
                Competency(id=SKILLS[0], name="Demo beta", sort_order=0),
                Competency(id=SKILLS[1], name="Demo Alpha", sort_order=1),
                Competency(id=SKILLS[2], name="Demo zeta", sort_order=1),
            ])
            types = ["TQ-"+key for key in keys]
            if variants:
                types.append(types[0].lower())
            types.append("TQ-EMPTY")
            session.add_all(AssetType(id=key, code=key, label=key, category="EQUIPMENT")
                            for key in types)
            session.flush()
            session.add_all(AssetTypeCompetency(asset_type_id=key, competency_id=skill)
                            for key in types if key!="TQ-EMPTY" for skill in SKILLS)
            session.add_all(ResourceCompetency(resource_id=key, competency_id=skill)
                            for key in keys for skill in SKILLS)
            # One physical asset per reservation: no artificial overlap.
            for prefix in ("QH-", "QS-"):
                session.add_all(Asset(id=prefix+key, code=prefix+key, label=key,
                                      asset_type_id="TQ-"+key) for key in keys)
            session.flush()
            session.add_all(AssetRequirement(id="QH-"+key, project_id=key,
                origin="SHIFT_AD_HOC", shift_id=key, asset_type_id="TQ-"+key,
                start_date=WEEK,end_date=END,status="Affecté") for key in keys)
            session.add_all(AssetRequirement(id="QS-"+key, project_id=key,
                origin="SEGMENT", resource_requirement_id=key, asset_type_id="TQ-"+key,
                start_date=WEEK,end_date=END,status="Affecté") for key in keys)
            session.flush()
            for i,key in enumerate(keys):
                session.add(AssetAllocation(id="QH-"+key,asset_requirement_id="QH-"+key,
                    asset_id="QH-"+key,operator_resource_id=keys[(i+1)%count],
                    start_date=WEEK,end_date=END))
                session.add(AssetAllocation(id="QS-"+key,asset_requirement_id="QS-"+key,
                    asset_id="QS-"+key,operator_resource_id=key,start_date=WEEK,end_date=END))
        return keys

    def seed_availability(self, count, *, variants=False):
        keys = [f"QA{i:04}" for i in range(count)]
        if variants:
            keys.append(keys[0].lower())
        keys.append("QA-EMPTY")
        with self.factory.begin() as session:
            session.add_all(ResourceClassConfig(code=code,label=code,active=True) for code in CLASSES)
            session.flush()
            session.get(Resource,"R1").resource_class=CLASSES[2]
            session.add(ResourceAvailabilityRule(id="Q-STD",resource_id="R1",
                availability_type="Horaire standard",weekdays="Lun,Mar,Mer,Jeu,Ven",
                start_time=time(8),end_time=time(16)))
            session.add_all(ResourceAvailabilityRule(id=key,availability_type="Jour férié",
                start_date=WEEK if i==0 else END+timedelta(days=10),
                end_date=WEEK if i==0 else END+timedelta(days=10))
                for i,key in enumerate(keys))
            session.flush()
            session.add_all(AvailabilityRuleResourceClass(availability_rule_id=key,
                resource_class_code=code) for key in keys if key!="QA-EMPTY"
                for code in (CLASSES[:2] if key==keys[0] else CLASSES))
        return keys

    @staticmethod
    def all_pairs(session):
        return tuple(session.execute(select(AssetRequirement,AssetAllocation)
            .outerjoin(AssetAllocation,AssetAllocation.asset_requirement_id==AssetRequirement.id)
            .order_by(AssetRequirement.id)).all())

    @staticmethod
    def required_shape(grouped):
        return {key:tuple((row.id,row.name) for row in values) for key,values in grouped.items()}

    @staticmethod
    def human_ids(rows):
        return {(shift.id,req.id) for shift,req in rows}

    def test_p01c_large_qualification_all_five_families_and_cross_partition_matches(self):
        keys=self.seed_assets(1101)
        with self.factory() as session:
            pairs=self.all_pairs(session)
            with parent_queries(), self.trace(session) as before:
                expected=qualification.evaluate_asset_qualifications(session,pairs=pairs)
            with self.trace(session) as after:
                actual=qualification.evaluate_asset_qualifications(session,pairs=pairs)
            self.assertEqual(actual,expected)
            self.assertEqual(len(actual),2202)
            self.assertTrue(all(value.state==qualification.QUALIFICATION_SATISFIED
                                for value in actual.values()))
            self.assertEqual([n for _,n in before],[1101,1101,1101,1101,2205])
            self.assertEqual([n for _,n in after],
                             [900,201]*4+[1803,1104,1104,405])
            with self.trace(session) as prior:
                oracle=self.human_ids(original_human(session,keys,keys,WEEK,END))
            with self.trace(session) as bounded:
                rows=qualification._human_assignment_rows(session,keys,keys,WEEK,END)
            self.assertEqual(self.human_ids(rows),oracle)
            self.assertEqual(len(rows),1101)
            self.assertEqual(oracle,{(key,key) for key in keys})
            self.assertIn((keys[899],keys[899]),oracle)
            self.assertIn((keys[-1],keys[-1]),oracle)
            self.assertEqual([n for _,n in prior],[2205])
            self.assertEqual([n for _,n in bounded],[1803,1104,1104,405])
            with self.trace(session) as none:
                self.assertEqual(qualification.evaluate_asset_qualifications(session,pairs=()),{})
            self.assertEqual(none,[])

    def test_p01c_ordered_competencies_and_physical_overlap_at_all_boundaries(self):
        keys=self.seed_assets(901,variants=True)
        types=["TQ-"+key for key in keys]
        with self.factory() as session:
            for size in (0,1,899,900,901):
                scope=types[:size]
                with self.trace(session,collation=True) as before:
                    expected=self.required_shape(original_required(session,scope))
                with self.trace(session,collation=True) as after:
                    actual=self.required_shape(qualification._required_by_type(session,scope))
                self.assertEqual(actual,expected)
                self.assertEqual(len(actual),size+bool(size))
                self.assertEqual([n for _,n in after],[min(900,size-i) for i in range(0,size,900)])
                self.assertEqual(len(before),bool(size))
                for model in (Resource,ResourceRequirement):
                    with self.trace(session) as prior_entities:
                        prior_ids=set(original_entities(session,model,keys[:size]))
                    with self.trace(session) as actual_entities:
                        found=qualification._qualification_entities(session,model,keys[:size])
                    self.assertEqual(set(found),prior_ids)
                    self.assertEqual(set(found),set(keys[:size]))
                    self.assertEqual([n for _,n in actual_entities],
                                     [min(900,size-i) for i in range(0,size,900)])
                    self.assertEqual(len(prior_entities),bool(size))
                with self.trace(session) as prior_held:
                    expected_held=original_held(session,keys[:size])
                with self.trace(session) as actual_held:
                    found_held=qualification._held_by_resource(session,keys[:size])
                self.assertEqual(found_held,expected_held)
                self.assertEqual(set(found_held),set(keys[:size]))
                self.assertEqual([n for _,n in actual_held],
                                 [min(900,size-i) for i in range(0,size,900)])
                self.assertEqual(len(prior_held),bool(size))
            scope=types+[types[0].lower(),"TQ-EMPTY",types[0]]
            with self.trace(session,collation=True):
                expected=self.required_shape(original_required(session,set(scope)))
            with self.trace(session,collation=True) as actual_counts:
                actual=self.required_shape(qualification._required_by_type(session,scope))
            self.assertEqual(actual,expected)
            self.assertEqual([n for _,n in actual_counts],[900,3])
            self.assertNotIn("TQ-EMPTY",actual)
            for key in (types[0],types[0].lower()):
                self.assertEqual(tuple(row[0] for row in actual[key]),
                                 (SKILLS[0],SKILLS[2],SKILLS[1]))

    def test_p01c_human_and_scope_no_project_collation_and_date_exclusions(self):
        keys=self.seed_assets(901)
        with self.factory.begin() as session:
            session.get(ResourceRequirement,keys[1]).status="Annulé"
            session.get(Shift,keys[2]).work_date=WEEK-timedelta(days=1)
            session.get(Shift,keys[3]).work_date=END+timedelta(days=1)
            session.get(Shift,keys[4]).work_date=END
            # Distinct legitimate physical rows in the fixture's BINARY PKs.
            key=keys[0].lower()
            session.add(Project(id=key,number="N-"+key,name=key))
            session.add(Resource(id=key,name=key))
            session.flush()
            session.add(ResourceRequirement(id=key,project_id=key,
                origin="AD_HOC",approved_contact_context_status="NOT_APPLICABLE",
                assigned_resource_id=key,start_date=WEEK,end_date=END,
                planned_hours=1,status="Planifié"))
            session.flush()
            session.add(Shift(id=key,resource_requirement_id=key,resource_id=key,
                              work_date=WEEK,hours=1,allocation_type="Flexible"))
        scope=keys+[keys[0].lower()]
        with self.factory() as session:
            for projects in (scope,()):
                with self.trace(session,collation=True) as prior:
                    expected=self.human_ids(original_human(session,scope,projects,WEEK,END))
                with self.trace(session,collation=True) as actual_counts:
                    rows=qualification._human_assignment_rows(session,scope,projects,WEEK,END)
                self.assertEqual(self.human_ids(rows),expected)
                self.assertEqual(len(rows),len(expected))
                self.assertEqual(expected,{(k,k) for k in scope if k not in keys[1:4]})
                self.assertEqual([n for _,n in actual_counts],
                    [1803,905,905,7] if projects else [903,5])
                self.assertEqual(len(prior),1)

    def test_p01c_availability_membership_order_empty_groups_and_planning_reader(self):
        keys=self.seed_availability(1802,variants=True)
        with self.factory() as session:
            for size in (0,1,899,900,901,1802,len(keys)):
                scope=keys[:size]+keys[:1] if size else []
                with self.trace(session,collation=True) as before:
                    expected=original_availability(session,scope)
                with self.trace(session,collation=True) as after:
                    actual=availability_class_codes_by_rule(session,scope)
                self.assertEqual(actual,expected)
                self.assertEqual(tuple(actual)[:size],tuple(keys[:size]))
                self.assertEqual([n for _,n in after],[min(900,size-i) for i in range(0,size,900)])
                self.assertEqual(len(before),bool(size))
            self.assertEqual(actual["QA-EMPTY"],())
            self.assertEqual(actual[keys[0]],("Q-b","Q-A"))
            self.assertEqual(actual[keys[0].lower()],("Q-Z","Q-b","Q-A"))
            self.assertEqual(availability_class_codes_by_rule(session,["MISSING"]),{"MISSING":()})
            with parent_queries():
                expected=SqlPlanningReadRepository(session)._availability()
            actual=SqlPlanningReadRepository(session)._availability()
            self.assertEqual(actual,expected)
            self.assertEqual(len(actual),len(keys)+1)

    def test_p01c_qualification_origins_and_genuine_changes_with_valid_relationships(self):
        keys=self.seed_assets(8)
        with self.factory.begin() as session:
            # Existing accepted fixtures establish actual D1/L1/S1 authority.
            for key in ("R1","R2"):
                session.add_all(ResourceCompetency(resource_id=key,competency_id=skill) for skill in SKILLS)
            session.flush()
            session.get(Resource,keys[1]).active=False
            session.get(Resource,keys[2]).external_id="DEMO-ERP"
            session.get(Resource,keys[2]).active=False
            session.get(Resource,keys[2]).erp_active=True
            session.get(Resource,keys[3]).external_id="DEMO-OFF"
            session.get(Resource,keys[3]).erp_active=False
            session.get(ResourceRequirement,keys[4]).status="Annulé"
            session.get(AssetRequirement,"QS-"+keys[5]).project_id=keys[6]
            session.get(AssetRequirement,"QH-"+keys[6]).project_id=keys[7]
            session.get(AssetAllocation,"QH-"+keys[7]).start_date=END
            session.get(AssetAllocation,"QH-"+keys[7]).end_date=END
            type_id="TQ-"+keys[0]
            # Same-project request without any matching human assignment.
            session.add(WorkforceRequest(id="D-Q",project_id="P1"))
            session.flush()
            session.add(RequestLine(id="L-Q",workforce_request_id="D-Q",position=0))
            session.flush()
            cases=(
                ("REQ-OK","REQUEST",dict(project_id="P1",workforce_request_id="D1",
                     source_request_line_id="L1",approved_entry_key="Q-REQ-OK"),"R1"),
                ("REQ-WRONG","REQUEST",dict(project_id="P1",workforce_request_id="D1",
                     source_request_line_id="L1",approved_entry_key="Q-REQ-WRONG"),keys[0]),
                ("REQ-WRONG-REQUEST","REQUEST",dict(project_id="P1",workforce_request_id="D-Q",
                     source_request_line_id="L-Q",approved_entry_key="Q-WRONG-REQUEST"),"R1"),
                ("DIRECT","PROJECT_DIRECT",dict(project_id=keys[0]),keys[0]),
                ("MISSING","PROJECT_DIRECT",dict(project_id=keys[0]),None),
                ("FREE","PROJECT_DIRECT",dict(project_id=keys[0]),None),
                ("CONT-ON","RESOURCE_PERIOD",dict(context_resource_id=keys[2],
                      resource_period_mode="CONTINUOUS"),keys[2]),
                ("CONT-OFF","RESOURCE_PERIOD",dict(context_resource_id=keys[3],
                      resource_period_mode="CONTINUOUS"),keys[3]),
                ("PERIOD-WRONG","RESOURCE_PERIOD",dict(context_resource_id=keys[0],
                      resource_period_mode="BOUNDED"),keys[6]),
            )
            for suffix,origin,fields,operator in cases:
                key="QX-"+suffix
                chosen="TQ-EMPTY" if suffix=="FREE" else type_id
                session.add(Asset(id=key,code=key,label=key,asset_type_id=chosen))
                session.add(AssetRequirement(id=key,origin=origin,asset_type_id=chosen,
                    start_date=WEEK,end_date=END,status="Affecté",**fields))
                session.flush()
                session.add(AssetAllocation(id=key,asset_requirement_id=key,asset_id=key,
                    operator_resource_id=operator,start_date=WEEK,end_date=END))
        with self.factory() as session:
            pairs=self.all_pairs(session)
            with parent_queries(), self.trace(session) as before:
                expected=qualification.evaluate_asset_qualifications(session,pairs=pairs)
            with self.trace(session) as after:
                actual=qualification.evaluate_asset_qualifications(session,pairs=pairs)
            self.assertEqual(actual,expected)
            self.assertEqual([n for _,n in after],[n for _,n in before])
            self.assertEqual(len(after),5)
            states={key[0]:value.state for key,value in actual.items()}
            for key in ("REQ-OK","DIRECT","FREE","CONT-ON"):
                self.assertEqual(states["QX-"+key],qualification.QUALIFICATION_SATISFIED)
            for key in ("REQ-WRONG","REQ-WRONG-REQUEST","PERIOD-WRONG"):
                self.assertEqual(states["QX-"+key],qualification.QUALIFICATION_NO_OVERLAP)
            self.assertEqual(states["QX-CONT-OFF"],qualification.QUALIFICATION_SKILL_MISMATCH)
            self.assertEqual(states["QX-MISSING"],qualification.QUALIFICATION_MISSING_OPERATOR)
            for key in ("QS-"+keys[4],"QS-"+keys[5],"QH-"+keys[6],"QH-"+keys[7]):
                self.assertEqual(states[key],qualification.QUALIFICATION_NO_OVERLAP)
            self.assertEqual(states["QH-"+keys[0]],qualification.QUALIFICATION_SKILL_MISMATCH)
            # Shared actual asset Planning caller, without changing its own queries.
            with parent_queries():
                prior=SqlAssetPlanningQuery(session).planning_window(start=WEEK,end=END)
            self.assertEqual(SqlAssetPlanningQuery(session).planning_window(start=WEEK,end=END),prior)
        with self.factory.begin() as session:
            session.delete(session.get(ResourceCompetency,(keys[0],SKILLS[0])))
        with self.factory() as session:
            changed=qualification.evaluate_asset_qualifications(session,pairs=self.all_pairs(session))
            self.assertEqual(changed[("QX-DIRECT","QX-DIRECT")].state,
                             qualification.QUALIFICATION_SKILL_MISMATCH)

    def test_p01c_large_communication_v4_owned_v5_and_availability_change(self):
        keys=self.seed_assets(901)
        rules=self.seed_availability(901)
        with self.factory.begin() as session:
            add_project_diagnostics(session,"P1","Q-DIAG")
        def v4():
            with self.factory() as session:
                return self.fixture._service(session).project_projection(week_start=WEEK)
        def v5():
            return acquire_canonical_current_projection(self.factory,week_start=WEEK,
                                                         superintendent_competency_id=None)
        with parent_queries():
            expected4,expected5=v4(),v5()
        actual4=v4()
        checkouts,owners,selected=[],[],[]
        def checkout(dbapi,*_):
            checkouts.append(dbapi)
        def observe(conn,cur,sql,params,ctx,many):
            if sql=="BEGIN":
                return
            self.assertTrue(ctx.compiled is not None and ctx.compiled.statement.is_select)
            owners.append((conn,conn.get_transaction(),len(params)))
            if any(table in sql for table in ("asset_type_competencies","resource_competencies",
                                              "availability_rule_resource_classes")):
                selected.append(len(params))
        event.listen(self.engine.pool,"checkout",checkout)
        event.listen(self.engine,"before_cursor_execute",observe)
        try:
            actual5=v5()
            self.assertEqual(len(checkouts),1)
            self.assertTrue(owners)
            self.assertTrue(all(c is owners[0][0] and t is owners[0][1] for c,t,_ in owners))
            self.assertTrue(selected)
            self.assertTrue(all(0<n<=900 for n in selected))
            self.assertTrue(all(n<=1803 for _,_,n in owners))
            self.assertTrue(owners[0][0].closed)
            self.assertEqual(self.engine.pool.checkedout(),0)
        finally:
            event.remove(self.engine.pool,"checkout",checkout)
            event.remove(self.engine,"before_cursor_execute",observe)
        self.assertEqual(actual4,expected4)
        self.assertEqual(actual5,expected5)
        # Real P01-B materialized contact facts remain equal too, not just
        # asset and availability state.
        self.assertEqual(serialize_project_projection(actual4),
                         serialize_project_projection(expected4))
        self.assertEqual(actual5.recipient_policy_context,expected5.recipient_policy_context)
        self.assertEqual(replace(actual5,request_provenance_version=4,recipient_policy_context=None),actual4)
        self.assertTrue(set(keys)<={project.project_id for project in actual4.projects})
        def unexpected(*_):
            self.fail("SQL after immutable result")
        event.listen(self.engine,"before_cursor_execute",unexpected)
        try:
            for actual,expected in ((actual4,expected4),(actual5,expected5)):
                self.assertEqual(project_projection_fingerprint(actual),project_projection_fingerprint(expected))
                self.assertEqual(deserialize_project_projection(serialize_project_projection(actual)),actual)
        finally:
            event.remove(self.engine,"before_cursor_execute",unexpected)
        project=next(p for p in actual4.projects if p.project_id=="P1")
        self.assertEqual(tuple(code for code in project.diagnostics if code.startswith("ASSET_QUALIFICATION_")),
                         ("ASSET_QUALIFICATION_MISSING_OPERATOR","ASSET_QUALIFICATION_SKILL_MISMATCH"))
        def hours(projection):
            project=next(p for p in projection.projects if p.project_id=="P1")
            return next(row.standard_hours for day in project.days for task in day.tasks
                        for row in task.request_assignments if row.resource_id=="R1")
        self.assertEqual(hours(actual4),8.0)
        with self.assertRaisesRegex(ValueError,"v5/P5"):
            build_project_confirmation_batch(actual5)
        with self.assertRaisesRegex(ValueError,"v5/P5"):
            build_project_delta_batch(actual4,actual5)
        with self.factory.begin() as session:
            session.add(AvailabilityRuleResourceClass(availability_rule_id=rules[0],
                                                     resource_class_code=CLASSES[2]))
            session.delete(session.get(ResourceCompetency,(keys[1],SKILLS[0])))
        changed=v4()
        self.assertEqual(hours(changed),0.0)
        self.assertNotEqual(project_projection_fingerprint(changed),project_projection_fingerprint(actual4))
        changed_project=next(p for p in changed.projects if p.project_id==keys[0])
        self.assertIn("ASSET_QUALIFICATION_SKILL_MISMATCH",changed_project.diagnostics)
    def test_p01c_provisional_compatibility_and_owned_failure_no_retry(self):
        self.seed_assets(2)
        self.seed_availability(2)
        with parent_queries():
            with self.factory() as session:
                prior=self.fixture._service(session).capture_v5_projection(week_start=WEEK)
        with self.factory() as session:
            provisional=self.fixture._service(session).capture_v5_projection(week_start=WEEK)
        self.assertEqual(provisional,prior)
        self.assertEqual(provisional.recipient_policy_context.presentation_mode,"UNAVAILABLE_PRE_R2B")
        origin=RuntimeError("synthetic qualification acquisition failure")
        calls=[]
        def fail(session,*args):
            calls.append(session)
            raise origin
        with patch.object(qualification,"_human_assignment_rows",side_effect=fail):
            with self.assertRaises(RuntimeError) as caught:
                acquire_canonical_current_projection(self.factory,week_start=WEEK,
                                                       superintendent_competency_id=None)
            self.assertIs(caught.exception,origin)
        self.assertEqual(len(calls),1)
        self.assertFalse(calls[0].in_transaction())
        self.assertEqual(self.engine.pool.checkedout(),0)
