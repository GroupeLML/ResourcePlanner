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

test("778 expose les trois actions ADR-034 et confirme leurs mutations", async ({ browser, request }) => {
  test.setTimeout(120_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();

  let shiftPosts = 0;
  let segmentPosts = 0;
  let reductionPosts = 0;
  let lastShiftPayload: Record<string, unknown> | null = null;
  let lastSegmentPayload: Record<string, unknown> | null = null;
  let lastReductionPayload: Record<string, unknown> | null = null;
  let lastIdempotencyKey: string | null = null;

  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await request.get(route.request().url(), { headers: { "X-E2E-Role": "COORDINATOR" } });
    const body = await response.json() as Record<string, any>;
    const resource = body.resources?.[0];
    if (!resource) {
      await route.fulfill({
        status: response.status(),
        headers: response.headers(),
        contentType: "application/json",
        body: JSON.stringify(body),
      });
      return;
    }
    const day = body.start;
    body.planning_version = 778;
    body.shifts = [
      ...(body.shifts || []).filter((row: any) => row.allocation_id !== "ALLOC-778"),
      {
        allocation_id: "ALLOC-778",
        segment_id: "SEG-778",
        requirement_id: "REQ-778",
        resource_id: resource.id,
        resource_name: resource.name,
        work_date: day,
        hours: 8,
        allocation_type: "FLEXIBLE",
        source: "AUTO",
        locked: false,
        outside_standard_hours: false,
        confirmation: "Confirmée",
        confirmation_override: null,
        load_kind: "FIRM",
        note: "Issue 778 E2E",
        demand_number: "DMO-778",
        project_number: "P-778",
        project_name: "Projet 778 E2E",
        project_manager: "CP 778",
        requester: "Demandeur 778",
        asset_assignment: null,
        related_asset_reservations: [],
        asset_actions: {
          assign: { allowed: true, reason: null },
          change: { allowed: false, reason: "asset_assignment_missing" },
          release: { allowed: false, reason: "asset_assignment_missing" },
        },
        asset_diagnostics: [],
      },
    ];
    await route.fulfill({
      status: response.status(),
      headers: response.headers(),
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });

  await page.route("**/api/v1/demands/DMO-778/detail**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        demand: { number: "DMO-778" },
        version: 14,
        lines: [],
        periods: [],
        materialized_plan: {
          requirement_count: 1,
          planned_hours: 40,
          covered_hours: 40,
          locked_hours: 0,
          requirements: [{
            requirement_id: "REQ-778",
            segment_id: "SEG-778",
            source_request_line_id: "LINE-778",
            status: "Planifié",
            start_date: "2026-10-26",
            end_date: "2026-10-30",
            planned_hours: 40,
            covered_hours: 40,
            locked_hours: 0,
            remaining_hours: 0,
            excess_hours: 0,
            automatic_target_resource_id: null,
            automatic_target_resource_name: null,
            mobilized_resources: [],
            approval_revision_id: "REV-778",
            approved_entry_key: "ENTRY-778",
            approval_reference_status: "CAPTURED",
          }],
          asset_requirement_count: 0,
          asset_assigned_count: 0,
          asset_usage_hours: 0,
          asset_unbudgeted_requirement_count: 0,
          asset_requirements: [],
        },
        workflow: { demand_number: "DMO-778", status: "En planification", version: 14, available_actions: [], actions: [] },
        approval_cycle: null,
        approval_state: {
          active_revision_id: "REV-778",
          approved_request_version: 13,
          operational_version: 7,
          envelope_decision: null,
          envelope_reason: null,
          approval_reference_status: "CAPTURED",
        },
        policy: {
          can_modify_candidate: true,
          can_edit_periods: true,
          can_change_operational_choices: true,
          editable_candidate_fields: [],
          expected_request_version: 14,
          expected_operational_version: 7,
          envelope_decision: null,
          reapproval_required: false,
        },
        diagnostics: [],
        technical_context: null,
      }),
    });
  });

  await page.route("**/api/v1/allocations/ALLOC-778/work-queue-preview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        allocation_id: "ALLOC-778",
        requirement_id: "REQ-778",
        segment_id: "SEG-778",
        demand_number: "DMO-778",
        source_request_line_id: "LINE-778",
        planning_version: 778,
        request_version: 14,
        approval_revision_id: "REV-778",
        operational_version: 7,
        shift_hours: 8,
        effective_need: 40,
        covered_hours: 40,
        remaining_hours: 0,
        return_shift_allowed: true,
        return_shift_block_code: null,
        return_segment_allowed: true,
        return_segment_block_code: null,
        permanent_reduction_allowed: true,
        permanent_reduction_block_code: null,
        effective_need_after_reduction: 32,
        remaining_after_return_shift: 8,
        remaining_after_return_segment: 40,
        remaining_after_reduction: 0,
      }),
    });
  });

  const fulfillMutation = async (
    route: any,
    operation: string,
    onPayload: (payload: Record<string, unknown>) => void,
  ) => {
    const request = route.request();
    onPayload(request.postDataJSON() as Record<string, unknown>);
    lastIdempotencyKey = request.headers()["idempotency-key"] || null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        operation,
        requirement_id: "REQ-778",
        segment_id: "SEG-778",
        demand_number: "DMO-778",
        planning_version: 779,
        approval_revision_id: "REV-778",
        operational_version: 7,
        request_version: operation === "PERMANENTLY_REDUCE_SHIFT_HOURS" ? 15 : null,
        shift_id: "SHIFT-778",
        release_id: operation === "RETURN_SHIFT_TO_WORK_QUEUE" ? "REL-778" : null,
        reduction_id: operation === "PERMANENTLY_REDUCE_SHIFT_HOURS" ? "RED-778" : null,
        removed_shift_count: operation === "RETURN_SEGMENT_TO_WORK_QUEUE" ? 5 : 1,
        removed_hours: operation === "RETURN_SEGMENT_TO_WORK_QUEUE" ? 40 : 8,
        effective_need_before: 40,
        effective_need_after: operation === "PERMANENTLY_REDUCE_SHIFT_HOURS" ? 32 : 40,
        covered_hours_after: 32,
        remaining_hours_after: operation === "RETURN_SHIFT_TO_WORK_QUEUE" ? 8 : 0,
        no_op: false,
      }),
    });
  };

  await page.route("**/api/v1/allocations/ALLOC-778/return-to-work-queue", async (route) => {
    shiftPosts += 1;
    await fulfillMutation(route, "RETURN_SHIFT_TO_WORK_QUEUE", (payload) => { lastShiftPayload = payload; });
  });
  await page.route("**/api/v1/segments/REQ-778/return-to-work-queue", async (route) => {
    segmentPosts += 1;
    await fulfillMutation(route, "RETURN_SEGMENT_TO_WORK_QUEUE", (payload) => { lastSegmentPayload = payload; });
  });
  await page.route("**/api/v1/allocations/ALLOC-778/permanent-reduction", async (route) => {
    reductionPosts += 1;
    await fulfillMutation(route, "PERMANENTLY_REDUCE_SHIFT_HOURS", (payload) => { lastReductionPayload = payload; });
  });

  async function openShift() {
    const card = page.locator('.shift-card-main[title*="Issue 778 E2E"]');
    await expect(card).toBeVisible();
    await card.click();
    return page.getByRole("dialog", { name: "Modifier le quart" });
  }

  try {
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();

    let dialog = await openShift();
    const actions = dialog.getByTestId("request-work-queue-actions");
    await expect(actions.getByRole("button", { name: "Retourner ce quart à la File de Travail" })).toBeVisible();
    await expect(actions.getByRole("button", { name: "Retourner le segment à la File de Travail" })).toBeVisible();
    await expect(actions.getByRole("button", { name: "Supprimer définitivement ce quart" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Revenir à l’automatique" })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Supprimer le quart manuel" })).toHaveCount(0);

    await actions.getByRole("button", { name: "Retourner ce quart à la File de Travail" }).click();
    let confirm = dialog.getByTestId("request-work-queue-confirmation");
    await expect(confirm).toContainText("Le quart de 8 h sera retiré");
    await confirm.getByRole("button", { name: "Retourner le quart" }).click();
    await expect(dialog).toBeHidden();
    expect(shiftPosts).toBe(1);
    expect(lastShiftPayload).toEqual({
      expected_planning_version: 778,
      expected_approval_revision_id: "REV-778",
      expected_operational_version: 7,
    });
    expect(lastIdempotencyKey).toBeTruthy();

    dialog = await openShift();
    await dialog.getByTestId("request-work-queue-actions")
      .getByRole("button", { name: "Retourner le segment à la File de Travail" }).click();
    confirm = dialog.getByTestId("request-work-queue-confirmation");
    await expect(confirm).toContainText("Tous les quarts opérationnels du segment SEG-778");
    await confirm.getByRole("button", { name: "Retourner le segment" }).click();
    await expect(dialog).toBeHidden();
    expect(segmentPosts).toBe(1);
    expect(lastSegmentPayload).toEqual({
      expected_planning_version: 778,
      expected_approval_revision_id: "REV-778",
      expected_operational_version: 7,
    });

    dialog = await openShift();
    await dialog.getByTestId("request-work-queue-actions")
      .getByRole("button", { name: "Supprimer définitivement ce quart" }).click();
    let permanentDialog = page.getByTestId("permanent-reduction-confirmation-dialog");
    await expect(permanentDialog).toBeVisible();
    await expect(permanentDialog).toContainText("Supprimer définitivement ce quart?");
    await expect(permanentDialog).toContainText("8 h");
    await expect(permanentDialog).toContainText("Segment SEG-778");
    await expect(permanentDialog).toContainText("Demande DMO-778 / ligne LINE-778");
    await expect(permanentDialog).toContainText("40 h → 32 h");
    await expect(permanentDialog).toContainText("Reliquat File de Travail après suppression : 0 h");
    await expect(permanentDialog).toContainText("ne retourneront pas dans la File de Travail");

    await permanentDialog.getByRole("button", { name: "Annuler" }).click();
    await expect(permanentDialog).toBeHidden();
    expect(reductionPosts).toBe(0);
    await dialog.getByTestId("request-work-queue-actions")
      .getByRole("button", { name: "Supprimer définitivement ce quart" }).click();
    permanentDialog = page.getByTestId("permanent-reduction-confirmation-dialog");
    await permanentDialog.getByRole("button", { name: "Supprimer définitivement" }).click();
    await expect(dialog).toBeHidden();
    expect(reductionPosts).toBe(1);
    expect(lastReductionPayload).toEqual({
      expected_planning_version: 778,
      expected_approval_revision_id: "REV-778",
      expected_operational_version: 7,
      expected_request_version: 14,
    });
  } finally {
    await context.close();
  }
});


