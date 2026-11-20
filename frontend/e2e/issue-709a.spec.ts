import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";
const SHIFT_ID = "SHIFT-857-E2E";
const NEUTRAL_SHIFT_ID = "SHIFT-857-NEUTRAL";
const NEIGHBOR_SHIFT_ID = "SHIFT-857-NEIGHBOR";
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

test("857 — numéro projet dans badge PM, fallback neutre et confidentialité scope-neighbor", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  try {
    // Backend identity resolution and visibility projection have dedicated Python tests.
    // Exercise the React presentation with stable decorated snapshots only.
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json() as {
        resources: Array<{ id: string; name: string }>;
        shifts: Array<Record<string, unknown>>;
      };
      const alice = snapshot.resources.find((resource) => resource.name === "Alice");
      const start = new URL(route.request().url()).searchParams.get("start");
      if (!alice || !start) throw new Error("Fixture Planning 857: Alice et début de semaine requis");

      const common = {
        resource_id: alice.id,
        resource_name: alice.name,
        work_date: start,
        hours: 1,
        allocation_type: "Fixe",
        locked: true,
        outside_standard_hours: false,
        confirmation: "Confirmée",
        confirmation_override: null,
        load_kind: "FIRM",
        note: null,
        demand_id: null,
        requester: null,
        asset_assignment: null,
        related_asset_reservations: [],
        asset_actions: null,
        asset_diagnostics: [],
      };

      snapshot.shifts.push(
        {
          ...common,
          allocation_id: SHIFT_ID,
          segment_id: "SEG-857-E2E",
          source: "MANUAL",
          requirement_id: "REQ-857-E2E",
          demand_number: "DMO-2026-0108",
          project_id: "P-5110-ID",
          project_number: "5110",
          project_name: "Projet badge responsable",
          project_manager: "Nom secondaire à ne pas utiliser pour le tooltip",
          project_manager_color_id: MANAGER_COLOR_ID,
          project_manager_color_label: "12345 - Jean Tremblay",
          requester: "67890 — Jean Gagnon",
        },
        {
          ...common,
          allocation_id: NEUTRAL_SHIFT_ID,
          segment_id: "SEG-857-NEUTRAL",
          source: "MANUAL",
          requirement_id: "REQ-857-NEUTRAL",
          demand_number: "DMO-2026-0109",
          project_id: "P-LONG-ID",
          project_number: "PROJET-2026-TRÈS-LONG-000000000857",
          project_name: "Projet sans responsable",
          project_manager: null,
          project_manager_color_id: null,
          project_manager_color_label: null,
        },
        {
          ...common,
          allocation_id: NEIGHBOR_SHIFT_ID,
          segment_id: "SEG-857-NEIGHBOR",
          source: "SCOPE_NEIGHBOR",
          requirement_id: "REQ-857-NEIGHBOR",
          demand_number: null,
          project_id: "P-NEIGHBOR-ID",
          project_number: "VOISIN-857",
          project_name: null,
          project_manager: "Responsable privé",
          project_manager_color_id: MANAGER_COLOR_ID,
          project_manager_color_label: "Responsable privé",
        },
      );
      await route.fulfill({
        response,
        contentType: "application/json",
        body: JSON.stringify(snapshot),
      });
    });

    await page.reload();
    await navigatePlanning(page);

    const card = page.locator(`.shift-card[data-allocation-id="${SHIFT_ID}"]`);
    await expect(card).toBeVisible();
    const badge = card.locator(".shift-project-badge");
    await expect(badge).toHaveText("5110");
    await expect(card.locator(".shift-pm-dot")).toHaveCount(0);
    await expect(badge).toHaveAttribute("title", "Projet 5110 — Responsable : Jean Tremblay");
    await expect(badge).toHaveAttribute("aria-label", "Projet 5110 — Responsable : Jean Tremblay");
    const mainTitle = await card.locator(".shift-card-main").getAttribute("title");
    expect(mainTitle || "").toContain("Responsable: Jean Tremblay");
    expect(mainTitle || "").toContain("Demandeur: Jean Gagnon");
    expect(mainTitle || "").not.toContain("12345");
    expect(mainTitle || "").not.toContain("67890");
    await expect(card.getByText("#DMO-2026-0108", { exact: true })).toBeVisible();
    await expect(card.locator(".confirmation-badge")).toHaveText("Confirmée");
    await expect(card.locator(".shift-hours")).toHaveText("1 h");

    const badgeColor = await badge.evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(badgeColor).not.toBe("rgb(231, 237, 245)");
    const badgeForeground = await badge.evaluate((element) => getComputedStyle(element).color);
    expect(["rgb(255, 255, 255)", "rgb(17, 24, 39)"]).toContain(badgeForeground);

    const neutral = page.locator(`.shift-card[data-allocation-id="${NEUTRAL_SHIFT_ID}"]`);
    const neutralBadge = neutral.locator(".shift-project-badge");
    await expect(neutralBadge).toHaveClass(/is-neutral/);
    await expect(neutralBadge).not.toHaveAttribute("title", /.+/);
    expect(await neutralBadge.evaluate((element) => getComputedStyle(element).backgroundColor))
      .toBe("rgb(231, 237, 245)");
    const [neutralBox, neutralCardBox] = await Promise.all([
      neutralBadge.boundingBox(),
      neutral.boundingBox(),
    ]);
    expect(neutralBox).not.toBeNull();
    expect(neutralCardBox).not.toBeNull();
    expect((neutralBox?.width ?? Infinity)).toBeLessThan(neutralCardBox?.width ?? 0);

    const neighbor = page.locator(`.shift-card[data-allocation-id="${NEIGHBOR_SHIFT_ID}"]`);
    const neighborBadge = neighbor.locator(".shift-project-badge");
    await expect(neighborBadge).toHaveClass(/is-neutral/);
    await expect(neighborBadge).not.toHaveAttribute("title", /Responsable privé/);
    await expect(neighbor.getByText("Responsable privé", { exact: true })).toHaveCount(0);
    const neighborTitle = await neighbor.locator(".shift-card-main").getAttribute("title");
    expect(neighborTitle || "").not.toContain("Responsable privé");

    const stableColor = await badge.evaluate((element) => getComputedStyle(element).backgroundColor);
    await page.reload();
    await navigatePlanning(page);
    const restoredBadge = page
      .locator(`.shift-card[data-allocation-id="${SHIFT_ID}"]`)
      .locator(".shift-project-badge");
    expect(await restoredBadge.evaluate((element) => getComputedStyle(element).backgroundColor))
      .toBe(stableColor);
  } finally {
    await context.close();
  }
});
