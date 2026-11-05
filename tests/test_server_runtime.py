from __future__ import annotations

import unittest

from cryptography.fernet import Fernet
from unittest.mock import patch

from app.application.security import ROLE_ADMIN, ROLE_COORDINATOR
from app.server.runtime import (
    ACTOR_NAME_ENV,
    ACUMATICA_BASE_URL_ENV,
    ACUMATICA_PASSWORD_ENV,
    ACUMATICA_USERNAME_ENV,
    ALLOW_LOCAL_AUTH_NETWORK_ENV,
    API_DOCS_ENABLED_ENV,
    AUTH_MODE_ENV,
    BREAK_GLASS_ENABLED_ENV,
    CONFIG_ENCRYPTION_KEY_ENV,
    DATABASE_URL_ENV,
    DEV_USER_SWITCHER_ENV,
    ERP_SYNC_CATCHUP_HOURS_ENV,
    ERP_SYNC_ENABLED_ENV,
    ERP_SYNC_EVIDENCE_BUDGET_MIB_ENV,
    ERP_SYNC_FAILURE_RETENTION_DAYS_ENV,
    ERP_SYNC_HISTORY_DAYS_ENV,
    ERP_SYNC_MAX_EVIDENCE_MIB_ENV,
    ERP_SYNC_MIN_PROJECTS_ENV,
    ERP_SYNC_MIN_SOURCE_RATIO_ENV,
    ERP_SYNC_MIN_TASK_ROWS_ENV,
    ERP_SYNC_TIME_ENV,
    ERP_SYNC_TIMEZONE_ENV,
    HOST_ENV,
    LOCAL_AUTH_NAME_ENV,
    LOCAL_AUTH_ROLES_ENV,
    LOG_LEVEL_ENV,
    OIDC_CLIENT_ID_ENV,
    OIDC_DISCOVERY_URL_ENV,
    OIDC_REDIRECT_URI_ENV,
    OIDC_SCOPES_ENV,
    OIDC_SECURE_COOKIE_ENV,
    PORT_ENV,
    ServerConfigurationError,
    ServerSettings,
    create_configured_app,
    main,
    run_server,
)


