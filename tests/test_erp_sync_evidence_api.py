from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.application.erp_sync_evidence import ErpSyncEvidenceSettings
from app.application.erp_sync_scheduler import ErpSyncSchedulerSettings
from app.application.security import AuthPrincipal, PERMISSION_SYNC_PROJECTS
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    Base,
    ErpSyncControl,
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
