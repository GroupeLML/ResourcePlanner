from __future__ import annotations

from decimal import Decimal
import unittest
from xml.sax.saxutils import escape

import httpx

from app.application import ApplicationOperationError
from app.infrastructure.acumatica import (
    ODataProjectTaskSource,
    ODataProjectTaskSourceSettings,
    parse_rp_project_tasks_feed,
)


def _entry(
    *,
    project_erp_id: int,
    project_number: str,
    task_id: int,
    task_code: str,
    label: str,
    account_group: str = "DEPMO",
    budget_amount: str | None = "100.00",
    budget_actual: str | None = "25.00",
    status: str = "Actif",
    cost_code: str | None = "0000",
    inventory_id: str | None = "<N/A>",
) -> str:
    def prop(name: str, value: str | None, edm_type: str | None = None) -> str:
        type_attr = f' m:type="{edm_type}"' if edm_type else ""
        if value is None:
            return f'<d:{name} m:null="true"{type_attr} />'
        return f"<d:{name}{type_attr}>{escape(value)}</d:{name}>"

    return f"""<entry>
  <category term="PX.Data.RP_ProjectTasks" />
  <content type="application/xml">
    <m:properties>
      {prop("ProjetID", str(project_erp_id), "Edm.Int32")}
      {prop("ProjectID", project_number)}
      {prop("ProjectID_2", str(project_erp_id), "Edm.Int32")}
      {prop("TaskID", str(task_id), "Edm.Int32")}
      {prop("TaskCD", task_code)}
      {prop("TaskDescription", label)}
      {prop("Status", status)}
      {prop("AccountGroup", account_group)}
      {prop("BudgetAmount", budget_amount, "Edm.Decimal")}
      {prop("BudgetActual", budget_actual, "Edm.Decimal")}
      {prop("CostCode", cost_code)}
      {prop("InventoryID", inventory_id)}
    </m:properties>
  </content>
</entry>"""


def _feed(*entries: str) -> bytes:
    body = "".join(entries)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <title>RP_ProjectTasks</title>
  {body}
