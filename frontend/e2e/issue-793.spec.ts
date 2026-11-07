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
  // Match the established #777 real-browser convention: build the fixture in
  // next week, then explicitly navigate Planning to that week before locating
  // work-queue cards. This avoids depending on persisted/default Planning week.
  const monday = addDays(startOfWeek(new Date()), 7);
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

  async function positionPlanningOnFixtureWeek(options: { reload: boolean }) {
    if (options.reload) {
      await page.reload({ waitUntil: "domcontentloaded" });
    } else {
      await page.goto("/");
    }
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Planning opérationnel/i })
      .click();

    // Same product navigation used by the canonical #777 browser acceptance:
    // fixture week is exactly one week after "Aujourd’hui".
    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    await page.getByRole("button", { name: "Suivante →" }).click();

    // Prove the displayed Planning board is the fixture week before any card
    // assertion. Resource rows may load asynchronously after week navigation.
    const fixtureDay = page.locator(
      `.planning-drop-day[data-day="${d1}"]`,
    ).first();
    await expect(fixtureDay).toBeVisible();

    return {
      day: d1,
      fixtureDayCount: await page.locator(
        `.planning-drop-day[data-day="${d1}"]`,
      ).count(),
    };
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

    if (backendActionExistsBefore) {
      await card.waitFor({ state: "visible", timeout: 10_000 }).catch(() => {});
    }
    await target.waitFor({ state: "visible", timeout: 10_000 }).catch(() => {});

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

  async function reduceCurrentShift(
    demandNumber: string,
    resourceId: string,
    workDate: string,
    options: { capture: boolean; label: string },
  ) {
    const { capture, label } = options;
    const freshRows = await demandShifts(demandNumber);
    const backendRow = freshRows.find(
      (row) =>
        row.resource_id === resourceId
        && row.work_date === workDate,
    ) ?? null;
    expect(
      backendRow,
      `${label}: current backend Shift missing for ${resourceId} on ${workDate}`,
    ).toBeTruthy();

    const allocationId = String(backendRow.allocation_id);
    const planningPosition = await positionPlanningOnFixtureWeek({
      reload: true,
    });
    const fixtureDayVisible = await page.locator(
      `.planning-drop-day[data-day="${d1}"]`,
    ).first().isVisible();

    const card = page.locator(
      `.shift-card[data-allocation-id="${allocationId}"]`,
    );
    await card.waitFor({ state: "visible", timeout: 10_000 }).catch(() => {});
    const renderedCardCount = await card.count();
    const renderedCardVisible = renderedCardCount > 0
      ? await card.isVisible()
      : false;
    const cardBoundingBox = renderedCardCount > 0
      ? await card.boundingBox()
      : null;

    const preOpen = {
      label,
      allocationId,
      resourceId,
      workDate,
      hours: Number(backendRow.hours),
      backendRowExists: Boolean(backendRow),
      renderedCardCount,
      renderedCardVisible,
      cardBoundingBox,
      fixtureDayVisible,
      planningPosition,
    };
    console.log("ISSUE793_REDUCTION_PREOPEN", JSON.stringify(preOpen));

    let clickExecuted = false;
    let editorOpened = false;
    const editor = page.getByRole("dialog", { name: "Modifier le quart" });

    if (renderedCardCount === 1 && renderedCardVisible) {
      await card.locator(".shift-card-main").click();
      clickExecuted = true;
      editorOpened = await editor
        .waitFor({ state: "visible", timeout: 5_000 })
        .then(() => true)
        .catch(() => false);
    }

    const previewPath =
      `/api/v1/allocations/${encodeURIComponent(allocationId)}/work-queue-preview`;
    let previewRequestEmitted = false;
    let previewResponseEmitted = false;
    let previewStatus: number | null = null;
    let previewPayload: Record<string, any> | null = null;
    let previewRequestError: string | null = null;
    let previewResponseError: string | null = null;

    if (editorOpened) {
      const networkTimeout = 10_000;
      const previewRequestPromise = page.waitForRequest(
        (request) =>
          request.method() === "GET"
          && new URL(request.url()).pathname === previewPath,
        { timeout: networkTimeout },
      ).then((request) => ({ ok: true as const, request }))
        .catch((error) => ({ ok: false as const, error: String(error) }));
      const previewResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "GET"
          && new URL(response.url()).pathname === previewPath,
        { timeout: networkTimeout },
      ).then((response) => ({ ok: true as const, response }))
        .catch((error) => ({ ok: false as const, error: String(error) }));

      const actionButton = editor
        .getByTestId("request-work-queue-actions")
        .getByRole("button", { name: "Supprimer définitivement ce quart" });
      await expect(actionButton).toBeVisible();
      await actionButton.click();

      const [requestOutcome, responseOutcome] = await Promise.all([
        previewRequestPromise,
        previewResponsePromise,
      ]);
      previewRequestEmitted = requestOutcome.ok;
      previewResponseEmitted = responseOutcome.ok;
      previewRequestError = requestOutcome.ok ? null : requestOutcome.error;
      previewResponseError = responseOutcome.ok ? null : responseOutcome.error;
      if (responseOutcome.ok) {
        previewStatus = responseOutcome.response.status();
        try {
          previewPayload =
            await responseOutcome.response.json() as Record<string, any>;
        } catch {
          previewPayload = { raw: await responseOutcome.response.text() };
        }
      }
    }

    let redopenClassification:
      | "REDOPEN1"
      | "REDOPEN2"
      | "REDOPEN3"
      | "REDOPEN4";
    if (!editorOpened) {
      redopenClassification = "REDOPEN1";
    } else if (!previewRequestEmitted || !previewResponseEmitted) {
      redopenClassification = "REDOPEN2";
    } else if ((previewStatus ?? 500) >= 400) {
      redopenClassification = "REDOPEN3";
    } else {
      redopenClassification = "REDOPEN4";
    }

    const openEvidence = {
      ...preOpen,
      clickExecuted,
      editorOpened,
      preview: {
        requestEmitted: previewRequestEmitted,
        responseEmitted: previewResponseEmitted,
        status: previewStatus,
        payload: previewPayload,
        requestError: previewRequestError,
        responseError: previewResponseError,
      },
      classification: redopenClassification,
    };
    console.log("ISSUE793_REDUCTION_OPEN_RUNTIME", JSON.stringify(openEvidence));

    if (redopenClassification !== "REDOPEN4") {
      return {
        ...openEvidence,
        mutation: null,
        persisted: null,
        editor,
        confirmation: page.getByTestId(
          "permanent-reduction-confirmation-dialog",
        ),
        card,
      };
    }

    expect(previewStatus, JSON.stringify(previewPayload)).toBe(200);
    const confirmation = page.getByTestId(
      "permanent-reduction-confirmation-dialog",
    );
    await expect(confirmation).toBeVisible();

    const mutationPath =
      `/api/v1/allocations/${encodeURIComponent(allocationId)}/permanent-reduction`;
    const networkTimeout = 10_000;
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

    await confirmation
      .getByRole("button", { name: "Supprimer définitivement" })
      .click();

    const [mutationRequestOutcome, mutationResponseOutcome] = await Promise.all([
      mutationRequestPromise,
      mutationResponsePromise,
    ]);

    const mutationRequestEmitted = mutationRequestOutcome.ok;
    const mutationResponseEmitted = mutationResponseOutcome.ok;
    const mutationBody = mutationRequestOutcome.ok
      ? mutationRequestOutcome.request.postDataJSON() as Record<string, any>
      : null;
    const idempotencyKey = mutationRequestOutcome.ok
      ? mutationRequestOutcome.request.headers()["idempotency-key"] || null
      : null;
    const mutationStatus = mutationResponseOutcome.ok
      ? mutationResponseOutcome.response.status()
      : null;
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

    const afterMutationRows = await demandShifts(demandNumber);
    const allocationStillPersisted = afterMutationRows.some(
      (row) => String(row.allocation_id) === allocationId,
    );
    const resourceRowsAfter = afterMutationRows
      .filter((row) => row.resource_id === resourceId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)))
      .map((row) => ({
        allocationId: String(row.allocation_id),
        date: String(row.work_date),
        hours: Number(row.hours),
      }));

    if (!capture) {
      expect(
        mutationStatus,
        JSON.stringify({ mutationPayload, openEvidence }),
      ).toBe(200);
      await editor.waitFor({ state: "hidden", timeout: 5_000 });
    }

    const persisted = {
      allocationStillPersisted,
      resourceRows: resourceRowsAfter,
    };
    const mutation = {
      requestEmitted: mutationRequestEmitted,
      responseEmitted: mutationResponseEmitted,
      status: mutationStatus,
      payload: mutationPayload,
      body: mutationBody,
      idempotencyKey,
      requestError: mutationRequestOutcome.ok
        ? null
        : mutationRequestOutcome.error,
      responseError: mutationResponseOutcome.ok
        ? null
        : mutationResponseOutcome.error,
    };

    console.log(
      "ISSUE793_REDUCTION_MUTATION_RUNTIME",
      JSON.stringify({
        label,
        allocationId,
        preview: previewPayload,
        mutation,
        persisted,
      }),
    );

    return {
      ...openEvidence,
      mutation,
      persisted,
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

    const firstPeriodPosition = await positionPlanningOnFixtureWeek({
      reload: false,
    });
    console.log(
      "ISSUE793_FIRST_PERIOD_POSITION",
      JSON.stringify(firstPeriodPosition),
    );

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

    // Treat the second assignment as a separate real user interaction. Reload,
    // reopen Planning, explicitly reposition to the fixture week, then reacquire
    // Segment B by stable segment identity.
    const secondPeriodPosition = await positionPlanningOnFixtureWeek({
      reload: true,
    });
    console.log(
      "ISSUE793_SECOND_PERIOD_POSITION",
      JSON.stringify(secondPeriodPosition),
    );

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

      if (
        (timmyDrag.preview.emitted && (timmyDrag.preview.status ?? 500) >= 400)
        || (
          timmyDrag.mutation.emitted
          && (timmyDrag.mutation.status ?? 500) >= 400
        )
      ) {
        dragClassification = "DRA1";
      } else if (!timmyDrag.preview.emitted || !timmyDrag.mutation.emitted) {
        dragClassification = "DRA3";
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

    const ericInitialIds = initialEric.map((row) => String(row.allocation_id));
    const ericFirst = await reduceCurrentShift(
      demandNumber,
      ericId,
      d3,
      { capture: false, label: "ERIC_FIRST" },
    );
    expect(ericFirst.classification).toBe("REDOPEN4");
    expect(ericFirst.mutation?.status).toBe(200);

    shifts = await demandShifts(demandNumber);
    const ericAfterFirst = shifts
      .filter((row) => row.resource_id === ericId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    const timmyAfterEric = shifts
      .filter((row) => row.resource_id === timmyId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    expect(ericAfterFirst.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 8],
      [d2, 8],
    ]);
    expect(timmyAfterEric.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 8],
      [d2, 8],
      [d3, 8],
    ]);

    const timmyCurrentWednesday = timmyAfterEric.find(
      (row) => row.work_date === d3,
    );
    expect(timmyCurrentWednesday).toBeTruthy();
    const timmySecond = await reduceCurrentShift(
      demandNumber,
      timmyId,
      d3,
      { capture: false, label: "TIMMY_SECOND" },
    );
    expect(timmySecond.classification).toBe("REDOPEN4");
    expect(timmySecond.mutation?.status).toBe(200);

    shifts = await demandShifts(demandNumber);
    const ericAfterSecond = shifts
      .filter((row) => row.resource_id === ericId)
      .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
    expect(ericAfterSecond.map((row) => [row.work_date, Number(row.hours)])).toEqual([
      [d1, 5.33],
      [d2, 5.33],
      [d3, 5.34],
    ]);
    expect(
      ericAfterSecond.reduce((sum, row) => sum + Number(row.hours), 0),
    ).toBe(16);

    const regenerated = ericAfterSecond.find((row) => row.work_date === d3);
    expect(regenerated).toBeTruthy();
    const regeneratedAllocationId = String(regenerated.allocation_id);

    const third = await reduceCurrentShift(
      demandNumber,
      ericId,
      d3,
      { capture: true, label: "ERIC_THIRD" },
    );

    const thirdPreview = third.preview.payload ?? {};
    const thirdMutation = third.mutation;
    const mutationError = thirdMutation?.payload?.error?.code ?? null;
    const versionsMatchPreview = Boolean(
      thirdMutation?.body
      && Number(thirdMutation.body.expected_planning_version)
        === Number(thirdPreview.planning_version)
      && Number(thirdMutation.body.expected_request_version)
        === Number(thirdPreview.request_version)
      && String(thirdMutation.body.expected_approval_revision_id)
        === String(thirdPreview.approval_revision_id)
      && Number(thirdMutation.body.expected_operational_version)
        === Number(thirdPreview.operational_version)
    );

    const afterThird = await demandShifts(demandNumber);
    const allocationStillPersisted = afterThird.some(
      (row) => String(row.allocation_id) === regeneratedAllocationId,
    );

    let modalVisible = await third.editor.isVisible();
    let confirmationVisible = await third.confirmation.isVisible();
    let renderedOldCard = await third.card.count() > 0;

    let classification: "U1" | "U2" | "U3" | "U4" | null = null;
    if (third.classification !== "REDOPEN4") {
      classification = null;
    } else if ((thirdMutation?.status ?? 500) >= 400) {
      const staleCodes = new Set([
        "request_shift_not_found",
        "planning_version_conflict",
        "request_version_conflict",
        "operational_choice_version_conflict",
        "planning_authorization_revision_conflict",
      ]);
      classification = staleCodes.has(String(mutationError)) ? "U2" : "U1";
    } else {
      expect(
        thirdMutation?.status,
        JSON.stringify(thirdMutation?.payload),
      ).toBe(200);
      try {
        await third.editor.waitFor({ state: "hidden", timeout: 2_000 });
      } catch {
        // Characterization: successful backend mutation may leave editor open.
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
      const ericRowsAfterThird = afterThird
        .filter((row) => row.resource_id === ericId)
        .sort((a, b) => String(a.work_date).localeCompare(String(b.work_date)));
      expect(
        ericRowsAfterThird.map((row) => [row.work_date, Number(row.hours)]),
      ).toEqual([
        [d1, 5.33],
        [d2, 5.33],
      ]);
      expect(
        ericRowsAfterThird.reduce(
          (sum, row) => sum + Number(row.hours),
          0,
        ),
      ).toBe(10.66);
      expect(
        Number(thirdMutation?.payload?.effective_need_after),
      ).toBe(10.66);
      expect(
        Number(thirdMutation?.payload?.covered_hours_after),
      ).toBe(10.66);
      expect(
        Number(thirdMutation?.payload?.remaining_hours_after),
      ).toBe(0);

      classification =
        persistedChanged
        && !modalVisible
        && !confirmationVisible
        && !renderedOldCard
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
      initialEricAllocationIds: ericInitialIds,
      firstReduction: ericFirst,
      secondReduction: timmySecond,
      regeneratedAllocationId,
      thirdReduction: third,
      mutation: {
        errorCode: mutationError,
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

    expect(third.classification).toBe("REDOPEN4");
    expect(third.preview.status).toBe(200);
    expect(versionsMatchPreview).toBeTruthy();
    expect(thirdMutation?.idempotencyKey).toBeTruthy();
    expect(classification).not.toBeNull();
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
