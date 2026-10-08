import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";
const SHIFT_ID = "SHIFT-709A-E2E";
const MANAGER_COLOR_ID = "709a5eed12345678";

async function openPlanning(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return { context, page };
}

async function navigatePlanning(page: Page) {
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
}

test("709A — repère PM lisible au clavier et statut Shift inchangé", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  try {
    // Backend identity resolution and SQL projection have dedicated Python tests.
    // Exercise the React presentation with a stable decorated snapshot, without
    // persisting a Quick Shift in the SQLite fixture shared by other browser tests.
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json() as {
        resources: Array<{ id: string; name: string }>;
        shifts: Array<Record<string, unknown>>;
      };
      const alice = snapshot.resources.find((resource) => resource.name === "Alice");
      const start = new URL(route.request().url()).searchParams.get("start");
      if (!alice || !start) throw new Error("Fixture Planning 709A: Alice et début de semaine requis");

      snapshot.shifts.push({
        allocation_id: SHIFT_ID,
        segment_id: "SEG-709A-E2E",
        resource_id: alice.id,
        resource_name: alice.name,
        work_date: start,
        hours: 1,
        allocation_type: "Fixe",
        source: "MANUAL",
        locked: true,
        outside_standard_hours: false,
        confirmation: "Confirmée",
        confirmation_override: null,
        load_kind: "FIRM",
        note: null,
        requirement_id: "REQ-709A-E2E",
        demand_id: null,
        demand_number: null,
        project_id: "P-251-ID",
        project_number: "P-251",
        project_name: "Projet Playwright V2",
        project_manager: "Chargé E2E",
        project_manager_color_id: MANAGER_COLOR_ID,
        project_manager_color_label: "Chargé E2E",
        requester: null,
        asset_assignment: null,
        related_asset_reservations: [],
        asset_actions: null,
        asset_diagnostics: [],
      });
      await route.fulfill({
        response,
        contentType: "application/json",
        body: JSON.stringify(snapshot),
      });
    });

    await navigatePlanning(page);
    const card = page.locator(`.shift-card[data-allocation-id="${SHIFT_ID}"]`);
    await expect(card).toBeVisible();
    const marker = card.locator(".shift-pm-marker");
    await expect(marker).toContainText("Chargé : Chargé E2E");
    await expect(marker).toHaveAttribute("aria-label", /Chargé de projet :/);
    await expect(marker).toHaveAttribute("tabindex", "0");
    await marker.focus();
    await expect(marker).toBeFocused();

    const color = await marker.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    );
    expect(color).not.toBe("rgb(100, 116, 139)");
    const status = await card.locator(".confirmation-badge").textContent();
    await page.reload();
    await navigatePlanning(page);

    const restored = page.locator(`.shift-card[data-allocation-id="${SHIFT_ID}"]`);
    await expect(restored.locator(".shift-pm-marker")).toBeVisible();
    expect(await restored.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    )).toBe(color);
    expect(await restored.locator(".confirmation-badge").textContent()).toBe(status);
  } finally {
    await context.close();
  }
});