</feed>""".encode("utf-8")


class ODataProjectTaskSourceTests(unittest.TestCase):
    def test_parser_separates_numeric_project_identity_from_padded_business_number(self) -> None:
        payload = _feed(
            _entry(
                project_erp_id=5469,
                project_number="5118      ",
                task_id=42,
                task_code=" 216 ",
                label="Programmation",
                budget_amount="1234.5678901234",
                budget_actual="-25.1250",
            )
        )

        rows = parse_rp_project_tasks_feed(payload)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].project_erp_id, 5469)
        self.assertEqual(rows[0].project_code, "5118")
        self.assertEqual(rows[0].task_id, 42)
        self.assertEqual(rows[0].task_code, "216")
        self.assertEqual(rows[0].budget_amount_cad, Decimal("1234.5678901234"))
        self.assertEqual(rows[0].budget_actual_cad, Decimal("-25.1250"))

    def test_targeted_source_uses_projetid_paginates_and_keeps_business_number(self) -> None:
        requests: list[httpx.Request] = []
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118      ",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="100.1250000000",
                    budget_actual="20.0000000000",
                    cost_code="C1",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118      ",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="-0.1250000000",
                    budget_actual="5.5000000000",
                    cost_code="C2",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118      ",
                    task_id=102,
                    task_code="999",
                    label="Matériel",
                    account_group="MAT",
                    budget_amount="900.00",
                ),
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=pages[request.url.params["$skip"]])

        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(handler),
        )

        snapshot = source.fetch_project_snapshot(
            project_external_id="5469",
            project_number="5118",
        )

        self.assertEqual(snapshot.project_number, "5118")
        self.assertEqual(snapshot.source_rows, 3)
        self.assertEqual(snapshot.rejected_rows, 1)
        self.assertEqual(len(snapshot.items), 1)
        task = snapshot.items[0]
        self.assertEqual(task.project_number, "5118")
        self.assertEqual(task.erp_task_id, "101")
        self.assertEqual(task.code, "216")
        self.assertEqual(task.account_group, "DEPMO")
        self.assertEqual(task.budget_amount_cad, Decimal("100.0000000000"))
        self.assertEqual(task.budget_actual_cad, Decimal("25.5000000000"))
        self.assertIsNone(task.cost_code)
        self.assertIsNone(task.budget_diagnostic)

        self.assertEqual(len(requests), 2)
        first = requests[0].url.params
        second = requests[1].url.params
        self.assertEqual(
            first["$filter"],
            "ProjetID eq 5469 and AccountGroup eq 'DEPMO'",
        )
        self.assertNotIn("'5469'", first["$filter"])
        self.assertEqual(first["$orderby"], "TaskID asc")
        self.assertEqual(first["$top"], "2")
        self.assertEqual(first["$skip"], "0")
        self.assertEqual(second["$skip"], "2")

    def test_portfolio_source_reads_depmo_once_and_partitions_active_targets(self) -> None:
        requests: list[httpx.Request] = []
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118      ",
                    task_id=101,
                    task_code="216",
                    label="Programmation projet 1",
                ),
                _entry(
                    project_erp_id=5470,
                    project_number="5119",
                    task_id=201,
                    task_code="216",
                    label="Programmation projet 2",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=9999,
                    project_number="OLD-1",
                    task_id=301,
                    task_code="216",
                    label="Projet hors cible",
                ),
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=pages[request.url.params["$skip"]])

        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(handler),
        )

        portfolio = source.fetch_project_snapshots(
            project_targets=(("5469", "5118"), ("5470", "5119")),
        )

        self.assertEqual(portfolio.source_pages, 2)
        self.assertEqual(portfolio.source_rows, 3)
        self.assertEqual(
            [snapshot.project_number for snapshot in portfolio.snapshots],
            ["5118", "5119"],
        )
        self.assertEqual(
            [snapshot.source_rows for snapshot in portfolio.snapshots],
            [1, 1],
        )
        self.assertEqual(
            [snapshot.items[0].erp_task_id for snapshot in portfolio.snapshots],
            ["101", "201"],
        )
        self.assertEqual(len(requests), 2)
        first = requests[0].url.params
        second = requests[1].url.params
        self.assertEqual(first["$filter"], "AccountGroup eq 'DEPMO'")
        self.assertEqual(first["$orderby"], "TaskID asc")
        self.assertEqual(first["$top"], "2")
        self.assertEqual(first["$skip"], "0")
        self.assertEqual(second["$skip"], "2")

    def test_adapter_rejects_other_projetid_non_depmo_and_code_mismatch(self) -> None:
        payload = _feed(
            _entry(
                project_erp_id=9999,
                project_number="5118",
                task_id=1,
                task_code="216",
                label="Autre projet technique",
            ),
            _entry(
                project_erp_id=5469,
                project_number="5118",
                task_id=2,
                task_code="117",
                label="Matériel",
                account_group="MAT",
            ),
            _entry(
                project_erp_id=5469,
                project_number="9999",
                task_id=3,
                task_code="117",
                label="Code projet incohérent",
            ),
            _entry(
                project_erp_id=5469,
                project_number="5118      ",
                task_id=4,
                task_code="117",
                label="Main-d'oeuvre",
                account_group=" DEPMO ",
            ),
        )

        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=10,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=payload, request=request)
            ),
        )

        snapshot = source.fetch_project_snapshot(
            project_external_id="5469",
            project_number="5118",
        )

        self.assertEqual(snapshot.source_rows, 4)
        self.assertEqual(snapshot.rejected_rows, 3)
        self.assertEqual([item.erp_task_id for item in snapshot.items], ["4"])
        self.assertEqual(snapshot.items[0].project_number, "5118")
        self.assertEqual(snapshot.items[0].account_group, "DEPMO")

    def test_zero_and_negative_budget_are_kept_with_diagnostic(self) -> None:
        payload = _feed(
            _entry(
                project_erp_id=1001,
                project_number="P-1",
                task_id=1,
                task_code="117",
                label="Installation",
                budget_amount="0.0000000000",
            ),
            _entry(
                project_erp_id=1001,
                project_number="P-1",
                task_id=2,
                task_code="216",
                label="Programmation",
                budget_amount="-250.5000000000",
            ),
        )
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=10,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=payload, request=request)
            ),
        )

        items = source.fetch_project_snapshot(
            project_external_id="1001",
            project_number="P-1",
        ).items

        self.assertEqual(items[0].budget_amount_cad, Decimal("0.0000000000"))
        self.assertEqual(items[0].budget_diagnostic, "budget_amount_zero")
        self.assertEqual(items[1].budget_amount_cad, Decimal("-250.5000000000"))
        self.assertEqual(items[1].budget_diagnostic, "budget_amount_negative")

    def test_invalid_decimal_is_reported_without_payload_value(self) -> None:
        marker = "sensitive-not-a-decimal"
        payload = _feed(
            _entry(
                project_erp_id=1001,
                project_number="P-1",
                task_id=1,
                task_code="216",
                label="Programmation",
                budget_amount=marker,
            )
        )
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=10,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=payload, request=request)
            ),
        )

        with self.assertRaises(ApplicationOperationError) as raised:
            source.fetch_project_snapshot(
                project_external_id="1001",
                project_number="P-1",
            )

        self.assertEqual(
            raised.exception.code,
            "acumatica_project_task_response_invalid",
        )
        self.assertEqual(raised.exception.context["reason"], "invalid_decimal")
        self.assertNotIn(marker, str(raised.exception.as_dict()))


if __name__ == "__main__":
    unittest.main()
