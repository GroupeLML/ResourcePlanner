import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
}

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

async function openAdmin(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  return { context, page };
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

test("744 gère l’attribution continue depuis le profil ressource", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  const today = localIso(new Date());
  const tomorrow = localIso(addDays(new Date(), 1));

  try {
    await navigateMain(page, "Ressources");
    await page.locator(".resource-list-item").filter({ hasText: "Alice" }).click();

    const panel = page.getByTestId("continuous-vehicle-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("Aucune attribution continue");

    const createEditor = panel.locator(".admin-editor").filter({
      hasText: "Nouvelle attribution continue",
    });
    await expect(createEditor).toBeVisible();
    await createEditor.getByRole("combobox").selectOption("A-VEHICLE-744");
    await createEditor.getByLabel("Date de début effective").fill(today);
    await panel.getByRole("button", { name: "Attitrer le véhicule" }).click();

    await expect(panel).toContainText("VEH-744-01");
    await expect(panel).toContainText("Actif");
    await expect(panel).toContainText(`${today} → sans fin`);

    await panel.getByLabel("Date effective").fill(tomorrow);
    await panel.getByRole("button", { name: "Révoquer à cette date" }).click();

    await expect(panel).toContainText(`${today} → ${today}`);
    await expect(panel).toContainText("CONTINUOUS").toHaveCount(0);

    await page.reload();
    await navigateMain(page, "Ressources");
    await page.locator(".resource-list-item").filter({ hasText: "Alice" }).click();
    const reloaded = page.getByTestId("continuous-vehicle-panel");
    await expect(reloaded).toContainText("VEH-744-01");
    await expect(reloaded).toContainText(`${today} → ${today}`);
  } finally {
    await context.close();
  }
});
