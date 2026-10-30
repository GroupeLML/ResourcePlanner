import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";

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

test("777 glisse un reliquat approuvé sur une ressource/journée via le vrai backend", async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  const fixtureKey = randomUUID();
  const monday = addDays(startOfWeek(new Date()), 7);
  const d1 = localIso(monday);
  const d4 = localIso(addDays(monday, 3));
  const d5 = localIso(addDays(monday, 4));
  const resourceName = `Ressource E2E 777 ${fixtureKey}`;
  let resourceId: string | null = null;
  let availabilityRuleId: string | null = null;

  try {
    const createdResource = await page.request.post("/api/v1/resources", {
      headers: { "X-E2E-Role": "ADMIN" },
      data: {
        name: resourceName,
        resource_class: "PROGRAMMEUR",
        competencies: "SCADA",
        sort_order: 777,
      },
    });
    expect(createdResource.status(), await createdResource.text()).toBe(201);
    resourceId = String((await createdResource.json()).resource_id);

    const availability = await page.request.post("/api/v1/availability-rules", {
      headers: {
        "X-E2E-Role": "ADMIN",
        "Idempotency-Key": `${fixtureKey}-availability`,
      },
      data: {
        availability_type: "Horaire standard",
        resource_id: resourceId,
        weekdays: "Lun,Mar,Mer,Jeu,Ven",
        start_time: "08:00:00",
        end_time: "16:00:00",
        active: true,
      },
    });
    expect(availability.status(), await availability.text()).toBe(201);
    availabilityRuleId = String((await availability.json()).rule_id);

    const createdDemand = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        desired_start: d1,
        desired_end: d5,
        estimated_hours: 40,
        task_code: "210",
        description: `Drop daté #777 ${fixtureKey}`,
        submit: true,
      },
    });
    expect(createdDemand.status(), await createdDemand.text()).toBe(201);
    const demandNumber = String((await createdDemand.json()).demand_number);

    const preApprovalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d5}&scope=global`,
    );
    expect(preApprovalSnapshot.ok(), await preApprovalSnapshot.text()).toBeTruthy();
    const preApproval = await preApprovalSnapshot.json() as Record<string, any>;
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "Approbation E2E #777",
          expected_planning_version: Number(preApproval.planning_version),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Suivante →", exact: true }).click();

    const source = page.locator(".planning-action-card.action-assignment")
      .filter({ hasText: demandNumber })
      .first();
    await expect(source).toBeVisible();
    const segmentId = await source.getAttribute("data-segment-id");
    expect(segmentId).toBeTruthy();

    const target = page.locator(
      `.planning-drop-day[data-resource-id="${resourceId}"][data-day="${d4}"]`,
    );
    await expect(target).toBeVisible();

    const mutationPromise = page.waitForResponse((response) =>
      response.request().method() === "POST"
      && new URL(response.url()).pathname
        === `/api/v1/segments/${encodeURIComponent(String(segmentId))}/assign-from-drop`,
    );
    await source.dragTo(target);
    const mutation = await mutationPromise;
    expect(mutation.status(), await mutation.text()).toBe(200);
    const requestBody = mutation.request().postDataJSON() as Record<string, any>;
    expect(requestBody.resource_id).toBe(resourceId);
    expect(requestBody.dropped_date).toBe(d4);
    expect(Number(requestBody.expected_planning_version)).toBeGreaterThan(0);
    expect(String(requestBody.expected_approval_revision_id)).not.toBe("");
    expect(Number(requestBody.expected_operational_version)).toBeGreaterThan(0);

    const result = await mutation.json() as Record<string, any>;
    expect(result.placement_not_before_date).toBe(d4);
    expect(result.resource_id).toBe(resourceId);
    expect(result.covered_hours).toBe(16);
    expect(result.remaining_hours).toBe(24);

    const feedback = page.locator(".planning-feedback").first();
    await expect(feedback).toContainText(resourceName);
    await expect(feedback).toContainText(d4);

    const shiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(shiftsResponse.ok(), await shiftsResponse.text()).toBeTruthy();
    const shifts = (await shiftsResponse.json() as Array<any>).filter(
      (row) => row.demand_number === demandNumber
        && row.allocation_type !== "Hors horaire requis",
    );
    expect(shifts.reduce((sum, row) => sum + Number(row.hours), 0)).toBe(16);
    expect(shifts.every((row) => String(row.work_date) >= d4)).toBeTruthy();

    const segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(String(segmentId))}`,
    );
    expect(segmentResponse.ok(), await segmentResponse.text()).toBeTruthy();
    expect((await segmentResponse.json()).placement_not_before_date).toBe(d4);
  } finally {
    try {
      if (availabilityRuleId) {
        const result = await page.request.post(
          `/api/v1/availability-rules/${encodeURIComponent(availabilityRuleId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
        expect.soft(result.ok(), await result.text()).toBeTruthy();
      }
      if (resourceId) {
        const result = await page.request.post(
          `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
        expect.soft(result.ok(), await result.text()).toBeTruthy();
      }
    } finally {
      await context.close();
    }
  }
});
