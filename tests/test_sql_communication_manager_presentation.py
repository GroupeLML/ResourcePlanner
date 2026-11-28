"""R2a standalone presentation lookup; no operational v5 integration."""
from dataclasses import replace
import json
import unittest

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.application.operational_contacts import OperationalContactService
from app.application.project_managers import ProjectManagerResolutionService
from app.domain.communication_recipient_authority import (
    ManagerPresentationEvidence, ManagerPresentationLookup, ManagerPresentationResource,
)
from app.infrastructure.sql import AppUser, BusinessContact, Project, Resource
from app.infrastructure.sql.communication_erp_manager_capture import capture_erp_manager_authorities
from app.infrastructure.sql.communication_manager_presentation_capture import capture_manager_presentation
from app.infrastructure.sql.communication_rp_co_manager_capture import capture_rp_co_manager_authorities
from app.infrastructure.sql.operational_contact_repository import SqlOperationalContactRepository
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.infrastructure.sql.project_manager_resolution_repository import SqlProjectManagerResolutionRepository
from tests import test_sql_communication_rp_co_manager_capture as persisted

mailbox = persisted.mailbox


def standalone_role(key, project="P1"):
    return ManagerPresentationEvidence(
        project, "MANAGER", key, key, "UNRESOLVED_USER", key,
        None, None, None, None, None, None, "ABSENT", None, None, None,
    )


