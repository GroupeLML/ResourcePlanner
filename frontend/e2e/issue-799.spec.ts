import { randomUUID } from "node:crypto";

import { Browser, Page, expect, test } from "@playwright/test";

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

function mondayOf(value: Date) {
  const next = new Date(value);
  const day = next.getDay();
  const delta = day === 0 ? -6 : 1 - day;
  next.setDate(next.getDate() + delta);
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

async function planningVersion(page: Page) {
  const response = await page.request.get("/api/v1/assets/requirements");
  expect(response.ok(), await response.text()).toBeTruthy();
  return Number((await response.json()).planning_version);
}

async function shiftsForDemand(
  page: Page,
  start: string,
  end: string,
  demandNumber: string,
) {
  const response = await page.request.get(
    `/api/v1/shifts?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json() as Array<any>)
    .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis")
    .sort((left, right) => String(left.work_date).localeCompare(String(right.work_date)));
}

async function selectComboboxOption(page: Page, label: string, option: string) {
  const input = page.getByRole("combobox", { name: label, exact: true });
  await input.fill(option);
  await page.getByRole("option", { name: option, exact: true }).click();
}

async function expectAssetOnEveryShift(
  page: Page,
  resourceName: string,
  demandNumber: string,
  days: string[],
  assetCode: string,
) {
  const row = page.locator(".resource-row")
    .filter({ has: page.locator(".resource-identity", { hasText: resourceName }) })
    .first();
  await expect(row).toBeVisible();
  for (const day of days) {
    const card = row
      .locator(`.planning-drop-day[data-day="${day}"] .shift-card`)
      .filter({ hasText: demandNumber });
    await expect(card).toHaveCount(1);
    await expect(card.locator(".shift-assets")).toContainText(assetCode);
  }
}

test("799 projette une période d'actif depuis un quart et depuis Actifs sur tous les quarts couverts", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await openAdmin(browser);
  const fixtureKey = randomUUID();
  const monday = mondayOf(new Date());
  const d1 = localIso(monday);
  const d2 = localIso(addDays(monday, 1));
  const d3 = localIso(addDays(monday, 2));
  const d4 = localIso(addDays(monday, 3));
  const days = [d1, d2, d3, d4];

  try {
    const resourcesResponse = await page.request.get("/api/v1/resources?active_only=false");
    expect(resourcesResponse.ok(), await resourcesResponse.text()).toBeTruthy();
    const resources = await resourcesResponse.json() as Array<{ id: string; name: string }>;
    const alice = resources.find((row) => row.name === "Alice");
    expect(alice, "Ressource Alice introuvable dans la fixture E2E").toBeTruthy();

    const catalogResponse = await page.request.get("/api/v1/assets/catalog");
    expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy();
    const catalog = await catalogResponse.json() as {
      types: Array<{ id: string; code: string; active: boolean }>;
    };
    const vehicleType = catalog.types.find((row) => row.code === "VEH-744" && row.active);
    expect(vehicleType, "Type VEH-744 introuvable dans la fixture E2E").toBeTruthy();

    const suffix = fixtureKey.slice(0, 8).toUpperCase();
    const assetACode = `A799-${suffix}`;
    const assetBCode = `B799-${suffix}`;
    const createAsset = async (code: string, label: string) => {
      const response = await page.request.post("/api/v1/assets", {
        data: {
          code,
          label,
          asset_type_id: vehicleType!.id,
        },
      });
      expect(response.status(), await response.text()).toBe(201);
      return String((await response.json()).id);
    };
    const assetAId = await createAsset(assetACode, `Actif #799 quart ${suffix}`);
    const assetBId = await createAsset(assetBCode, `Actif #799 section ${suffix}`);

    const createdDemand = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        desired_start: d1,
        desired_end: d4,
        estimated_hours: 32,
        task_code: "210",
        proposed_technician: "Alice",
        description: `Projection actif #799 ${suffix}`,
        submit: true,
      },
    });
    expect(createdDemand.status(), await createdDemand.text()).toBe(201);
    const demandNumber = String((await createdDemand.json()).demand_number);

    const approvalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d4}&scope=global`,
    );
    expect(approvalSnapshot.ok(), await approvalSnapshot.text()).toBeTruthy();
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "Approbation E2E #799",
          expected_planning_version: Number((await approvalSnapshot.json()).planning_version),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const actionsResponse = await page.request.get(
      `/api/v1/planning/actions?start=${d1}&end=${d4}&scope=global`,
    );
    expect(actionsResponse.ok(), await actionsResponse.text()).toBeTruthy();
    const assignment = (await actionsResponse.json() as Array<any>).find(
      (row) => row.kind === "ASSIGNMENT" && row.demand_number === demandNumber,
    );
    expect(assignment).toBeTruthy();
    const assigned = await page.request.post(
      `/api/v1/segments/${encodeURIComponent(String(assignment.segment_id))}/assign`,
      { data: { resource_id: alice!.id } },
    );
    expect(assigned.status(), await assigned.text()).toBe(200);

    let shifts = await shiftsForDemand(page, d1, d4, demandNumber);
    expect(shifts).toHaveLength(4);
    expect(shifts.map((row) => row.work_date)).toEqual(days);

    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();

    const aliceRow = page.locator(".resource-row")
      .filter({ has: page.locator(".resource-identity", { hasText: "Alice" }) })
      .first();
    const mondayCard = aliceRow
      .locator(`.planning-drop-day[data-day="${d1}"] .shift-card`)
      .filter({ hasText: demandNumber });
    await expect(mondayCard).toHaveCount(1);
    await mondayCard.getByRole("button", { name: /Assigner un actif/i }).click();

    const dialog = page.getByRole("dialog", { name: "Assigner un actif" });
    await expect(dialog).toBeVisible();
    const assetInput = dialog.getByRole("combobox", { name: "Actif", exact: true });
    await assetInput.fill(assetACode);
    await dialog.getByRole("option", { name: new RegExp(assetACode) }).click();
    await dialog.getByLabel("Date début réelle").fill(d1);
    await dialog.getByLabel("Date fin réelle").fill(d4);

    const shiftMutation = page.waitForResponse((response) => (
      response.request().method() === "PUT"
      && new URL(response.url()).pathname
        === `/api/v1/assets/shifts/${encodeURIComponent(String(shifts[0].allocation_id))}/assignment`
    ));
    await dialog.getByRole("button", { name: "Assigner l’actif", exact: true }).click();
    const shiftMutationResponse = await shiftMutation;
    expect(shiftMutationResponse.status(), await shiftMutationResponse.text()).toBe(200);
    const shiftPeriod = await shiftMutationResponse.json() as {
      requirement_id: string;
      requirement_origin: string;
      resource_period_mode: string;
      allocation_id: string;
      context_resource_id: string;
      operator_resource_id: string;
      start_date: string;
      end_date: string;
    };
    expect(shiftPeriod.requirement_origin).toBe("RESOURCE_PERIOD");
    expect(shiftPeriod.resource_period_mode).toBe("BOUNDED");
    expect(shiftPeriod.context_resource_id).toBe(alice!.id);
    expect(shiftPeriod.operator_resource_id).toBe(alice!.id);
    expect(shiftPeriod.start_date).toBe(d1);
    expect(shiftPeriod.end_date).toBe(d4);

    await expectAssetOnEveryShift(page, "Alice", demandNumber, days, assetACode);

    shifts = await shiftsForDemand(page, d1, d4, demandNumber);
    for (const shift of shifts) {
      const inherited = shift.related_asset_reservations.filter(
        (row: any) => row.allocation_id === shiftPeriod.allocation_id,
      );
      expect(inherited).toHaveLength(1);
      expect(inherited[0].association_kind).toBe("INHERITED_RESOURCE_PERIOD");
    }

    const assetStateResponse = await page.request.get("/api/v1/assets/requirements");
    expect(assetStateResponse.ok(), await assetStateResponse.text()).toBeTruthy();
    const assetState = await assetStateResponse.json() as {
      requirements: Array<any>;
      allocations: Array<any>;
    };
    expect(
      assetState.requirements.filter((row) => row.id === shiftPeriod.requirement_id),
    ).toHaveLength(1);
    expect(
      assetState.allocations.filter((row) => row.id === shiftPeriod.allocation_id),
    ).toHaveLength(1);

    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    await expectAssetOnEveryShift(page, "Alice", demandNumber, days, assetACode);

    const directPanel = page.locator(".asset-direct-reservation-panel");
    await expect(directPanel).toBeVisible();
    await directPanel.getByLabel("Réserver pour").selectOption("RESOURCE_PERIOD");
    const resourceInput = directPanel.getByRole("combobox", { name: "Ressource", exact: true });
    await resourceInput.fill("Alice");
    await directPanel.getByRole("option", { name: "Alice", exact: true }).click();
    await directPanel.getByLabel("Type d’actif").selectOption(vehicleType!.id);
    await directPanel.getByLabel("Unité").selectOption(assetBId);
    await directPanel.getByLabel("Début réel").fill(d1);
    await directPanel.getByLabel("Fin réelle").fill(d4);

    const directMutation = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/assets/resource-period-reservations"
    ));
    await directPanel.getByRole("button", { name: "Réserver l’actif", exact: true }).click();
    const directMutationResponse = await directMutation;
    expect(directMutationResponse.status(), await directMutationResponse.text()).toBe(201);
    const directPeriod = await directMutationResponse.json() as {
      requirement_origin: string;
      resource_period_mode: string;
      allocation_id: string;
      context_resource_id: string;
      operator_resource_id: string;
    };
    expect(directPeriod.requirement_origin).toBe("RESOURCE_PERIOD");
    expect(directPeriod.resource_period_mode).toBe("BOUNDED");
    expect(directPeriod.context_resource_id).toBe(alice!.id);
    expect(directPeriod.operator_resource_id).toBe(alice!.id);

    await expectAssetOnEveryShift(page, "Alice", demandNumber, days, assetBCode);
    shifts = await shiftsForDemand(page, d1, d4, demandNumber);
    for (const shift of shifts) {
      const inherited = shift.related_asset_reservations.filter(
        (row: any) => row.allocation_id === directPeriod.allocation_id,
      );
      expect(inherited).toHaveLength(1);
      expect(inherited[0].association_kind).toBe("INHERITED_RESOURCE_PERIOD");
    }

    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    await expectAssetOnEveryShift(page, "Alice", demandNumber, days, assetACode);
    await expectAssetOnEveryShift(page, "Alice", demandNumber, days, assetBCode);

    expect(await planningVersion(page)).toBeGreaterThan(1);
  } finally {
    await context.close();
  }
});
