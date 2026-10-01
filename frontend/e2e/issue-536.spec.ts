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

async function dragWithDataTransfer(page: Page, source: Locator, target: Locator) {
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  await source.dispatchEvent("dragstart", { dataTransfer });
  await target.dispatchEvent("dragenter", { dataTransfer });
  await target.dispatchEvent("dragover", { dataTransfer });
  await target.dispatchEvent("drop", { dataTransfer });
  await source.dispatchEvent("dragend", { dataTransfer });
  await dataTransfer.dispose();
}

test("DnD hors horaire conserve le consentement et le transmet au MOVE", async ({ browser }) => {
  test.setTimeout(120_000);
  const nextMonday = addDays(startOfWeek(new Date()), 7);
  const sourceDay = localIso(addDays(nextMonday, 4));
  const targetDay = localIso(addDays(nextMonday, 5));
  const endDay = localIso(addDays(nextMonday, 6));
  const { context, page } = await openCoordinator(browser);

  try {
    const limitedSchedule = await page.request.patch(
      "/api/v1/availability-rules/E2E-STD-BOB",
      {
        headers: { "X-E2E-Role": "ADMIN" },
        data: { weekdays: "Lun,Mar,Mer,Jeu,Ven" },
      },
    );
    expect(limitedSchedule.status(), await limitedSchedule.text()).toBe(200);

  const created = await page.request.post("/api/v1/demands", {
    data: {
      project_number: "P-251",
      desired_start: sourceDay,
      desired_end: endDay,
      estimated_hours: 2,
      task_code: "210",
      proposed_technician: "Alice",
      description: "DnD hors horaire #536",
      submit: true,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const demandNumber = (await created.json()).demand_number as string;

  const snapshot = await page.request.get(
    `/api/v1/planning/snapshot?start=${sourceDay}&end=${endDay}&scope=global`,
  );
  expect(snapshot.ok()).toBeTruthy();
  const planningVersion = (await snapshot.json()).planning_version as number;

  const approved = await page.request.post(
    `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
    {
      data: {
        comment: "Approbation DnD hors horaire #536",
        expected_planning_version: planningVersion,
      },
    },
  );
  expect(approved.status(), await approved.text()).toBe(200);

  const shiftsResponse = await page.request.get(
    `/api/v1/shifts?start=${sourceDay}&end=${endDay}`,
  );
  expect(shiftsResponse.ok()).toBeTruthy();
  const shift = (await shiftsResponse.json() as Array<{
    allocation_id: string;
    demand_number: string | null;
    resource_name: string;
    resource_id: string;
    work_date: string;
    outside_standard_hours: boolean;
    allocation_type: string | null;
  }>).find((row) => (
    row.demand_number === demandNumber
    && row.allocation_type !== "Hors horaire requis"
  ));
  expect(shift, "Quart #536 introuvable après approbation").toBeDefined();
  expect(shift!.resource_name).toBe("Alice");
  expect(shift!.work_date).toBe(sourceDay);

  await navigateMain(page, "Planning opérationnel");
  await page.getByRole("button", { name: /Suivante/ }).click();

  const aliceRow = page.locator(".resource-identity").filter({ hasText: "Alice" }).first().locator("..");
  const bobRow = page.locator(".resource-identity").filter({ hasText: "Bob" }).first().locator("..");
  const source = aliceRow
    .locator(`.planning-drop-day[data-day="${sourceDay}"]`)
    .locator(`.shift-card[data-allocation-id="${shift!.allocation_id}"]`);
  const target = bobRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
  await expect(source).toBeVisible();

  const firstEvaluationPromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/v1/allocations/${encodeURIComponent(shift!.allocation_id)}/evaluate-drop`,
    )
  ));
  await dragWithDataTransfer(page, source, target);
  const firstEvaluation = await firstEvaluationPromise;
  expect(firstEvaluation.status(), await firstEvaluation.text()).toBe(200);
  expect(firstEvaluation.request().postDataJSON().outside_standard_hours).toBe(false);

  const dialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
  const override = dialog.getByRole("checkbox", {
    name: /Autoriser explicitement le quart hors horaire standard/,
  });
  await expect(dialog).toContainText("OUTSIDE_STANDARD_HOURS_REQUIRED");
  await expect(override).toBeVisible();
  await expect(override).not.toBeChecked();

  const reevaluationPromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/v1/allocations/${encodeURIComponent(shift!.allocation_id)}/evaluate-drop`,
    )
    && response.request().postDataJSON().outside_standard_hours === true
  ));
  await override.check();
  const reevaluated = await reevaluationPromise;
  expect(reevaluated.status(), await reevaluated.text()).toBe(200);
  await expect(override).toBeVisible();
  await expect(override).toBeChecked();

  const movePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/v1/allocations/${encodeURIComponent(shift!.allocation_id)}/move`,
    )
  ));
  await dialog.getByRole("button", { name: "Déplacer", exact: true }).click();
  const moved = await movePromise;
  expect(moved.status(), await moved.text()).toBe(200);
  expect(moved.request().postDataJSON().outside_standard_hours).toBe(true);
  expect(typeof moved.request().postDataJSON().expected_planning_version).toBe("number");

  const afterResponse = await page.request.get(
    `/api/v1/shifts?start=${sourceDay}&end=${endDay}`,
  );
  const after = (await afterResponse.json() as Array<{
    allocation_id: string;
    resource_id: string;
    work_date: string;
    outside_standard_hours: boolean;
  }>).find((row) => row.allocation_id === shift!.allocation_id);
  expect(after).toBeDefined();
  expect(after!.resource_id).toBe("R-BOB");
  expect(after!.work_date).toBe(targetDay);
  expect(after!.outside_standard_hours).toBe(true);
  } finally {
    const restoredSchedule = await page.request.patch(
      "/api/v1/availability-rules/E2E-STD-BOB",
      {
        headers: { "X-E2E-Role": "ADMIN" },
        data: { weekdays: "Lun,Mar,Mer,Jeu,Ven,Sam,Dim" },
      },
    );
    expect(restoredSchedule.status(), await restoredSchedule.text()).toBe(200);
    await closeContext(context);
  }
});
