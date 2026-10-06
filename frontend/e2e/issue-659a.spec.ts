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

async function dragWithDataTransfer(page: Page, source: Locator, target: Locator) {
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  await source.dispatchEvent("dragstart", { dataTransfer });
  await target.dispatchEvent("dragenter", { dataTransfer });
  await target.dispatchEvent("dragover", { dataTransfer });
  await target.dispatchEvent("drop", { dataTransfer });
  await source.dispatchEvent("dragend", { dataTransfer });
  await dataTransfer.dispose();
}

async function moveWeeks(page: Page, count: number) {
  for (let index = 0; index < count; index += 1) {
    await page.getByRole("button", { name: /Suivante/ }).click();
  }
}

async function createQuickShift(
  page: Page,
  day: string,
  note: string,
): Promise<{ card: Locator; segmentId: string; allocationId: string }> {
  const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
  await expect(aliceRow).toBeVisible();
  const targetCell = aliceRow.locator(`.planning-drop-day[data-day="${day}"]`);
  await expect(targetCell).toBeVisible();
  await targetCell.hover();
  await targetCell.getByRole("button", {
    name: `Créer un Quick Shift pour Alice le ${day}`,
  }).click();

  const dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
  await expect(dialog).toBeVisible();
  await chooseCombobox(dialog, "Projet", "251", "P-251");
  await dialog.getByLabel("Heures").fill("8");
  await dialog.getByLabel("Confirmation").selectOption("Confirmée");
  await dialog.getByLabel("Note").fill(note);
  await dialog.getByRole("button", { name: "Créer le Quick Shift" }).click();
  await expect(dialog).toBeHidden();

  const cardButton = targetCell.locator(`.shift-card-main[title*="${note}"]`);
  await expect(cardButton).toHaveCount(1);
  const card = cardButton.locator("..");
  const segmentId = await card.getAttribute("data-segment-id");
  const allocationId = await card.getAttribute("data-allocation-id");
  expect(segmentId).toBeTruthy();
  expect(allocationId).toBeTruthy();
  return { card, segmentId: segmentId!, allocationId: allocationId! };
}

test("659A — le DnD Quick Shift étend automatiquement la fenêtre sans dialogue intermédiaire", async ({ browser }) => {
  test.setTimeout(120_000);
  const weekStart = addDays(startOfWeek(new Date()), 28);
  const sourceDay = localIso(weekStart);
  const targetDay = localIso(addDays(weekStart, 1));
  const note = "Issue 659A DnD";
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    await moveWeeks(page, 4);
    const created = await createQuickShift(page, sourceDay, note);

    const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    const targetCell = aliceRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
    await expect(targetCell).toBeVisible();

    const mutationPromise = page.waitForResponse((response) => (
      response.url().includes(`/api/v1/allocations/${created.allocationId}/extend-and-move`)
      && response.request().method() === "POST"
    ));
    await dragWithDataTransfer(page, created.card, targetCell);
    const mutation = await mutationPromise;
    expect(mutation.status(), await mutation.text()).toBe(200);
    expect(mutation.request().postDataJSON().confirm_window_extension).toBe(false);

    await expect(page.getByRole("dialog", { name: "Choisir l’action du déplacement" })).toHaveCount(0);
    await expect(page.locator(".planning-drag-feedback")).toContainText("Fenêtre du besoin étendue automatiquement");
    await expect(targetCell.locator(`.shift-card-main[title*="${note}"]`)).toHaveCount(1);

    const segmentResponse = await page.request.get(`/api/v1/segments/${created.segmentId}`);
    expect(segmentResponse.status(), await segmentResponse.text()).toBe(200);
    const segment = await segmentResponse.json();
    expect(segment.start_date).toBe(sourceDay);
    expect(segment.end_date).toBe(targetDay);
    expect(Number(segment.planned_hours)).toBe(8);
  } finally {
    await closeContext(context);
  }
});

test("659A — éditer un Quick Shift vers la semaine suivante étend le segment sans multiplier les heures", async ({ browser }) => {
  test.setTimeout(120_000);
  const weekStart = addDays(startOfWeek(new Date()), 35);
  const sourceDay = localIso(weekStart);
  const targetDay = localIso(addDays(weekStart, 7));
  const note = "Issue 659A semaine suivante";
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    await moveWeeks(page, 5);
    const created = await createQuickShift(page, sourceDay, note);

    await created.card.locator(".shift-card-main").click();
    const shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await expect(shiftDialog).toBeVisible();
    await shiftDialog.getByLabel("Date").fill(targetDay);

    const updatePromise = page.waitForResponse((response) => (
      response.url().includes(`/api/v1/allocations/${created.allocationId}`)
      && response.request().method() === "PUT"
    ));
    await shiftDialog.getByRole("button", { name: "Enregistrer les modifications" }).click();
    const update = await updatePromise;
    expect(update.status(), await update.text()).toBe(200);
    await expect(shiftDialog).toBeHidden();

    const segmentResponse = await page.request.get(`/api/v1/segments/${created.segmentId}`);
    expect(segmentResponse.status(), await segmentResponse.text()).toBe(200);
    const segment = await segmentResponse.json();
    expect(segment.start_date).toBe(sourceDay);
    expect(segment.end_date).toBe(targetDay);
    expect(Number(segment.planned_hours)).toBe(8);

    await page.getByRole("button", { name: /Suivante/ }).click();
    const aliceRow = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    const targetCell = aliceRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
    await expect(targetCell.locator(`.shift-card-main[title*="${note}"]`)).toHaveCount(1);
  } finally {
    await closeContext(context);
  }
});
