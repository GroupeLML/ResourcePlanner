from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.server.erp_sync_evidence import ErpSyncEvidenceSettings
from app.application.erp_sync_scheduler import ErpSyncSchedulerSettings
from app.application.security import AuthPrincipal, PERMISSION_SYNC_PROJECTS
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    Base,
    ErpSyncControl,
    ErpSyncPlausibilityReviewDecision,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.erp_sync_control_repository import (
    SqlErpSyncControlRepository,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


SYNC_ONLY = static_auth_resolver(
    AuthPrincipal(
        local_user_id="SYNC-ONLY",
        issuer="urn:resourceplanner:test",
        subject="sync-only",
        display_name="Synchronisateur",
        email=None,
        employee_external_id=None,
        roles=(),
        permissions=(PERMISSION_SYNC_PROJECTS,),
        auth_mode="test",
    )
)


class ErpSyncEvidenceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "evidence-api.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with transactional_session(factory) as session:
            session.add(ErpSyncControl(id="ERP_PROJECTS_TASKS", generation=0))
        engine.dispose()
        self.settings = ErpSyncSchedulerSettings(enabled=False)
        self.cipher = FernetSecretCipher(Fernet.generate_key().decode("ascii"))
        self.evidence_settings = ErpSyncEvidenceSettings(
            max_evidence_mib=16,
            evidence_budget_mib=32,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _app(self, resolver):
        return create_api_app(
            self.database_url,
            auth_resolver=resolver,
            smtp_cipher=self.cipher,
            erp_sync_settings=self.settings,
            erp_sync_evidence_settings=self.evidence_settings,
        )

    def _sealed_manifest(self, app) -> tuple[str, str]:
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="admin",
            idempotency_key="evidence-api",
            settings=self.settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="admin",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        manifest = app.state.erp_sync_evidence_service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=self.settings.config_fingerprint,
            payload={
                "records": [
                    {
                        "external_id": "101",
                        "number": "P-1",
                        "name": "PRIVATE-SOURCE-MARKER",
                        "status": "Actif",
                    }
                ],
                "page_evidence": {"complete": True, "pages": 1, "rows": 1},
            },
            row_count=1,
        )
        return attempt.id, manifest.manifest_id

    def test_failed_bootstrap_candidates_reuse_exact_retained_manifest_identity(self) -> None:
        app = self._app(TEST_ADMIN_AUTH_RESOLVER)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        occurrence = control.accept_manual(
            intent="PROJECTS_AND_ACTIVE_TASKS",
            actor_name="admin",
            idempotency_key="bootstrap-candidates",
            settings=self.settings,
        )
        attempt = control.claim(
            occurrence_id=occurrence.id,
            trigger="MANUAL",
            actor_name="admin",
            settings=self.settings,
        )
        self.assertIsNotNone(attempt)
        service = app.state.erp_sync_evidence_service
        profile = service.bind_attempt_policy(
            attempt_id=attempt.id,
            generation=attempt.generation,
        )
        projects = service.capture_source(
            attempt_id=attempt.id,
            source_type="PROJECTS",
            profile_fingerprint=profile,
            payload={
                "records": [
                    {
                        "external_id": "BOOT-1",
                        "number": "BOOT-1",
                        "name": "PRIVATE-BOOTSTRAP-PAYLOAD",
                        "status": "Actif",
                    }
                ],
                "page_evidence": {"complete": True, "pages": 1, "rows": 1},
            },
            row_count=1,
            generation=attempt.generation,
        )
        tasks = service.capture_source(
            attempt_id=attempt.id,
            source_type="TASKS",
            profile_fingerprint=profile,
            payload={
                "portfolio": {
                    "source_rows": 1,
                    "source_pages": 1,
                    "snapshots": [],
                },
                "page_evidence": {"complete": True, "pages": 1, "rows": 1},
            },
            row_count=1,
            generation=attempt.generation,
        )
        self.assertTrue(
            control.finish(
                attempt_id=attempt.id,
                generation=attempt.generation,
                status="FAILED",
                phase="FAILED",
                error_code="erp_sync_source_plausibility_failed",
                diagnostic="bootstrap requires baseline",
                settings=self.settings,
            )
        )
        service.finalize_attempt(attempt_id=attempt.id, successful=False)

        with TestClient(app) as client:
            candidates = client.get(
                "/api/v1/integrations/acumatica/erp-sync/bootstrap-baseline-candidates"
            )
            self.assertEqual(candidates.status_code, 200, candidates.text)
            payload = candidates.json()
            self.assertEqual(payload["attempt_id"], attempt.id)
            self.assertEqual(
                {row["manifest_id"] for row in payload["sources"]},
                {projects.manifest_id, tasks.manifest_id},
            )
            self.assertNotIn("PRIVATE-BOOTSTRAP-PAYLOAD", candidates.text)

            approved = client.post(
                "/api/v1/integrations/acumatica/erp-sync/evidence/"
                + projects.manifest_id
                + "/approve-baseline",
                json={"reason": "Bootstrap PROJECTS vérifié"},
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            after = client.get(
                "/api/v1/integrations/acumatica/erp-sync/bootstrap-baseline-candidates"
            )

        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(
            [row["manifest_id"] for row in after.json()["sources"]],
            [tasks.manifest_id],
        )
        terminal = control.get_attempt(attempt.id)
        self.assertEqual(terminal.status, "FAILED")
        self.assertEqual(
            terminal.error_code,
            "erp_sync_source_plausibility_failed",
        )

    def test_sensitive_approval_requires_admin_settings_in_addition_to_sync_projects(self) -> None:
        app = self._app(SYNC_ONLY)
        attempt_id, manifest_id = self._sealed_manifest(app)

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/evidence/{manifest_id}/approve-baseline",
                json={"reason": "reviewed"},
            )
            evidence = client.get(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/evidence"
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "erp_sync_sensitive_authority_required",
        )
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertNotIn("PRIVATE-SOURCE-MARKER", evidence.text)

    def test_sync_only_operator_cannot_approve_exception_or_compensate(self) -> None:
        app = self._app(SYNC_ONLY)
        attempt_id, manifest_id = self._sealed_manifest(app)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        attempt = control.get_attempt(attempt_id)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )

        with TestClient(app) as client:
            exception = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/plausibility-exceptions",
                json={
                    "manifest_id": manifest_id,
                    "threshold_scope": "GLOBAL",
                    "reason": "unauthorized",
                },
            )
            preview = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/compensation/preview",
                json={"reason": "unauthorized"},
            )
            confirm = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/compensation/confirm",
                headers={"Idempotency-Key": "unauthorized-confirm"},
                json={"preview_hash": "0" * 64, "reason": "unauthorized"},
            )

        for response in (exception, preview, confirm):
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "erp_sync_sensitive_authority_required",
            )

    def test_plausibility_review_decline_requires_sensitive_authority_and_preserves_state(self) -> None:
        app = self._app(SYNC_ONLY)
        attempt_id, _manifest_id = self._sealed_manifest(app)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        attempt = control.get_attempt(attempt_id)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/plausibility-decline",
                json={"reason": "Source temporaire incorrecte"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "erp_sync_sensitive_authority_required",
        )
        unchanged = control.get_attempt(attempt_id)
        self.assertEqual(unchanged.status, "PENDING")
        self.assertEqual(
            unchanged.phase,
            "PLAUSIBILITY_REVIEW_REQUIRED",
        )

    def test_admin_can_decline_plausibility_review_and_actor_is_audited(self) -> None:
        app = self._app(TEST_ADMIN_AUTH_RESOLVER)
        attempt_id, _manifest_id = self._sealed_manifest(app)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        attempt = control.get_attempt(attempt_id)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/plausibility-decline",
                json={"reason": "Source obsolète après correction amont"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_decision"], "DECLINED")
        terminal = control.get_attempt(attempt_id)
        self.assertEqual(terminal.status, "FAILED")
        self.assertEqual(
            terminal.error_code,
            "erp_sync_plausibility_review_declined",
        )
        with factory() as session:
            decisions = tuple(
                session.scalars(
                    select(ErpSyncPlausibilityReviewDecision).where(
                        ErpSyncPlausibilityReviewDecision.attempt_id
                        == attempt_id
                    )
                )
            )
        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0].actor_name,
            "Administrateur de test explicite",
        )

    def test_phase2_read_projections_are_safe_for_sync_operator(self) -> None:
        app = self._app(SYNC_ONLY)
        attempt_id, _manifest_id = self._sealed_manifest(app)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        attempt = control.get_attempt(attempt_id)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )

        with TestClient(app) as client:
            baselines = client.get(
                "/api/v1/integrations/acumatica/erp-sync/baselines"
            )
            review = client.get(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/plausibility-review"
            )

        self.assertEqual(baselines.status_code, 200, baselines.text)
        self.assertTrue(baselines.json()["available"])
        self.assertIsNone(baselines.json()["sources"]["PROJECTS"])
        self.assertIsNone(baselines.json()["sources"]["TASKS"])

        self.assertEqual(review.status_code, 200, review.text)
        payload = review.json()
        self.assertEqual(payload["attempt_id"], attempt_id)
        self.assertEqual(payload["phase"], "PLAUSIBILITY_REVIEW_REQUIRED")
        self.assertEqual(payload["allowed_exception_scopes"], [])
        self.assertEqual(payload["sources"][0]["source_type"], "PROJECTS")
        self.assertIn(
            "erp_sync_baseline_required",
            payload["sources"][0]["diagnostics"],
        )
        self.assertNotIn("PRIVATE-SOURCE-MARKER", review.text)

    def test_sensitive_payload_cannot_supply_authoritative_actor(self) -> None:
        app = self._app(TEST_ADMIN_AUTH_RESOLVER)
        attempt_id, manifest_id = self._sealed_manifest(app)
        factory = app.state.erp_sync_orchestrator._session_factory
        control = SqlErpSyncControlRepository(factory)
        attempt = control.get_attempt(attempt_id)
        self.assertTrue(
            control.pause_for_review(
                attempt_id=attempt.id,
                generation=attempt.generation,
                settings=self.settings,
            )
        )

        with TestClient(app) as client:
            baseline = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/evidence/{manifest_id}/approve-baseline",
                json={"reason": "reviewed", "actor_name": "SYSTEM"},
            )
            decline = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/runs/{attempt_id}/plausibility-decline",
                json={"reason": "reviewed", "actor": "scheduler"},
            )

        self.assertEqual(baseline.status_code, 422, baseline.text)
        self.assertEqual(decline.status_code, 422, decline.text)
        unchanged = control.get_attempt(attempt_id)
        self.assertEqual(unchanged.status, "PENDING")
        self.assertEqual(unchanged.phase, "PLAUSIBILITY_REVIEW_REQUIRED")

    def test_admin_can_approve_sealed_baseline_and_actor_is_authenticated_identity(self) -> None:
        app = self._app(TEST_ADMIN_AUTH_RESOLVER)
        _attempt_id, manifest_id = self._sealed_manifest(app)

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/integrations/acumatica/erp-sync/evidence/{manifest_id}/approve-baseline",
                json={"reason": "Baseline complet revu"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        summary = app.state.erp_sync_evidence_service.get_baseline_summary(
            source_type="PROJECTS",
            profile_fingerprint=self.settings.config_fingerprint,
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary["approved_by"], "Administrateur de test explicite")
        self.assertEqual(summary["approval_reason"], "Baseline complet revu")


if __name__ == "__main__":
    unittest.main()
