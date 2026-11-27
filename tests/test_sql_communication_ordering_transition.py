"""P01-A disclosed forward-order transition through existing workflow services."""
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import create_session_factory, create_sql_engine
from app.infrastructure.sql.communication_models import (
    CommunicationBatchRow, CommunicationDeliveryRow, CommunicationMessageRow,
)
from app.infrastructure.sql.project_communication_repository import SqlProjectCommunicationRepository
from app.server import create_api_app
from tests import test_sql_project_communication_projection as persisted
from tests.test_sql_communication_query_batching import add_project_diagnostics, reverse_asset_codes

PATH = "/api/v1/communications/project-batches"
WEEK = "2026-09-23"


class SqlCommunicationOrderingTransitionTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixture = persisted.SqlProjectCommunicationProjectionTests()
        self.url = fixture._database(directory.name)
        fixture._add_second_project(self.url)
        self.excluded = fixture._add_r29_project(self.url)
        self.engine = create_sql_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.factory = create_session_factory(self.engine)
        self.graph = persisted.FakeProjectDraftTransport()
        self.smtp = persisted.FakeSmtpDeliveryClient()
        app = create_api_app(self.url, auth_resolver=persisted.TEST_ADMIN_AUTH_RESOLVER,
                             communication_transport=self.graph, smtp_client=self.smtp)
        self.client = self.enterContext(TestClient(app))
        response = self.client.put("/api/v1/admin/settings/smtp", json={
            "host": "smtp.example.invalid", "port": 587, "security": "STARTTLS",
            "username": None, "password": None, "clear_password": False,
            "from_email": "planning"+chr(64)+persisted.TEST_DOMAIN,
            "from_name": "Demo", "reply_to": None, "timeout_seconds": 20, "enabled": True,
        })
        self.assertEqual(response.status_code, 200, response.text)

    def seed_diagnostics(self):
        with self.factory.begin() as session:
            add_project_diagnostics(session, self.excluded, "P01-X")

    @contextmanager
    def prior_order(self):
        # Simulate the disclosed historical representation at the actual
        # persisted assignment boundary; all business facts and workflow code
        # remain genuine, and no qualification/blocking rule is bypassed.
        original = SqlProjectCommunicationRepository.list_assignments
        def read(instance, **kwargs):
            return tuple(reverse_asset_codes(row) for row in original(instance, **kwargs))
        with patch.object(SqlProjectCommunicationRepository, "list_assignments", read):
            yield

    def preview(self, *, delta=False):
        url = "/api/v1/communications/project-preview?week_start="+WEEK
        if delta:
            url += "&mode=project_planning_change"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, preview, *, status=201):
        excluded = next(row for row in preview["drafts"] if row["project_id"] == self.excluded)
        response = self.client.post(PATH, json={
            "week_start": WEEK, "expected_fingerprint": preview["snapshot_fingerprint"],
            "reviews": [{"message_key": excluded["message_key"], "include": False}],
        })
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def action(self, batch_id, action, *, status=200):
        result = self.client.post(f"{PATH}/{batch_id}/{action}")
        self.assertEqual(result.status_code, status, result.text)
        if status == 409:
            self.assertEqual(result.json()["error"]["code"], "project_communication_batch_stale")
        return result.json()

    def frozen(self, batch_id):
        with self.factory() as session:
            batch = session.get(CommunicationBatchRow, batch_id)
            messages = session.scalars(select(CommunicationMessageRow)
                .where(CommunicationMessageRow.batch_id == batch_id)
                .order_by(CommunicationMessageRow.id)).all()
            deliveries = session.scalars(select(CommunicationDeliveryRow)
                .where(CommunicationDeliveryRow.batch_id == batch_id)
                .order_by(CommunicationDeliveryRow.id)).all()
            def data(row):
                return tuple(getattr(row, column.key) for column in row.__table__.columns)
            return data(batch), tuple(data(row) for row in messages), tuple(data(row) for row in deliveries)

    def test_p01a_old_preview_prepared_approved_and_manual_actions_reject_stale_order(self):
        self.seed_diagnostics()
        with self.prior_order():
            prior = self.preview()
            batch = self.prepare(prior)
        current = self.preview()
        self.assertNotEqual(prior["snapshot_fingerprint"], current["snapshot_fingerprint"])
        blocked = next(row for row in current["drafts"] if row["project_id"] == self.excluded)
        self.assertFalse(blocked["approvable"])
        self.assertEqual(self.prepare(prior, status=409)["error"]["code"],
                         "project_communication_preview_stale")
        before = self.frozen(batch["id"])
        self.action(batch["id"], "approve", status=409)
        self.assertEqual(self.frozen(batch["id"]), before)
        with self.prior_order():
            self.action(batch["id"], "approve")
        approved = self.frozen(batch["id"])
        for action in ("create-drafts", "send-smtp", "mark-communicated"):
            with self.subTest(action=action):
                self.action(batch["id"], action, status=409)
                self.assertEqual(self.frozen(batch["id"]), approved)
        self.assertEqual(self.graph.create_calls, 0)
        self.assertEqual(self.smtp.attempt_subjects, [])

    def test_p01a_excluded_project_stale_partial_smtp_and_immutable_graph_download(self):
        # Establish a real communicated baseline before the affected project
        # gains blocking diagnostics. No fabricated baseline or approval bypass.
        baseline_preview = self.preview()
        baseline = self.prepare(baseline_preview)
        self.action(baseline["id"], "approve")
        self.action(baseline["id"], "mark-communicated")
        baseline_frozen = self.frozen(baseline["id"])
        self.seed_diagnostics()
        with self.prior_order():
            old_preview = self.preview()
            batch = self.prepare(old_preview)
            self.action(batch["id"], "approve")
            self.action(batch["id"], "create-drafts")
            download_before = self.client.get(f"{PATH}/{batch['id']}/draft-download")
            self.assertEqual(download_before.status_code, 200, download_before.text)
            partial = self.action(batch["id"], "send-smtp")
        self.assertEqual(partial["status"], "APPROVED")
        def deliveries(payload):
            return {row["project_id"]: [(d["status"],d["attempt_count"]) for d in row["deliveries"]
                        if d["provider"]=="SMTP"] for row in payload["messages"]}
        self.assertEqual(deliveries(partial), {"P1":[("SENT",1)], "P2":[("FAILED",1)], self.excluded:[]})
        self.assertEqual(self.graph.create_calls, 1)
        frozen = self.frozen(batch["id"])
        self.assertNotEqual(self.preview()["snapshot_fingerprint"], old_preview["snapshot_fingerprint"])
        self.action(batch["id"], "send-smtp", status=409)
        self.assertEqual(self.frozen(batch["id"]), frozen)
        self.assertEqual(len(self.smtp.attempt_subjects), 2)
        download_after = self.client.get(f"{PATH}/{batch['id']}/draft-download")
        self.assertEqual(download_after.status_code, 200, download_after.text)
        # ZIP timestamps and generated MIME boundaries are not persisted.
        # Compare recipients, subject and decoded text/HTML content.
        def files(response):
            with ZipFile(BytesIO(response.content)) as archive:
                result = {}
                for name in archive.namelist():
                    message = BytesParser(policy=policy.default).parsebytes(archive.read(name))
                    result[name] = (str(message["To"]), str(message["Cc"]), str(message["Subject"]),
                                    tuple((part.get_content_type(), part.get_content())
                                          for part in message.walk() if not part.is_multipart()))
                return result
        self.assertEqual(files(download_after), files(download_before))
        self.assertEqual(self.graph.create_calls, 1)
        self.assertEqual(self.frozen(baseline["id"]), baseline_frozen)
        self.assertEqual(self.preview(delta=True)["baseline_fingerprint"],
                         baseline_preview["snapshot_fingerprint"])
        with self.prior_order():
            completed = self.action(batch["id"], "send-smtp")
        self.assertEqual(completed["status"], "COMMUNICATED")
        self.assertEqual(deliveries(completed), {"P1":[("SENT",1)], "P2":[("SENT",2)], self.excluded:[]})
        self.assertEqual(sum("1000" in subject for subject in self.smtp.attempt_subjects),1)
        self.assertEqual(sum("2000" in subject for subject in self.smtp.attempt_subjects),2)
        self.assertEqual(self.frozen(baseline["id"]), baseline_frozen)
        # The legitimately communicated batch retains its old full snapshot.
        self.assertEqual(self.preview(delta=True)["baseline_fingerprint"], old_preview["snapshot_fingerprint"])
        with self.prior_order():
            self.assertEqual(self.preview(delta=True)["drafts"], [])
