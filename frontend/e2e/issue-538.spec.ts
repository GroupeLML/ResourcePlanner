import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
}

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

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

test("Quick Shift depuis une cellule préremplit la ressource et accepte une date hors semaine", async ({ browser }) => {
  test.setTimeout(120_000);
  const today = localIso(new Date());
  const outsideDisplayedWeek = localIso(addDays(new Date(), 14));
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");

    const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    await expect(aliceRow).toBeVisible();
    const todayCell = aliceRow.locator(`.planning-drop-day[data-day="${today}"]`);
    await expect(todayCell).toBeVisible();
    await todayCell.hover();

    const cellShortcut = todayCell.getByRole("button", {
      name: `Créer un Quick Shift pour Alice le ${today}`,
    });
    await expect(cellShortcut).toBeVisible();
    await cellShortcut.click();

    let dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel("Technicien")).toHaveValue("Alice");
    await expect(dialog.getByLabel("Date")).toHaveValue(today);
    await dialog.getByLabel("Projet").selectOption("P-251");
    await dialog.getByLabel("Heures").fill("1");
    await dialog.getByRole("button", { name: "Créer le Quick Shift" }).click();

    await expect(dialog).toBeHidden();
    await expect(page.locator(".planning-drag-feedback")).toContainText(
      `Quick Shift créé pour Alice le ${today}.`,
    );
    await expect(todayCell.locator(".shift-card").filter({ hasText: "P-251" })).not.toHaveCount(0);

    await page.getByRole("button", { name: "+ Quick Shift", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Projet").selectOption("P-251");
    await dialog.getByLabel("Technicien").selectOption("Alice");
    await dialog.getByLabel("Date").fill(outsideDisplayedWeek);
    await dialog.getByLabel("Heures").fill("1");
    await dialog.getByRole("button", { name: "Créer le Quick Shift" }).click();

    await expect(dialog).toBeHidden();
    await expect(page.locator(".planning-drag-feedback")).toContainText(
      `Quick Shift créé pour Alice le ${outsideDisplayedWeek}.`,
    );
    await expect(page.getByText("La date doit être comprise dans la semaine affichée.")).toHaveCount(0);
  } finally {
    await closeContext(context);
  }
});