class ServerRuntimeTests(unittest.TestCase):
    def test_database_url_is_required(self) -> None:
        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment({})
        self.assertIn(DATABASE_URL_ENV, str(caught.exception))

    def test_defaults_are_safe_for_local_startup(self) -> None:
        settings = ServerSettings.from_environment(
            {DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:"}
        )
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.log_level, "info")
        self.assertEqual(settings.actor_name, "api")
        self.assertEqual(settings.auth_mode, "local")
        self.assertFalse(settings.dev_user_switcher)
        self.assertTrue(settings.api_docs_enabled)
        assert settings.auth_principal is not None
        self.assertEqual(settings.auth_principal.auth_mode, "local")
        self.assertEqual(settings.auth_principal.roles, (ROLE_ADMIN,))

    def test_erp_scheduler_defaults_are_disabled_and_toronto_midnight(self) -> None:
        settings = ServerSettings.from_environment(
            {DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:"}
        )
        self.assertFalse(settings.erp_sync.enabled)
        self.assertEqual(settings.erp_sync.local_time.strftime("%H:%M"), "00:00")
        self.assertEqual(settings.erp_sync.timezone_name, "America/Toronto")
        self.assertEqual(settings.erp_sync.catchup_hours, 4)

    def test_erp_sync_evidence_defaults_match_accepted_contract(self) -> None:
        settings = ServerSettings.from_environment(
            {DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:"}
        )
        evidence = settings.erp_sync_evidence
        self.assertEqual(evidence.min_source_ratio, 0.50)
        self.assertEqual(evidence.min_projects, 1)
        self.assertEqual(evidence.min_task_rows, 1)
        self.assertEqual(evidence.max_evidence_mib, 256)
        self.assertEqual(evidence.evidence_budget_mib, 2048)
        self.assertEqual(evidence.failure_retention_days, 14)
        self.assertEqual(evidence.history_days, 90)

    def test_erp_sync_evidence_environment_is_parsed_and_invalid_values_fail_closed(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                ERP_SYNC_MIN_SOURCE_RATIO_ENV: "0.75",
                ERP_SYNC_MIN_PROJECTS_ENV: "2",
                ERP_SYNC_MIN_TASK_ROWS_ENV: "3",
                ERP_SYNC_MAX_EVIDENCE_MIB_ENV: "64",
                ERP_SYNC_EVIDENCE_BUDGET_MIB_ENV: "512",
                ERP_SYNC_FAILURE_RETENTION_DAYS_ENV: "21",
                ERP_SYNC_HISTORY_DAYS_ENV: "120",
            }
        )
        evidence = settings.erp_sync_evidence
        self.assertEqual(evidence.min_source_ratio, 0.75)
        self.assertEqual(evidence.min_projects, 2)
        self.assertEqual(evidence.min_task_rows, 3)
        self.assertEqual(evidence.max_evidence_mib, 64)
        self.assertEqual(evidence.evidence_budget_mib, 512)
        self.assertEqual(evidence.failure_retention_days, 21)
        self.assertEqual(evidence.history_days, 120)

        invalid_cases = (
            {ERP_SYNC_MIN_SOURCE_RATIO_ENV: "0"},
            {ERP_SYNC_MIN_SOURCE_RATIO_ENV: "1.1"},
            {ERP_SYNC_MAX_EVIDENCE_MIB_ENV: "15"},
            {
                ERP_SYNC_MAX_EVIDENCE_MIB_ENV: "256",
                ERP_SYNC_EVIDENCE_BUDGET_MIB_ENV: "128",
            },
            {ERP_SYNC_FAILURE_RETENTION_DAYS_ENV: "91"},
            {ERP_SYNC_HISTORY_DAYS_ENV: "29"},
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(ServerConfigurationError):
                    ServerSettings.from_environment(
                        {
                            DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                            **values,
                        }
                    )

    def test_erp_scheduler_environment_is_parsed_and_invalid_timezone_fails_closed(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                ERP_SYNC_ENABLED_ENV: "true",
                ERP_SYNC_TIME_ENV: "01:30",
                ERP_SYNC_TIMEZONE_ENV: "America/Toronto",
                ERP_SYNC_CATCHUP_HOURS_ENV: "3",
            }
        )
        self.assertTrue(settings.erp_sync.enabled)
        self.assertEqual(settings.erp_sync.local_time.strftime("%H:%M"), "01:30")
        self.assertEqual(settings.erp_sync.catchup_hours, 3)

        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    ERP_SYNC_TIMEZONE_ENV: "Invalid/Zone",
                }
            )
        self.assertIn("Fuseau IANA", str(caught.exception))

    def test_erp_scheduler_catchup_above_twelve_hours_fails_closed(self) -> None:
        with self.assertRaises(ServerConfigurationError):
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    ERP_SYNC_CATCHUP_HOURS_ENV: "13",
                }
            )

    def test_erp_scheduler_source_identity_excludes_credentials_and_query_secrets(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                ACUMATICA_BASE_URL_ENV: (
                    "https://"
                    + "embedded-user"
                    + ":"
                    + "embedded-secret"
                    + chr(64)
                    + "ERP.EXAMPLE.test/Instance?token=hidden"
                ),
                ACUMATICA_USERNAME_ENV: "configured-user",
                ACUMATICA_PASSWORD_ENV: "configured-secret",
            }
        )
        identity = settings.erp_sync.source_identity
        self.assertIn("https://erp.example.test/Instance", identity)
        self.assertNotIn("embedded-user", identity)
        self.assertNotIn("embedded-secret", identity)
        self.assertNotIn("token", identity)
        self.assertNotIn("configured-user", identity)
        self.assertNotIn("configured-secret", identity)

    def test_erp_scheduler_invalid_time_fails_closed(self) -> None:
        with self.assertRaises(ServerConfigurationError):
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    ERP_SYNC_TIME_ENV: "25:90",
                }
            )

    def test_environment_overrides_server_values_and_local_roles(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                HOST_ENV: "0.0.0.0",
                PORT_ENV: "8123",
                LOG_LEVEL_ENV: "WARNING",
                ACTOR_NAME_ENV: "local-admin",
                LOCAL_AUTH_NAME_ENV: "Coordination locale",
                LOCAL_AUTH_ROLES_ENV: ROLE_COORDINATOR,
                ALLOW_LOCAL_AUTH_NETWORK_ENV: "true",
            }
        )
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 8123)
        self.assertEqual(settings.log_level, "warning")
        self.assertEqual(settings.actor_name, "local-admin")
        assert settings.auth_principal is not None
        self.assertEqual(settings.auth_principal.display_name, "Coordination locale")
        self.assertEqual(settings.auth_principal.roles, (ROLE_COORDINATOR,))

    def test_local_auth_refuses_network_bind_without_explicit_override(self) -> None:
        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    HOST_ENV: "0.0.0.0",
                }
            )
        self.assertIn(ALLOW_LOCAL_AUTH_NETWORK_ENV, str(caught.exception))

    def test_dev_user_switcher_can_be_enabled_only_for_local_auth(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                DEV_USER_SWITCHER_ENV: "true",
            }
        )
        self.assertTrue(settings.dev_user_switcher)

        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    AUTH_MODE_ENV: "oidc",
                    DEV_USER_SWITCHER_ENV: "true",
                    OIDC_DISCOVERY_URL_ENV: "https://identity.example.invalid/.well-known/openid-configuration",
                    OIDC_CLIENT_ID_ENV: "resourceplanner",
                    OIDC_REDIRECT_URI_ENV: "https://planner.example.invalid/api/v1/auth/callback",
                }
            )
        self.assertIn(DEV_USER_SWITCHER_ENV, str(caught.exception))

    def test_break_glass_is_disabled_by_default_and_restricted_to_oidc(self) -> None:
        local = ServerSettings.from_environment(
            {DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:"}
        )
        self.assertFalse(local.break_glass_enabled)

        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    BREAK_GLASS_ENABLED_ENV: "true",
                }
            )
        self.assertIn(BREAK_GLASS_ENABLED_ENV, str(caught.exception))

    def test_break_glass_can_be_enabled_with_oidc_without_provider_contact(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                AUTH_MODE_ENV: "oidc",
                BREAK_GLASS_ENABLED_ENV: "true",
                OIDC_DISCOVERY_URL_ENV: "https://identity.example.invalid/.well-known/openid-configuration",
                OIDC_CLIENT_ID_ENV: "resourceplanner",
                OIDC_REDIRECT_URI_ENV: "https://planner.example.invalid/api/v1/auth/callback",
            }
        )
        self.assertTrue(settings.break_glass_enabled)

    def test_oidc_mode_requires_complete_configuration(self) -> None:
        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    AUTH_MODE_ENV: "oidc",
                    OIDC_CLIENT_ID_ENV: "client-only",
                }
            )
        self.assertIn(OIDC_DISCOVERY_URL_ENV, str(caught.exception))
        self.assertIn(OIDC_REDIRECT_URI_ENV, str(caught.exception))

    def test_oidc_mode_builds_separate_identity_configuration(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                AUTH_MODE_ENV: "oidc",
                OIDC_DISCOVERY_URL_ENV: "https://identity.example.invalid/.well-known/openid-configuration",
                OIDC_CLIENT_ID_ENV: "resourceplanner",
                OIDC_REDIRECT_URI_ENV: "https://planner.example.invalid/api/v1/auth/callback",
                OIDC_SCOPES_ENV: "openid profile email",
                OIDC_SECURE_COOKIE_ENV: "true",
                HOST_ENV: "0.0.0.0",
            }
        )

        self.assertEqual(settings.auth_mode, "oidc")
        self.assertIsNone(settings.auth_principal)
        self.assertIsNotNone(settings.oidc)
        assert settings.oidc is not None
        self.assertEqual(settings.oidc.client_id, "resourceplanner")
        self.assertEqual(settings.oidc.scopes, ("openid", "profile", "email"))
        self.assertTrue(settings.oidc_secure_cookie)
        self.assertFalse(settings.api_docs_enabled)

    def test_api_docs_can_be_explicitly_enabled_for_oidc(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                AUTH_MODE_ENV: "oidc",
                OIDC_DISCOVERY_URL_ENV: "https://identity.example.invalid/.well-known/openid-configuration",
                OIDC_CLIENT_ID_ENV: "resourceplanner",
                OIDC_REDIRECT_URI_ENV: "https://planner.example.invalid/api/v1/auth/callback",
                API_DOCS_ENABLED_ENV: "true",
            }
        )
        self.assertTrue(settings.api_docs_enabled)

    def test_oidc_scopes_must_include_openid(self) -> None:
        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    AUTH_MODE_ENV: "oidc",
                    OIDC_DISCOVERY_URL_ENV: "https://identity.example.invalid/.well-known/openid-configuration",
                    OIDC_CLIENT_ID_ENV: "resourceplanner",
                    OIDC_REDIRECT_URI_ENV: "https://planner.example.invalid/api/v1/auth/callback",
                    OIDC_SCOPES_ENV: "profile email",
                }
            )
        self.assertIn("openid", str(caught.exception))

    def test_invalid_auth_mode_is_rejected(self) -> None:
        with self.assertRaises(ServerConfigurationError) as caught:
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    AUTH_MODE_ENV: "header",
                }
            )
        self.assertIn(AUTH_MODE_ENV, str(caught.exception))

    def test_invalid_local_role_is_rejected(self) -> None:
        with self.assertRaises(ServerConfigurationError):
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    LOCAL_AUTH_ROLES_ENV: "SUPERUSER",
                }
            )

    def test_invalid_port_is_rejected(self) -> None:
        for value in ("abc", "0", "65536"):
            with self.subTest(value=value):
                with self.assertRaises(ServerConfigurationError):
                    ServerSettings.from_environment(
                        {
                            DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                            PORT_ENV: value,
                        }
                    )

    def test_invalid_log_level_is_rejected(self) -> None:
        with self.assertRaises(ServerConfigurationError):
            ServerSettings.from_environment(
                {
                    DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                    LOG_LEVEL_ENV: "verbose",
                }
            )

    def test_config_encryption_key_is_loaded_but_hidden_from_repr(self) -> None:
        key_value = Fernet.generate_key().decode("ascii")
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                CONFIG_ENCRYPTION_KEY_ENV: key_value,
            }
        )

        self.assertEqual(settings.config_encryption_key, key_value)
        self.assertNotIn(key_value, repr(settings))

    def test_invalid_config_encryption_key_fails_before_server_start(self) -> None:
        settings = ServerSettings.from_environment(
            {
                DATABASE_URL_ENV: "sqlite+pysqlite:///:memory:",
                CONFIG_ENCRYPTION_KEY_ENV: "invalid-fernet-key",
            }
        )

        with self.assertRaises(ServerConfigurationError) as caught:
            create_configured_app(settings)

        self.assertIn(CONFIG_ENCRYPTION_KEY_ENV, str(caught.exception))

    def test_database_url_is_not_exposed_by_repr(self) -> None:
        secret_url = "mssql+pyodbc://user:secret@example/database"
        settings = ServerSettings(database_url=secret_url)
        self.assertNotIn(secret_url, repr(settings))
        self.assertNotIn("secret", repr(settings))

    def test_run_server_passes_explicit_uvicorn_configuration(self) -> None:
        settings = ServerSettings(
            database_url="sqlite+pysqlite:///:memory:",
            host="0.0.0.0",
            port=8123,
            log_level="debug",
            actor_name="test",
        )
        fake_app = object()
        with (
            patch("app.server.runtime.create_configured_app", return_value=fake_app) as create_app,
            patch("app.server.runtime.uvicorn.run") as uvicorn_run,
        ):
            run_server(settings)

        create_app.assert_called_once_with(settings)
        uvicorn_run.assert_called_once_with(
            fake_app,
            host="0.0.0.0",
            port=8123,
            log_level="debug",
            reload=False,
        )

    def test_main_reports_configuration_error_without_starting_uvicorn(self) -> None:
        with patch(
            "app.server.runtime.run_server",
            side_effect=ServerConfigurationError("base absente"),
        ):
            with self.assertRaises(SystemExit) as caught:
                main()

        self.assertIn("Configuration serveur invalide", str(caught.exception))
        self.assertIn("base absente", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
