import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

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

async function openCoordinator(browser: Browser, disableBroadcast = false) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  if (disableBroadcast) {
    await context.addInitScript(() => {
      Object.defineProperty(window, "BroadcastChannel", {
        value: undefined,
        configurable: true,
      });
    });
  }
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
  return { context, page };
}

async function chooseCombobox(scope: Locator, label: string, query: string, optionName: string) {
  const input = scope.getByRole("combobox", { name: label, exact: true });
  await input.click();
  await input.fill(query);
  const listbox = scope.getByRole("listbox", { name: `${label} options`, exact: true });
  await expect(listbox).toBeVisible();
  await listbox.getByRole("option", { name: optionName, exact: false }).click();
}

async function createQuickShiftFromCell(
  page: Page,
  resourceName: string,
  resourceId: string,
  day: string,
  note: string,
) {
  const row = page.locator(".resource-row").filter({ hasText: resourceName }).first();
  await expect(row).toBeVisible();
  const cell = row.locator(`.planning-drop-day[data-day="${day}"]`);
  await expect(cell).toBeVisible();
  await cell.hover();
  await cell.getByRole("button", {
    name: `Créer un Quick Shift pour ${resourceName} le ${day}`,
  }).click();

  const dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });
  await expect(dialog).toBeVisible();
  await chooseCombobox(dialog, "Projet", "251", "P-251");
  await expect(dialog.getByRole("combobox", { name: "Technicien", exact: true }))
    .toHaveAttribute("data-combobox-value", resourceId);
  await dialog.getByLabel("Heures").fill("1");
  await dialog.getByLabel("Note").fill(note);
  await dialog.getByRole("button", { name: "Créer le Quick Shift" }).click();
  await expect(dialog).toBeHidden();
}

test("765 ouvre un Planning détaché avec contexte et synchronise les mutations dans les deux sens", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await openCoordinator(browser);
  const weekStart = addDays(startOfWeek(new Date()), 14);
  const monday = localIso(weekStart);
  const tuesday = localIso(addDays(weekStart, 1));

  try {
    for (let index = 0; index < 2; index += 1) {
      await page.getByRole("button", { name: /Suivante/ }).click();
      await expect(page.locator(".planning-board-scroll")).not.toHaveClass(/is-loading/);
    }

    await page.getByRole("button", { name: /^Filtres/ }).click();
    await page.getByLabel("Recherche").fill("Alice");

    const mainWeekHeading = await page.locator(".planning-sticky-heading h1").innerText();
    const [detached] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await detached.waitForLoadState("domcontentloaded");

    await expect(detached).toHaveTitle("Ressources et quarts — ResourcePlanner");
    const detachedUrl = new URL(detached.url());
    expect(detachedUrl.pathname).toBe("/planning/resources-window");
    expect(detachedUrl.searchParams.get("week")).toBe(monday);
    expect(detachedUrl.searchParams.get("q")).toBe("Alice");
    expect(detachedUrl.searchParams.get("scope")).toBeTruthy();

    await expect(detached.locator(".app-sidebar")).toHaveCount(0);
    await expect(detached.locator(".metric-grid")).toHaveCount(0);
    await expect(detached.locator(".asset-planning-panel")).toHaveCount(0);
    await expect(detached.getByText("Ressources et quarts", { exact: true }).first()).toBeVisible();
    await expect(detached.getByLabel("Recherche")).toHaveValue("Alice");

    const requestedScope = detachedUrl.searchParams.get("scope");
    if (requestedScope === "mine") {
      await expect(detached.getByRole("button", { name: "Mon périmètre" })).toHaveClass(/active/);
    } else if (requestedScope === "global") {
      await expect(detached.getByRole("button", { name: "Vue globale" })).toHaveClass(/active/);
    }

    await detached.setViewportSize({ width: 1600, height: 900 });
    await expect(detached.locator(".planning-board-panel")).toBeVisible();
    await detached.setViewportSize({ width: 1024, height: 768 });
    await expect(detached.locator(".planning-board-panel")).toBeVisible();

    await detached.reload();
    await expect(detached).toHaveTitle("Ressources et quarts — ResourcePlanner");
    await expect(detached.getByLabel("Recherche")).toHaveValue("Alice");

    await createQuickShiftFromCell(
      detached,
      "Alice",
      "R-ALICE",
      monday,
      "Issue 765 detached to main",
    );

    const mainAlice = page.locator(".resource-row").filter({ hasText: "Alice" }).first();
    const mainMonday = mainAlice.locator(`.planning-drop-day[data-day="${monday}"]`);
    await expect(
      mainMonday.locator('.shift-card-main[title*="Issue 765 detached to main"]'),
    ).toHaveCount(1);

    await createQuickShiftFromCell(
      page,
      "Alice",
      "R-ALICE",
      tuesday,
      "Issue 765 main to detached",
    );

    const detachedAlice = detached.locator(".resource-row").filter({ hasText: "Alice" }).first();
    const detachedTuesday = detachedAlice.locator(`.planning-drop-day[data-day="${tuesday}"]`);
    await expect(
      detachedTuesday.locator('.shift-card-main[title*="Issue 765 main to detached"]'),
    ).toHaveCount(1);

    await detached.getByRole("button", { name: /Suivante/ }).click();
    expect(new URL(detached.url()).searchParams.get("week")).toBe(
      localIso(addDays(weekStart, 7)),
    );
    await expect(page.locator(".planning-sticky-heading h1")).toHaveText(mainWeekHeading);

    await detached.close();
    await expect(page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" })).toBeVisible();

    const [reopened] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await reopened.waitForLoadState("domcontentloaded");
    const reopenedAlice = reopened.locator(".resource-row").filter({ hasText: "Alice" }).first();
    await expect(
      reopenedAlice
        .locator(`.planning-drop-day[data-day="${monday}"]`)
        .locator('.shift-card-main[title*="Issue 765 detached to main"]'),
    ).toHaveCount(1);
    await reopened.close();
  } finally {
    await context.close();
  }
});

