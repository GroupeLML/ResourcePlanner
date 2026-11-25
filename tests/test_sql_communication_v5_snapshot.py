"""SQLite evidence for the existing project_v2 snapshot boundary.

Communicated rows are historical fixtures, not application lifecycle transitions.
No live v5 generation, approval, or delivery is exercised here.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from app.application.communications import STATUS_COMMUNICATED, STATUS_PREPARED
from app.application.project_communications import MODEL_VERSION_PROJECT_V2
from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.communication_recipient_provenance import RecipientPolicyContext
from app.domain.project_communication import ProjectCommunicationParticipant
from app.domain.project_communication_messages import (
    ProjectCommunicationDraft, deserialize_project_projection,
    project_projection_fingerprint, serialize_project_projection,
)
from app.infrastructure.sql import (
    Base, CommunicationBatchRow, CommunicationDeliveryRow, CommunicationMessageRow,
    SqlCommunicationRepository, create_session_factory, create_sql_engine,
)
from tests import test_project_communication_v5 as fixtures
from tests.test_project_communication_historical_golden import GOLDEN, historical_fixture


WEEK = fixtures.WEEK
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
KIND = "project_confirmation"


class SqlCommunicationV5SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_sql_engine("sqlite+pysqlite:///" + (Path(self.directory.name) / "snapshots.db").as_posix())
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine, tables=[
            CommunicationBatchRow.__table__, CommunicationMessageRow.__table__,
            CommunicationDeliveryRow.__table__,
        ])
        self.factory = create_session_factory(self.engine)
        fixture = fixtures.ProjectCommunicationV5Tests()
        fixture.setUp()
        population = fixture.population.to_population()
        population = RecipientPolicyPopulation.capture(replace(population, employees=(
            population.employees[0], replace(population.employees[1], email=fixtures.mailbox("r-2")),
        )))
        context = RecipientPolicyContext.capture(population, fixture.identities, (fixture.inputs,))
        self.snapshot = fixture.build(5, context)
        self.fingerprint = project_projection_fingerprint(self.snapshot)
        self.payload = serialize_project_projection(self.snapshot)
        project = self.snapshot.projects[0]
        assigned = project.days[0].tasks[0].resources[0].contact
        # Explicit synthetic persisted-artifact fixture, not operational composition.
        self.draft = ProjectCommunicationDraft(
            message_key="project:" + project.project_id, audience="project",
            project_id=project.project_id, project_number=project.project_number,
            to_recipient=project.project_manager,
            to_recipients=(project.project_manager, *project.project_co_managers, *project.requesters),
            cc_recipients=(assigned, ProjectCommunicationParticipant(
                None, None, "Demo R-2", fixtures.mailbox("r-2"), None, True,
            )),
            subject="Demo subject", body="Demo body", body_html="<p>Demo body</p>",
            message_kind=KIND, week_start=WEEK, content_fingerprint="a" * 64,
            approvable=True, diagnostics=(),
        )
        self.assertTrue(context.messages[0].mandatory_complete)

    def _seed_communicated(self, session, payload, fingerprint, *, offset=0, week=WEEK):
        """Represent an existing historical row; never transition a live batch."""
        row = CommunicationBatchRow(
            week_start=week, kind=KIND, snapshot_fingerprint=fingerprint,
            status=STATUS_COMMUNICATED, prepared_at=NOW, approved_at=NOW,
            communicated_at=NOW + timedelta(seconds=offset),
            model_version=MODEL_VERSION_PROJECT_V2, project_snapshot_json=payload,
        )
        session.add(row)
        session.flush()
        return row.id

    def test_v5_repository_write_and_fresh_session_read(self):
        with self.factory.begin() as session:
            repository = SqlCommunicationRepository(session)
            prepared = repository.create_project_prepared_batch(
                week_start=WEEK, kind=KIND, fingerprint=self.fingerprint,
                actor_name="Demo operator", messages=((self.draft, True),), snapshot=self.snapshot,
            )
            batch_id = prepared.id
            self.assertEqual(prepared.status, STATUS_PREPARED)
            self.assertIsNone(repository.latest_communicated_project_snapshot(week_start=WEEK))
        with self.factory.begin() as session:
            repository = SqlCommunicationRepository(session)
            row = session.get(CommunicationBatchRow, batch_id)
            self.assertEqual(row.model_version, MODEL_VERSION_PROJECT_V2)
            self.assertEqual(row.project_snapshot_json, self.payload)
            restored = deserialize_project_projection(row.project_snapshot_json)
            self.assertEqual(restored, self.snapshot)
            self.assertEqual(restored.recipient_policy_context, self.snapshot.recipient_policy_context)
            self.assertEqual(project_projection_fingerprint(restored), row.snapshot_fingerprint)
            persisted = repository.get_batch(batch_id)
            message = persisted.messages[0]
            self.assertEqual(message.to_emails, tuple(p.email for p in self.draft.to_recipients))
            self.assertEqual(message.cc_emails, tuple(p.email for p in self.draft.cc_recipients))
            self.assertEqual((message.subject, message.body, message.body_html),
                             (self.draft.subject, self.draft.body, self.draft.body_html))
            self.assertEqual(message.content_fingerprint, self.draft.content_fingerprint)
            self.assertIsNone(repository.latest_communicated_project_snapshot(week_start=WEEK))
            # Separately seed an already-communicated fixture to exercise the
            # authoritative reader, without invoking or bypassing application approval.
            self._seed_communicated(session, row.project_snapshot_json, row.snapshot_fingerprint)
        with self.factory() as session:
            self.assertEqual(SqlCommunicationRepository(session).latest_communicated_project_snapshot(week_start=WEEK),
                             (self.fingerprint, self.snapshot))

    def test_historical_p0_p4_baselines_and_artifacts_are_not_rewritten(self):
        for offset, (version, (serialized_hash, fingerprint)) in enumerate(GOLDEN.items()):
            historical = historical_fixture(version)
            payload = serialize_project_projection(historical)
            self.assertEqual(hashlib.sha256(payload.encode("utf-8")).hexdigest(), serialized_hash)
            with self.factory.begin() as session:
                batch_id = self._seed_communicated(session, payload, fingerprint, offset=offset)
                session.add(CommunicationMessageRow(
                    batch_id=batch_id, audience="project", recipient_id="C-PM",
                    recipient_email=fixtures.mailbox("historical"), project_id="PROJECT-1",
                    to_recipients_json=json.dumps([{"email": fixtures.mailbox("historical")}]),
                    cc_recipients_json=json.dumps([{"email": fixtures.mailbox("original-cc")}]),
                    subject="Original subject", body="Original body", body_html=None, included=True,
                ))
            with self.factory.begin() as session:
                repository = SqlCommunicationRepository(session)
                stored_fingerprint, restored = repository.latest_communicated_project_snapshot(week_start=WEEK)
                self.assertEqual(stored_fingerprint, fingerprint)
                self.assertEqual(restored, historical)
                self.assertIsNone(restored.recipient_policy_context)
                message = repository.get_batch(batch_id).messages[0]
                self.assertEqual(message.to_emails, (fixtures.mailbox("historical"),))
                self.assertEqual(message.cc_emails, (fixtures.mailbox("original-cc"),))
                self.assertEqual((message.subject, message.body, message.body_html), ("Original subject", "Original body", None))
            with self.factory() as session:
                row = session.get(CommunicationBatchRow, batch_id)
                self.assertEqual((row.project_snapshot_json, row.snapshot_fingerprint), (payload, fingerprint))
                message = session.scalar(select(CommunicationMessageRow).where(CommunicationMessageRow.batch_id == batch_id))
                self.assertEqual(message.to_recipients_json, json.dumps([{"email": fixtures.mailbox("historical")}]))
                self.assertEqual(message.cc_recipients_json, json.dumps([{"email": fixtures.mailbox("original-cc")}]))
                self.assertEqual((message.subject, message.body, message.body_html), ("Original subject", "Original body", None))

    def test_corrupt_v5_or_erased_capability_is_not_a_baseline(self):
        with self.factory.begin() as session:
            old = historical_fixture(4)
            self._seed_communicated(session, serialize_project_projection(old), GOLDEN[4][1])
            broken_id = self._seed_communicated(session, self.payload, self.fingerprint, offset=1)
        invalid = []
        for mutate in (
            lambda value: value.pop("recipient_policy_context"),
            lambda value: value["recipient_policy_context"]["messages"][0]["mandatory"].pop(),
            lambda value: value["recipient_policy_context"]["messages"][0]["mandatory"][0].update(fulfilled_by_existing=False),
        ):
            value = json.loads(self.payload)
            mutate(value)
            invalid.append(json.dumps(value))
        erased = json.loads(self.payload)
        erased.pop("recipient_policy_context")
        erased["request_provenance_version"] = 4
        erased_json = json.dumps(erased)
        self.assertEqual(deserialize_project_projection(erased_json).request_provenance_version, 4)
        invalid.append(erased_json)
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.factory.begin() as session:
                    session.get(CommunicationBatchRow, broken_id).project_snapshot_json = payload
                with self.factory() as session, self.assertRaises(ValueError):
                    SqlCommunicationRepository(session).latest_communicated_project_snapshot(week_start=WEEK)
        with self.factory.begin() as session:
            row = session.get(CommunicationBatchRow, broken_id)
            row.project_snapshot_json = self.payload
            row.snapshot_fingerprint = "0" * 64
        with self.factory() as session, self.assertRaises(ValueError):
            SqlCommunicationRepository(session).latest_communicated_project_snapshot(week_start=WEEK)
        with self.factory.begin() as session:
            row = session.get(CommunicationBatchRow, broken_id)
            row.snapshot_fingerprint = self.fingerprint
            row.week_start = WEEK + timedelta(days=7)
        with self.factory() as session, self.assertRaises(ValueError):
            SqlCommunicationRepository(session).latest_communicated_project_snapshot(week_start=WEEK + timedelta(days=7))

    def test_prepared_write_rejects_mismatched_snapshot_claim_before_mutation(self):
        for week, fingerprint in ((WEEK, "0" * 64), (WEEK + timedelta(days=7), self.fingerprint)):
            with self.factory.begin() as session:
                with self.assertRaises(ValueError):
                    SqlCommunicationRepository(session).create_project_prepared_batch(
                        week_start=week, kind=KIND, fingerprint=fingerprint,
                        actor_name="Demo operator", messages=((self.draft, True),), snapshot=self.snapshot,
                    )
                self.assertEqual(session.scalars(select(CommunicationBatchRow)).all(), [])
