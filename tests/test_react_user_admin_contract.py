from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class ReactUserAdminContractTests(unittest.TestCase):
    def test_shell_exposes_user_admin_only_with_backend_permission(self) -> None:
        source = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('permission: "admin_users"', source)
        self.assertIn('<UserAdminPage />', source)
        self.assertIn('can("admin_users")', source)

    def test_user_admin_uses_backend_role_catalog_without_role_permission_matrix(self) -> None:
        source = (FRONTEND / "UserAdminPage.tsx").read_text(encoding="utf-8")
        api = (FRONTEND / "userAdminApi.ts").read_text(encoding="utf-8")

        self.assertIn("getAdminRoleCatalog", source)
        self.assertIn("roleCatalog.map", source)
        self.assertIn("definition.permissions", source)
        self.assertNotIn("ROLE_PERMISSIONS", source)
        self.assertNotIn("manage_demands:", source)
        self.assertIn('"/api/v1/admin/users/roles"', api)
        self.assertIn('"/api/v1/admin/users"', api)
        self.assertIn('import { csrfHeaders } from "./csrf"', api)
        self.assertIn("...csrfHeaders()", api)
        self.assertIn('credentials: "include"', api)
        self.assertIn('method: "PATCH"', api)
        self.assertIn("business_contact_id", api)
        self.assertIn("app_user_id", api)
        self.assertIn("erp_user_id", api)
        self.assertIn("oidc_state: OidcState", api)
        self.assertIn("issuer: string | null", api)
        self.assertIn("subject: string | null", api)
        self.assertIn("phone", api)
        self.assertIn("Téléphone", source)
        self.assertIn("profils métier", source)
        self.assertIn("Compte ·", source)
        self.assertIn("OIDC ·", source)
        self.assertIn("app-user-identity-status", source)
        self.assertIn("accountStateLabel", source)
        self.assertIn("oidcStateLabel", source)
        self.assertNotIn("issuer === null", source)
        self.assertNotIn("subject === null", source)

    def test_identity_coordinates_are_create_only_and_self_lockout_is_visible(self) -> None:
        source = (FRONTEND / "UserAdminPage.tsx").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("readOnly={!creating}"), 2)
        self.assertIn("principal?.local_user_id === selected.user_id", source)
        self.assertIn('definition.role === "ADMIN"', source)
        self.assertIn("disabled={isSelf}", source)

    def test_long_user_lists_scroll_without_hiding_editors(self) -> None:
        css = (FRONTEND / "user-admin.css").read_text(encoding="utf-8")

        self.assertIn(".user-admin-user-list", css)
        self.assertIn("max-height: min(58vh, 620px)", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn(".erp-user-directory-table-wrap", css)
        self.assertIn("overflow: auto", css)
        self.assertIn(".erp-user-directory-table th", css)
        self.assertIn("position: sticky", css)


if __name__ == "__main__":
    unittest.main()
