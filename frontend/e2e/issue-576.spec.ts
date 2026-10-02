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

test("Quick Shift sans demande peut étendre son segment à la semaine sans multiplier les heures", async ({ browser }) => {
  test.setTimeout(120_000);
  const targetWeekStart = addDays(startOfWeek(new Date()), 28);
  const targetDay = localIso(targetWeekStart);
  const targetFriday = localIso(addDays(targetWeekStart, 4));
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    for (let index = 0; index < 4; index += 1) {
      await page.getByRole("button", { name: /Suivante/ }).click();
    }

    const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    await expect(aliceRow).toBeVisible();
    const targetCell = aliceRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
    await expect(targetCell).toBeVisible();
    await targetCell.hover();
    await targetCell.getByRole("button", {
      name: `Créer un Quick Shift pour Alice le ${targetDay}`,
    }).click();

    const quickShiftDialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await expect(quickShiftDialog).toBeVisible();
    await chooseCombobox(quickShiftDialog, "Projet", "251", "P-251");
    await quickShiftDialog.getByLabel("Heures").fill("8");
    await quickShiftDialog.getByLabel("Confirmation").selectOption("Tentative");
    await quickShiftDialog.getByRole("button", { name: "Créer le Quick Shift" }).click();
    await expect(quickShiftDialog).toBeHidden();

    const createdCard = targetCell.locator(".shift-card").filter({ hasText: "P-251" }).last();
    await expect(createdCard).toBeVisible();
    const segmentId = await createdCard.getAttribute("data-segment-id");
    expect(segmentId).toBeTruthy();
    await createdCard.locator(".shift-card-main").click();

    const shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await expect(shiftDialog).toBeVisible();
    await shiftDialog.getByRole("button", { name: "Modifier le segment parent" }).click();

    const segmentDialog = page.getByRole("dialog", { name: "Modifier le segment" });
    await expect(segmentDialog).toBeVisible();
    await expect(segmentDialog).toContainText("Besoin ad hoc sans demande");
    const confirmation = segmentDialog.getByLabel("Confirmation");
    await expect(confirmation).toHaveValue("Tentative");
    await expect(confirmation.getByRole("option", { name: "Héritée de la demande" })).toHaveCount(0);
    await segmentDialog.getByLabel("Date de fin").fill(targetFriday);
    await expect(segmentDialog.getByLabel("Heures prévues")).toHaveValue("8");
    await segmentDialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    await expect(segmentDialog).toBeHidden();

    const segmentCards = page.locator(`.shift-card[data-segment-id="${segmentId}"]`);
    await expect(segmentCards).toHaveCount(1);
    await expect(segmentCards.first()).toContainText("8 h");
    await expect(segmentCards.first().locator(".confirmation-badge")).toContainText("Tentative");
  } finally {
    await closeContext(context);
  }
});
