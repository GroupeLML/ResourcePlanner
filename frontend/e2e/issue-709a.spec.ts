import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openPlanning(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
  return { context, page };
}

test("709A — repère PM lisible au clavier et statut Shift inchangé", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  try {
    const card = page.locator(".shift-card").first();
    await expect(card).toBeVisible();
    const marker = card.locator(".shift-pm-marker");
    await expect(marker).toContainText("Chargé :");
    await expect(marker).toHaveAttribute("aria-label", /Chargé de projet :/);
    await expect(marker).toHaveAttribute("tabindex", "0");
    await marker.focus();
    await expect(marker).toBeFocused();
    const color = await marker.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    );
    const status = await card.locator(".confirmation-badge").textContent();
    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    const restored = page.locator(".shift-card").first();
    await expect(restored.locator(".shift-pm-marker")).toBeVisible();
    expect(await restored.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    )).toBe(color);
    expect(await restored.locator(".confirmation-badge").textContent()).toBe(status);
  } finally {
    await context.close();
  }
});
