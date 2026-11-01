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
  const sourceName = `Ressource E2E 777 source ${fixtureKey}`;
  const targetName = `Ressource E2E 777 cible ${fixtureKey}`;
  const resourceIds: string[] = [];
  const availabilityIds: string[] = [];

  try {
    for (const [index, name] of [sourceName, targetName].entries()) {
      const resourceCreate = await page.request.post("/api/v1/resources", {
        headers: { "X-E2E-Role": "ADMIN" },
        data: {
          name,
          resource_class: "PROGRAMMEUR",
          competencies: "SCADA",
          sort_order: 7770 + index,
        },
      });
      expect(resourceCreate.status(), await resourceCreate.text()).toBe(201);
      const resourceId = String((await resourceCreate.json()).resource_id);
      resourceIds.push(resourceId);

      const availabilityCreate = await page.request.post("/api/v1/availability-rules", {
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
      expect(availabilityCreate.status(), await availabilityCreate.text()).toBe(201);
      availabilityIds.push(String((await availabilityCreate.json()).rule_id));
    }

    const created = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        desired_start: d1,
        desired_end: d5,
        estimated_hours: 40,
        task_code: "210",
        proposed_technician: sourceName,
        description: "Placement daté #777",
        submit: true,
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const demandNumber = String((await created.json()).demand_number);

    const approvalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d5}&scope=global`,
    );
    expect(approvalSnapshot.ok(), await approvalSnapshot.text()).toBeTruthy();
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "Approbation E2E #777",
          expected_planning_version: Number((await approvalSnapshot.json()).planning_version),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const afterApprovalShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(afterApprovalShiftsResponse.ok(), await afterApprovalShiftsResponse.text()).toBeTruthy();
    const afterApprovalShifts = (await afterApprovalShiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(afterApprovalShifts).toHaveLength(0);

    const afterApprovalActions = await page.request.get(
      `/api/v1/planning/actions?start=${d1}&end=${d5}&scope=global`,
    );
    expect(afterApprovalActions.ok(), await afterApprovalActions.text()).toBeTruthy();
    const initialAssignment = (await afterApprovalActions.json() as Array<any>)
      .find((row) => row.demand_number === demandNumber && row.kind === "ASSIGNMENT");
    expect(initialAssignment).toBeTruthy();

    const explicitBaseline = await page.request.post(
      `/api/v1/segments/${encodeURIComponent(String(initialAssignment.segment_id))}/assign`,
      { data: { resource_id: resourceIds[0] } },
    );
    expect(explicitBaseline.status(), await explicitBaseline.text()).toBe(200);

    const initialShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(initialShiftsResponse.ok(), await initialShiftsResponse.text()).toBeTruthy();
    const initialShifts = (await initialShiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(initialShifts).toHaveLength(5);

    const returnTarget = [...initialShifts].sort(
      (left, right) => String(left.work_date).localeCompare(String(right.work_date)),
    )[0];
    const preview = await page.request.get(
      `/api/v1/allocations/${encodeURIComponent(returnTarget.allocation_id)}/work-queue-preview`,
    );
    expect(preview.ok(), await preview.text()).toBeTruthy();
    const contract = await preview.json() as Record<string, any>;
    const returned = await page.request.post(
      `/api/v1/allocations/${encodeURIComponent(returnTarget.allocation_id)}/return-to-work-queue`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-return` },
        data: {
          expected_planning_version: contract.planning_version,
          expected_approval_revision_id: contract.approval_revision_id,
          expected_operational_version: contract.operational_version,
        },
      },
    );
    expect(returned.status(), await returned.text()).toBe(200);

    const beforeDropResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    const beforeDrop = (await beforeDropResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(beforeDrop).toHaveLength(4);
    const survivorIds = beforeDrop.map((row) => String(row.allocation_id)).sort();
    const survivorState = beforeDrop
      .map((row) => ({
        allocation_id: String(row.allocation_id),
        work_date: String(row.work_date),
        hours: Number(row.hours),
      }))
      .sort((left, right) => left.allocation_id.localeCompare(right.allocation_id));

    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: "Suivante →" }).click();

    const approvedCard = page
      .locator(".planning-work-queue .planning-action-card.action-assignment")
      .filter({ hasText: demandNumber });
    await expect(approvedCard).toHaveCount(1);
    await expect(approvedCard).toBeVisible();
    await expect(approvedCard).toHaveAttribute("draggable", "true");
    const segmentId = await approvedCard.getAttribute("data-segment-id");
    expect(segmentId).toBeTruthy();

    const targetCell = page.locator(
      `.planning-drop-day[data-resource-id="${resourceIds[1]}"][data-day="${d4}"]`,
    );
    await expect(targetCell).toBeVisible();

    const previewPath = `/api/v1/segments/${encodeURIComponent(segmentId!)}/place-from-drop/preview`;
    const mutationPath = `/api/v1/segments/${encodeURIComponent(segmentId!)}/place-from-drop`;
    const previewRequestPromise = page.waitForRequest(
      (request) => request.method() === "POST" && new URL(request.url()).pathname === previewPath,
    );
    const previewResponsePromise = page.waitForResponse(
      (response) => response.request().method() === "POST" && new URL(response.url()).pathname === previewPath,
    );
    const mutationRequestPromise = page.waitForRequest(
      (request) => request.method() === "POST" && new URL(request.url()).pathname === mutationPath,
    );
    const mutationResponsePromise = page.waitForResponse(
      (response) => response.request().method() === "POST" && new URL(response.url()).pathname === mutationPath,
    );

    await approvedCard.dragTo(targetCell);

    const previewRequest = await previewRequestPromise;
    const previewResponse = await previewResponsePromise;
    expect(previewResponse.status(), await previewResponse.text()).toBe(200);
    const previewPayload = previewRequest.postDataJSON() as Record<string, any>;
    expect(previewPayload).toMatchObject({
      resource_id: resourceIds[1],
      dropped_date: d4,
    });
    const previewResult = await previewResponse.json() as Record<string, any>;
    expect(previewResult.allowed).toBe(true);
    expect(previewResult.segment_id).toBe(segmentId);
    expect(previewResult.resource_id).toBe(resourceIds[1]);
    expect(previewResult.proposed_placement_not_before_date).toBe(d4);

    const mutationRequest = await mutationRequestPromise;
    const mutationResponse = await mutationResponsePromise;
    expect(mutationResponse.status(), await mutationResponse.text()).toBe(200);
    const mutationPayload = mutationRequest.postDataJSON() as Record<string, any>;
    expect(mutationPayload).toEqual({
      resource_id: resourceIds[1],
      dropped_date: d4,
      expected_planning_version: previewResult.planning_version,
      expected_approval_revision_id: previewResult.approval_revision_id,
      expected_operational_version: previewResult.operational_version,
    });
    const idempotencyKey = mutationRequest.headers()["idempotency-key"];
    expect(idempotencyKey).toBeTruthy();

    await expect(page.locator(".planning-drag-feedback"))
      .toContainText(`Besoin assigné à ${targetName} à partir du ${d4}`);

    const afterDropResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(afterDropResponse.ok(), await afterDropResponse.text()).toBeTruthy();
    const afterDrop = (await afterDropResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    const survivorsAfter = afterDrop
      .filter((row) => survivorIds.includes(String(row.allocation_id)))
      .map((row) => String(row.allocation_id)).sort();
    expect(survivorsAfter).toEqual(survivorIds);
    const survivorStateAfter = afterDrop
      .filter((row) => survivorIds.includes(String(row.allocation_id)))
      .map((row) => ({
        allocation_id: String(row.allocation_id),
        work_date: String(row.work_date),
        hours: Number(row.hours),
      }))
      .sort((left, right) => left.allocation_id.localeCompare(right.allocation_id));
    expect(survivorStateAfter).toEqual(survivorState);

    const generated = afterDrop.filter(
      (row) => !survivorIds.includes(String(row.allocation_id)),
    );
    expect(generated.length).toBeGreaterThan(0);
    expect(generated.every((row) => row.resource_id === resourceIds[1])).toBeTruthy();
    expect(generated.every((row) => String(row.work_date) >= d4)).toBeTruthy();
    expect(generated.reduce((sum, row) => sum + Number(row.hours), 0)).toBe(8);
    expect(afterDrop.reduce((sum, row) => sum + Number(row.hours), 0)).toBe(40);

    await expect(approvedCard).toHaveCount(0);
    await expect(page.locator(".planning-drag-feedback"))
      .toContainText(`Besoin assigné à ${targetName} à partir du ${d4}`);
  } finally {
    for (const ruleId of availabilityIds) {
      try {
        await page.request.post(
          `/api/v1/availability-rules/${encodeURIComponent(ruleId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Failure-safe cleanup continues with the remaining fixture objects.
      }
    }
    for (const resourceId of resourceIds) {
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
