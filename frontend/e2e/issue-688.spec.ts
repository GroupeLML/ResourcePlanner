import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

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

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigatePlanning(page: Page) {
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
}

test("688 replie les filtres sans perdre leur état ni la navigation semaine", async ({ browser }) => {
  const { context, page } = await openCoordinator(browser);

  try {
    await navigatePlanning(page);

    const sticky = page.locator(".planning-sticky-controls");
    const toggle = page.getByRole("button", { name: /^Filtres/ });
    await expect(sticky).toHaveCSS("position", "sticky");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("#planning-filters")).toBeVisible();

    const confirmation = page.getByLabel("Confirmation");
    await confirmation.selectOption("confirmed");
    await expect(toggle).toContainText("1 filtre actif");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator("#planning-filters")).toHaveCount(0);
    await expect(toggle).toContainText("1 filtre actif");
    await expect(page.getByRole("button", { name: "Aujourd’hui", exact: true })).toBeVisible();

    const before = await sticky.boundingBox();
    expect(before).not.toBeNull();
    await page.locator(".planning-board-scroll").evaluate((element) => {
      element.scrollTop = 320;
    });
    const after = await sticky.boundingBox();
    expect(after).not.toBeNull();
    expect(Math.abs((after?.y ?? 0) - (before?.y ?? 0))).toBeLessThan(2);

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByLabel("Confirmation")).toHaveValue("confirmed");

    await page.setViewportSize({ width: 760, height: 900 });
    await expect(toggle).toBeVisible();
    await expect(page.locator("#planning-filters")).toBeVisible();
    const horizontalOverflow = await page.locator(".planning-filter-section").evaluate(
      (element) => element.scrollWidth - element.clientWidth,
    );
    expect(horizontalOverflow).toBeLessThanOrEqual(1);
  } finally {
    await closeContext(context);
  }
});
