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

test("755 supprimer un jour Quick Shift réduit le besoin sans recréer le jour", async ({ browser }) => {
  test.setTimeout(120_000);
  // Use Bob six weeks ahead: #659A consumes Alice/Tue four weeks ahead and\n  // the Playwright suite shares one database across specs.\n  const weekStart = addDays(startOfWeek(new Date()), 42);\n  const monday = localIso(weekStart);
  const tuesday = localIso(addDays(weekStart, 1));
  const wednesday = localIso(addDays(weekStart, 2));
  const thursday = localIso(addDays(weekStart, 3));
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    for (let index = 0; index < 6; index += 1) {\n      await page.getByRole("button", { name: /Suivante/ }).click();\n    }\n\n    const bobRow = page.locator(".resource-row").filter({ hasText: "Bob" }).first();\n    await expect(bobRow).toBeVisible();\n    const mondayCell = bobRow.locator(`.planning-drop-day[data-day="${monday}"]`);\n    await mondayCell.hover();
    await mondayCell.getByRole("button", {
      name: `Créer un Quick Shift pour Bob le ${monday}`,\n    }).click();

    const quickShift = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await chooseCombobox(quickShift, "Projet", "251", "P-251");
    await quickShift.getByLabel("Heures").fill("8");
    await quickShift.getByLabel("Note").fill("Issue 755 E2E");
    await quickShift.getByRole("button", { name: "Créer le Quick Shift" }).click();
    await expect(quickShift).toBeHidden();

    const mondayCardButton = mondayCell.locator('.shift-card-main[title*="Issue 755 E2E"]');
    await expect(mondayCardButton).toHaveCount(1);
    const mondayCard = mondayCardButton.locator("..");
    const segmentId = await mondayCard.getAttribute("data-segment-id");
    expect(segmentId).toBeTruthy();

    await mondayCardButton.click();
    let shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await shiftDialog.getByRole("button", { name: "Modifier le segment parent" }).click();

    let segmentDialog = page.getByRole("dialog", { name: "Modifier le segment" });
    await segmentDialog.getByLabel("Date de fin").fill(thursday);
    await segmentDialog.getByLabel("Heures prévues").fill("32");
    await segmentDialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    await expect(segmentDialog).toBeHidden();

    const segmentSelector = `.shift-card[data-segment-id="${segmentId}"]`;
    const dayCard = (day: string) =>
      bobRow.locator(`.planning-drop-day[data-day="${day}"]`).locator(segmentSelector);\n
    for (const day of [monday, tuesday, wednesday, thursday]) {\n      await expect(dayCard(day)).toHaveCount(1);\n      await expect(dayCard(day)).toContainText("8 h");\n    }\n
    // Turn the Wednesday automatic output into the manual/locked Shift that the
    // coordinator explicitly deletes. Tuesday and Thursday remain automatic.
    await dayCard(wednesday).locator(".shift-card-main").click();
    shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await shiftDialog.getByRole("button", { name: "Enregistrer et verrouiller" }).click();
    await expect(shiftDialog).toBeHidden();

    await dayCard(wednesday).locator(".shift-card-main").click();
    shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      await dialog.accept();
    });
    await shiftDialog.getByRole("button", { name: "Supprimer le quart manuel" }).click();
    await expect(shiftDialog).toBeHidden();

    for (const day of [monday, tuesday, thursday]) {\n      await expect(dayCard(day)).toHaveCount(1);\n      await expect(dayCard(day)).toContainText("8 h");\n    }\n    await expect(dayCard(wednesday)).toHaveCount(0);\n\n    // Tuesday and Thursday must remain automatic survivors, not silently locked.\n    for (const day of [tuesday, thursday]) {\n      await dayCard(day).locator(".shift-card-main").click();\n      shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });\n      await expect(\n        shiftDialog.getByRole("button", { name: "Supprimer le quart manuel" }),\n      ).toHaveCount(0);\n      await shiftDialog.getByRole("button", { name: "Annuler" }).click();\n      await expect(shiftDialog).toBeHidden();\n    }\n\n    // Reload verifies persisted displayed state. A later unrelated rebuild is\n    // covered by tests/test_issue_755.py. Planning restores the selected week.\n    await page.reload();\n    await navigateMain(page, "Planning opérationnel");\n    const reloadedBob = page.locator(".resource-row").filter({ hasText: "Bob" }).first();\n    await expect(reloadedBob.locator(`.planning-drop-day[data-day="${monday}"]`)).toBeVisible();\n    const reloadedCard = (day: string) =>\n      reloadedBob\n        .locator(`.planning-drop-day[data-day="${day}"]`)
        .locator(`.shift-card[data-segment-id="${segmentId}"]`);

    for (const day of [monday, tuesday, thursday]) {\n      await expect(reloadedCard(day)).toHaveCount(1);\n      await expect(reloadedCard(day)).toContainText("8 h");\n    }\n    await expect(reloadedCard(wednesday)).toHaveCount(0);\n
    await reloadedCard(monday).locator(".shift-card-main").click();
    shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await shiftDialog.getByRole("button", { name: "Modifier le segment parent" }).click();
    segmentDialog = page.getByRole("dialog", { name: "Modifier le segment" });
    await expect(segmentDialog.getByLabel("Heures prévues")).toHaveValue("24");
  } finally {
    await context.close();
  }
});
