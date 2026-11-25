import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

const sections = [
  ["GIR", "Skyjack", 2],
  ["LIF", "Chariots élévateurs", 1],
  ["MAT", "Véhicules", 1],
  ["PEP", "Pelles et pépines", 1],
  ["ROU", "Roulottes de chantier", 1],
  ["TAY", "Taylor Dunn", 1],
  ["TRA", "Remorques", 1],
  ["OTHER", "Autres / Non classés", 3],
] as const;

function unit(id: string, code: string | undefined, label: string, active = true, visible = true) {
  return { id, code, label, asset_type_id: "TYPE-860", active, visible_in_unit_occupancy: visible };
}

// Every unit has the same catalogue category: grouping must use the unit code.
const units = [
  unit("A-860-G1", "GIR-860-1", "Skyjack fixture one"),
  unit("A-860-G2", "GIR8602", "Skyjack fixture two"),
  unit("A-860-L", "LIF-860", "Lift fixture"),
  unit("A-860-M", "MAT-860", "Vehicle fixture"),
  unit("A-860-P", "PEP-860", "Excavator fixture"),
  unit("A-860-R", "ROU-860", "Trailer office fixture"),
  unit("A-860-T", "TAY-860", "Taylor fixture"),
  unit("A-860-TR", "TRA-860", "Trailer fixture"),
  unit("A-860-U", "ZZZ-860", "Unknown fixture"),
  unit("A-860-E", "", "Empty code fixture"),
  unit("A-860-N", undefined, "Missing code fixture"),
  unit("A-860-I", "GIR-860-INACTIVE", "Inactive fixture", false),
  unit("A-860-H", "MAT-860-HIDDEN", "Outside projection fixture", true, false),
];

function panel(page: Page) {
  return page.getByTestId("asset-unit-occupancy-panel");
}

function section(page: Page, prefix: string) {
  return panel(page).getByTestId(`asset-unit-section-${prefix}`);
}

function rows(page: Page, prefix: string) {
  return panel(page).getByTestId(`asset-unit-section-rows-${prefix}`);
}

async function expectCounts(page: Page, counts: readonly number[]) {
  const headers = panel(page).locator('button[data-testid^="asset-unit-section-"]');
  await expect(headers).toHaveCount(sections.length);
  await expect.poll(() => headers.evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("data-testid")),
  )).toEqual(sections.map(([prefix]) => `asset-unit-section-${prefix}`));
  for (const [index, [prefix, label]] of sections.entries()) {
    const count = counts[index];
    const button = section(page, prefix);
    await expect(button).toHaveAccessibleName(`${label} — ${count} unité${count === 1 ? "" : "s"}`);
    await expect(button.getByTestId("asset-unit-section-count")).toHaveText(String(count));
  }
}

async function expectInitiallyCollapsed(page: Page) {
  for (const [prefix] of sections) {
    const button = section(page, prefix);
    await expect(button).toHaveAttribute("aria-expanded", "false");
    const id = await button.getAttribute("aria-controls");
    expect(id).toBeTruthy();
    await expect(rows(page, prefix)).toHaveAttribute("id", id!);
    await expect(rows(page, prefix)).toHaveAttribute("hidden", "");
  }
  await expect(panel(page).locator(".asset-capacity-row")).toHaveCount(0);
}

