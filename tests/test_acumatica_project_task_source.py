from __future__ import annotations

from decimal import Decimal
import unittest
from xml.sax.saxutils import escape

import httpx

from app.application import ApplicationOperationError, TaskCatalogSyncService
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
                    label="Deuxième tâche valide",
                    account_group="DEPMO",
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
        self.assertEqual(snapshot.rejected_rows, 0)
        self.assertEqual(len(snapshot.items), 2)
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
        self.assertEqual(
            first["$orderby"],
            "TaskID asc, CostCode asc, InventoryID asc",
        )
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
        self.assertEqual(
            first["$orderby"],
            "ProjetID asc, TaskID asc, CostCode asc, InventoryID asc",
        )
        self.assertEqual(first["$top"], "2")
        self.assertEqual(first["$skip"], "0")
        self.assertEqual(second["$skip"], "2")

    def test_portfolio_attributes_project_code_corruption_to_only_that_project(self) -> None:
        payload = _feed(
            _entry(
                project_erp_id=5469,
                project_number="WRONG-5118",
                task_id=101,
                task_code="216",
                label="Corrupt A",
            ),
            _entry(
                project_erp_id=5470,
                project_number="5119",
                task_id=201,
                task_code="216",
                label="Valid B",
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
        portfolio = source.fetch_project_snapshots(
            project_targets=(("5469", "5118"), ("5470", "5119")),
        )
        by_project = {
            snapshot.project_number: snapshot
            for snapshot in portfolio.snapshots
        }
        self.assertEqual(
            by_project["5118"].hard_error_code,
            "acumatica_project_task_response_invalid",
        )
        self.assertEqual(
            by_project["5118"].hard_error_reason,
            "project_code_conflict",
        )
        self.assertEqual(by_project["5118"].items, ())
        self.assertIsNone(by_project["5119"].hard_error_code)
        self.assertEqual(
            [item.erp_task_id for item in by_project["5119"].items],
            ["201"],
        )

    def test_targeted_source_rejects_repeated_non_progressive_page(self) -> None:
        page = _feed(
            _entry(
                project_erp_id=5469,
                project_number="5118",
                task_id=101,
                task_code="216",
                label="Repeated",
            ),
            _entry(
                project_erp_id=5469,
                project_number="5118",
                task_id=102,
                task_code="217",
                label="Repeated 2",
            ),
        )
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=page, request=request)
            ),
        )

        with self.assertRaises(ApplicationOperationError) as raised:
            source.fetch_project_snapshot(
                project_external_id="5469",
                project_number="5118",
            )

        self.assertEqual(
            raised.exception.code,
            "acumatica_project_task_response_invalid",
        )
        self.assertEqual(
            raised.exception.context["reason"],
            "pagination_non_progressive",
        )

    def test_portfolio_source_rejects_repeated_non_progressive_page(self) -> None:
        page = _feed(
            _entry(
                project_erp_id=5469,
                project_number="5118",
                task_id=101,
                task_code="216",
                label="Repeated",
            ),
            _entry(
                project_erp_id=5470,
                project_number="5119",
                task_id=201,
                task_code="216",
                label="Repeated 2",
            ),
        )
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=page, request=request)
            ),
        )

        with self.assertRaises(ApplicationOperationError) as raised:
            source.fetch_project_snapshots(
                project_targets=(("5469", "5118"), ("5470", "5119")),
            )

        self.assertEqual(
            raised.exception.code,
            "acumatica_project_task_response_invalid",
        )
        self.assertEqual(
            raised.exception.context["reason"],
            "pagination_non_progressive",
        )

    def test_adapter_fails_closed_on_conflicting_project_identity(self) -> None:
        payload = _feed(
            _entry(
                project_erp_id=5469,
                project_number="9999",
                task_id=3,
                task_code="117",
                label="Code projet incohérent",
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
        with self.assertRaises(ApplicationOperationError) as raised:
            source.fetch_project_snapshot(
                project_external_id="5469",
                project_number="5118",
            )
        self.assertEqual(
            raised.exception.code,
            "acumatica_project_task_response_invalid",
        )
        self.assertEqual(
            raised.exception.context["reason"],
            "project_code_conflict",
        )

    def test_adapter_accepts_canonical_project_and_depmo_scope(self) -> None:
        payload = _feed(
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
        self.assertEqual(snapshot.source_rows, 1)
        self.assertEqual(snapshot.rejected_rows, 0)
        self.assertEqual([item.erp_task_id for item in snapshot.items], ["4"])

    def test_targeted_source_rejects_partial_page_overlap(self) -> None:
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="A",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=102,
                    task_code="217",
                    label="B",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="A",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=103,
                    task_code="218",
                    label="C",
                ),
            ),
        }
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=pages[request.url.params["$skip"]],
                    request=request,
                )
            ),
        )
        with self.assertRaises(ApplicationOperationError) as raised:
            source.fetch_project_snapshot(
                project_external_id="5469",
                project_number="5118",
            )
        self.assertEqual(
            raised.exception.context["reason"],
            "pagination_overlap",
        )


    def test_pagination_progress_rejects_changed_overlap_backward_and_partial_in_both_paths(self) -> None:
        scenarios = (
            (
                (
                    (101, "A", "100.00"),
                    (102, "B", "100.00"),
                ),
                (
                    (101, "A changed", "101.00"),
                    (103, "C", "100.00"),
                ),
            ),
            (
                (
                    (102, "B", "100.00"),
                    (103, "C", "100.00"),
                ),
                (
                    (101, "A", "100.00"),
                    (104, "D", "100.00"),
                ),
            ),
            (
                (
                    (101, "A", "100.00"),
                    (102, "B", "100.00"),
                ),
                (
                    (102, "B changed", "101.00"),
                    (103, "C", "100.00"),
                ),
            ),
        )

        for mode in ("targeted", "portfolio"):
            for first_rows, second_rows in scenarios:
                with self.subTest(mode=mode, second_first=second_rows[0][0]):
                    def page(rows):
                        return _feed(
                            *(
                                _entry(
                                    project_erp_id=5469,
                                    project_number="5118",
                                    task_id=task_id,
                                    task_code=str(task_id),
                                    label=label,
                                    budget_amount=amount,
                                )
                                for task_id, label, amount in rows
                            )
                        )

                    pages = {"0": page(first_rows), "2": page(second_rows)}
                    source = ODataProjectTaskSource(
                        ODataProjectTaskSourceSettings(
                            base_url="https://example.invalid",
                            page_size=2,
                        ),
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(
                                200,
                                content=pages[request.url.params["$skip"]],
                                request=request,
                            )
                        ),
                    )
                    with self.assertRaises(ApplicationOperationError) as raised:
                        if mode == "targeted":
                            source.fetch_project_snapshot(
                                project_external_id="5469",
                                project_number="5118",
                            )
                        else:
                            source.fetch_project_snapshots(
                                project_targets=(("5469", "5118"),),
                            )
                    self.assertIn(
                        raised.exception.context["reason"],
                        {"pagination_overlap", "pagination_non_progressive"},
                    )

    def test_pagination_progress_preserves_same_task_financial_aggregation_in_both_paths(self) -> None:
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="A",
                    budget_amount="100.00",
                    budget_actual="10.00",
                    cost_code="C1",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="A",
                    budget_amount="25.00",
                    budget_actual="5.00",
                    cost_code="C2",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=102,
                    task_code="217",
                    label="B",
                    budget_amount="1.00",
                ),
            ),
        }
        for mode in ("targeted", "portfolio"):
            with self.subTest(mode=mode):
                source = ODataProjectTaskSource(
                    ODataProjectTaskSourceSettings(
                        base_url="https://example.invalid",
                        page_size=2,
                    ),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            content=pages[request.url.params["$skip"]],
                            request=request,
                        )
                    ),
                )
                if mode == "targeted":
                    snapshot = source.fetch_project_snapshot(
                        project_external_id="5469",
                        project_number="5118",
                    )
                else:
                    portfolio = source.fetch_project_snapshots(
                        project_targets=(("5469", "5118"),),
                    )
                    snapshot = portfolio.snapshots[0]
                task = next(item for item in snapshot.items if item.erp_task_id == "101")
                self.assertEqual(task.budget_amount_cad, Decimal("125.00"))
                self.assertEqual(task.budget_actual_cad, Decimal("15.00"))

    def test_pagination_accepts_financial_group_split_at_page_boundary_in_both_paths(self) -> None:
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=100,
                    task_code="215",
                    label="Prelude",
                    budget_amount="1.00",
                    budget_actual="0.00",
                    cost_code="C0",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="100.00",
                    budget_actual="10.00",
                    cost_code="C1",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="25.00",
                    budget_actual="5.00",
                    cost_code="C2",
                ),
            ),
        }
        for mode in ("targeted", "portfolio"):
            with self.subTest(mode=mode):
                source = ODataProjectTaskSource(
                    ODataProjectTaskSourceSettings(
                        base_url="https://example.invalid",
                        page_size=2,
                    ),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            content=pages[request.url.params["$skip"]],
                            request=request,
                        )
                    ),
                )
                if mode == "targeted":
                    snapshot = source.fetch_project_snapshot(
                        project_external_id="5469",
                        project_number="5118",
                    )
                    self.assertEqual(snapshot.source_rows, 3)
                else:
                    portfolio = source.fetch_project_snapshots(
                        project_targets=(("5469", "5118"),),
                    )
                    self.assertEqual(portfolio.source_rows, 3)
                    snapshot = portfolio.snapshots[0]
                self.assertEqual(len(snapshot.items), 2)
                task = next(
                    item for item in snapshot.items
                    if item.erp_task_id == "101"
                )
                self.assertEqual(task.budget_amount_cad, Decimal("125.00"))
                self.assertEqual(task.budget_actual_cad, Decimal("15.00"))

    def test_pagination_accepts_one_task_spanning_several_pages_in_both_paths(self) -> None:
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=100,
                    task_code="215",
                    label="Prelude",
                    budget_amount="1.00",
                    cost_code="C0",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="10.00",
                    cost_code="C1",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="20.00",
                    cost_code="C2",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="30.00",
                    cost_code="C3",
                ),
            ),
            "4": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="40.00",
                    cost_code="C4",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=102,
                    task_code="217",
                    label="Suite",
                    budget_amount="2.00",
                    cost_code="C0",
                ),
            ),
            "6": _feed(),
        }
        for mode in ("targeted", "portfolio"):
            with self.subTest(mode=mode):
                source = ODataProjectTaskSource(
                    ODataProjectTaskSourceSettings(
                        base_url="https://example.invalid",
                        page_size=2,
                    ),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            content=pages[request.url.params["$skip"]],
                            request=request,
                        )
                    ),
                )
                if mode == "targeted":
                    snapshot = source.fetch_project_snapshot(
                        project_external_id="5469",
                        project_number="5118",
                    )
                else:
                    portfolio = source.fetch_project_snapshots(
                        project_targets=(("5469", "5118"),),
                    )
                    snapshot = portfolio.snapshots[0]
                    self.assertEqual(portfolio.source_rows, 6)
                self.assertEqual(snapshot.source_rows, 6)
                self.assertEqual(len(snapshot.items), 3)
                task = next(
                    item for item in snapshot.items
                    if item.erp_task_id == "101"
                )
                self.assertEqual(task.budget_amount_cad, Decimal("100.00"))

    def test_pagination_rejects_exact_and_changed_physical_row_replay_in_both_paths(self) -> None:
        for changed_amount in ("100.00", "101.00"):
            for mode in ("targeted", "portfolio"):
                with self.subTest(
                    mode=mode,
                    changed_amount=changed_amount,
                ):
                    pages = {
                        "0": _feed(
                            _entry(
                                project_erp_id=5469,
                                project_number="5118",
                                task_id=101,
                                task_code="216",
                                label="Programmation",
                                budget_amount="100.00",
                                cost_code="C1",
                                inventory_id="LABOUR",
                            )
                        ),
                        "1": _feed(
                            _entry(
                                project_erp_id=5469,
                                project_number="5118",
                                task_id=101,
                                task_code="216",
                                label="Programmation",
                                budget_amount=changed_amount,
                                cost_code="C1",
                                inventory_id="LABOUR",
                            )
                        ),
                    }
                    source = ODataProjectTaskSource(
                        ODataProjectTaskSourceSettings(
                            base_url="https://example.invalid",
                            page_size=1,
                        ),
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(
                                200,
                                content=pages[request.url.params["$skip"]],
                                request=request,
                            )
                        ),
                    )
                    with self.assertRaises(ApplicationOperationError) as raised:
                        if mode == "targeted":
                            source.fetch_project_snapshot(
                                project_external_id="5469",
                                project_number="5118",
                            )
                        else:
                            source.fetch_project_snapshots(
                                project_targets=(("5469", "5118"),),
                            )
                    self.assertEqual(
                        raised.exception.context["reason"],
                        "pagination_overlap",
                    )

    def test_pagination_rejects_repeated_page_backward_and_partial_overlap_in_both_paths(self) -> None:
        scenarios = {
            "repeated_page": (
                (
                    (100, "A", "C0"),
                    (101, "B", "C1"),
                ),
                (
                    (100, "A", "C0"),
                    (101, "B", "C1"),
                ),
            ),
            "backward": (
                (
                    (101, "B", "C1"),
                    (102, "C", "C0"),
                ),
                (
                    (100, "A", "C0"),
                ),
            ),
            "partial_overlap": (
                (
                    (100, "A", "C0"),
                    (101, "B", "C1"),
                ),
                (
                    (100, "A", "C0"),
                    (102, "C", "C0"),
                ),
            ),
        }
        for scenario, (first_rows, second_rows) in scenarios.items():
            for mode in ("targeted", "portfolio"):
                with self.subTest(scenario=scenario, mode=mode):
                    def page(rows):
                        return _feed(
                            *(
                                _entry(
                                    project_erp_id=5469,
                                    project_number="5118",
                                    task_id=task_id,
                                    task_code=str(task_id),
                                    label=label,
                                    budget_amount="100.00",
                                    cost_code=cost_code,
                                )
                                for task_id, label, cost_code in rows
                            )
                        )

                    pages = {"0": page(first_rows), "2": page(second_rows)}
                    source = ODataProjectTaskSource(
                        ODataProjectTaskSourceSettings(
                            base_url="https://example.invalid",
                            page_size=2,
                        ),
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(
                                200,
                                content=pages[request.url.params["$skip"]],
                                request=request,
                            )
                        ),
                    )
                    with self.assertRaises(ApplicationOperationError) as raised:
                        if mode == "targeted":
                            source.fetch_project_snapshot(
                                project_external_id="5469",
                                project_number="5118",
                            )
                        else:
                            source.fetch_project_snapshots(
                                project_targets=(("5469", "5118"),),
                            )
                    self.assertIn(
                        raised.exception.context["reason"],
                        {"pagination_overlap", "pagination_non_progressive"},
                    )

    def test_split_financial_group_reaches_application_sync_path(self) -> None:
        pages = {
            "0": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=100,
                    task_code="215",
                    label="Prelude",
                    budget_amount="1.00",
                    cost_code="C0",
                ),
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="100.00",
                    cost_code="C1",
                ),
            ),
            "2": _feed(
                _entry(
                    project_erp_id=5469,
                    project_number="5118",
                    task_id=101,
                    task_code="216",
                    label="Programmation",
                    budget_amount="25.00",
                    cost_code="C2",
                ),
            ),
        }
        source = ODataProjectTaskSource(
            ODataProjectTaskSourceSettings(
                base_url="https://example.invalid",
                page_size=2,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=pages[request.url.params["$skip"]],
                    request=request,
                )
            ),
        )

        class Repository:
            def __init__(self):
                self.items = []

            def upsert(self, item, *, source_observed_at=None):
                self.items.append(item)
                return "created"

        repository = Repository()
        result = TaskCatalogSyncService(
            source,
            repository,
        ).synchronize_project(
            project_external_id="5469",
            project_number="5118",
        )

        self.assertEqual(result.source_rows, 3)
        self.assertEqual(result.task_count, 2)
        self.assertEqual(result.created, 2)
        task = next(
            item for item in repository.items
            if item.erp_task_id == "101"
        )
        self.assertEqual(task.budget_amount_cad, Decimal("125.00"))

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
