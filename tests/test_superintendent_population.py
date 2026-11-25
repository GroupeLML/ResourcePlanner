from __future__ import annotations

import unittest

from sqlalchemy import event

from app.application.operational_contacts import OperationalContactService
from app.infrastructure.sql import Base, create_session_factory, create_sql_engine
from app.infrastructure.sql.models import Competency, Resource, ResourceCompetency
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.server.runtime import DATABASE_URL_ENV, SUPERINTENDENT_COMPETENCY_ID_ENV, ServerSettings


class SuperintendentPopulationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all([
                Competency(id="COMP-869", name="Surintendant"),
                Competency(id="COMP-OTHER", name="Other"),
                Competency(id="COMP-INACTIVE", name="Inactive", active=False),
                Resource(id="R-ELIGIBLE", name="Eligible", active=True, erp_active=True),
                Resource(id="R-LOCAL-OFF", name="Local inactive", active=False, erp_active=True),
                Resource(id="R-ERP-OFF", name="ERP inactive", active=True, erp_active=False),
                Resource(id="R-TEXT", name="Legacy only", competencies="Surintendant"),
                Resource(id="R-OTHER", name="Other membership"),
            ])
            session.flush()
            session.add_all([
                ResourceCompetency(resource_id=rid, competency_id="COMP-869")
                for rid in ("R-ELIGIBLE", "R-LOCAL-OFF", "R-ERP-OFF")
            ])
            session.add(ResourceCompetency(resource_id="R-OTHER", competency_id="COMP-OTHER"))

    def read(self, binding):
        with self.factory() as session:
            repo = SqlProjectCommunicationRepository(
                session,
                operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
                superintendent_competency_id=binding,
            )
            return repo.read_superintendent_population()

    def test_canonical_membership_and_independent_active_flags(self):
        population = self.read("COMP-869")
        self.assertTrue(population.complete)
        self.assertEqual(tuple(row.resource_id for row in population.employees), ("R-ELIGIBLE",))
        self.assertIsNone(population.employees[0].email)
        # No projects, shifts, users or roles are needed.

    def test_binding_diagnostics_and_valid_empty(self):
        for value, code in [
            (None, "MISSING"), ("  ", "MISSING"), ("unknown", "INVALID"),
            ("comp-869", "INVALID"), ("COMP-869 ", "INVALID"),
            ("x" * 37, "INVALID"), ("COMP-INACTIVE", "INACTIVE"),
        ]:
            with self.subTest(value=value):
                result = self.read(value)
                self.assertFalse(result.complete)
                self.assertEqual(result.diagnostic, "SUPERINTENDENT_BINDING_" + code)
                self.assertEqual(result.employees, ())
        with self.factory.begin() as session:
            session.add(Competency(id="COMP-EMPTY", name="Empty"))
        result = self.read("COMP-EMPTY")
        self.assertTrue(result.complete)
        self.assertEqual(result.employees, ())

    def test_rename_does_not_rebind(self):
        before = self.read("COMP-869")
        with self.factory.begin() as session:
            session.get(Competency, "COMP-869").name = "Renamed"
        with self.factory.begin() as session:
            session.add(Competency(id="COMP-NEW", name="Surintendant"))
        self.assertEqual(self.read("COMP-869"), before)

    def test_population_is_not_truncated_and_uses_one_statement(self):
        with self.factory.begin() as session:
            ids = [f"R-BULK-{index:04d}" for index in range(301)]
            session.add_all(Resource(id=rid, name=rid) for rid in ids)
            session.flush()
            session.add_all(ResourceCompetency(resource_id=rid, competency_id="COMP-869") for rid in ids)
        statements = []
        def capture(*args):
            statements.append(args[2])
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            population = self.read("COMP-869")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertTrue(population.complete)
        self.assertEqual(len(statements), 1)
        self.assertEqual(tuple(row.resource_id for row in population.employees), tuple(ids + ["R-ELIGIBLE"]))

    def test_environment_binding_does_not_block_unrelated_startup_configuration(self):
        base = {DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:"}
        self.assertIsNone(ServerSettings.from_environment(base).superintendent_competency_id)
        for value in ("COMP-869", "invalid "):
            settings = ServerSettings.from_environment({**base, SUPERINTENDENT_COMPETENCY_ID_ENV: value})
            self.assertEqual(settings.superintendent_competency_id, value)