class SqlManagerPresentationTests(unittest.TestCase):
    def setUp(self):
        self.db = persisted.SqlRPCoManagerCaptureTests()
        self.db.setUp()
        self.addCleanup(self.db.doCleanups)

    def capture(self, projects=("P1",)):
        with self.db.factory() as session:
            result = capture_manager_presentation(session, projects)
            self.assertFalse(session.new or session.dirty or session.deleted)
            return result

    def legacy(self, projects):
        # Exercise the actual v4 participant presenter with the originating
        # shared lookup algorithm and real ORM rows, not a new email formula.
        with self.db.factory() as session:
            effective = ProjectManagerResolutionService(
                SqlProjectManagerResolutionRepository(session),
            ).resolve_projects(list(projects))
            managers = []
            for project, projection in effective.items():
                if projection.primary is not None:
                    managers.append((project, "MANAGER", projection.primary))
                managers.extend((project, "CO_MANAGER", row) for row in projection.co_managers)
            keys = sorted({(row.employee_external_id or "").strip()
                           for _, _, row in managers if (row.employee_external_id or "").strip()})
            occurrences = {}
            for offset in range(0, len(keys), 900):
                for resource in session.scalars(select(Resource).where(
                        Resource.external_id.in_(keys[offset:offset + 900]))).all():
                    key = (resource.external_id or "").strip()
                    if key in keys:
                        occurrences.setdefault(key, []).append(resource)
            resources = {key: rows[0] for key, rows in occurrences.items() if len(rows) == 1}
            contact_ids = {row.business_contact_id for _, _, row in managers if row.business_contact_id}
            contacts = {row.id: row for row in session.scalars(select(BusinessContact).where(
                BusinessContact.id.in_(contact_ids))).all()} if contact_ids else {}
            users = {row.business_contact_id: row for row in session.scalars(select(AppUser).where(
                AppUser.business_contact_id.in_(contact_ids))).all()} if contact_ids else {}
            repository = SqlProjectCommunicationRepository(
                session, operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
            )
            result = {}
            for project, role, manager in managers:
                participant = repository._project_manager(
                    manager=manager, contacts=contacts, users_by_contact=users,
                    resources_by_employee=resources, prefer_user_display_name=role == "MANAGER",
                )
                key = manager.employee_external_id if role == "MANAGER" else manager.business_contact_id
                result[(project, role, key)] = (
                    participant.email, participant.active, participant.display_name,
                    participant.contact_id, participant.user_id,
                )
            return result

    def assert_legacy(self, lookup):
        actual = {row.binding: (*lookup.derive(row), row.display_name, row.contact_id, row.linked_user_id)
                  for row in lookup.roles}
        self.assertEqual(actual, self.legacy(lookup.project_ids))

    def collation(self, name):
        def instrument(conn, cursor, statement, parameters, context, executemany):
            return statement.replace("resources.external_id IN",
                                     "resources.external_id COLLATE " + name + " IN"), parameters

        event.listen(self.db.engine, "before_cursor_execute", instrument, retval=True)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", instrument)

    def test_exact_unique_resource_and_activity_independent_fallback(self):
        self.db.change(BusinessContact, "C-PM", email=None)
        self.db.change(Resource, "R-PM", active=False, erp_active=False)
        lookup = self.capture()
        role, = lookup.roles
        resource, = lookup.resources
        self.assertEqual(lookup.employee_keys, ("E-PM",))
        self.assertEqual(lookup.partition_results, (("R-PM",),))
        self.assertEqual((resource.employee_external_id, resource.active, resource.erp_active),
                         ("E-PM", False, False))
        self.assertEqual(lookup.derive(role), (mailbox("resource"), True))
        self.assert_legacy(lookup)

    def test_padded_raw_resource_identifier_retained(self):
        self.db.change(BusinessContact, "C-PM", email=None)
        self.db.change(Resource, "R-PM", external_id="E-PM  ")
        self.collation("RTRIM")
        with self.db.factory() as session:
            self.assertEqual(session.scalar(select(Resource.external_id).where(
                Resource.external_id.collate("RTRIM") == "E-PM")), "E-PM  ")
        lookup = self.capture()
        self.assertEqual(lookup.resources[0].employee_external_id, "E-PM  ")
        self.assertEqual(lookup.derive(lookup.roles[0]), (mailbox("resource"), True))
        self.assert_legacy(lookup)

    def test_collation_equivalent_nonidentical_key_not_in_complete_scope(self):
        self.db.change(BusinessContact, "C-PM", email=None)
        self.db.change(Resource, "R-PM", external_id="e-pm")
        self.collation("NOCASE")
        with self.db.factory() as session:
            self.assertEqual(session.scalar(select(Resource.external_id).where(
                Resource.external_id.collate("NOCASE") == "E-PM")), "e-pm")
        lookup = self.capture()
        self.assertEqual(lookup.employee_keys, ("E-PM",))
        self.assertEqual(lookup.resources, ())
        self.assertEqual(lookup.partition_results, ((),))
        self.assertEqual(lookup.derive(lookup.roles[0]), (None, True))
        self.assert_legacy(lookup)

    def test_same_physical_resource_across_partitions_and_bounded_queries(self):
        keys = ("A-KEY", *(f"B-{index:04d}" for index in range(899)), "a-key")
        projects = tuple(f"X-{index:04d}" for index in range(len(keys)))
        with self.db.factory.begin() as session:
            session.add_all(Project(id=project, number=f"X{index:04d}", name="Demo project",
                                    project_manager_external_id=key)
                            for index, (project, key) in enumerate(zip(projects, keys)))
            session.add(Resource(id="R-CROSS", external_id="a-key", name="Demo resource",
                                 email=mailbox("cross")))
        self.collation("NOCASE")
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("select"):
                statements.append((statement.lower(), len(parameters)))

        event.listen(self.db.engine, "before_cursor_execute", record)
        try:
            lookup = self.capture(projects)
        finally:
            event.remove(self.db.engine, "before_cursor_execute", record)
        self.assertEqual(lookup.employee_keys, keys)
        self.assertEqual(tuple(map(len, lookup.partitions)), (900, 1))
        self.assertEqual([row.resource_id for row in lookup.resources], ["R-CROSS"])
        self.assertEqual(lookup.partition_results, (("R-CROSS",), ("R-CROSS",)))
        self.assertEqual(len(lookup.occurrences("a-key")), 2)
        role = next(row for row in lookup.roles if row.employee_key == "a-key")
        self.assertEqual(lookup.derive(role), (None, False))
        resource_statements = [(sql, count) for sql, count in statements if "from resources" in sql]
        self.assertEqual([count for _, count in resource_statements], [900, 1])
        self.assertTrue(all(count <= 900 for _, count in statements))
        self.assertLess(len(statements), 30)
        self.assert_legacy(lookup)

    def test_distinct_physical_resources_prevent_unique_fallback(self):
        self.db.change(BusinessContact, "C-PM", email=None)
        # Keep the unique index intact: binary-distinct persisted identifiers
        # can both match the legacy lookup under SQLite RTRIM instrumentation.
        with self.db.factory.begin() as session:
            session.add(Resource(id="R-PM-2", external_id="E-PM  ", name="Demo duplicate",
                                 email=mailbox("other")))
        self.collation("RTRIM")
        with self.db.factory() as session:
            rows = session.execute(select(Resource.id, Resource.external_id).where(
                Resource.external_id.in_(("E-PM",))
            ).order_by(Resource.id)).all()
        self.assertEqual([tuple(row) for row in rows], [("R-PM", "E-PM"), ("R-PM-2", "E-PM  ")])
        self.assertEqual({employee.strip() for _, employee in rows}, {"E-PM"})
        lookup = self.capture()
        self.assertEqual(lookup.employee_keys, ("E-PM",))
        self.assertEqual(lookup.partition_results, (("R-PM", "R-PM-2"),))
        self.assertEqual(len(lookup.resources), 2)
        self.assertEqual([(row.resource_id, row.employee_external_id) for row in lookup.resources],
                         [("R-PM", "E-PM"), ("R-PM-2", "E-PM  ")])
        self.assertEqual(lookup.occurrences("E-PM"), lookup.resources)
        self.assertEqual(lookup.derive(lookup.roles[0]), (None, True))
        self.assert_legacy(lookup)

    def test_shared_scope_effective_suppression_and_manual_contact(self):
        self.db.nominate(("P1", "C-PM"), ("P1", "C-CO"), ("P2", "C-MANUAL"))
        with self.db.factory.begin() as session:
            session.add_all([
                Resource(id="R-CO", external_id="E-CO", name="Demo resource", email=mailbox("co-resource")),
                Resource(id="R-MANUAL", external_id="E-MANUAL", name="Demo manual resource",
                         email=mailbox("manual-resource")),
            ])
        self.db.change(BusinessContact, "C-MANUAL", external_system="RESOURCEPLANNER",
                       external_entity="EMPLOYEE", external_id="E-MANUAL")
        lookup = self.capture(("P2", "P1", "P2"))
        self.assertEqual(lookup.project_ids, ("P1", "P2"))
        self.assertEqual(lookup.employee_keys, ("E-CO", "E-PM"))
        self.assertEqual({row.binding for row in lookup.roles}, {
            ("P1", "MANAGER", "E-PM"), ("P1", "CO_MANAGER", "C-CO"),
            ("P2", "MANAGER", "E-PM"), ("P2", "CO_MANAGER", "C-MANUAL"),
        })
        manual = next(row for row in lookup.roles if row.role_id == "C-MANUAL")
        self.assertIsNone(manual.employee_key)
        self.assertIsNone(manual.linked_user_id)
        self.assertNotIn("R-MANUAL", {row.resource_id for row in lookup.resources})
        self.assertEqual(lookup.derive(manual), (mailbox("manual"), True))
        self.assert_legacy(lookup)

    def test_empty_project_role_and_partition_results(self):
        self.assertEqual(self.capture(()), ManagerPresentationLookup((), (), (), ()))
        self.db.change(Project, "P1", project_manager_external_id=None)
        self.assertEqual(self.capture(), ManagerPresentationLookup(("P1",), (), (), ()))
        self.db.change(Project, "P1", project_manager_external_id="E-MISSING")
        lookup = self.capture()
        self.assertEqual(lookup.partition_results, ((),))
        self.assertEqual(lookup.resources, ())
        self.assertEqual(lookup.derive(lookup.roles[0]), (None, False))
        self.assert_legacy(lookup)
        with self.assertRaises(ValueError):
            self.capture(("P-NOT-FOUND",))

    def test_actual_presenter_email_display_name_and_activity_matrix(self):
        self.db.nominate(("P1", "C-CO"), ("P1", "C-MANUAL"))
        self.db.change(Project, "P1", project_manager_name="Demo ERP label")
        self.db.change(AppUser, "U-PM", display_name="  Demo user label  ")
        self.db.change(AppUser, "U-CO", display_name="Demo ignored user label")
        for user_name in ("  Demo user label  ", "  "):
            self.db.change(AppUser, "U-PM", display_name=user_name)
            for user_active, contact_active in ((True, True), (False, True), (True, False), (False, False)):
                with self.subTest(user_name=user_name, user_active=user_active, contact_active=contact_active):
                    self.db.change(AppUser, "U-PM", active=user_active)
                    self.db.change(BusinessContact, "C-PM", active=contact_active)
                    self.db.change(AppUser, "U-CO", active=user_active)
                    self.db.change(BusinessContact, "C-CO", active=contact_active)
                    lookup = self.capture()
                    self.assert_legacy(lookup)
                    manager = next(row for row in lookup.roles if row.role == "MANAGER")
                    co = next(row for row in lookup.roles if row.role_id == "C-CO")
                    self.assertEqual(lookup.derive(manager), (mailbox("manager"), user_active and contact_active))
                    self.assertEqual(co.display_name, "Demo second")
                    self.assertEqual(manager.display_name, user_name.strip() or (
                        "Demo manager" if contact_active else "Demo ERP label"))

    def test_contactless_and_unresolved_erp_user_display_binding(self):
        self.db.change(Project, "P1", project_manager_name="Demo ERP label")
        # A broken direct contact FK is not a permitted persisted fixture.
        # Verify that rather than disabling constraints to manufacture one.
        with self.assertRaises(IntegrityError):
            self.db.change(AppUser, "U-PM", business_contact_id="C-NOT-FOUND")
        self.db.change(AppUser, "U-PM", business_contact_id=None, display_name="Demo user label")
        lookup = self.capture()
        role, = lookup.roles
        self.assertEqual(role.resolution_status, "UNRESOLVED_CONTACT")
        self.assertEqual(role.display_name, "Demo ERP label")
        self.assertEqual(lookup.derive(role), (mailbox("resource"), True))
        self.assert_legacy(lookup)
        self.db.change(AppUser, "U-PM", employee_external_id=None)
        lookup = self.capture()
        role, = lookup.roles
        self.assertEqual(role.resolution_status, "UNRESOLVED_USER")
        self.assertIsNone(role.linked_user_id)
        self.assertEqual(role.display_name, "Demo ERP label")
        self.assert_legacy(lookup)

    def test_canonical_adapters_unchanged_by_presentation_read(self):
        self.db.nominate(("P1", "C-PM"), ("P1", "C-CO"))
        with self.db.factory() as session:
            before = (capture_erp_manager_authorities(session, ("P1",)),
                      capture_rp_co_manager_authorities(session, ("P1",)))
            lookup = capture_manager_presentation(session, ("P1",))
            after = (capture_erp_manager_authorities(session, ("P1",)),
                     capture_rp_co_manager_authorities(session, ("P1",)))
            self.assertEqual(before, after)
            self.assertEqual(len(lookup.roles), 2)
            self.assertEqual(len(before[1][0][0].candidates), 2)