test("860 — unit prefix disclosure, scoped counters and occupancy in both Planning windows", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  let initialStart: string | undefined;
  const assetMutations: string[] = [];
  context.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/v1/assets/")
      && ["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) {
      assetMutations.push(request.url());
    }
  });

  try {
    // Exercise the real Planning page and its normal scope/week fetches with a
    // controlled server projection, including malformed/missing legacy codes.
    await context.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const start = String(body.start);
      initialStart ??= start;
      const scope = new URL(route.request().url()).searchParams.get("scope");
      const assets = start !== initialStart ? [] : scope === "global"
        ? [units[0], units[2], units[11], units[12]]
        : units;
      const occupation = {
        allocation_id: "ALLOC-860",
        requirement_id: "REQ-860",
        asset_id: units[0].id,
        asset_code: units[0].code,
        asset_label: units[0].label,
        project_number: "P-860",
        start_date: start,
        end_date: start,
        locked: true,
        source: "MANUAL",
        operator_resource_id: "R-ALICE",
        operator_resource_name: "Alice",
        qualification_state: "SATISFIED",
        required_competency_ids: [],
        required_competency_names: [],
      };
      const capacity = [
        { asset_id: units[0].id, day: start, capacity_units: 1, occupied_units: 2,
          remaining_units: 0, unavailable: false, available: false,
          visible_occupations: [occupation], has_hidden_occupancy: true },
        { asset_id: units[1].id, day: start, capacity_units: 1, occupied_units: 0,
          remaining_units: 0, unavailable: true, available: false,
          visible_occupations: [], has_hidden_occupancy: false },
        { asset_id: units[2].id, day: start, capacity_units: 1, occupied_units: 1,
          remaining_units: 0, unavailable: false, available: false,
          visible_occupations: [], has_hidden_occupancy: true },
      ].filter((entry) => assets.some((asset) => asset.id === entry.asset_id));
      await route.fulfill({
        response,
        json: {
          ...body,
          asset_types: [{
            id: "TYPE-860", code: "TYPE-860", label: "Same catalogue type",
            category: "VEHICLE", occupancy_policy: "EXCLUSIVE_DAY", active: true,
            qualification_policy: "ANY_ASSIGNED_WORKFORCE",
            required_competency_ids: [], required_competency_names: [],
          }],
          assets,
          asset_requirements: [],
          asset_allocations: [],
          asset_unavailability: [],
          asset_diagnostics: [],
          asset_capacity: capacity,
        },
      });
    });

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    const mine = page.getByRole("button", { name: "Mon périmètre" });
    await mine.click();
    await expect(mine).toHaveClass(/active/);
    const fullCounts = sections.map(([, , count]) => count);
    await expectCounts(page, fullCounts);
    await expectInitiallyCollapsed(page);

    const gir = section(page, "GIR");
    const lif = section(page, "LIF");
    await gir.focus();
    await expect(gir).toBeFocused();
    await gir.press("Enter");
    await expect(gir).toHaveAttribute("aria-expanded", "true");
    await expect(rows(page, "GIR").locator(".asset-capacity-row")).toHaveCount(2);
    await expect(lif).toHaveAttribute("aria-expanded", "false");

    await lif.focus();
    await expect(lif).toBeFocused();
    await lif.press("Space");
    await expect(lif).toHaveAttribute("aria-expanded", "true");
    await expect(gir).toHaveAttribute("aria-expanded", "true");
    await gir.press("Space");
    await expect(gir).toHaveAttribute("aria-expanded", "false");
    await expect(rows(page, "GIR").locator(".asset-capacity-row")).toHaveCount(0);
    await expect(lif).toHaveAttribute("aria-expanded", "true");

    for (const [prefix] of sections) {
      const button = section(page, prefix);
      if (await button.getAttribute("aria-expanded") === "false") await button.click();
    }
    const identities = panel(page).locator(".asset-capacity-identity strong");
    await expect(identities).toHaveCount(11);
    for (const asset of units.filter((asset) => asset.active && asset.visible_in_unit_occupancy)) {
      await expect(identities.filter({ hasText: asset.label })).toHaveCount(1);
    }
    await expect(identities.filter({ hasText: "Inactive fixture" })).toHaveCount(0);
    await expect(identities.filter({ hasText: "Outside projection fixture" })).toHaveCount(0);
    const expectedSectionCodes = {
      GIR: ["GIR-860-1 — Skyjack fixture one", "GIR8602 — Skyjack fixture two"],
      LIF: ["LIF-860 — Lift fixture"],
      MAT: ["MAT-860 — Vehicle fixture"],
      PEP: ["PEP-860 — Excavator fixture"],
      ROU: ["ROU-860 — Trailer office fixture"],
      TAY: ["TAY-860 — Taylor fixture"],
      TRA: ["TRA-860 — Trailer fixture"],
    };
    for (const [prefix, expected] of Object.entries(expectedSectionCodes)) {
      await expect(rows(page, prefix).locator(".asset-capacity-identity strong")).toHaveText(expected);
    }
    await expect(rows(page, "OTHER").locator(".asset-capacity-identity strong")).toHaveText([
      "ZZZ-860 — Unknown fixture", "— Empty code fixture", "— Missing code fixture",
    ]);

    const firstRow = rows(page, "GIR").locator(".asset-capacity-row").first();
    const firstCell = firstRow.locator(".asset-capacity-cell").first();
    await expect(firstRow.locator(".asset-capacity-cell")).toHaveCount(7);
    await expect(panel(page).locator(".asset-capacity-header")).toHaveCount(8);
    await expect(firstCell).toContainText("Occupé");
    await expect(firstCell).toContainText("P-860");
    await expect(firstCell).toContainText("Alice");
    await expect(firstCell).toContainText("+ occupation hors périmètre");
    await expect(firstCell).toContainText("2 occupations · conflit");
    await expect(firstRow.locator(".asset-capacity-cell").nth(1)).toContainText("Libre");
    await expect(rows(page, "GIR").locator(".asset-capacity-row").nth(1)
      .locator(".asset-capacity-cell").first()).toContainText("Indisponible");
    await expect(rows(page, "LIF").locator(".asset-capacity-cell").first())
      .toContainText("Occupé hors périmètre");
    const dayBox = await panel(page).locator(".asset-capacity-header").nth(1).boundingBox();
    const cellBox = await firstCell.boundingBox();
    expect(dayBox).not.toBeNull();
    expect(cellBox).not.toBeNull();
    expect(Math.abs(dayBox!.x - cellBox!.x)).toBeLessThan(1);
    const headerBox = await gir.boundingBox();
    const countBox = await gir.getByTestId("asset-unit-section-count").boundingBox();
    expect(headerBox).not.toBeNull();
    expect(countBox).not.toBeNull();
    expect(Math.abs(headerBox!.x + headerBox!.width - countBox!.x - countBox!.width))
      .toBeLessThan(20);

    // The existing resource search is local to the resource board; it must not
    // replace the backend's visible-unit selection or reset disclosures.
    await page.getByRole("button", { name: /^Filtres/ }).click();
    await page.getByLabel("Recherche").fill("Alice");
    await expectCounts(page, fullCounts);
    await expect(identities).toHaveCount(11);
    const parentToggle = panel(page).getByTestId("asset-unit-occupancy-toggle");
    await parentToggle.click();
    await expect(panel(page).getByTestId("asset-unit-occupancy-content")).toHaveCount(0);
    await parentToggle.click();
    await expect(identities).toHaveCount(11);

    const [detached] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await detached.waitForLoadState("domcontentloaded");
    await expect(detached).toHaveTitle("Ressources et quarts — ResourcePlanner");
    await expect(detached.getByLabel("Recherche")).toHaveValue("Alice");
    await expectCounts(detached, fullCounts);
    await expectInitiallyCollapsed(detached);
    await section(detached, "GIR").press("Enter");
    await expect(rows(detached, "GIR").locator(".asset-capacity-row")).toHaveCount(2);
    await expect(rows(detached, "GIR").locator(".asset-capacity-cell").first()).toContainText("P-860");
    await expect(section(detached, "LIF")).toHaveAttribute("aria-expanded", "false");
    await expect(lif).toHaveAttribute("aria-expanded", "true");

    const scopedCounts = [1, 1, 0, 0, 0, 0, 0, 0];
    await page.getByRole("button", { name: "Vue globale" }).click();
    await expectCounts(page, scopedCounts);
    await expect(identities).toHaveCount(2);
    await expect(gir).toHaveAttribute("aria-expanded", "true");
    await expect(lif).toHaveAttribute("aria-expanded", "true");
    await expectCounts(detached, fullCounts);

    await detached.getByRole("button", { name: "Vue globale" }).click();
    await expectCounts(detached, scopedCounts);
    await expect(section(detached, "GIR")).toHaveAttribute("aria-expanded", "true");
    await expect(section(detached, "LIF")).toHaveAttribute("aria-expanded", "false");
    await expect(panel(detached).locator(".asset-capacity-row")).toHaveCount(1);

    const zeroCounts = sections.map(() => 0);
    for (const window of [page, detached]) {
      await window.getByRole("button", { name: /Suivante/ }).click();
      await expectCounts(window, zeroCounts);
      await expect(panel(window).locator(".asset-capacity-row")).toHaveCount(0);
      await expect(section(window, "GIR")).toHaveAttribute("aria-expanded", "true");
      await expect(window.getByLabel("Recherche")).toHaveValue("Alice");
      await window.getByRole("button", { name: /Précédente/ }).click();
      await expectCounts(window, scopedCounts);
      await expect(section(window, "GIR")).toHaveAttribute("aria-expanded", "true");
      await expect(rows(window, "GIR").locator(".asset-capacity-row")).toHaveCount(1);
    }

    await page.getByRole("button", { name: "Mon périmètre" }).click();
    await expectCounts(page, fullCounts);
    await expect(identities).toHaveCount(11);
    expect(assetMutations).toEqual([]);
    await detached.close();
  } finally {
    await context.close();
  }
});
