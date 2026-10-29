import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

test("778 expose les trois actions ADR-034 et confirme leurs mutations", async ({ browser }) => {
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
    const response = await route.fetch();
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
    confirm = dialog.getByTestId("request-work-queue-confirmation");
    await expect(confirm).toContainText("Supprimer définitivement ce quart?");
    await expect(confirm).toContainText("8 h");
    await expect(confirm).toContainText("segment SEG-778");
    await expect(confirm).toContainText("demande DMO-778 / ligne LINE-778");
    await expect(confirm).toContainText("Elles ne retourneront pas dans la File de Travail");

    await confirm.getByRole("button", { name: "Annuler" }).click();
    expect(reductionPosts).toBe(0);
    await dialog.getByTestId("request-work-queue-actions")
      .getByRole("button", { name: "Supprimer définitivement ce quart" }).click();
    confirm = dialog.getByTestId("request-work-queue-confirmation");
    await confirm.getByRole("button", { name: "Supprimer définitivement" }).click();
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