class FrozenManagerPresentationTests(unittest.TestCase):
    def lookup(self):
        roles = tuple(standalone_role(key, f"P-{index:04d}") for index, key in enumerate(
            ("A-KEY", *(f"B-{index:04d}" for index in range(899)), "a-key")))
        resource = ManagerPresentationResource("R1", "a-key ", mailbox("resource"), True, False)
        return ManagerPresentationLookup.capture(
            (row.project_id for row in roles), reversed(roles), ((resource,), (resource,)),
        )

    def test_consistent_repeated_facts_frozen_round_trip(self):
        lookup = self.lookup()
        self.assertEqual(len(lookup.resources), 1)
        self.assertEqual(len(lookup.occurrences("a-key")), 2)
        self.assertEqual(lookup.partition_results, (("R1",), ("R1",)))
        self.assertEqual(lookup.employee_keys, tuple(sorted(lookup.employee_keys)))
        self.assertEqual(ManagerPresentationLookup.deserialize(lookup.serialize()), lookup)
        self.assertEqual(ManagerPresentationLookup.from_payload(lookup.to_payload()).serialize(),
                         lookup.serialize())
        self.assertEqual(ManagerPresentationLookup.capture(
            reversed(lookup.project_ids), reversed(lookup.roles),
            ((lookup.resources[0],), (lookup.resources[0],))), lookup)

    def test_contradictory_repeated_physical_observation_rejected(self):
        lookup = self.lookup()
        resource, = lookup.resources
        with self.assertRaisesRegex(ValueError, "Contradictory physical"):
            ManagerPresentationLookup.capture(
                lookup.project_ids, lookup.roles, ((resource,), (replace(resource, active=False),)),
            )

    def test_missing_unknown_duplicate_unreferenced_and_out_of_scope_evidence_rejected(self):
        lookup = self.lookup()
        resource, = lookup.resources
        for changes in (
            {"partition_results": (("R1",),)},
            {"resources": ()},
            {"partition_results": (("R1", "R1"), ("R1",))},
            {"partition_results": (("UNKNOWN",), ("R1",))},
            {"partition_results": ((), ())},
            {"resources": (replace(resource, employee_external_id="E-FABRICATED"),)},
            {"resources": (resource, resource)},
            {"resources": (resource, replace(resource, resource_id="R2"))},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(lookup, **changes)

    def test_deterministic_physical_order_and_strict_codec(self):
        role = standalone_role("E-PM")
        first = ManagerPresentationResource("R1", "E-PM", None, True, True)
        second = replace(first, resource_id="R2")
        lookup = ManagerPresentationLookup.capture(("P1",), (role,), ((second, first),))
        self.assertEqual(lookup.partition_results, (("R1", "R2"),))
        self.assertEqual(lookup.resources, (first, second))
        self.assertEqual(lookup, ManagerPresentationLookup.capture(("P1",), (role,), ((first, second),)))
        malformed = []
        for key, value in (("version", True), ("roles", None), ("partition_results", [None]),
                           ("project_ids", ["P1", "P1"]), ("resources", [])):
            payload = lookup.to_payload()
            payload[key] = value
            malformed.append(payload)
        payload = lookup.to_payload()
        del payload["partition_results"]
        malformed.append(payload)
        payload = lookup.to_payload()
        payload["roles"][0]["employee_key"] = "E-FABRICATED"
        malformed.append(payload)
        payload = lookup.to_payload()
        payload["resources"][0]["active"] = 1
        malformed.append(payload)
        payload = lookup.to_payload()
        payload["resources"][0]["extra"] = "unexpected"
        malformed.append(payload)
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ManagerPresentationLookup.deserialize(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            ManagerPresentationLookup.deserialize(lookup.serialize().replace(
                '"version":1', '"version":1,"version":1'))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            ManagerPresentationLookup.deserialize(lookup.serialize().replace(
                '"resource_id":"R1"', '"resource_id":"R1","resource_id":"R1"'))
