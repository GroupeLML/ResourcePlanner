import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

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

function mondayOf(value: Date) {
  const next = new Date(value);
  const day = next.getDay();
  next.setDate(next.getDate() + (day === 0 ? -6 : 1 - day));
  return next;
}

test("843 projette automatiquement un actif REQUEST avec opérateur sur le quart couvert", async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  const fixtureKey = randomUUID();
  const suffix = fixtureKey.slice(0, 8).toUpperCase();
  const monday = mondayOf(new Date());
  const day = localIso(monday);
  let resourceId = "";
  let availabilityId = "";
  let assetId = "";
  let requirementId = "";
  let allocationId = "";
  let segmentId = "";

  try {
    const resourcesResponse = await page.request.get("/api/v1/resources?active_only=false");
    expect(resourcesResponse.ok(), await resourcesResponse.text()).toBeTruthy();
    const template = (await resourcesResponse.json() as Array<any>).find((row) => row.name === "Alice");
    expect(template).toBeTruthy();

    const resourceName = `Ressource E2E 843 ${suffix}`;
    const resourceCreate = await page.request.post("/api/v1/resources", {
      data: {
        name: resourceName,
        resource_class: template.resource_class,
        competencies: template.competencies,
        competency_ids: template.competency_ids,
        active: true,
        sort_order: 8430,
      },
    });
    expect(resourceCreate.status(), await resourceCreate.text()).toBe(201);
    resourceId = String((await resourceCreate.json()).resource_id);

    const availabilityCreate = await page.request.post("/api/v1/availability-rules", {
      headers: { "Idempotency-Key": `${fixtureKey}-availability` },
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

    const catalogResponse = await page.request.get("/api/v1/assets/catalog");
    const catalog = await catalogResponse.json() as { types: Array<any> };
    const assetType = catalog.types.find((row) => row.code === "VEH-744" && row.active)
      ?? catalog.types.find((row) => row.category === "VEHICLE" && row.active);
    expect(assetType).toBeTruthy();

    const assetCode = `REQ843-${suffix}`;
    const assetCreate = await page.request.post("/api/v1/assets", {
      data: { code: assetCode, label: `Actif demande 843 ${suffix}`, asset_type_id: assetType.id },
    });
    expect(assetCreate.status(), await assetCreate.text()).toBe(201);
    assetId = String((await assetCreate.json()).id);

    const demandCreate = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        description: `R27 #843 ${suffix}`,
        lines: [
          {
            position: 0,
            kind: "WORKFORCE",
            required_resource_class: template.resource_class,
            required_competency_ids: template.competency_ids,
            desired_start: day,
            desired_end: day,
            estimated_hours: 8,
            task_code: "210",
            proposed_resource_id: resourceId,
            asset_type_id: null,
            proposed_asset_id: null,
            confirmation: "Confirmée",
            description: "Besoin main-d'œuvre #843",
          },
          {
            position: 1,
            kind: "ASSET",
            required_resource_class: null,
            required_competency_ids: [],
            desired_start: day,
            desired_end: day,
            estimated_hours: null,
            task_code: "210",
            proposed_resource_id: null,
            asset_type_id: assetType.id,
            proposed_asset_id: null,
            confirmation: "Confirmée",
            description: "Actif demande #843",
          },
        ],
        submit: true,
      },
    });
    expect(demandCreate.status(), await demandCreate.text()).toBe(201);
    const demandNumber = String((await demandCreate.json()).demand_number);
    const demandResponse = await page.request.get(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}`,
    );
    expect(demandResponse.ok(), await demandResponse.text()).toBeTruthy();
    const demand = await demandResponse.json() as {
      lines: Array<{
        line_id: string;
        kind: "WORKFORCE" | "ASSET";
        asset_type_id: string | null;
      }>;
    };
    const assetLine = demand.lines.find(
      (row) => row.kind === "ASSET" && row.asset_type_id === assetType.id,
    );
    expect(assetLine).toBeTruthy();

    const snapshotResponse = await page.request.get(
      `/api/v1/planning/snapshot?start=${day}&end=${day}&scope=global`,
    );
    const snapshot = await snapshotResponse.json() as { planning_version: number };
    const approve = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      { data: { comment: "Approbation #843", expected_planning_version: snapshot.planning_version } },
    );
    expect(approve.status(), await approve.text()).toBe(200);

    const assetState = await page.request.get("/api/v1/assets/requirements");
    expect(assetState.ok(), await assetState.text()).toBeTruthy();
    const state = await assetState.json() as {
      requirements: Array<{
        id: string;
        origin: string;
        request_line_id: string | null;
        asset_type_id: string;
        start_date: string;
        end_date: string | null;
      }>;
      allocations: Array<{ requirement_id: string }>;
      planning_version: number;
    };
    const requestRequirement = state.requirements.find(
      (row) =>
        row.origin === "REQUEST"
        && row.request_line_id === assetLine!.line_id
        && row.asset_type_id === assetType.id,
    );
    expect(requestRequirement).toBeTruthy();
    requirementId = String(requestRequirement!.id);
    const coveredDay = String(requestRequirement!.start_date);
    expect(coveredDay).not.toBe("");
    if (requestRequirement!.end_date) {
      expect(coveredDay <= requestRequirement!.end_date).toBeTruthy();
    }

    const actions = await page.request.get(
      `/api/v1/planning/actions?start=${coveredDay}&end=${coveredDay}&scope=global`,
    );
    expect(actions.ok(), await actions.text()).toBeTruthy();
    const assignment = (await actions.json() as Array<any>).find(
      (row) => row.kind === "ASSIGNMENT" && row.demand_number === demandNumber,
    );
    expect(assignment).toBeTruthy();
    segmentId = String(assignment.segment_id);

    const assigned = await page.request.post(
      `/api/v1/segments/${encodeURIComponent(segmentId)}/assign`,
      { data: { resource_id: resourceId } },
    );
    expect(assigned.status(), await assigned.text()).toBe(200);

    const refreshedAssetState = await page.request.get("/api/v1/assets/requirements");
    expect(refreshedAssetState.ok(), await refreshedAssetState.text()).toBeTruthy();
    const refreshedState = await refreshedAssetState.json() as { planning_version: number };

    const reserve = await page.request.put(
      `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/reservation`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-reserve` },
        data: {
          asset_id: assetId,
          start_date: coveredDay,
          end_date: coveredDay,
          expected_planning_version: refreshedState.planning_version,
        },
      },
    );
    expect(reserve.status(), await reserve.text()).toBe(200);
    allocationId = String((await reserve.json()).allocation_id);

    const afterReserveState = await page.request.get("/api/v1/assets/requirements");
    const afterReserve = await afterReserveState.json() as { allocations: Array<any>; planning_version: number };
    expect(afterReserve.allocations.filter((row) => row.requirement_id === requirementId)).toHaveLength(1);

    const operator = await page.request.put(
      `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/operator`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-operator` },
        data: {
          operator_resource_id: resourceId,
          expected_planning_version: afterReserve.planning_version,
        },
      },
    );
    expect(operator.status(), await operator.text()).toBe(200);

    const shiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${coveredDay}&end=${coveredDay}`,
    );
    expect(shiftsResponse.ok(), await shiftsResponse.text()).toBeTruthy();
    const shifts = await shiftsResponse.json() as Array<any>;
    const targetShift = shifts.find(
      (row) => row.demand_number === demandNumber && row.resource_id === resourceId,
    );
    expect(targetShift).toBeTruthy();
    const inherited = targetShift.related_asset_reservations.filter(
      (row: any) => row.allocation_id === allocationId,
    );
    expect(inherited).toHaveLength(1);
    expect(inherited[0].association_kind).toBe("INHERITED_REQUEST");

    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    const currentWeek = mondayOf(new Date());
    const coveredWeek = mondayOf(new Date(`${coveredDay}T12:00:00`));
    const weekOffset = Math.round(
      (coveredWeek.getTime() - currentWeek.getTime()) / (7 * 24 * 60 * 60 * 1000),
    );
    const weekButton = weekOffset >= 0 ? "Suivante →" : "← Précédente";
    for (let index = 0; index < Math.abs(weekOffset); index += 1) {
      await page.getByRole("button", { name: weekButton, exact: true }).click();
    }

    const card = page
      .locator(`.planning-drop-day[data-resource-id="${resourceId}"][data-day="${coveredDay}"] .shift-card`)
      .filter({ hasText: demandNumber });
    await expect(card).toHaveCount(1);
    await expect(card.locator(".shift-assets")).toContainText(assetCode);
    await expect(card.locator(".shift-assets")).not.toContainText("Aucun actif");

    const finalStateResponse = await page.request.get("/api/v1/assets/requirements");
    const finalState = await finalStateResponse.json() as { allocations: Array<any> };
    expect(finalState.allocations.filter((row) => row.requirement_id === requirementId)).toHaveLength(1);
  } finally {
    if (requirementId) {
      try {
        const stateResponse = await page.request.get("/api/v1/assets/requirements");
        const state = await stateResponse.json() as { planning_version: number };
        await page.request.put(
          `/api/v1/assets/requirements/${encodeURIComponent(requirementId)}/reservation`,
          {
            headers: { "Idempotency-Key": `${fixtureKey}-release` },
            data: {
              asset_id: null,
              start_date: null,
              end_date: null,
              expected_planning_version: state.planning_version,
            },
          },
        );
      } catch {}
    }
    if (segmentId) {
      try { await page.request.post(`/api/v1/segments/${encodeURIComponent(segmentId)}/cancel`); } catch {}
    }
    if (assetId) {
      try {
        const stateResponse = await page.request.get("/api/v1/assets/requirements");
        const state = await stateResponse.json() as { planning_version: number };
        await page.request.patch(`/api/v1/assets/${encodeURIComponent(assetId)}/active`, {
          data: { active: false, expected_planning_version: state.planning_version },
        });
      } catch {}
    }
    if (availabilityId) {
      try { await page.request.post(`/api/v1/availability-rules/${encodeURIComponent(availabilityId)}/deactivate`); } catch {}
    }
    if (resourceId) {
      try { await page.request.post(`/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`); } catch {}
    }
    await context.close();
  }
});
