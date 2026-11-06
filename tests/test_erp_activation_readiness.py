from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import delete

from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    Base,
    ERP_SYNC_CONTROL_ROW_ID,
    ErpSyncBaseline,
    ErpSyncControl,
    create_session_factory,
    create_sql_engine,
)
from app.server.erp_sync_evidence import (
    ErpSyncEvidenceService,
    canonical_json_bytes,
)
from app.server.runtime import ServerSettings
from tools.check_erp_activation_readiness import (
    ErpActivationReadinessError,
    check_activation_readiness,
)


class ErpActivationReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "readiness.db"
        self.database_url = f"sqlite+pysqlite:///{self.path.as_posix()}"
        self.key = Fernet.generate_key().decode("ascii")
        self.settings = ServerSettings.from_environment(
            {
                "RESOURCEPLANNER_DATABASE_URL": self.database_url,
                "RESOURCEPLANNER_ACUMATICA_BASE_URL": "https://erp.example.test",
                "RESOURCEPLANNER_ACUMATICA_USERNAME": "dedicated-service",
                "RESOURCEPLANNER_ACUMATICA_PASSWORD": "not-a-real-secret",
                "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY": self.key,
                "RESOURCEPLANNER_ERP_SYNC_ENABLED": "false",
            }
        )
        self.engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(
                ErpSyncControl(
                    id=ERP_SYNC_CONTROL_ROW_ID,
                    generation=0,
                    protocol_version=1,
                    config_fingerprint=self.settings.erp_sync.config_fingerprint,
                )
            )
        evidence = ErpSyncEvidenceService(
            self.factory,
            cipher=FernetSecretCipher(self.key),
            settings=self.settings.erp_sync_evidence,
            control_settings=self.settings.erp_sync,
        )
        with self.factory.begin() as session:
            policy = evidence.policy_fingerprint_in_session(session)
            now = datetime.now(timezone.utc)
            for source_type in ("PROJECTS", "TASKS"):
                session.add(
                    ErpSyncBaseline(
                        source_type=source_type,
                        profile_fingerprint=policy,
                        source_fingerprint=("a" if source_type == "PROJECTS" else "b") * 64,
                        row_count=1,
                        encrypted_project_counts=None,
                        approved_by="Readiness test",
                        approval_reason="Approved test baseline",
                        approved_at=now,
                    )
                )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def _check(
        self,
        settings: ServerSettings | None = None,
        *,
        activation: bool = False,
        database_name: str = "sqlite",
    ):
        with patch(
            "tools.check_erp_activation_readiness.check_database_preflight",
            return_value={
                "database": database_name,
                "alembic_revision": "test-head",
                "connectivity": "ok",
            },
        ):
            return check_activation_readiness(
                settings or self.settings,
                activation_mode=activation,
                dedicated_account_confirmed=True,
            )

    def test_pre_activation_gate_is_ready_while_automatic_sync_remains_disabled(self) -> None:
        result = self._check()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["mode"], "pre_activation")
        self.assertFalse(result["automatic_enabled"])
        self.assertEqual(result["baselines"], ["PROJECTS", "TASKS"])

    def test_activation_mode_requires_explicit_enabled_configuration(self) -> None:
        with self.assertRaisesRegex(
            ErpActivationReadinessError,
            "RESOURCEPLANNER_ERP_SYNC_ENABLED=true",
        ):
            self._check(activation=True)

    def test_activation_gate_rejects_sqlite_even_when_enabled(self) -> None:
        enabled = ServerSettings.from_environment(
            {
                "RESOURCEPLANNER_DATABASE_URL": self.database_url,
                "RESOURCEPLANNER_ACUMATICA_BASE_URL": "https://erp.example.test",
                "RESOURCEPLANNER_ACUMATICA_USERNAME": "dedicated-service",
                "RESOURCEPLANNER_ACUMATICA_PASSWORD": "not-a-real-secret",
                "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY": self.key,
                "RESOURCEPLANNER_ERP_SYNC_ENABLED": "true",
            }
        )
        with self.assertRaisesRegex(
            ErpActivationReadinessError,
            "exige SQL Server",
        ) as raised:
            self._check(enabled, activation=True)
        self.assertEqual(
            raised.exception.code,
            "erp_sync_activation_requires_sqlserver",
        )

    def test_activation_gate_accepts_enabled_configuration_after_mssql_preflight(self) -> None:
        enabled = ServerSettings.from_environment(
            {
                "RESOURCEPLANNER_DATABASE_URL": self.database_url,
                "RESOURCEPLANNER_ACUMATICA_BASE_URL": "https://erp.example.test",
                "RESOURCEPLANNER_ACUMATICA_USERNAME": "dedicated-service",
                "RESOURCEPLANNER_ACUMATICA_PASSWORD": "not-a-real-secret",
                "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY": self.key,
                "RESOURCEPLANNER_ERP_SYNC_ENABLED": "true",
            }
        )
        self.assertEqual(
            enabled.erp_sync.config_fingerprint,
            self.settings.erp_sync.config_fingerprint,
        )
        result = self._check(enabled, activation=True, database_name="mssql")
        self.assertEqual(result["mode"], "activation")
        self.assertTrue(result["automatic_enabled"])

    def test_dedicated_account_confirmation_is_never_inferred_from_credentials(self) -> None:
        with patch(
            "tools.check_erp_activation_readiness.check_database_preflight",
            return_value={
                "database": "sqlite",
                "alembic_revision": "test-head",
                "connectivity": "ok",
            },
        ):
            with self.assertRaisesRegex(
                ErpActivationReadinessError,
                "compte de service ERP dédié",
            ):
                check_activation_readiness(
                    self.settings,
                    activation_mode=False,
                    dedicated_account_confirmed=False,
                )

    def test_missing_baseline_fails_closed(self) -> None:
        with self.factory.begin() as session:
            session.execute(
                delete(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == "TASKS"
                )
            )
        with self.assertRaisesRegex(
            ErpActivationReadinessError,
            "Baseline ERP approuvée absente",
        ):
            self._check()

    def test_missing_crypto_key_fails_closed_without_exposing_secret_material(self) -> None:
        no_crypto = ServerSettings.from_environment(
            {
                "RESOURCEPLANNER_DATABASE_URL": self.database_url,
                "RESOURCEPLANNER_ACUMATICA_BASE_URL": "https://erp.example.test",
                "RESOURCEPLANNER_ACUMATICA_USERNAME": "dedicated-service",
                "RESOURCEPLANNER_ACUMATICA_PASSWORD": "not-a-real-secret",
                "RESOURCEPLANNER_ERP_SYNC_ENABLED": "false",
            }
        )
        with self.assertRaisesRegex(
            ErpActivationReadinessError,
            "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY",
        ):
            self._check(no_crypto)

    def test_wrong_key_blocks_when_existing_baseline_counts_are_unreadable(self) -> None:
        writer = ErpSyncEvidenceService(
            self.factory,
            cipher=FernetSecretCipher(self.key),
            settings=self.settings.erp_sync_evidence,
            control_settings=self.settings.erp_sync,
        )
        with self.factory.begin() as session:
            for baseline in session.query(ErpSyncBaseline).all():
                baseline.encrypted_project_counts = writer._encrypt_bytes(
                    canonical_json_bytes({"ERP-1": 1})
                )

        wrong_key = Fernet.generate_key().decode("ascii")
        incompatible = ServerSettings.from_environment(
            {
                "RESOURCEPLANNER_DATABASE_URL": self.database_url,
                "RESOURCEPLANNER_ACUMATICA_BASE_URL": "https://erp.example.test",
                "RESOURCEPLANNER_ACUMATICA_USERNAME": "dedicated-service",
                "RESOURCEPLANNER_ACUMATICA_PASSWORD": "not-a-real-secret",
                "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY": wrong_key,
                "RESOURCEPLANNER_ERP_SYNC_ENABLED": "false",
            }
        )
        with self.assertRaises(ErpActivationReadinessError) as raised:
            self._check(incompatible)
        self.assertEqual(
            raised.exception.code,
            "erp_sync_baseline_integrity_invalid",
        )

    def test_valid_key_validates_existing_baseline_counts(self) -> None:
        writer = ErpSyncEvidenceService(
            self.factory,
            cipher=FernetSecretCipher(self.key),
            settings=self.settings.erp_sync_evidence,
            control_settings=self.settings.erp_sync,
        )
        with self.factory.begin() as session:
            for baseline in session.query(ErpSyncBaseline).all():
                baseline.encrypted_project_counts = writer._encrypt_bytes(
                    canonical_json_bytes({"ERP-1": 1})
                )
        result = self._check()
        self.assertEqual(result["status"], "ready")

    def test_protocol_mismatch_fails_closed(self) -> None:
        with self.factory.begin() as session:
            session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID).protocol_version = 999
        with self.assertRaisesRegex(ErpActivationReadinessError, "protocole ERP SQL"):
            self._check()

    def test_config_fingerprint_mismatch_fails_closed(self) -> None:
        with self.factory.begin() as session:
            session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID).config_fingerprint = "0" * 64
        with self.assertRaisesRegex(ErpActivationReadinessError, "fingerprint ERP SQL"):
            self._check()


if __name__ == "__main__":
    unittest.main()