test("778 SegmentEditor conserve la base opérationnelle et utilise les choix backend réels", async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  const monday = addDays(startOfWeek(new Date()), 7);
  const d1 = localIso(monday);
  const d5 = localIso(addDays(monday, 4));
  let resourceId: string | null = null;
  let availabilityRuleId: string | null = null;
  const fixtureKey = randomUUID();
  const resourceName = `Ressource E2E 778 ${fixtureKey}`;
  const resourcesBefore = await page.request.get("/api/v1/resources?active_only=true");
  expect(resourcesBefore.ok(), await resourcesBefore.text()).toBeTruthy();
  const originalResourceIds = (await resourcesBefore.json() as Array<{ id: string }>)
    .map((row) => row.id).sort();
  const orderBefore = await page.request.get("/api/v1/planning/resource-order");
  expect(orderBefore.ok(), await orderBefore.text()).toBeTruthy();
  const originalOrder = (await orderBefore.json()).positions;

  const openSegmentEditor = async (demandNumber: string) => {
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Demandes/i }).click();
    await page.getByRole("button", { name: "Segments", exact: true }).click();
    const demandCard = page.locator(".segment-demand-card").filter({ hasText: demandNumber }).first();
    await expect(demandCard).toBeVisible();
    await demandCard.click();
    const segmentCard = page.locator(".segment-list .segment-card")
      .filter({ hasText: d1 })
      .filter({ hasText: d5 })
      .first();
    await expect(segmentCard).toBeVisible();
    await segmentCard.click();
    const dialog = page.getByRole("dialog", { name: "Modifier le segment" });
    await expect(dialog).toBeVisible();
    return dialog;
  };

  try {
    const resourceCreate = await page.request.post("/api/v1/resources", {
      headers: { "X-E2E-Role": "ADMIN" },
      data: {
        name: resourceName,
        resource_class: "PROGRAMMEUR",
        competencies: "SCADA",
        sort_order: 778,
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
        weekdays: "Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
        start_time: "08:00:00",
        end_time: "16:00:00",
        active: true,
      },
    });
    expect(availabilityCreate.status(), await availabilityCreate.text()).toBe(201);
    availabilityRuleId = String((await availabilityCreate.json()).rule_id);

    const created = await page.request.post("/api/v1/demands", {
      data: {
        project_number: "P-251",
        desired_start: d1,
        desired_end: d5,
        estimated_hours: 40,
        task_code: "210",
        proposed_technician: resourceName,
        description: "Base opérationnelle #778",
        submit: true,
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const demandNumber = String((await created.json()).demand_number);

    const approvalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d5}&scope=global`,
    );
    expect(approvalSnapshot.ok(), await approvalSnapshot.text()).toBeTruthy();
    const planningVersion = Number((await approvalSnapshot.json()).planning_version);
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "Approbation E2E base #778",
          expected_planning_version: planningVersion,
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const shiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(shiftsResponse.ok(), await shiftsResponse.text()).toBeTruthy();
    const approvedShifts = (await shiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(approvedShifts).toHaveLength(5);
    const segmentId = String(approvedShifts[0].requirement_id || approvedShifts[0].segment_id);

    const reductionTarget = approvedShifts[approvedShifts.length - 1];
    const reductionPreview = await page.request.get(
      `/api/v1/allocations/${encodeURIComponent(reductionTarget.allocation_id)}/work-queue-preview`,
    );
    expect(reductionPreview.ok(), await reductionPreview.text()).toBeTruthy();
    const reductionContract = await reductionPreview.json() as Record<string, any>;
    const reduced = await page.request.post(
      `/api/v1/allocations/${encodeURIComponent(reductionTarget.allocation_id)}/permanent-reduction`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-reduction` },
        data: {
          expected_planning_version: reductionContract.planning_version,
          expected_request_version: reductionContract.request_version,
          expected_approval_revision_id: reductionContract.approval_revision_id,
          expected_operational_version: reductionContract.operational_version,
        },
      },
    );
    expect(reduced.status(), await reduced.text()).toBe(200);

    const afterReductionShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(afterReductionShiftsResponse.ok(), await afterReductionShiftsResponse.text()).toBeTruthy();
    const afterReductionShifts = (await afterReductionShiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(afterReductionShifts).toHaveLength(4);
    expect(afterReductionShifts.map((row) => Number(row.hours)).sort()).toEqual([8, 8, 8, 8]);

    const returnTarget = afterReductionShifts[0];
    const returnPreview = await page.request.get(
      `/api/v1/allocations/${encodeURIComponent(returnTarget.allocation_id)}/work-queue-preview`,
    );
    expect(returnPreview.ok(), await returnPreview.text()).toBeTruthy();
    const returnContract = await returnPreview.json() as Record<string, any>;
    const returned = await page.request.post(
      `/api/v1/allocations/${encodeURIComponent(returnTarget.allocation_id)}/return-to-work-queue`,
      {
        headers: { "Idempotency-Key": `${fixtureKey}-return` },
        data: {
          expected_planning_version: returnContract.planning_version,
          expected_approval_revision_id: returnContract.approval_revision_id,
          expected_operational_version: returnContract.operational_version,
        },
      },
    );
    expect(returned.status(), await returned.text()).toBe(200);

    const preservedShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(preservedShiftsResponse.ok(), await preservedShiftsResponse.text()).toBeTruthy();
    const preservedShifts = (await preservedShiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(preservedShifts).toHaveLength(3);
    expect(preservedShifts.map((row) => Number(row.hours)).sort()).toEqual([8, 8, 8]);
    const survivorIds = preservedShifts.map((row) => String(row.allocation_id)).sort();

    let segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(segmentId)}`,
    );
    expect(segmentResponse.ok(), await segmentResponse.text()).toBeTruthy();
    let segment = await segmentResponse.json() as Record<string, any>;
    expect(segment.planned_hours).toBe(32);
    expect(segment.operational_base_hours).toBe(40);
    const preservedSnapshotResponse = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d5}&scope=global`,
    );
    expect(preservedSnapshotResponse.ok(), await preservedSnapshotResponse.text()).toBeTruthy();
    const preservedSnapshot = await preservedSnapshotResponse.json() as Record<string, any>;
    const preservedSegment = (preservedSnapshot.segments as Array<any>)
      .find((row) => row.demand_number === demandNumber);
    expect(preservedSegment?.planned_hours).toBe(32);
    expect(preservedSegment?.covered_hours).toBe(24);
    expect(preservedSegment?.remaining_hours).toBe(8);

    let dialog = await openSegmentEditor(demandNumber);
    await expect(dialog.getByLabel("Heures prévues")).toHaveValue("40");
    await dialog.getByLabel("Description").fill("Description seulement #778");
    const [descriptionSave] = await Promise.all([
      page.waitForResponse((response) => response.request().method() === "PATCH"
        && new URL(response.url()).pathname.startsWith("/api/v1/segments/")),
      dialog.getByRole("button", { name: "Enregistrer", exact: true }).click(),
    ]);
    expect(descriptionSave.status(), await descriptionSave.text()).toBe(200);
    expect(descriptionSave.request().postDataJSON()).toMatchObject({
      planned_hours: 40, description: "Description seulement #778",
    });
    await expect(dialog).toBeHidden();

    segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(segmentId)}`,
    );
    segment = await segmentResponse.json() as Record<string, any>;
    expect(segment.description).toBe("Description seulement #778");
    expect(segment.operational_base_hours).toBe(40);
    expect(segment.planned_hours).toBe(32);

    dialog = await openSegmentEditor(demandNumber);
    await expect(dialog.getByLabel("Heures prévues")).toHaveValue("40");
    await dialog.getByLabel("Heures prévues").fill("28");
    await dialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    const adjustChoice = dialog.getByRole("alert");
    await expect(adjustChoice).toContainText("Heures prévues proposées : 20 h");
    await expect(adjustChoice).toContainText("excédent : +4 h");
    const adjustButton = adjustChoice.getByRole("button", {
      name: /Ajuster les heures prévues à 32 h/,
    });
    await expect(adjustButton).toBeVisible();
    await adjustButton.click();
    await expect(dialog.getByLabel("Heures prévues")).toHaveValue("32");
    await dialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    await expect(dialog).toBeHidden();

    segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(segmentId)}`,
    );
    segment = await segmentResponse.json() as Record<string, any>;
    expect(segment.operational_base_hours).toBe(32);
    expect(segment.planned_hours).toBe(24);

    dialog = await openSegmentEditor(demandNumber);
    await dialog.getByLabel("Heures prévues").fill("28");
    await dialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    const exceptionChoice = dialog.getByRole("alert");
    await expect(exceptionChoice).toContainText("Heures prévues proposées : 20 h");
    await exceptionChoice.getByRole("button", { name: /Conserver la dérogation/ }).click();
    await expect(dialog).toContainText("Dérogation de surallocation sélectionnée");
    const [exceptionSave] = await Promise.all([
      page.waitForResponse((response) => response.request().method() === "PATCH"
        && new URL(response.url()).pathname.startsWith("/api/v1/segments/")),
      dialog.getByRole("button", { name: "Enregistrer", exact: true }).click(),
    ]);
    expect(exceptionSave.status(), await exceptionSave.text()).toBe(200);
    expect(exceptionSave.request().postDataJSON()).toMatchObject({
      planned_hours: 28, allow_locked_overallocation: true,
    });
    await expect(dialog).toBeHidden();

    segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(segmentId)}`,
    );
    segment = await segmentResponse.json() as Record<string, any>;
    expect(segment.operational_base_hours).toBe(28);
    expect(segment.planned_hours).toBe(20);
    const finalShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    const finalShifts = (await finalShiftsResponse.json() as Array<any>)
      .filter((row) => row.demand_number === demandNumber && row.allocation_type !== "Hors horaire requis");
    expect(finalShifts.map((row) => String(row.allocation_id)).sort()).toEqual(survivorIds);
    expect(finalShifts.map((row) => Number(row.hours)).sort()).toEqual([8, 8, 8]);
  } finally {
    try {
      const cleanupPaths = [
        availabilityRuleId && `/api/v1/availability-rules/${encodeURIComponent(availabilityRuleId)}/deactivate`,
        resourceId && `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
      ].filter((path): path is string => Boolean(path));
      for (const path of cleanupPaths) {
        try {
          const result = await page.request.post(path, { headers: { "X-E2E-Role": "ADMIN" } });
          expect.soft(result.ok(), `${path}: ${await result.text()}`).toBeTruthy();
        } catch (reason) {
          expect.soft(reason, `Nettoyage de ${path}`).toBeUndefined();
        }
      }
      const resourcesAfter = await page.request.get("/api/v1/resources?active_only=true");
      expect.soft(resourcesAfter.ok(), await resourcesAfter.text()).toBeTruthy();
      expect.soft((await resourcesAfter.json() as Array<{ id: string }>).map((row) => row.id).sort())
        .toEqual(originalResourceIds);
      const orderAfter = await page.request.get("/api/v1/planning/resource-order");
      expect.soft(orderAfter.ok(), await orderAfter.text()).toBeTruthy();
      expect.soft((await orderAfter.json()).positions).toEqual(originalOrder);
    } finally {
      await context.close();
    }
  }
});