test("765 affiche une erreur utile quand le navigateur bloque la fenêtre", async ({ browser }) => {
  const { context, page } = await openCoordinator(browser);
  try {
    await page.evaluate(() => {
      Object.defineProperty(window, "open", {
        value: () => null,
        configurable: true,
      });
    });
    await page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "Le navigateur a bloqué l’ouverture de la fenêtre",
    );
  } finally {
    await context.close();
  }
});

test("765 conserve la sécurité CAS si la synchronisation inter-fenêtres est indisponible", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openCoordinator(browser, true);
  let planningVersion = 100;
  let assignedAssetId: string | null = null;

  await context.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Record<string, any>;
    const requestUrl = new URL(route.request().url());
    const day = requestUrl.searchParams.get("start") || localIso(startOfWeek(new Date()));
    const fakeResource = {
      id: "R-765-CAS",
      name: "Ressource CAS 765",
      email: null,
      resource_class: "E2E",
      competencies: "",
      competency_ids: [],
      note: null,
      active: true,
      sort_order: -765,
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
    const fakeShift = {
      allocation_id: "SHIFT-765-CAS",
      segment_id: "SEG-765-CAS",
      resource_id: fakeResource.id,
      resource_name: fakeResource.name,
      work_date: day,
      hours: 2,
      allocation_type: "Travail",
      source: "MANUAL",
      locked: true,
      outside_standard_hours: false,
      confirmation: "Confirmée",
      confirmation_override: null,
      load_kind: "FIRM",
      note: "CAS 765",
      demand_number: null,
      project_number: "P-765",
      project_name: "Concurrence multi-fenêtres",
      project_manager: null,
      requester: null,
      asset_assignment: assignedAssetId ? {
        requirement_id: "REQ-765-ASSET",
        allocation_id: "ALLOC-765-ASSET",
        origin: "SHIFT_AD_HOC",
        association_kind: "OWNED_SHIFT",
        asset_id: assignedAssetId,
        asset_code: "VEH-765",
        asset_label: "Véhicule CAS 765",
        asset_active: true,
        operator_resource_id: fakeResource.id,
        qualification_state: "SATISFIED",
        project_id: "P-765",
        resource_requirement_id: null,
        context_resource_id: null,
        start_date: day,
        end_date: day,
      } : null,
      related_asset_reservations: [],
      asset_actions: assignedAssetId ? {
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
          ...((body.resources || []) as Array<Record<string, unknown>>)
            .filter((row) => row.id !== fakeResource.id),
        ],
        shifts: [
          fakeShift,
          ...((body.shifts || []) as Array<Record<string, unknown>>)
            .filter((row) => row.allocation_id !== fakeShift.allocation_id),
        ],
      },
    });
  });

  await context.route("**/api/v1/assets/shifts/SHIFT-765-CAS/assignment/candidates", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        shift_id: "SHIFT-765-CAS",
        work_date: localIso(startOfWeek(new Date())),
        operator_resource_id: "R-765-CAS",
        current_requirement_id: null,
        current_allocation_id: null,
        planning_version: planningVersion,
        candidates: [{
          id: "ASSET-765-CAS",
          code: "VEH-765",
          label: "Véhicule CAS 765",
          asset_type_id: "TYPE-765",
          asset_type_code: "VEH",
          asset_type_label: "Véhicule",
          active: true,
          compatible: true,
          available: true,
          qualification_state: "SATISFIED",
          allowed: true,
          reason: null,
          diagnostics: [],
          currently_assigned: false,
          selection_mode: "ASSIGN",
          existing_allocation_id: null,
          existing_requirement_id: null,
          existing_operator_resource_id: null,
          existing_start_date: null,
          existing_end_date: null,
          assignment_authority: {
            allowed: true,
            can_assign: true,
            status: "AUTHORIZED",
            code: null,
            message: "Autorisé à attribuer.",
            diagnostics: [],
          },
        }],
      }),
    });
  });

  await context.route("**/api/v1/assets/shifts/SHIFT-765-CAS/assignment", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const payload = route.request().postDataJSON() as {
      asset_id: string | null;
      expected_planning_version: number;
    };
    if (payload.expected_planning_version !== planningVersion) {
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
    assignedAssetId = payload.asset_id;
    planningVersion += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        operation: "ASSIGN",
        shift_id: "SHIFT-765-CAS",
        requirement_id: "REQ-765-ASSET",
        requirement_origin: "SHIFT_AD_HOC",
        allocation_id: "ALLOC-765-ASSET",
        asset_id: assignedAssetId,
        operator_resource_id: "R-765-CAS",
        qualification_state: "SATISFIED",
        shift_source: "MANUAL",
        shift_locked: true,
        planning_version: planningVersion,
      }),
    });
  });

  try {
    // openCoordinator() loads Planning before this test installs its mocked
    // snapshot routes. Reload and re-enter Planning so both windows start from
    // the same synthetic version-100 backend state.
    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await expect(page.locator('.shift-card[data-allocation-id="SHIFT-765-CAS"]')).toBeVisible();

    const [detached] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await detached.waitForLoadState("domcontentloaded");

    const mainCard = page.locator('.shift-card[data-allocation-id="SHIFT-765-CAS"]');
    const detachedCard = detached.locator('.shift-card[data-allocation-id="SHIFT-765-CAS"]');
    await expect(mainCard).toContainText("Aucun actif");
    await expect(detachedCard).toContainText("Aucun actif");

    // Window B prepares a mutation while both windows still hold version 100.
    await detachedCard.getByRole("button", { name: /Assigner un actif à Ressource CAS 765/ }).click();
    const detachedDialog = detached.getByRole("dialog", { name: "Assigner un actif" });
    await chooseCombobox(detachedDialog, "Actif", "VEH-765", "VEH-765");

    // Window A commits first and advances canonical Planning state to version 101.
    await mainCard.getByRole("button", { name: /Assigner un actif à Ressource CAS 765/ }).click();
    const mainDialog = page.getByRole("dialog", { name: "Assigner un actif" });
    await chooseCombobox(mainDialog, "Actif", "VEH-765", "VEH-765");
    await mainDialog.getByRole("button", { name: "Assigner l’actif" }).click();
    await expect(mainCard).toContainText("Actif : VEH-765");
    expect(planningVersion).toBe(101);

    // B submits its already-prepared version-100 mutation and must fail closed.
    await detachedDialog.getByRole("button", { name: "Assigner l’actif" }).click();

    await expect(detachedDialog).toContainText("Le planning a changé");
    expect(assignedAssetId).toBe("ASSET-765-CAS");
    expect(planningVersion).toBe(101);
    await detachedDialog.getByRole("button", { name: "Annuler" }).click();
    await expect(detachedCard).toContainText("Actif : VEH-765");

    await detached.close();
    await expect(page.getByRole("button", { name: /Suivante/ })).toBeEnabled();
  } finally {
    await context.close();
  }
});
