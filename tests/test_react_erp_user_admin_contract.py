from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "src" / "ErpUserDirectoryPanel.tsx"
API = ROOT / "frontend" / "src" / "erpUserAdminApi.ts"
USER_PAGE = ROOT / "frontend" / "src" / "UserAdminPage.tsx"
IDENTITY = ROOT / "frontend" / "src" / "identityAdmin.ts"


class ReactErpUserAdminContractTests(unittest.TestCase):
    def test_admin_page_surfaces_distinct_erp_directory(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        user_page = USER_PAGE.read_text(encoding="utf-8")

        self.assertIn("Annuaire RP_Users", page)
        self.assertIn("UserID", page)
        self.assertIn("EmployeID / ressource", page)
        self.assertIn("ERP User", page)
        self.assertIn("ERP Employé", page)
        self.assertIn("Activation RessourcePlanner", page)
        identity = IDENTITY.read_text(encoding="utf-8")
        self.assertIn("<th>AppUser</th>", page)
        self.assertIn("<th>Compte</th>", page)
        self.assertIn("<th>Rôles</th>", page)
        self.assertIn("<th>OIDC</th>", page)
        self.assertIn('state === "linked"', identity)
        self.assertIn('return "Lié"', identity)
        self.assertIn('return "Conflit"', identity)
        self.assertIn('return "En attente de première connexion"', identity)
        self.assertIn("<ErpUserDirectoryPanel roleCatalog={roleCatalog}", user_page)

    def test_api_exposes_preprovisioned_app_user_without_oidc_coordinates(self) -> None:
        api = API.read_text(encoding="utf-8")
        page = PAGE.read_text(encoding="utf-8")

        self.assertIn('"/api/v1/admin/erp-users"', api)
        self.assertIn('"/api/v1/integrations/acumatica/users/sync"', api)
        self.assertIn('import { csrfHeaders } from "./csrf"', api)
        self.assertIn("...csrfHeaders()", api)
        self.assertIn('credentials: "include"', api)
        self.assertIn("app_user_id", api)
        self.assertIn("erp_user_id", api)
        self.assertIn("issuer: string | null", api)
        self.assertIn("subject: string | null", api)
        self.assertIn("active: boolean", api)
        self.assertIn("oidc_state: OidcState", api)
        self.assertIn("preferred_username → UserID", page)
        self.assertNotIn("issuer === null", page)
        self.assertNotIn("subject === null", page)
        self.assertIn("accountStateLabel", page)
        self.assertIn("oidcStateLabel", page)
        self.assertIn("Non provisionné", page)


if __name__ == "__main__":
    unittest.main()
