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

test("793 real backend characterizes regenerated-shift permanent reduction UI", async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  const fixtureKey = randomUUID();
  const monday = startOfWeek(new Date());
  const d1 = localIso(monday);
  const d2 = localIso(addDays(monday, 1));
  const d3 = localIso(addDays(monday, 2));
  const ericName = `ERIC FONTAINE 793 ${fixtureKey}`;
  const timmyName = `TIMMY CURIE 793 ${fixtureKey}`;
  let ericId = "";
  let timmyId = "";
  const availabilityIds: string[] = [];
  let electricalScopeId = "";
  let electricalScopeVersion = 0;
  let coordinatorUserId = "";
  let coordinatorAdded = false;

  async function demandShifts(demandNumber: string) {
    const response = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d3}`,
    );
    expect(response.ok(), await response.text()).toBeTruthy();
    return (await response.json() as Array<any>)
      .filter(
        (row) =>
          row.demand_number === demandNumber
          && row.allocation_type !== "Hors horaire requis",
      );
  }

  async function assignmentActions(demandNumber: string) {
    const response = await page.request.get(
      `/api/v1/planning/actions?start=${d1}&end=${d3}&scope=global`,
    );
    expect(response.ok(), await response.text()).toBeTruthy();
    return (await response.json() as Array<any>)
      .filter(
        (row) =>
          row.demand_number === demandNumber
          && row.kind === "ASSIGNMENT",
      );
  }

  async function dragExactAssignment(
    demandNumber: string,
    segmentId: string,
    resourceId: string,
    day: string,
  ) {
    const card = page.locator(
      `.planning-work-queue .planning-action-card.action-assignment[data-segment-id="${segmentId}"]`,
    );
    const target = page.locator(
      `.planning-drop-day[data-resource-id="${resourceId}"][data-day="${day}"]`,
    );

    const backendActionsBefore = await assignmentActions(demandNumber);
    const backendActionExistsBefore = backendActionsBefore.some(
      (row) => String(row.segment_id) === segmentId,
    );
    const cardCountBefore = await card.count();
    const cardVisibleBefore = cardCountBefore > 0
      ? await card.isVisible()
      : false;
    const draggableBefore = cardCountBefore > 0
      ? await card.getAttribute("draggable")
      : null;
    const cardBoxBefore = cardCountBefore > 0
      ? await card.boundingBox()
      : null;
    const targetCountBefore = await target.count();
    const targetVisibleBefore = targetCountBefore > 0
      ? await target.isVisible()
      : false;
    const targetBoxBefore = targetCountBefore > 0
      ? await target.boundingBox()
      : null;

    const before = {
      backendActionExists: backendActionExistsBefore,
      cardCount: cardCountBefore,
      cardVisible: cardVisibleBefore,
      draggable: draggableBefore,
      cardBoundingBox: cardBoxBefore,
      targetCount: targetCountBefore,
      targetVisible: targetVisibleBefore,
      targetBoundingBox: targetBoxBefore,
    };

    if (
      !backendActionExistsBefore
      || cardCountBefore !== 1
      || !cardVisibleBefore
      || draggableBefore !== "true"
      || targetCountBefore !== 1
      || !targetVisibleBefore
      || !cardBoxBefore
      || !targetBoxBefore
    ) {
      return {
        segmentId,
        resourceId,
        day,
        before,
        preview: {
          emitted: false,
          status: null,
          request: null,
          payload: null,
          error: "pre_drag_state_invalid",
        },
        mutation: {
          emitted: false,
          status: null,
          request: null,
          payload: null,
          idempotencyKey: null,
          error: "pre_drag_state_invalid",
        },
        freshShifts: await demandShifts(demandNumber),
        backendActionStillPresent: backendActionExistsBefore,
        renderedCardCount: cardCountBefore,
      };
    }

    const previewPath =
      `/api/v1/segments/${encodeURIComponent(segmentId)}/place-from-drop/preview`;
    const mutationPath =
      `/api/v1/segments/${encodeURIComponent(segmentId)}/place-from-drop`;
    const networkTimeout = 10_000;
    const previewRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST"
        && new URL(request.url()).pathname === previewPath,
      { timeout: networkTimeout },
    ).then((request) => ({ ok: true as const, request }))
      .catch((error) => ({ ok: false as const, error: String(error) }));
    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST"
        && new URL(response.url()).pathname === previewPath,
      { timeout: networkTimeout },
    ).then((response) => ({ ok: true as const, response }))
      .catch((error) => ({ ok: false as const, error: String(error) }));
    const mutationRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST"
        && new URL(request.url()).pathname === mutationPath,
      { timeout: networkTimeout },
    ).then((request) => ({ ok: true as const, request }))
      .catch((error) => ({ ok: false as const, error: String(error) }));
    const mutationResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST"
        && new URL(response.url()).pathname === mutationPath,
      { timeout: networkTimeout },
    ).then((response) => ({ ok: true as const, response }))
      .catch((error) => ({ ok: false as const, error: String(error) }));

    await card.dragTo(target);

    const [
      previewRequestOutcome,
      previewResponseOutcome,
      mutationRequestOutcome,
      mutationResponseOutcome,
    ] = await Promise.all([
      previewRequestPromise,
      previewResponsePromise,
      mutationRequestPromise,
      mutationResponsePromise,
    ]);

    let previewRequestPayload: Record<string, any> | null = null;
    if (previewRequestOutcome.ok) {
      previewRequestPayload =
        previewRequestOutcome.request.postDataJSON() as Record<string, any>;
    }
    let previewPayload: Record<string, any> | null = null;
    if (previewResponseOutcome.ok) {
      try {
        previewPayload =
          await previewResponseOutcome.response.json() as Record<string, any>;
      } catch {
        previewPayload = {
          raw: await previewResponseOutcome.response.text(),
        };
      }
    }

    let mutationRequestPayload: Record<string, any> | null = null;
    let idempotencyKey: string | null = null;
    if (mutationRequestOutcome.ok) {
      mutationRequestPayload =
        mutationRequestOutcome.request.postDataJSON() as Record<string, any>;
      idempotencyKey =
        mutationRequestOutcome.request.headers()["idempotency-key"] || null;
    }
    let mutationPayload: Record<string, any> | null = null;
    if (mutationResponseOutcome.ok) {
      try {
        mutationPayload =
          await mutationResponseOutcome.response.json() as Record<string, any>;
      } catch {
        mutationPayload = {
          raw: await mutationResponseOutcome.response.text(),
        };
      }
    }

    const freshShifts = await demandShifts(demandNumber);
    const freshActions = await assignmentActions(demandNumber);
    const backendActionStillPresent = freshActions.some(
      (row) => String(row.segment_id) === segmentId,
    );
    const renderedCardCount = await card.count();

    return {
      segmentId,
      resourceId,
      day,
      before,
      preview: {
        emitted: previewRequestOutcome.ok || previewResponseOutcome.ok,
        status: previewResponseOutcome.ok
          ? previewResponseOutcome.response.status()
          : null,
        request: previewRequestPayload,
        payload: previewPayload,
        error: previewRequestOutcome.ok && previewResponseOutcome.ok
          ? null
          : [
              previewRequestOutcome.ok ? null : previewRequestOutcome.error,
              previewResponseOutcome.ok ? null : previewResponseOutcome.error,
            ].filter(Boolean).join(" | "),
      },
      mutation: {
        emitted: mutationRequestOutcome.ok || mutationResponseOutcome.ok,
        status: mutationResponseOutcome.ok
          ? mutationResponseOutcome.response.status()
          : null,
        request: mutationRequestPayload,
        payload: mutationPayload,
        idempotencyKey,
        error: mutationRequestOutcome.ok && mutationResponseOutcome.ok
          ? null
          : [
              mutationRequestOutcome.ok ? null : mutationRequestOutcome.error,
              mutationResponseOutcome.ok ? null : mutationResponseOutcome.error,
            ].filter(Boolean).join(" | "),
      },
      freshShifts,
      backendActionStillPresent,
      renderedCardCount,
    };
  }

  async function reduceRenderedShift(
    allocationId: string,
    options: { capture: boolean },
  ) {
    const { capture } = options;
    const card = page.locator(
      `.shift-card[data-allocation-id="${allocationId}"]`,
    );
    await expect(card).toBeVisible();

    const previewPath =
      `/api/v1/allocations/${encodeURIComponent(allocationId)}/work-queue-preview`;
    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname === previewPath,
    );
    await card.locator(".shift-card-main").click();
    const previewResponse = await previewResponsePromise;
    const previewStatus = previewResponse.status();
    let previewPayload: Record<string, any> = {};
    try {
      previewPayload = await previewResponse.json() as Record<string, any>;
    } catch {
      previewPayload = { raw: await previewResponse.text() };
    }
    expect(previewStatus, JSON.stringify(previewPayload)).toBe(200);

    const editor = page.getByRole("dialog", { name: "Modifier le quart" });
    await expect(editor).toBeVisible();
    await editor
      .getByTestId("request-work-queue-actions")
      .getByRole("button", { name: "Supprimer définitivement ce quart" })
      .click();
    const confirmation = page.getByTestId(
      "permanent-reduction-confirmation-dialog",
    );
    await expect(confirmation).toBeVisible();

    const mutationPath =
      `/api/v1/allocations/${encodeURIComponent(allocationId)}/permanent-reduction`;
    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST"
        && new URL(request.url()).pathname === mutationPath,
    );
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST"
        && new URL(response.url()).pathname === mutationPath,
    );

    await confirmation
      .getByRole("button", { name: "Supprimer définitivement" })
      .click();

    const mutationRequest = await requestPromise;
    const mutationResponse = await responsePromise;
    const mutationStatus = mutationResponse.status();
    let mutationPayload: Record<string, any> = {};
    try {
      mutationPayload = await mutationResponse.json() as Record<string, any>;
    } catch {
      mutationPayload = { raw: await mutationResponse.text() };
    }
    const mutationBody = mutationRequest.postDataJSON() as Record<string, any>;
    const idempotencyKey = mutationRequest.headers()["idempotency-key"] || null;

    if (!capture) {
      expect(mutationStatus, JSON.stringify(mutationPayload)).toBe(200);
      await expect(editor).toBeHidden();
    }

    return {
      allocationId,
      previewPath,
      previewStatus,
      previewPayload,
      mutationPath,
      mutationStatus,
      mutationPayload,
      mutationBody,
      idempotencyKey,
      editor,
      confirmation,
      card,
    };
  }

  try {
    const users = await page.request.get("/api/v1/admin/users", {
      headers: { "X-E2E-Role": "ADMIN" },
    });
    expect(users.ok(), await users.text()).toBeTruthy();
    const coordinator = (await users.json() as Array<any>).find(
      (row) => String(row.display_name || "").includes("Coordonnateur"),
    );
    expect(coordinator).toBeTruthy();
    coordinatorUserId = String(coordinator.user_id);

    const scopes = await page.request.get("/api/v1/admin/approval-scopes", {
      headers: { "X-E2E-Role": "ADMIN" },
    });
    expect(scopes.ok(), await scopes.text()).toBeTruthy();
    const electrical = (await scopes.json() as Array<any>).find(
      (row) => row.code === "ELECTRICAL",
    );
    expect(electrical).toBeTruthy();
    electricalScopeId = String(electrical.id);
    electricalScopeVersion = Number(electrical.version);

    if (!(electrical.approver_user_ids as Array<string>).includes(coordinatorUserId)) {
      const assigned = await page.request.put(
        `/api/v1/admin/approval-scopes/${encodeURIComponent(electricalScopeId)}/approvers/${encodeURIComponent(coordinatorUserId)}`,
        {
          headers: { "X-E2E-Role": "ADMIN" },
          data: { expected_version: electricalScopeVersion },
        },
      );
      expect(assigned.ok(), await assigned.text()).toBeTruthy();
      const assignedScope = await assigned.json() as Record<string, any>;
      electricalScopeVersion = Number(assignedScope.version);
      coordinatorAdded = true;
    }

    for (const [index, name] of [ericName, timmyName].entries()) {
      const created = await page.request.post("/api/v1/resources", {
        headers: { "X-E2E-Role": "ADMIN" },
        data: {
          name,
          resource_class: "ELECTRICAL",
          sort_order: 7930 + index,
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      const resourceId = String((await created.json()).resource_id);
      if (index === 0) ericId = resourceId;
      else timmyId = resourceId;

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
            position: 0,
            desired_start: d1,
            desired_end: d3,
            desired_active_days: 3,
            estimated_hours: 24,
            required_resource_class: "ELECTRICAL",
            task_code: "110",
            confirmation: "Confirmée",
            description: "#793 browser segment A",
          },
          {
            position: 1,
            desired_start: d1,
            desired_end: d3,
            desired_active_days: 3,
            estimated_hours: 24,
            required_resource_class: "ELECTRICAL",
            task_code: "110",
            confirmation: "Confirmée",
            description: "#793 browser segment B",
          },
        ],
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const demandNumber = String((await created.json()).demand_number);

    const approvalSnapshot = await page.request.get(
      `/api/v1/planning/snapshot?start=${d1}&end=${d3}&scope=global`,
    );
    expect(approvalSnapshot.ok(), await approvalSnapshot.text()).toBeTruthy();
    const approved = await page.request.post(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
      {
        data: {
          comment: "#793 browser sequential reduction",
          expected_planning_version: Number(
            (await approvalSnapshot.json()).planning_version,
          ),
        },
      },
    );
    expect(approved.status(), await approved.text()).toBe(200);

    const detailResponse = await page.request.get(
      `/api/v1/demands/${encodeURIComponent(demandNumber)}/detail`,
    );
    expect(detailResponse.ok(), await detailResponse.text()).toBeTruthy();
    const detail = await detailResponse.json() as Record<string, any>;
    const detailLines = detail.lines as Array<any>;
    const lineA = detailLines.find(
      (row) => row.line?.description === "#793 browser segment A",
    );
    const lineB = detailLines.find(
      (row) => row.line?.description === "#793 browser segment B",
    );
    expect(lineA).toBeTruthy();
    expect(lineB).toBeTruthy();

    const materialized = detail.materialized_plan?.requirements as Array<any>;
    const requirementA = materialized.find(
      (row) =>
        String(row.source_request_line_id)
        === String(lineA.line.line_id),
    );
    const requirementB = materialized.find(
      (row) =>
        String(row.source_request_line_id)
        === String(lineB.line.line_id),
    );
    expect(requirementA).toBeTruthy();
    expect(requirementB).toBeTruthy();

    const segmentAId = String(requirementA.segment_id);
    const segmentBId = String(requirementB.segment_id);
    expect(segmentAId).toBeTruthy();
    expect(segmentBId).toBeTruthy();
    expect(segmentAId).not.toBe(segmentBId);

    await page.goto("/");
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Planning opérationnel/i })
      .click();

    const ericDrag = await dragExactAssignment(
      demandNumber,
      segmentAId,
      ericId,
      d1,
    );
    const ericDragShifts = ericDrag.freshShifts
      .filter((row) => row.resource_id === ericId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    console.log(
      "ISSUE793_FIRST_DRAG_RUNTIME",
      JSON.stringify({
        demandNumber,
        segmentAId,
        segmentBId,
        ericDrag,
        ericFreshShifts: ericDragShifts.map((row) => ({
          allocationId: row.allocation_id,
          date: row.work_date,
          hours: Number(row.hours),
        })),
      }),
    );
    expect(ericDrag.preview.emitted).toBeTruthy();
    expect(
      ericDrag.preview.status,
      JSON.stringify(ericDrag.preview.payload),
    ).toBe(200);
    expect(ericDrag.preview.request).toMatchObject({
      resource_id: ericId,
      dropped_date: d1,
    });
    expect(ericDrag.preview.payload?.allowed).toBe(true);
    expect(String(ericDrag.preview.payload?.segment_id)).toBe(segmentAId);
    expect(String(ericDrag.preview.payload?.resource_id)).toBe(ericId);
    expect(ericDrag.mutation.emitted).toBeTruthy();
    expect(
      ericDrag.mutation.status,
      JSON.stringify(ericDrag.mutation.payload),
    ).toBe(200);
    expect(ericDrag.mutation.request).toEqual({
      resource_id: ericId,
      dropped_date: d1,
      expected_planning_version: ericDrag.preview.payload?.planning_version,
      expected_approval_revision_id:
        ericDrag.preview.payload?.approval_revision_id,
      expected_operational_version:
        ericDrag.preview.payload?.operational_version,
    });
    expect(ericDrag.mutation.idempotencyKey).toBeTruthy();
    expect(ericDragShifts.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 8],
      [d2, 8],
      [d3, 8],
    ]);

    // Treat the second assignment as a separate real user interaction. Reload the
    // authoritative Planning UI and reacquire Segment B by stable segment identity.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Planning opérationnel/i })
      .click();

    const segmentBActionsBefore = await assignmentActions(demandNumber);
    const segmentBBackendActionExistsBefore = segmentBActionsBefore.some(
      (row) => String(row.segment_id) === segmentBId,
    );
    const freshSegmentBCard = page.locator(
      `.planning-work-queue .planning-action-card.action-assignment[data-segment-id="${segmentBId}"]`,
    );
    const freshTimmyTarget = page.locator(
      `.planning-drop-day[data-resource-id="${timmyId}"][data-day="${d1}"]`,
    );
    const segmentBCardCountBefore = await freshSegmentBCard.count();
    const segmentBVisibleBefore = segmentBCardCountBefore > 0
      ? await freshSegmentBCard.isVisible()
      : false;
    const segmentBDraggableBefore = segmentBCardCountBefore > 0
      ? await freshSegmentBCard.getAttribute("draggable")
      : null;
    const segmentBCardBoxBefore = segmentBCardCountBefore > 0
      ? await freshSegmentBCard.boundingBox()
      : null;
    const timmyTargetCountBefore = await freshTimmyTarget.count();
    const timmyTargetVisibleBefore = timmyTargetCountBefore > 0
      ? await freshTimmyTarget.isVisible()
      : false;
    const timmyTargetBoxBefore = timmyTargetCountBefore > 0
      ? await freshTimmyTarget.boundingBox()
      : null;

    console.log(
      "ISSUE793_SECOND_DRAG_PRESTATE",
      JSON.stringify({
        demandNumber,
        segmentAId,
        segmentBId,
        backendActionExists: segmentBBackendActionExistsBefore,
        cardCount: segmentBCardCountBefore,
        visible: segmentBVisibleBefore,
        draggable: segmentBDraggableBefore,
        cardBoundingBox: segmentBCardBoxBefore,
        targetCount: timmyTargetCountBefore,
        targetVisible: timmyTargetVisibleBefore,
        targetBoundingBox: timmyTargetBoxBefore,
      }),
    );

    let dragClassification: "DRA1" | "DRA2" | "DRA3" | "DRA4";
    let timmyDrag: Awaited<ReturnType<typeof dragExactAssignment>> | null = null;

    if (!segmentBBackendActionExistsBefore) {
      dragClassification = "DRA4";
    } else {
      timmyDrag = await dragExactAssignment(
        demandNumber,
        segmentBId,
        timmyId,
        d1,
      );

      const timmyDragShifts = timmyDrag.freshShifts
        .filter((row) => row.resource_id === timmyId)
        .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
      const mutationTargetMatches =
        String(timmyDrag.mutation.request?.resource_id) === timmyId
        && String(timmyDrag.mutation.request?.dropped_date) === d1;
      const exactSegmentRendered =
        await page.locator(
          `.planning-work-queue .planning-action-card.action-assignment[data-segment-id="${segmentBId}"]`,
        ).count();

      if (!timmyDrag.preview.emitted || !timmyDrag.mutation.emitted) {
        dragClassification = "DRA3";
      } else if (
        (timmyDrag.preview.status ?? 500) >= 400
        || (timmyDrag.mutation.status ?? 500) >= 400
      ) {
        dragClassification = "DRA1";
      } else if (!mutationTargetMatches) {
        dragClassification = "DRA4";
      } else {
        expect(
          timmyDrag.preview.status,
          JSON.stringify(timmyDrag.preview.payload),
        ).toBe(200);
        expect(timmyDrag.preview.request).toMatchObject({
          resource_id: timmyId,
          dropped_date: d1,
        });
        expect(timmyDrag.preview.payload?.allowed).toBe(true);
        expect(String(timmyDrag.preview.payload?.segment_id)).toBe(segmentBId);
        expect(String(timmyDrag.preview.payload?.resource_id)).toBe(timmyId);
        expect(
          timmyDrag.mutation.status,
          JSON.stringify(timmyDrag.mutation.payload),
        ).toBe(200);
        expect(timmyDrag.mutation.request).toEqual({
          resource_id: timmyId,
          dropped_date: d1,
          expected_planning_version:
            timmyDrag.preview.payload?.planning_version,
          expected_approval_revision_id:
            timmyDrag.preview.payload?.approval_revision_id,
          expected_operational_version:
            timmyDrag.preview.payload?.operational_version,
        });
        expect(timmyDrag.mutation.idempotencyKey).toBeTruthy();
        expect(
          timmyDragShifts.map((row) => [row.work_date, Number(row.hours)]),
        ).toEqual([
          [d1, 8],
          [d2, 8],
          [d3, 8],
        ]);
        dragClassification = "DRA2";
      }

      console.log(
        "ISSUE793_SECOND_DRAG_RUNTIME",
        JSON.stringify({
          demandNumber,
          segmentAId,
          segmentBId,
          timmyDrag,
          exactSegmentBRenderedCount: exactSegmentRendered,
          classification: dragClassification,
        }),
      );
    }

    console.log(
      "ISSUE793_DATED_DROP_RUNTIME",
      JSON.stringify({
        demandNumber,
        segmentAId,
        segmentBId,
        eric: ericDrag,
        timmy: timmyDrag,
        classification: dragClassification,
      }),
    );

    expect(["DRA1", "DRA2", "DRA3", "DRA4"]).toContain(dragClassification);
    expect(
      dragClassification,
      JSON.stringify({
        segmentAId,
        segmentBId,
        secondPreState: {
          backendActionExists: segmentBBackendActionExistsBefore,
          cardCount: segmentBCardCountBefore,
          visible: segmentBVisibleBefore,
          draggable: segmentBDraggableBefore,
          cardBoundingBox: segmentBCardBoxBefore,
          targetCount: timmyTargetCountBefore,
          targetVisible: timmyTargetVisibleBefore,
          targetBoundingBox: timmyTargetBoxBefore,
        },
        timmyDrag,
      }),
    ).toBe("DRA2");

    let shifts = await demandShifts(demandNumber);
    const initialEric = shifts
      .filter((row) => row.resource_id === ericId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    const initialTimmy = shifts
      .filter((row) => row.resource_id === timmyId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    expect(initialEric.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 8],
      [d2, 8],
      [d3, 8],
    ]);
    expect(initialTimmy.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 8],
      [d2, 8],
      [d3, 8],
    ]);
    expect(
      initialEric.reduce((sum, row) => sum + Number(row.hours), 0),
    ).toBe(24);
    expect(
      initialTimmy.reduce((sum, row) => sum + Number(row.hours), 0),
    ).toBe(24);

    const ericWednesday = initialEric.find((row) => row.work_date === d3);
    expect(ericWednesday).toBeTruthy();
    await reduceRenderedShift(String(ericWednesday.allocation_id), {
      capture: false,
    });

    shifts = await demandShifts(demandNumber);
    const timmyAfterEric = shifts.filter((row) => row.resource_id === timmyId);
    const timmyWednesday = timmyAfterEric.find((row) => row.work_date === d3);
    expect(timmyWednesday).toBeTruthy();
    await reduceRenderedShift(String(timmyWednesday.allocation_id), {
      capture: false,
    });

    shifts = await demandShifts(demandNumber);
    const ericAfterSecond = shifts
      .filter((row) => row.resource_id === ericId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    expect(ericAfterSecond.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 5.33],
      [d2, 5.33],
      [d3, 5.34],
    ]);
    const regenerated = ericAfterSecond.find((row) => row.work_date === d3);
    expect(regenerated).toBeTruthy();
    const currentAllocationId = String(regenerated.allocation_id);

    const currentCard = page.locator(
      `.shift-card[data-allocation-id="${currentAllocationId}"]`,
    );
    await expect(currentCard).toBeVisible();

    const third = await reduceRenderedShift(currentAllocationId, {
      capture: true,
    });
    const previewError = third.previewPayload?.error?.code ?? null;
    const mutationError = third.mutationPayload?.error?.code ?? null;

    const versionsMatchPreview =
      Number(third.mutationBody.expected_planning_version)
        === Number(third.previewPayload.planning_version)
      && Number(third.mutationBody.expected_request_version)
        === Number(third.previewPayload.request_version)
      && String(third.mutationBody.expected_approval_revision_id)
        === String(third.previewPayload.approval_revision_id)
      && Number(third.mutationBody.expected_operational_version)
        === Number(third.previewPayload.operational_version);

    const afterThird = await demandShifts(demandNumber);
    const allocationStillPersisted = afterThird.some(
      (row) => String(row.allocation_id) === currentAllocationId,
    );

    let modalVisible = await third.editor.isVisible();
    let confirmationVisible = await third.confirmation.isVisible();
    let renderedOldCard = await third.card.count() > 0;

    let classification: "U1" | "U2" | "U3" | "U4";
    if (third.mutationStatus >= 400) {
      const staleCodes = new Set([
        "request_shift_not_found",
        "planning_version_conflict",
        "request_version_conflict",
        "operational_choice_version_conflict",
        "planning_authorization_revision_conflict",
      ]);
      classification = staleCodes.has(String(mutationError)) ? "U2" : "U1";
    } else {
      expect(third.mutationStatus, JSON.stringify(third.mutationPayload)).toBe(200);
      try {
        await third.editor.waitFor({ state: "hidden", timeout: 2_000 });
      } catch {
        // Characterization: a successful backend mutation may leave the editor open.
      }
      try {
        await third.card.waitFor({ state: "detached", timeout: 2_000 });
      } catch {
        // Characterization: retain stale rendered-card evidence for U3.
      }
      modalVisible = await third.editor.isVisible();
      confirmationVisible = await third.confirmation.isVisible();
      renderedOldCard = await third.card.count() > 0;
      const persistedChanged = !allocationStillPersisted;
      classification =
        persistedChanged && !modalVisible && !confirmationVisible && !renderedOldCard
          ? "U4"
          : "U3";
    }

    const evidence = {
      fixture: {
        demandNumber,
        ericId,
        timmyId,
        window: [d1, d3],
      },
      allocationId: currentAllocationId,
      preview: {
        url: third.previewPath,
        status: third.previewStatus,
        errorCode: previewError,
        planningVersion: third.previewPayload.planning_version,
        requestVersion: third.previewPayload.request_version,
        operationalVersion: third.previewPayload.operational_version,
        approvalRevisionId: third.previewPayload.approval_revision_id,
      },
      mutation: {
        url: third.mutationPath,
        status: third.mutationStatus,
        errorCode: mutationError,
        payload: third.mutationPayload,
        body: third.mutationBody,
        idempotencyKey: third.idempotencyKey,
        versionsMatchPreview,
      },
      persisted: {
        allocationStillPersisted,
        ericRows: afterThird
          .filter((row) => row.resource_id === ericId)
          .map((row) => ({
            allocationId: row.allocation_id,
            date: row.work_date,
            hours: Number(row.hours),
          })),
      },
      ui: {
        modalVisible,
        confirmationVisible,
        renderedOldCard,
      },
      classification,
    };
    console.log("ISSUE793_BROWSER_RUNTIME", JSON.stringify(evidence));

    expect(third.previewStatus).toBe(200);
    expect(versionsMatchPreview).toBeTruthy();
    expect(third.idempotencyKey).toBeTruthy();
    expect(["U1", "U2", "U3", "U4"]).toContain(classification);
  } finally {
    for (const ruleId of availabilityIds) {
      try {
        await page.request.post(
          `/api/v1/availability-rules/${encodeURIComponent(ruleId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Best-effort isolated fixture cleanup.
      }
    }
    for (const resourceId of [ericId, timmyId].filter(Boolean)) {
      try {
        await page.request.post(
          `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
          { headers: { "X-E2E-Role": "ADMIN" } },
        );
      } catch {
        // Best-effort isolated fixture cleanup.
      }
    }
    if (coordinatorAdded && electricalScopeId && coordinatorUserId) {
      try {
        await page.request.delete(
          `/api/v1/admin/approval-scopes/${encodeURIComponent(electricalScopeId)}/approvers/${encodeURIComponent(coordinatorUserId)}`,
          {
            headers: { "X-E2E-Role": "ADMIN" },
            data: { expected_version: electricalScopeVersion },
          },
        );
      } catch {
        // Best-effort restore of the shared approval fixture.
      }
    }
    await context.close();
  }
});
