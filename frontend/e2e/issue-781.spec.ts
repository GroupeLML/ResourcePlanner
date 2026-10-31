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

test("781 garde la ressource proposée advisory jusqu'au placement Planning explicite", async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  const fixtureKey = randomUUID();
  const monday = addDays(startOfWeek(new Date()), 7);
  const day = localIso(monday);
  const proposedName = `Ressource E2E 781 proposée ${fixtureKey}`;
  const targetName = `Ressource E2E 781 cible ${fixtureKey}`;
  const resourceIds: string[] = [];
  const availabilityIds: string[] = [];

  try {
    for (const [index, name] of [proposedName, targetName].entries()) {
      const createdResource = await page.request.post("/api/v1/resources", {
        headers: { "X-E2E-Role": "ADMIN" },
        data: {
          name,
          resource_class: "PROGRAMMEUR",
          competencies: "SCADA",
          sort_order: 7810 + index,
        },
      });
      expect(createdResource.status(), await createdResource.text()).toBe(201);
      const resourceId = String((await createdResource.json()).resource_id);
      resourceIds.push(resourceId);

      const availability = await page.request.post("/api/v1/availability-rules", {
        headers: {
          "X-E2E-Role": "ADMIN",
          "Idempotency-Key": `${fixtureKey}-availability-${index}`,
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
      availabilityIds.push(String((await availability.json()).rule_id));
    }

    const created = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        submit: true,
        lines: [
          {
            desired_start: day,
            desired_end: day,
            estimated_hours: 4,
            proposed_resource_id: resourceIds[0],
            required_resource_class: "PROGRAMMEUR",
            description: "Validation advisory #781",
          },
        ],
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const demandNumber = String((await created.json()).demand_number);

    const approvalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${day}&end=${day}&scope=global`,
    );
    expect(approvalSnapshot.ok(), await approvalSnapshot.text()).toBeTruthy();
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "Approbation E2E #781",
          expected_planning_version: Number((await approvalSnapshot.json()).planning_version),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const afterApproval = await page.request.get(
      `/api/v1/shifts?start=${day}&end=${day}`,
    );
    expect(afterApproval.ok(), await afterApproval.text()).toBeTruthy();
    expect(
      (await afterApproval.json() as Array<any>)
        .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis"),
    ).toHaveLength(0);

    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Suivante →" }).click();

    const card = page
      .locator(".planning-work-queue .planning-action-card.action-assignment")
      .filter({ hasText: demandNumber });
    await expect(card).toHaveCount(1);
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute("draggable", "true");

    const targetCell = page.locator(
      `.planning-drop-day[data-resource-id="${resourceIds[1]}"][data-day="${day}"]`,
    );
    await expect(targetCell).toBeVisible();

    await card.dragTo(targetCell);

    await expect(page.locator(".planning-drag-feedback"))
      .toContainText(`Besoin assigné à ${targetName} à partir du ${day}`);

    const afterPlacement = await page.request.get(
      `/api/v1/shifts?start=${day}&end=${day}`,
    );
    expect(afterPlacement.ok(), await afterPlacement.text()).toBeTruthy();
    const shifts = (await afterPlacement.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(shifts).toHaveLength(1);
    expect(shifts[0].resource_id).toBe(resourceIds[1]);
    expect(Number(shifts[0].hours)).toBe(4);
    await expect(card).toHaveCount(0);
  } finally {
    for (const ruleId of availabilityIds) {
      try {
        await page.request.post(
          `/api/v1/availability-rules/${encodeURIComponent(ruleId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Best-effort cleanup.
      }
    }
    for (const resourceId of resourceIds) {
      try {
        await page.request.post(
          `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Best-effort cleanup.
      }
    }
    await context.close();
  }
});
