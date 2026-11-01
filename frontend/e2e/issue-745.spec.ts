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
  return Number((await response.json() as { planning_version: number }).planning_version);
}

async function createCompetency(page: Page, name: string, sortOrder: number) {
  const response = await page.request.post("/api/v1/competencies", {
    data: { name, sort_order: sortOrder, active: true },
  });
  expect(response.status(), await response.text()).toBe(201);
  return String((await response.json() as { competency_id: string }).competency_id);
}

async function vehicleType(page: Page) {
  const response = await page.request.get("/api/v1/assets/catalog");
  expect(response.ok(), await response.text()).toBeTruthy();
  const payload = await response.json() as {
    types: Array<{ id: string; code: string; category: string }>;
  };
  const type = payload.types.find((row) => row.category === "VEHICLE" && row.code === "VEH-744")
    ?? payload.types.find((row) => row.category === "VEHICLE");
  expect(type).toBeTruthy();
  return type!.id;
}

test("745 affiche uniquement les métadonnées compactes autorisées et retire le véhicule révoqué", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openAdmin(browser);
  const stamp = Date.now();
  const today = new Date();
  const yesterday = localIso(addDays(today, -1));
  const todayIso = localIso(today);
  const future = localIso(addDays(today, 30));
  const resourceName = `Ressource E2E 745 ${stamp}`;
  const city = `Ville E2E 745 ${stamp}`;
  let resourceId = "";

  try {
    const competencyA = await createCompetency(page, `Alpha 745 ${stamp}`, 5);
    const competencyB = await createCompetency(page, `Beta 745 ${stamp}`, 19);
    const competencyC = await createCompetency(page, `Gamma 745 ${stamp}`, 20);
    const competencyD = await createCompetency(page, `Delta 745 ${stamp}`, 30);

    const createdResource = await page.request.post("/api/v1/resources", {
      headers: { "Idempotency-Key": `745-resource-${stamp}` },
      data: {
        name: resourceName,
        residence_city: city,
        resource_class: "PROGRAMMEUR",
        competency_ids: [competencyC, competencyA, competencyD, competencyB],
        active: true,
        sort_order: 0,
      },
    });
    expect(createdResource.status(), await createdResource.text()).toBe(201);
    resourceId = String((await createdResource.json() as { resource_id: string }).resource_id);

    const schedule = await page.request.post("/api/v1/availability-rules", {
      headers: { "Idempotency-Key": `745-schedule-${stamp}` },
      data: {
        availability_type: "Horaire standard",
        resource_id: resourceId,
        start_date: yesterday,
        end_date: future,
        weekdays: "Lun,Mar,Mer,Jeu,Ven",
        start_time: "08:00:00",
        end_time: "16:00:00",
        active: true,
        resource_class_codes: [],
      },
    });
    expect(schedule.status(), await schedule.text()).toBe(201);

    const typeId = await vehicleType(page);
    const continuousAsset = await page.request.post("/api/v1/assets", {
      data: {
        code: `VEH-745-C-${stamp}`,
        label: `Camion continu 745 ${stamp}`,
        asset_type_id: typeId,
      },
    });
    expect(continuousAsset.status(), await continuousAsset.text()).toBe(201);
    const continuousAssetId = String((await continuousAsset.json() as { id: string }).id);

    const boundedAsset = await page.request.post("/api/v1/assets", {
      data: {
        code: `VEH-745-B-${stamp}`,
        label: `Camion temporaire 745 ${stamp}`,
        asset_type_id: typeId,
      },
    });
    expect(boundedAsset.status(), await boundedAsset.text()).toBe(201);
    const boundedAssetId = String((await boundedAsset.json() as { id: string }).id);

    const continuous = await page.request.post("/api/v1/assets/continuous-resource-assignments", {
      headers: { "Idempotency-Key": `745-continuous-${stamp}` },
      data: {
        resource_id: resourceId,
        asset_id: continuousAssetId,
        start_date: yesterday,
        expected_planning_version: await planningVersion(page),
      },
    });
    expect(continuous.status(), await continuous.text()).toBe(201);
    const continuousPayload = await continuous.json() as {
      requirement_id: string;
      planning_version: number;
    };

    const bounded = await page.request.post("/api/v1/assets/resource-period-reservations", {
      headers: { "Idempotency-Key": `745-bounded-${stamp}` },
      data: {
        resource_id: resourceId,
        asset_type_id: typeId,
        asset_id: boundedAssetId,
        start_date: yesterday,
        end_date: future,
        expected_planning_version: continuousPayload.planning_version,
      },
    });
    expect(bounded.status(), await bounded.text()).toBe(201);

    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    const row = page.locator(".resource-identity").filter({ hasText: resourceName }).first();
    await expect(row).toBeVisible();
    await expect(row.locator(".resource-card-competency")).toHaveText([
      `Alpha 745 ${stamp}`,
      `Beta 745 ${stamp}`,
    ]);
    await expect(row).not.toContainText(`Gamma 745 ${stamp}`);
    await expect(row).not.toContainText(`Delta 745 ${stamp}`);
    await expect(row.locator(".resource-card-city")).toContainText(city);
    await expect(row.locator(".resource-card-vehicle")).toContainText(`Camion continu 745 ${stamp}`);
    await expect(row).not.toContainText(`Camion temporaire 745 ${stamp}`);

    const revoked = await page.request.post(
      `/api/v1/assets/continuous-resource-assignments/${encodeURIComponent(continuousPayload.requirement_id)}/revoke`,
      {
        headers: { "Idempotency-Key": `745-revoke-${stamp}` },
        data: {
          effective_from: todayIso,
          expected_planning_version: await planningVersion(page),
        },
      },
    );
    expect(revoked.ok(), await revoked.text()).toBeTruthy();

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    const refreshed = page.locator(".resource-identity").filter({ hasText: resourceName }).first();
    await expect(refreshed).toBeVisible();
    await expect(refreshed.locator(".resource-card-vehicle")).toHaveCount(0);
    await expect(refreshed).not.toContainText(`Camion temporaire 745 ${stamp}`);
    await expect(refreshed.locator(".resource-card-city")).toContainText(city);
  } finally {
    if (resourceId) {
      await page.request.post(`/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`);
    }
    await context.close();
  }
});
