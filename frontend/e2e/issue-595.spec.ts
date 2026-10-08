import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openCoordinator(browser: Browser) {
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

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

async function resourceNames(group: ReturnType<Page["locator"]>) {
  return group.locator(".resource-identity > strong").allTextContents();
}

async function sortedShiftTexts(page: Page) {
  return (await page.locator(".shift-card").allTextContents()).sort();
}

test("595 — le mode Manuel réordonne, persiste et reste cohérent sous filtre", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /^Filtres/ }).click();
    const sort = page.getByLabel("Ordre des ressources");
    await sort.selectOption("manual");

    const programmeurGroup = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]').first();
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Alice", "Bob"]);
    const shiftsBefore = await sortedShiftTexts(page);

    await page.getByRole("button", { name: "Monter Bob", exact: true }).click();
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Bob", "Alice"]);
    await expect(page.locator(".planning-drag-feedback")).toContainText("Ordre manuel mis à jour pour Bob");

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /^Filtres/ }).click();
    const restoredGroup = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]').first();
    await expect(page.getByLabel("Ordre des ressources")).toHaveValue("manual");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Bob", "Alice"]);

    await page.getByLabel("Ordre des ressources").selectOption("alphabetical");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Alice", "Bob"]);
    await page.getByLabel("Ordre des ressources").selectOption("manual");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Bob", "Alice"]);
    await page.getByLabel("Ordre des ressources").selectOption("availability");
    await page.getByLabel("Ordre des ressources").selectOption("manual");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Bob", "Alice"]);

    await page.getByLabel("Recherche").fill("Bob");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Bob"]);
    const down = page.getByRole("button", { name: "Descendre Bob", exact: true });
    await expect(down).toBeEnabled();
    await down.click();
    await page.getByLabel("Recherche").fill("");
    await expect.poll(() => resourceNames(restoredGroup)).toEqual(["Alice", "Bob"]);
    await expect.poll(() => sortedShiftTexts(page)).toEqual(shiftsBefore);
  } finally {
    try {
      const sort = page.getByLabel("Ordre des ressources");
      if (await sort.isVisible()) await sort.selectOption("manual");
      const search = page.getByLabel("Recherche");
      if (await search.isVisible()) await search.fill("");
      const bobDown = page.getByRole("button", { name: "Descendre Bob", exact: true });
      if (await bobDown.isVisible() && await bobDown.isEnabled()) await bobDown.click();
    } catch {
      // Best-effort restoration so other E2E specs keep their canonical seed order.
    }
    await context.close();
  }
});
