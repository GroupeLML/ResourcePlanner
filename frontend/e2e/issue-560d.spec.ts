import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openCoordinator(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

async function chooseCombobox(scope: Locator, label: string, query: string, optionName: string) {
  const input = scope.getByRole("combobox", { name: label, exact: true });
  await input.click();
  await input.fill(query);
  const listbox = scope.getByRole("listbox", { name: `${label} options`, exact: true });
  await expect(listbox).toBeVisible();
  await listbox.getByRole("option", { name: optionName, exact: false }).click();
}

test("560D gère assign/change/release depuis le Shift sans état optimiste mensonger", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openCoordinator(browser);
  let assignedAssetId: string | null = null;
  let planningVersion = 560;
  let staleNext = false;
  let abortNext = false;
  const idempotencyKeys: string[] = [];

  const assetById = {
    "ASSET-A-560D": { code: "TRUCK-560D-A", label: "Camion A" },
    "ASSET-B-560D": { code: "TRUCK-560D-B", label: "Camion B" },
  } as const;

  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Record<string, any>;
    const requestUrl = new URL(route.request().url());
    const day = requestUrl.searchParams.get("start") || new Date().toISOString().slice(0, 10);
    const assigned = assignedAssetId ? assetById[assignedAssetId as keyof typeof assetById] : null;

    const fakeResource = {
      id: "R-560D-E2E",
      name: "Ressource 560D",
      email: null,
      resource_class: "E2E",
      competencies: "Permis actif",
      competency_ids: ["COMP-560D"],
      note: null,
      active: true,
      sort_order: -560,
      external_id: null,
      erp_status: null,
      erp_active: true,
      erp_department_description: null,
      erp_department_code: null,
      erp_employee_class: null,
      erp_supervisor_external_id: null,
      erp_phone: null,
      erp_branch_code: null,
      erp_contact_id: null,
    };
    const relatedReservation = {
      requirement_id: "REQ-REQUEST-560D",
      allocation_id: "ALLOC-REQUEST-560D",
      origin: "REQUEST",
      association_kind: "RELATED_REQUEST",
      asset_id: "ASSET-REQUEST-560D",
      asset_code: "LIFT-REQUEST-560D",
      asset_label: "Nacelle liée à la demande",
      asset_active: true,
      operator_resource_id: "R-560D-E2E",
      qualification_state: "SATISFIED",
      project_id: "PROJECT-560D",
      resource_requirement_id: null,
      context_resource_id: null,
      start_date: day,
      end_date: day,
    };
    const fakeShift = {
      allocation_id: "SHIFT-560D-E2E",
      segment_id: "SEG-560D-E2E",
      resource_id: "R-560D-E2E",
      resource_name: "Ressource 560D",
      work_date: day,
      hours: 2,
      allocation_type: "Travail",
      source: assigned ? "MANUAL" : "AUTO",
      locked: Boolean(assigned),
      outside_standard_hours: false,
      confirmation: "Confirmée",
      confirmation_override: null,
      load_kind: "FIRM",
      note: "Acceptation 560D",
      demand_number: "D-560D",
      project_number: "P-560D",
      project_name: "Acceptation actifs",
      project_manager: "Coordonnateur",
      requester: "E2E",
      asset_assignment: assigned && assignedAssetId ? {
        requirement_id: "REQ-ADHOC-560D",
        allocation_id: "ALLOC-ADHOC-560D",
        origin: "SHIFT_AD_HOC",
        association_kind: "OWNED_SHIFT",
        asset_id: assignedAssetId,
        asset_code: assigned.code,
        asset_label: assigned.label,
        asset_active: true,
        operator_resource_id: "R-560D-E2E",
        qualification_state: "SATISFIED",
        project_id: "PROJECT-560D",
        resource_requirement_id: null,
        context_resource_id: null,
        start_date: day,
        end_date: day,
      } : null,
      related_asset_reservations: [relatedReservation],
      asset_actions: assigned ? {
        assign: { allowed: false, reason: "asset_assignment_attached" },
        change: { allowed: true, reason: null },
        release: { allowed: true, reason: null },
      } : {
        assign: { allowed: true, reason: null },
        change: { allowed: false, reason: "asset_assignment_missing" },
        release: { allowed: false, reason: "asset_assignment_missing" },
      },
      asset_diagnostics: [],
    };

    await route.fulfill({
      response,
      json: {
        ...body,
        planning_version: planningVersion,
        resources: [
          fakeResource,
          ...((body.resources || []) as Array<Record<string, unknown>>).filter((row) => row.id !== fakeResource.id),
        ],
        shifts: [
          fakeShift,
          ...((body.shifts || []) as Array<Record<string, unknown>>).filter((row) => row.allocation_id !== fakeShift.allocation_id),
        ],
      },
    });
  });

  await page.route("**/api/v1/assets/shifts/SHIFT-560D-E2E/assignment/candidates", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        shift_id: "SHIFT-560D-E2E",
        work_date: "2026-10-02",
        operator_resource_id: "R-560D-E2E",
        current_requirement_id: assignedAssetId ? "REQ-ADHOC-560D" : null,
        current_allocation_id: assignedAssetId ? "ALLOC-ADHOC-560D" : null,
        planning_version: planningVersion,
        candidates: [
          {
            id: "ASSET-A-560D",
            code: "TRUCK-560D-A",
            label: "Camion A",
            asset_type_id: "TYPE-TRUCK",
            asset_type_code: "TRUCK",
            asset_type_label: "Camion",
            active: true,
            compatible: true,
            available: true,
            qualification_state: "SATISFIED",
            allowed: true,
            reason: null,
            diagnostics: [],
            currently_assigned: assignedAssetId === "ASSET-A-560D",
            selection_mode: "ASSIGN",
            existing_allocation_id: null,
            existing_requirement_id: null,
            existing_operator_resource_id: null,
            existing_start_date: null,
            existing_end_date: null,
          },
          {
            id: "ASSET-B-560D",
            code: "TRUCK-560D-B",
            label: "Camion B",
            asset_type_id: "TYPE-TRUCK",
            asset_type_code: "TRUCK",
            asset_type_label: "Camion",
            active: true,
            compatible: true,
            available: true,
            qualification_state: "SATISFIED",
            allowed: true,
            reason: null,
            diagnostics: [],
            currently_assigned: assignedAssetId === "ASSET-B-560D",
            selection_mode: "ASSIGN",
            existing_allocation_id: null,
            existing_requirement_id: null,
            existing_operator_resource_id: null,
            existing_start_date: null,
            existing_end_date: null,
          },
          {
            id: "ASSET-BUSY-560D",
            code: "BUSY-560D",
            label: "Actif occupé hors scope",
            asset_type_id: "TYPE-TRUCK",
            asset_type_code: "TRUCK",
            asset_type_label: "Camion",
            active: true,
            compatible: true,
            available: false,
            qualification_state: "SATISFIED",
            allowed: false,
            reason: "asset_unavailable",
            diagnostics: ["asset_unavailable"],
            currently_assigned: false,
            selection_mode: "ASSIGN",
            existing_allocation_id: null,
            existing_requirement_id: null,
            existing_operator_resource_id: null,
            existing_start_date: null,
            existing_end_date: null,
          },
          {
            id: "ASSET-UNQUALIFIED-560D",
            code: "UNQUALIFIED-560D",
            label: "Actif permis spécial",
            asset_type_id: "TYPE-SPECIAL",
            asset_type_code: "SPECIAL",
            asset_type_label: "Équipement spécial",
            active: true,
            compatible: true,
            available: true,
            qualification_state: "SKILL_MISMATCH",
            allowed: false,
            reason: "operator_not_qualified",
            diagnostics: ["operator_not_qualified"],
            currently_assigned: false,
            selection_mode: "ASSIGN",
            existing_allocation_id: null,
            existing_requirement_id: null,
            existing_operator_resource_id: null,
            existing_start_date: null,
            existing_end_date: null,
          },
        ],
      }),
    });
  });

  await page.route("**/api/v1/assets/shifts/SHIFT-560D-E2E/assignment", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    idempotencyKeys.push(route.request().headers()["idempotency-key"] || "");
    if (abortNext) {
      abortNext = false;
      await route.abort("failed");
      return;
    }
    if (staleNext) {
      staleNext = false;
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "planning_version_conflict",
            message: "Le planning a changé.",
          },
        }),
      });
      return;
    }
    const payload = route.request().postDataJSON() as {
      asset_id: string | null;
      asset_requirement_id: string | null;
      expected_planning_version: number;
    };
    assignedAssetId = payload.asset_id;
    planningVersion += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        operation: payload.asset_id ? "ASSIGN" : "RELEASE",
        shift_id: "SHIFT-560D-E2E",
        requirement_id: "REQ-ADHOC-560D",
        requirement_origin: "SHIFT_AD_HOC",
        allocation_id: payload.asset_id ? "ALLOC-ADHOC-560D" : null,
        asset_id: payload.asset_id,
        operator_resource_id: payload.asset_id ? "R-560D-E2E" : null,
        qualification_state: payload.asset_id ? "SATISFIED" : null,
        shift_source: "MANUAL",
        shift_locked: true,
        planning_version: planningVersion,
      }),
    });
  });

  try {
    await navigateMain(page, "Planning opérationnel");
    const card = page.locator('.shift-card[data-allocation-id="SHIFT-560D-E2E"]');
    await expect(card).toBeVisible();
    await expect(card).toContainText("Aucun actif");
    await expect(card).not.toContainText("LIFT-REQUEST-560D");

    await card.getByRole("button", { name: /Assigner un actif à Ressource 560D/ }).click();
    let assetDialog = page.getByRole("dialog", { name: "Assigner un actif" });
    await expect(assetDialog).toBeVisible();

    const assetCombo = assetDialog.getByRole("combobox", { name: "Actif", exact: true });
    await assetCombo.click();
    await assetCombo.fill("BUSY-560D");
    const busyOption = assetDialog.getByRole("option", { name: /BUSY-560D/ });
    await expect(busyOption).toHaveAttribute("aria-disabled", "true");

    await chooseCombobox(assetDialog, "Actif", "TRUCK-560D-A", "TRUCK-560D-A");
    await assetDialog.getByRole("button", { name: "Assigner l’actif" }).click();
    await expect(assetDialog).toBeHidden();
    await expect(card).toContainText("Actif : TRUCK-560D-A");

    await card.locator(".shift-card-main").click();
    let shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await expect(shiftDialog.getByRole("region", { name: "Actif" })).toContainText("TRUCK-560D-A");
    await expect(shiftDialog.getByRole("region", { name: "Actif" })).toContainText("Réservations liées à la demande");
    await expect(shiftDialog.getByRole("region", { name: "Actif" })).toContainText("LIFT-REQUEST-560D");
    await shiftDialog.getByRole("button", { name: "Changer", exact: true }).click();

    assetDialog = page.getByRole("dialog", { name: "Changer l’actif" });
    await chooseCombobox(assetDialog, "Actif", "TRUCK-560D-B", "TRUCK-560D-B");
    await assetDialog.getByRole("button", { name: "Changer l’actif" }).click();
    await expect(assetDialog).toBeHidden();
    await expect(card).toContainText("Actif : TRUCK-560D-B");

    await card.locator(".shift-card-main").click();
    shiftDialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await shiftDialog.getByRole("button", { name: "Libérer", exact: true }).click();
    assetDialog = page.getByRole("dialog", { name: "Libérer l’actif" });
    await expect(assetDialog).toContainText("TRUCK-560D-B");
    await assetDialog.getByRole("button", { name: "Libérer l’actif" }).click();
    await expect(assetDialog).toBeHidden();
    await expect(card).toContainText("Aucun actif");

    staleNext = true;
    await card.getByRole("button", { name: /Assigner un actif à Ressource 560D/ }).click();
    assetDialog = page.getByRole("dialog", { name: "Assigner un actif" });
    await chooseCombobox(assetDialog, "Actif", "TRUCK-560D-A", "TRUCK-560D-A");
    await assetDialog.getByRole("button", { name: "Assigner l’actif" }).click();
    await expect(assetDialog).toContainText("Le planning a changé");
    expect(assignedAssetId).toBeNull();
    await assetDialog.getByRole("button", { name: "Annuler" }).click();
    await expect(card).toContainText("Aucun actif");

    abortNext = true;
    await card.getByRole("button", { name: /Assigner un actif à Ressource 560D/ }).click();
    assetDialog = page.getByRole("dialog", { name: "Assigner un actif" });
    await chooseCombobox(assetDialog, "Actif", "TRUCK-560D-A", "TRUCK-560D-A");
    await assetDialog.getByRole("button", { name: "Assigner l’actif" }).click();
    await expect(assetDialog).toContainText("la même clé d’idempotence sera réutilisée");
    const firstRetryKey = idempotencyKeys[idempotencyKeys.length - 1];
    await assetDialog.getByRole("button", { name: "Assigner l’actif" }).click();
    await expect(assetDialog).toBeHidden();
    const secondRetryKey = idempotencyKeys[idempotencyKeys.length - 1];
    expect(firstRetryKey).toBeTruthy();
    expect(secondRetryKey).toBe(firstRetryKey);
    await expect(card).toContainText("Actif : TRUCK-560D-A");
  } finally {
    await closeContext(context);
  }
});
