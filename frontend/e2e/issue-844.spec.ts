import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

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

test("844 permet un actif possédé et une réservation projet héritée sur le même quart", async ({ browser }) => {
  test.setTimeout(180_000);
  const fixtureKey = randomUUID();
  const suffix = fixtureKey.slice(0, 8).toUpperCase();
  const day = localIso(mondayOf(new Date()));
  const admin = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const setup = await admin.newPage();
  let resourceId = "";
  let availabilityId = "";
  let segmentId = "";
  let shiftAllocationId = "";
  let ownedAssetId = "";
  let ownedRequirementId = "";
  let ownedAllocationId = "";
  let projectAssetId = "";
  let projectRequirementId = "";
  let projectAllocationId = "";

  try {
    const resourcesResponse = await setup.request.get("/api/v1/resources?active_only=false");
    expect(resourcesResponse.ok(), await resourcesResponse.text()).toBeTruthy();
    const template = (await resourcesResponse.json() as Array<any>).find((row) => row.name === "Alice");
    expect(template).toBeTruthy();

    const resourceName = `Ressource E2E 844 ${suffix}`;
    const resourceCreate = await setup.request.post("/api/v1/resources", {
      data: {
        name: resourceName,
        resource_class: template.resource_class,
        competencies: template.competencies,
        competency_ids: template.competency_ids,
        active: true,
        sort_order: 8440,
      },
    });
    expect(resourceCreate.status(), await resourceCreate.text()).toBe(201);
    resourceId = String((await resourceCreate.json()).resource_id);

    const availabilityCreate = await setup.request.post("/api/v1/availability-rules", {
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

    const catalogResponse = await setup.request.get("/api/v1/assets/catalog");
    expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy();
    const catalog = await catalogResponse.json() as { types: Array<any> };
    const assetType = catalog.types.find((row) => row.code === "VEH-744" && row.active);
    expect(assetType).toBeTruthy();

    const ownedCode = `R28A-${suffix}`;
    const projectCode = `R28B-${suffix}`;
    const ownedCreate = await setup.request.post("/api/v1/assets", {
      data: { code: ownedCode, label: `R28 owned ${suffix}`, asset_type_id: assetType.id },
    });
    expect(ownedCreate.status(), await ownedCreate.text()).toBe(201);
    ownedAssetId = String((await ownedCreate.json()).id);
    const projectCreate = await setup.request.post("/api/v1/assets", {
      data: { code: projectCode, label: `R28 project ${suffix}`, asset_type_id: assetType.id },
    });
    expect(projectCreate.status(), await projectCreate.text()).toBe(201);
    projectAssetId = String((await projectCreate.json()).id);

    const demandCreate = await setup.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        description: `R28 #844 ${suffix}`,
        lines: [{
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
          description: "Besoin main-d'œuvre #844",
        }],
        submit: true,
      },
    });
    expect(demandCreate.status(), await demandCreate.text()).toBe(201);
    const demandNumber = String((await demandCreate.json()).demand_number);

    const snapshotResponse = await setup.request.get(
      `/api/v1/planning/snapshot?start=${day}&end=${day}&scope=global`,
    );
    expect(snapshotResponse.ok(), await snapshotResponse.text()).toBeTruthy();
    const approve = await setup.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      { data: { comment: "Approbation #844", expected_planning_version: (await snapshotResponse.json()).planning_version } },
    );
    expect(approve.status(), await approve.text()).toBe(200);

    const actions = await setup.request.get(
      `/api/v1/planning/actions?start=${day}&end=${day}&scope=global`,
    );
    expect(actions.ok(), await actions.text()).toBeTruthy();
    const assignment = (await actions.json() as Array<any>).find(
      (row) => row.kind === "ASSIGNMENT" && row.demand_number === demandNumber,
    );
    expect(assignment).toBeTruthy();
    segmentId = String(assignment.segment_id);
    const assigned = await setup.request.post(
      `/api/v1/segments/${encodeURIComponent(segmentId)}/assign`,
      { data: { resource_id: resourceId } },
    );
    expect(assigned.status(), await assigned.text()).toBe(200);

    const shiftsResponse = await setup.request.get(`/api/v1/shifts?start=${day}&end=${day}`);
    expect(shiftsResponse.ok(), await shiftsResponse.text()).toBeTruthy();
    const targetShift = (await shiftsResponse.json() as Array<any>).find(
      (row) => row.demand_number === demandNumber && row.resource_id === resourceId,
    );
    expect(targetShift).toBeTruthy();
    shiftAllocationId = String(targetShift.allocation_id);
    const projectId = String(targetShift.project_id);

    let stateResponse = await setup.request.get("/api/v1/assets/requirements");
    let state = await stateResponse.json() as { planning_version: number };
    const owned = await setup.request.put(
      `/api/v1/assets/shifts/${encodeURIComponent(shiftAllocationId)}/assignment`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-owned` },
        data: {
          asset_id: ownedAssetId,
          asset_requirement_id: null,
          asset_allocation_id: null,
          start_date: day,
          end_date: day,
          expected_planning_version: state.planning_version,
        },
      },
    );
    expect(owned.status(), await owned.text()).toBe(200);
    const ownedPayload = await owned.json() as any;
    ownedRequirementId = String(ownedPayload.requirement_id);
    ownedAllocationId = String(ownedPayload.allocation_id);
    expect(ownedPayload.requirement_origin).toBe("SHIFT_AD_HOC");

    stateResponse = await setup.request.get("/api/v1/assets/requirements");
    state = await stateResponse.json() as { planning_version: number };
    const projectReservation = await setup.request.post("/api/v1/assets/project-reservations", {
      headers: { "Idempotency-Key": `${fixtureKey}-project` },
      data: {
        project_id: projectId,
        asset_type_id: assetType.id,
        asset_id: projectAssetId,
        start_date: day,
        end_date: day,
        operator_resource_id: null,
        expected_planning_version: state.planning_version,
      },
    });
    expect(projectReservation.status(), await projectReservation.text()).toBe(201);
    const projectPayload = await projectReservation.json() as any;
    projectRequirementId = String(projectPayload.requirement_id);
    projectAllocationId = String(projectPayload.allocation_id);

    const coordinator = await browser.newContext({
      baseURL: BASE_URL,
      locale: "fr-CA",
      extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
    });
    const page = await coordinator.newPage();
    try {
      await page.goto("/");
      await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
      await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
      await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();

      const card = page
        .locator(`.planning-drop-day[data-resource-id="${resourceId}"][data-day="${day}"] .shift-card`)
        .filter({ hasText: demandNumber });
      await expect(card).toHaveCount(1);
      await expect(card.locator(".shift-assets")).toContainText(ownedCode);
      await expect(card.locator(".shift-assets")).not.toContainText(projectCode);

      await card.getByRole("button", { name: /Associer une réservation projet/ }).click();
      const dialog = page.getByRole("dialog", { name: "Associer une réservation projet" });
      await expect(dialog).toBeVisible();
      const input = dialog.getByRole("combobox", { name: "Actif", exact: true });
      await input.fill(projectCode);
      await dialog.getByRole("option", { name: new RegExp(projectCode) }).click();
      await expect(dialog).toContainText("Aucune nouvelle réservation physique n’est créée.");
      await dialog.getByRole("button", { name: "Associer la réservation", exact: true }).click();
      await expect(dialog).toBeHidden();

      await expect(card.locator(".shift-assets")).toContainText(ownedCode);
      await expect(card.locator(".shift-assets")).toContainText(projectCode);

      await card.locator(".shift-card-main").click();
      const editor = page.getByRole("dialog", { name: "Modifier le quart" });
      await expect(editor).toContainText(ownedCode);
      await expect(editor).toContainText(projectCode);
      await editor.getByRole("button", { name: "Fermer" }).click();

      const finalShifts = await page.request.get(`/api/v1/shifts?start=${day}&end=${day}`);
      expect(finalShifts.ok(), await finalShifts.text()).toBeTruthy();
      const finalShift = (await finalShifts.json() as Array<any>).find(
        (row) => row.demand_number === demandNumber && row.resource_id === resourceId,
      );
      expect(finalShift.asset_assignment.allocation_id).toBe(ownedAllocationId);
      const inherited = finalShift.related_asset_reservations.filter(
        (row: any) => row.allocation_id === projectAllocationId,
      );
      expect(inherited).toHaveLength(1);
      expect(inherited[0].association_kind).toBe("INHERITED_PROJECT_DIRECT");

      const finalStateResponse = await page.request.get("/api/v1/assets/requirements");
      const finalState = await finalStateResponse.json() as { allocations: Array<any> };
      const physical = finalState.allocations.filter(
        (row) => row.id === ownedAllocationId || row.id === projectAllocationId,
      );
      expect(physical).toHaveLength(2);
      expect(new Set(physical.map((row) => row.id)).size).toBe(2);
      const projectAllocation = physical.find((row) => row.id === projectAllocationId);
      expect(projectAllocation.operator_resource_id).toBe(resourceId);
    } finally {
      await coordinator.close();
    }
  } finally {
    try {
      let response = await setup.request.get("/api/v1/assets/requirements");
      let state = await response.json() as { planning_version: number };
      if (projectRequirementId) {
        await setup.request.delete(
          `/api/v1/assets/project-reservations/${encodeURIComponent(projectRequirementId)}`,
          {
            params: { expected_planning_version: state.planning_version },
            headers: { "Idempotency-Key": `${fixtureKey}-project-release` },
          },
        );
        response = await setup.request.get("/api/v1/assets/requirements");
        state = await response.json() as { planning_version: number };
      }
      if (ownedRequirementId && shiftAllocationId) {
        await setup.request.put(
          `/api/v1/assets/shifts/${encodeURIComponent(shiftAllocationId)}/assignment`,
          {
            headers: { "Idempotency-Key": `${fixtureKey}-owned-release` },
            data: {
              asset_id: null,
              asset_requirement_id: ownedRequirementId,
              asset_allocation_id: null,
              start_date: null,
              end_date: null,
              expected_planning_version: state.planning_version,
            },
          },
        );
      }
    } catch {}
    if (segmentId) {
      try { await setup.request.post(`/api/v1/segments/${encodeURIComponent(segmentId)}/cancel`); } catch {}
    }
    for (const assetId of [ownedAssetId, projectAssetId].filter(Boolean)) {
      try {
        const response = await setup.request.get("/api/v1/assets/requirements");
        const state = await response.json() as { planning_version: number };
        await setup.request.patch(`/api/v1/assets/${encodeURIComponent(assetId)}/active`, {
          data: { active: false, expected_planning_version: state.planning_version },
        });
      } catch {}
    }
    if (availabilityId) {
      try { await setup.request.post(`/api/v1/availability-rules/${encodeURIComponent(availabilityId)}/deactivate`); } catch {}
    }
    if (resourceId) {
      try { await setup.request.post(`/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`); } catch {}
    }
    await admin.close();
  }
});
