import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openPlanning(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  return { context, page };
}

test("859 — besoins approuvés maître-détail, recherche, filtres et invalidation", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  let requestAReserved = false;
  let requestAVisible = true;
  let mutationCount = 0;
  try {
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const body = await response.json() as Record<string, any>;
      const start = String(body.start);
      const end = String(body.end);
      const requestA = {
        requirement_id: "REQ-859-A",
        origin: "REQUEST",
        request_id: "WR-859-A",
        demand_number: "DMO-2026-0112",
        project_id: "P-5189",
        project_number: "5189",
        source_request_line_id: "LINE-859-A",
        source_period_id: null,
        approval_revision_id: "REV-859",
        approved_entry_key: "asset:859:a",
        resource_requirement_id: null,
        segment_reference: null,
        shift_id: null,
        context_resource_id: null,
        slot_index: 0,
        asset_type_id: "TYPE-859-A",
        asset_type_code: "CN40",
        asset_type_label: "Nacelle 40'",
        start_date: start,
        end_date: end,
        usage_hours: 8,
        status: requestAReserved ? "Réservé" : "À affecter",
        resource_period_mode: null,
        effective_state: null,
        allocation_id: requestAReserved ? "ALLOC-859-A" : null,
        asset_id: requestAReserved ? "ASSET-859-A" : null,
        asset_code: requestAReserved ? "CN40-01" : null,
        asset_label: requestAReserved ? "Nacelle 40 principale" : null,
        allocation_start_date: requestAReserved ? start : null,
        allocation_end_date: requestAReserved ? end : null,
        allocation_locked: false,
        operator_resource_id: null,
        operator_resource_name: null,
        qualification_state: "SATISFIED",
        required_competency_ids: [],
        required_competency_names: [],
        eligible_asset_ids: ["ASSET-859-A"],
      };
      const requestB = {
        ...requestA,
        requirement_id: "REQ-859-B",
        request_id: "WR-859-B",
        demand_number: "DMO-2026-0118",
        project_id: "P-5231",
        project_number: "5231",
        source_request_line_id: "LINE-859-B",
        approved_entry_key: "asset:859:b",
        asset_type_id: "TYPE-859-B",
        asset_type_code: "CAM",
        asset_type_label: "Camion",
        status: "Réservé",
        allocation_id: "ALLOC-859-B",
        asset_id: "ASSET-859-B",
        asset_code: "CAM-01",
        asset_label: "Camion réservé",
        allocation_start_date: start,
        allocation_end_date: end,
        eligible_asset_ids: ["ASSET-859-B"],
      };
      const projectDirect = {
        ...requestA,
        requirement_id: "REQ-859-C",
        origin: "PROJECT_DIRECT",
        request_id: null,
        demand_number: null,
        project_id: "P-6000",
        project_number: "6000",
        source_request_line_id: null,
        approval_revision_id: null,
        approved_entry_key: null,
        asset_type_id: "TYPE-859-C",
        asset_type_code: "GEN",
        asset_type_label: "Génératrice",
        usage_hours: null,
        eligible_asset_ids: [],
      };
      await route.fulfill({
        response,
        json: {
          ...body,
          planning_version: Number(body.planning_version ?? 0) + mutationCount,
          demands: [
            ...((body.demands || []) as Array<Record<string, unknown>>),
            {
              number: "DMO-2026-0112",
              status: "En planification",
              project_number: "5189",
              project_name: "École Saint-Jérôme",
            },
            {
              number: "DMO-2026-0118",
              status: "En planification",
              project_number: "5231",
              project_name: "Centre industriel",
            },
          ],
          asset_types: [
            { id: "TYPE-859-A", code: "CN40", label: "Nacelle 40'", category: "VEHICLE", occupancy_policy: "EXCLUSIVE_DAY", active: true },
            { id: "TYPE-859-B", code: "CAM", label: "Camion", category: "VEHICLE", occupancy_policy: "EXCLUSIVE_DAY", active: true },
            { id: "TYPE-859-C", code: "GEN", label: "Génératrice", category: "VEHICLE", occupancy_policy: "EXCLUSIVE_DAY", active: true },
          ],
          assets: [
            { id: "ASSET-859-A", code: "CN40-01", label: "Nacelle 40 principale", asset_type_id: "TYPE-859-A", active: true, visible_in_unit_occupancy: true },
            { id: "ASSET-859-B", code: "CAM-01", label: "Camion réservé", asset_type_id: "TYPE-859-B", active: true, visible_in_unit_occupancy: true },
          ],
          asset_requirements: [
            ...(requestAVisible ? [requestA] : []),
            requestB,
            projectDirect,
          ],
          asset_allocations: [],
          asset_unavailability: [],
          asset_capacity: [],
          asset_diagnostics: [],
        },
      });
    });

    await page.route("**/api/v1/assets/requirements/REQ-859-A/reservation", async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      const payload = route.request().postDataJSON() as { asset_id: string | null };
      requestAReserved = Boolean(payload.asset_id);
      mutationCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          planning_version: mutationCount + 1,
          allocation_id: requestAReserved ? "ALLOC-859-A" : null,
          changed: true,
        }),
      });
    });

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();

    const panel = page.getByTestId("approved-asset-requirements");
    const master = panel.getByTestId("approved-asset-master-list");
    const detail = panel.getByTestId("approved-asset-detail");
    const search = panel.getByRole("searchbox", { name: "Rechercher les besoins approuvés" });
    const unreservedFilter = panel.getByRole("button", { name: /À réserver \(/ });
    const reservedFilter = panel.getByRole("button", { name: /Réservé \(/ });

    await expect(detail).toContainText("Aucun besoin d'actif sélectionné");
    await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toContainText("5189");
    await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toContainText("DMO-2026-0112");
    await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toContainText("CN40 — Nacelle 40'");
    await expect(master.locator('[data-requirement-id="REQ-859-C"]')).toContainText("6000");
    await expect(master.locator('[data-requirement-id="REQ-859-C"]')).not.toContainText("DMO");

    await search.fill("ecole saint-jerome");
    await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toHaveCount(1);
    await expect(master.locator('[data-requirement-id="REQ-859-B"]')).toHaveCount(0);
    await expect(unreservedFilter).toHaveText("À réserver (1)");
    await expect(reservedFilter).toHaveText("Réservé (0)");

    for (const query of ["DMO-2026-0112", "CN40", "nacelle 40"]) {
      await search.fill(query);
      await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toHaveCount(1);
    }

    await search.fill("aucun-resultat-859");
    await expect(master).toContainText("Aucun besoin ne correspond à la recherche ou au filtre.");
    await search.fill("");

    await unreservedFilter.click();
    await expect(unreservedFilter).toHaveAttribute("aria-pressed", "true");
    await expect(reservedFilter).toHaveAttribute("aria-pressed", "false");
    await expect(master.locator('[data-requirement-id="REQ-859-B"]')).toHaveCount(0);
    await unreservedFilter.click();
    await expect(unreservedFilter).toHaveAttribute("aria-pressed", "false");
    await reservedFilter.click();
    await expect(reservedFilter).toHaveAttribute("aria-pressed", "true");
    await expect(unreservedFilter).toHaveAttribute("aria-pressed", "false");
    await reservedFilter.click();

    const rowA = master.locator('[data-requirement-id="REQ-859-A"]');
    await rowA.focus();
    await rowA.press("Enter");
    await expect(rowA).toHaveAttribute("aria-selected", "true");
    await expect(detail.locator('[data-requirement-id="REQ-859-A"]')).toBeVisible();

    await search.fill("5231");
    await expect(detail).toContainText("Aucun besoin d'actif sélectionné");
    await search.fill("");

    await rowA.click();
    await unreservedFilter.click();
    const detailCard = detail.locator('[data-requirement-id="REQ-859-A"]');
    await detailCard.getByRole("combobox", { name: /Unité pour Nacelle 40/ }).selectOption("ASSET-859-A");
    await detailCard.getByRole("button", { name: "Enregistrer la réservation" }).click();
    await expect(detail).toContainText("Aucun besoin d'actif sélectionné");
    await expect(master.locator('[data-requirement-id="REQ-859-A"]')).toHaveCount(0);
    await expect(unreservedFilter).toHaveText("À réserver (1)");
    await expect(reservedFilter).toHaveText("Réservé (2)");

    await unreservedFilter.click();
    await master.locator('[data-requirement-id="REQ-859-A"]').click();
    await expect(detail.locator('[data-requirement-id="REQ-859-A"]')).toBeVisible();
    requestAVisible = false;
    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await expect(page.getByTestId("approved-asset-detail")).toContainText("Aucun besoin d'actif sélectionné");

    await page.setViewportSize({ width: 600, height: 900 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  } finally {
    await context.close();
  }
});
