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

test("858 — besoins approuvés filtrent les unités occupées sans fuite CONTINUOUS", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  let candidateRefreshes = 0;
  try {
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const body = await response.json() as Record<string, any>;
      const start = String(body.start);
      const approvedEndDate = new Date(`${start}T00:00:00Z`);
      approvedEndDate.setUTCDate(approvedEndDate.getUTCDate() + 2);
      const approvedEnd = approvedEndDate.toISOString().slice(0, 10);
      const baseRequirement = {
        origin: "REQUEST",
        request_id: "WR-858",
        project_id: "P-858",
        project_number: "8580",
        source_request_line_id: "LINE-858",
        source_period_id: null,
        approval_revision_id: "REV-858",
        approved_entry_key: "asset:858",
        resource_requirement_id: null,
        segment_reference: null,
        shift_id: null,
        context_resource_id: null,
        slot_index: 0,
        asset_type_id: "TYPE-858",
        asset_type_code: "VEH-858",
        asset_type_label: "Véhicule 858",
        start_date: start,
        end_date: approvedEnd,
        usage_hours: null,
        status: "À affecter",
        resource_period_mode: null,
        effective_state: null,
        allocation_id: null,
        asset_id: null,
        asset_code: null,
        asset_label: null,
        allocation_start_date: null,
        allocation_end_date: null,
        allocation_locked: false,
        operator_resource_id: null,
        operator_resource_name: null,
        qualification_state: "SATISFIED",
        required_competency_ids: [],
        required_competency_names: [],
      };
      await route.fulfill({
        response,
        json: {
          ...body,
          asset_types: [
            {
              id: "TYPE-858",
              code: "VEH-858",
              label: "Véhicule 858",
              category: "VEHICLE",
              occupancy_policy: "EXCLUSIVE_DAY",
              active: true,
              qualification_policy: "ANY_ASSIGNED_WORKFORCE",
              required_competency_ids: [],
              required_competency_names: [],
            },
            {
              id: "TYPE-ZERO-858",
              code: "ZERO-858",
              label: "Type sans unité",
              category: "VEHICLE",
              occupancy_policy: "EXCLUSIVE_DAY",
              active: true,
              qualification_policy: "ANY_ASSIGNED_WORKFORCE",
              required_competency_ids: [],
              required_competency_names: [],
            },
          ],
          assets: [
            { id: "FREE-858", code: "FREE-858", label: "Libre", asset_type_id: "TYPE-858", active: true, visible_in_unit_occupancy: true },
            { id: "BLOCKED-858", code: "BLOCKED-858", label: "Occupé", asset_type_id: "TYPE-858", active: true, visible_in_unit_occupancy: false },
            { id: "CURRENT-858", code: "CURRENT-858", label: "Réservation actuelle", asset_type_id: "TYPE-858", active: true, visible_in_unit_occupancy: true },
            { id: "ZERO-858", code: "ZERO-858", label: "Occupé privé", asset_type_id: "TYPE-ZERO-858", active: true, visible_in_unit_occupancy: false },
          ],
          asset_requirements: [
            {
              ...baseRequirement,
              requirement_id: "REQ-858",
              demand_number: "DMO-2026-0858",
              eligible_asset_ids: ["FREE-858"],
            },
            {
              ...baseRequirement,
              requirement_id: "REQ-CURRENT-858",
              demand_number: "DMO-2026-0859",
              status: "Réservé",
              allocation_id: "ALLOC-CURRENT-858",
              asset_id: "CURRENT-858",
              asset_code: "CURRENT-858",
              asset_label: "Réservation actuelle",
              allocation_start_date: start,
              allocation_end_date: start,
              allocation_locked: true,
              eligible_asset_ids: ["FREE-858"],
            },
            {
              ...baseRequirement,
              requirement_id: "REQ-ZERO-858",
              demand_number: "DMO-2026-0860",
              asset_type_id: "TYPE-ZERO-858",
              asset_type_code: "ZERO-858",
              asset_type_label: "Type sans unité",
              eligible_asset_ids: [],
            },
          ],
          asset_allocations: [],
          asset_unavailability: [],
          asset_capacity: [],
          asset_diagnostics: [],
        },
      });
    });

    await page.route(
      "**/api/v1/assets/requirements/REQ-858/reservation-candidates?**",
      async (route) => {
        candidateRefreshes += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            requirement_id: "REQ-858",
            eligible_asset_ids: ["FREE-858"],
          }),
        });
      },
    );

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();

    const panel = page.getByTestId("approved-asset-requirements");
    await expect(panel).toBeVisible();
    const master = panel.getByTestId("approved-asset-master-list");
    const detail = panel.getByTestId("approved-asset-detail");

    await master.locator('[data-requirement-id="REQ-858"]').click();
    let requestCard = detail.locator('.asset-requirement-card[data-requirement-id="REQ-858"]');
    await expect(requestCard).toContainText("DMO-2026-0858");
    await expect(requestCard).toContainText("8580");
    let requestSelect = requestCard.getByRole("combobox", { name: /Unité pour Véhicule 858/ });
    await expect(requestSelect.locator('option[value="FREE-858"]')).toHaveCount(1);
    await expect(requestSelect.locator('option[value="BLOCKED-858"]')).toHaveCount(0);
    await expect(requestCard).not.toContainText("PRIVATE");
    await expect(requestCard).not.toContainText("CONTINUOUS");

    await master.locator('[data-requirement-id="REQ-CURRENT-858"]').click();
    const currentCard = detail.locator('.asset-requirement-card[data-requirement-id="REQ-CURRENT-858"]');
    const currentSelect = currentCard.getByRole("combobox", { name: /Unité pour Véhicule 858/ });
    await expect(currentSelect.locator('option[value="CURRENT-858"]')).toHaveCount(1);
    await expect(currentSelect).toHaveValue("CURRENT-858");

    await master.locator('[data-requirement-id="REQ-ZERO-858"]').click();
    const zeroCard = detail.locator('.asset-requirement-card[data-requirement-id="REQ-ZERO-858"]');
    await expect(zeroCard).toBeVisible();
    await expect(zeroCard).toContainText("À réserver");
    await expect(zeroCard).toContainText("Aucune unité disponible pour cette période.");

    await master.locator('[data-requirement-id="REQ-858"]').click();
    requestCard = detail.locator('.asset-requirement-card[data-requirement-id="REQ-858"]');
    requestSelect = requestCard.getByRole("combobox", { name: /Unité pour Véhicule 858/ });
    await requestSelect.selectOption("FREE-858");
    // Native date inputs are not role=textbox in every browser; use the stable aria-label instead.
    const dateEnd = requestCard.locator('input[aria-label^="Date fin réelle"]');
    const allowedEnd = await dateEnd.getAttribute("max");
    expect(allowedEnd).not.toBeNull();
    await dateEnd.fill(allowedEnd!);

    await expect.poll(() => candidateRefreshes).toBeGreaterThan(0);
    await expect(requestSelect.locator('option[value="BLOCKED-858"]')).toHaveCount(0);
    await expect(requestSelect.locator('option[value="FREE-858"]')).toHaveCount(1);
  } finally {
    await context.close();
  }
});
