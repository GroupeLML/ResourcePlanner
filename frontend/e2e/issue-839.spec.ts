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

test("839 propage Confirmé/Tentatif après approbation via édition et action rapide", async ({ browser }) => {
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
  const resourceName = `Ressource E2E 839 ${fixtureKey}`;
  let resourceId = "";
  let availabilityId = "";

  async function demandShifts(demandNumber: string) {
    const response = await page.request.get(
      `/api/v1/shifts?start=${day}&end=${day}`,
    );
    expect(response.ok(), await response.text()).toBeTruthy();
    return (await response.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber);
  }

  async function openDemand(demandNumber: string) {
    await page.goto("/");
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Demandes/i })
      .click();
    await expect(
      page.getByRole("heading", { name: "Demandes", level: 1 }),
    ).toBeVisible();
    const card = page.locator(".demand-card").filter({ hasText: demandNumber });
    await expect(card).toHaveCount(1);
    await card.locator(".demand-card-select").click();
    await expect(page.locator(".demand-editor-heading")).toContainText(demandNumber);
  }

  async function openPlanning() {
    await page.goto("/");
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Planning opérationnel/i })
      .click();
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    await page.getByRole("button", { name: "Suivante →" }).click();
    await expect(
      page.locator(`.planning-drop-day[data-day="${day}"]`).first(),
    ).toBeVisible();
  }

  try {
    const resourceCreate = await page.request.post("/api/v1/resources", {
      headers: { "X-E2E-Role": "ADMIN" },
      data: {
        name: resourceName,
        resource_class: "PROGRAMMEUR",
        competencies: "SCADA",
        sort_order: 8390,
      },
    });
    expect(resourceCreate.status(), await resourceCreate.text()).toBe(201);
    resourceId = String((await resourceCreate.json()).resource_id);

    const availabilityCreate = await page.request.post("/api/v1/availability-rules", {
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
    expect(availabilityCreate.status(), await availabilityCreate.text()).toBe(201);
    availabilityId = String((await availabilityCreate.json()).rule_id);

    const created = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        desired_start: day,
        desired_end: day,
        estimated_hours: 8,
        task_code: "210",
        proposed_technician: resourceName,
        description: "Confirmation post-approbation #839",
        confirmation: "Tentative",
        submit: true,
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
          comment: "Approbation E2E #839",
          expected_planning_version: Number(
            (await approvalSnapshot.json()).planning_version,
          ),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const actions = await page.request.get(
      `/api/v1/planning/actions?start=${day}&end=${day}&scope=global`,
    );
    expect(actions.ok(), await actions.text()).toBeTruthy();
    const assignment = (await actions.json() as Array<any>)
      .find((row) => row.demand_number === demandNumber && row.kind === "ASSIGNMENT");
    expect(assignment).toBeTruthy();

    const assigned = await page.request.post(
      `/api/v1/segments/${encodeURIComponent(String(assignment.segment_id))}/assign`,
      { data: { resource_id: resourceId } },
    );
    expect(assigned.status(), await assigned.text()).toBe(200);

    const initial = await demandShifts(demandNumber);
    expect(initial).toHaveLength(1);
    const invariant = {
      allocation_id: String(initial[0].allocation_id),
      segment_id: String(initial[0].segment_id),
      resource_id: String(initial[0].resource_id),
      hours: Number(initial[0].hours),
    };
    expect(initial[0].confirmation).toBe("Tentative");

    await openPlanning();
    const planningCard = page.locator(
      `.shift-card[data-allocation-id="${invariant.allocation_id}"]`,
    );
    await expect(planningCard).toHaveClass(/shift-tentative/);
    await expect(planningCard.locator(".confirmation-badge")).toHaveText("Tentative");

    await openDemand(demandNumber);
    const toggle = page.getByTestId("demand-confirmation-toggle");
    await expect(toggle).toHaveText("Confirmé");
    await toggle.click();
    await expect(toggle).toHaveText("Tentatif");

    let after = await demandShifts(demandNumber);
    expect(after).toHaveLength(1);
    expect(after[0].confirmation).toBe("Confirmée");
    expect({
      allocation_id: String(after[0].allocation_id),
      segment_id: String(after[0].segment_id),
      resource_id: String(after[0].resource_id),
      hours: Number(after[0].hours),
    }).toEqual(invariant);

    await openPlanning();
    await expect(planningCard).toHaveClass(/shift-confirmed/);
    await expect(planningCard.locator(".confirmation-badge")).toHaveText("Confirmée");

    await openDemand(demandNumber);
    await expect(toggle).toHaveText("Tentatif");
    await toggle.click();
    await expect(toggle).toHaveText("Confirmé");

    after = await demandShifts(demandNumber);
    expect(after[0].confirmation).toBe("Tentative");
    expect({
      allocation_id: String(after[0].allocation_id),
      segment_id: String(after[0].segment_id),
      resource_id: String(after[0].resource_id),
      hours: Number(after[0].hours),
    }).toEqual(invariant);

    await openPlanning();
    await expect(planningCard).toHaveClass(/shift-tentative/);

    // Retain browser acceptance for the normal editor path, not only the quick action.
    await openDemand(demandNumber);
    await page.getByLabel("Confirmation").selectOption("Confirmée");
    await page.getByRole("button", { name: "Enregistrer les modifications" }).click();
    await expect(page.locator(".demand-notice")).toContainText("Modification enregistrée");

    after = await demandShifts(demandNumber);
    expect(after[0].confirmation).toBe("Confirmée");
    expect({
      allocation_id: String(after[0].allocation_id),
      segment_id: String(after[0].segment_id),
      resource_id: String(after[0].resource_id),
      hours: Number(after[0].hours),
    }).toEqual(invariant);

    await openPlanning();
    await expect(planningCard).toHaveClass(/shift-confirmed/);
  } finally {
    if (availabilityId) {
      try {
        await page.request.post(
          `/api/v1/availability-rules/${encodeURIComponent(availabilityId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Failure-safe cleanup continues.
      }
    }
    if (resourceId) {
      try {
        await page.request.post(
          `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Failure-safe cleanup continues.
      }
    }
    await context.close();
  }
});
