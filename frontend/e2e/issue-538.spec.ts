import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

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

function startOfWeek(value: Date) {
  const day = value.getDay();
  return addDays(value, -(day === 0 ? 6 : day - 1));
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

async function chooseCombobox(scope: Locator, label: string, query: string, optionName: string) {
  const input = scope.getByRole("combobox", { name: label, exact: true });
  await input.click();
  await input.fill(query);
  const listbox = scope.getByRole("listbox", { name: `${label} options`, exact: true });
  await expect(listbox).toBeVisible();
  await listbox.getByRole("option", { name: optionName, exact: false }).click();
}

test("Quick Shift depuis une cellule préremplit la ressource et accepte une date hors semaine", async ({ browser }) => {
  test.setTimeout(120_000);
  const targetWeekStart = addDays(startOfWeek(new Date()), 14);
  const targetDay = localIso(targetWeekStart);
  const outsideDisplayedWeek = localIso(addDays(targetWeekStart, 21));
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /Suivante/ }).click();
    await page.getByRole("button", { name: /Suivante/ }).click();

    const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    await expect(aliceRow).toBeVisible();
    const targetCell = aliceRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
    await expect(targetCell).toBeVisible();
    await targetCell.hover();

    const cellShortcut = targetCell.getByRole("button", {
      name: `Créer un Quick Shift pour Alice le ${targetDay}`,
    });
    await expect(cellShortcut).toBeVisible();
    await cellShortcut.click();

    let dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await expect(dialog).toBeVisible();
    const seededTechnician = dialog.getByRole("combobox", { name: "Technicien", exact: true });
    await expect(seededTechnician).toHaveValue(/Alice/);
    await expect(seededTechnician).toHaveAttribute("data-combobox-value", "R-ALICE");
    await expect(dialog.getByLabel("Date")).toHaveValue(targetDay);
    await chooseCombobox(dialog, "Projet", "251", "P-251");
    await dialog.getByLabel("Heures").fill("1");
    await dialog.getByRole("button", { name: "Créer le Quick Shift" }).click();

    await expect(dialog).toBeHidden();
    await expect(page.locator(".planning-drag-feedback")).toContainText(
      `Quick Shift créé pour Alice le ${targetDay}.`,
    );
    await expect(targetCell.locator(".shift-card").filter({ hasText: "P-251" })).not.toHaveCount(0);

    await page.getByRole("button", { name: "+ Quick Shift", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await expect(dialog).toBeVisible();
    await chooseCombobox(dialog, "Projet", "251", "P-251");
    await chooseCombobox(dialog, "Technicien", "lic", "Alice");
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
