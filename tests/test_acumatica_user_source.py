from __future__ import annotations

from pathlib import Path
import unittest

import httpx

from app.infrastructure.acumatica import (
    ODataUserSource,
    ODataUserSourceSettings,
    parse_rp_users_feed,
)


FIXTURE = Path("tests/fixtures/acumatica/rp_users_atom.xml")


class ODataUserSourceTests(unittest.TestCase):
    def test_contract_fixture_preserves_user_and_employee_states_separately(self) -> None:
        rows = parse_rp_users_feed(FIXTURE.read_bytes())

        alice = next(row for row in rows if row.user_id == "AEXEMPLE")
        self.assertEqual(alice.employee_id, "EXEMALI")
        self.assertTrue(alice.user_active)
        self.assertEqual(alice.employee_status, "Actif")

        user_inactive = next(row for row in rows if row.user_id == "c.test")
        self.assertFalse(user_inactive.user_active)
        self.assertEqual(user_inactive.employee_status, "Actif")

        employee_inactive = next(row for row in rows if row.user_id == "DMARTIN")
        self.assertTrue(employee_inactive.user_active)
        self.assertEqual(employee_inactive.employee_status, "Inactif")

    def test_source_reads_full_directory_without_employee_status_filter(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=FIXTURE.read_bytes())

        source = ODataUserSource(
            ODataUserSourceSettings(
                base_url="https://erp.example.test/Instance",
                username="odata-user",
                credential="dummy-passphrase",
                page_size=100,
            ),
            transport=httpx.MockTransport(handler),
        )

        rows = source.list_users()

        self.assertEqual(rows[0].user_id, "AEXEMPLE")
        self.assertEqual(rows[0].employee_external_id, "EXEMALI")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertIsNone(request.url.params.get("$filter"))
        self.assertEqual(request.url.params.get("$orderby"), "UserID asc")
        self.assertEqual(request.url.params.get("$top"), "100")
        self.assertEqual(request.url.params.get("$skip"), "0")
        self.assertTrue(request.headers.get("Authorization", "").startswith("Basic "))
        self.assertNotIn("odata-user", repr(source._settings))
        self.assertNotIn("dummy-passphrase", repr(source._settings))


if __name__ == "__main__":
    unittest.main()
