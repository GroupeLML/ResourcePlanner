from __future__ import annotations

import unittest

from app.server.runtime import (
    OIDC_AUTO_PROVISION_ENV,
    OIDC_CLAIM_DIAGNOSTICS_ENV,
    ServerSettings,
)


class ServerAutoProvisioningConfigTests(unittest.TestCase):
    def _environment(self) -> dict[str, str]:
        return {
            "RESOURCEPLANNER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RESOURCEPLANNER_AUTH_MODE": "oidc",
            "RESOURCEPLANNER_OIDC_DISCOVERY_URL": "https://identity.example.invalid/.well-known/openid-configuration",
            "RESOURCEPLANNER_OIDC_CLIENT_ID": "client-id",
            "RESOURCEPLANNER_OIDC_REDIRECT_URI": "https://planner.example.invalid/api/v1/auth/callback",
        }

    def test_auto_provisioning_is_disabled_by_default(self) -> None:
        settings = ServerSettings.from_environment(self._environment())
        self.assertFalse(settings.oidc_auto_provision)

    def test_auto_provisioning_requires_explicit_true(self) -> None:
        environment = self._environment()
        environment[OIDC_AUTO_PROVISION_ENV] = "true"
        settings = ServerSettings.from_environment(environment)
        self.assertTrue(settings.oidc_auto_provision)

    def test_claim_diagnostics_are_disabled_by_default(self) -> None:
        settings = ServerSettings.from_environment(self._environment())
        self.assertFalse(settings.oidc_claim_diagnostics)

    def test_claim_diagnostics_require_explicit_true(self) -> None:
        environment = self._environment()
        environment[OIDC_CLAIM_DIAGNOSTICS_ENV] = "true"
        settings = ServerSettings.from_environment(environment)
        self.assertTrue(settings.oidc_claim_diagnostics)


if __name__ == "__main__":
    unittest.main()
