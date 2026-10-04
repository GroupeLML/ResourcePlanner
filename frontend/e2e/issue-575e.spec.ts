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

async function reserveAndWaitForPlanningRefresh(page: Page, form: Locator) {
  const snapshotRefresh = page.waitForResponse((response) => (
    response.request().method() === "GET"
    && response.url().includes("/api/v1/planning/snapshot?")
  ));
  await form.getByRole("button", { name: "Réserver l’actif" }).click();
  await snapshotRefresh;
  await expect(form).toBeVisible();
}

test("575E relie navigation REQUEST et trois contextes de réservation directe", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openCoordinator(browser);
  const captured: Record<string, Record<string, unknown>> = {};

  try {
    await page.route("**/api/v1/projects?**", async (route) => {
      const response = await route.fetch();
      const body = await response.json() as Array<Record<string, unknown>>;
      await route.fulfill({
        response,
        json: [
          {
            id: "P-575E",
            number: "P-575E",
            name: "Projet acceptation 575E",
            client: "Client 575E",
            status: "Actif",
          },
          ...body.filter((row) => row.id !== "P-575E"),
        ],
      });
    });

    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const body = await response.json() as Record<string, any>;
      const start = String(body.start);
      const end = String(body.end);
      const fakeResource = {
        id: "R-575E",
        name: "Ressource 575E",
        email: null,
        resource_class: "Programmation",
        competencies: "Permis 575E",
        competency_ids: [],
        note: null,
        active: true,
        sort_order: -575,
        external_id: "EMP-575E",
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
      const fakeSegment = {
        segment_id: "SEG-575E",
        project_number: "P-575E",
        project_name: "Projet acceptation 575E",
        description: "Segment acceptation 575E",
        resource_name: "Ressource 575E",
        automatic_target_resource_name: "Ressource 575E",
        start_date: start,
        end_date: end,
        status: "Planifié",
      };
      const requestNeed = {
        requirement_id: "AREQ-REQUEST-575E",
        origin: "REQUEST",
        request_id: "REQUEST-575E",
        demand_number: "DMO-575E",
        project_id: "P-575E",
        project_number: "P-575E",
        source_request_line_id: "LINE-575E",
        source_period_id: null,
        approval_revision_id: "REV-575E",
        approved_entry_key: "asset:575e",
        resource_requirement_id: null,
        segment_reference: null,
        shift_id: null,
        context_resource_id: null,
        slot_index: 0,
        asset_type_id: "TYPE-575E",
        asset_type_code: "TRUCK-575E",
        asset_type_label: "Camion 575E",
        start_date: start,
        end_date: end,
        usage_hours: 8,
        status: "À affecter",
        allocation_id: null,
        asset_id: null,
        asset_code: null,
        asset_label: null,
        allocation_start_date: null,
        allocation_end_date: null,
        allocation_locked: false,
        operator_resource_id: null,
        operator_resource_name: null,
        qualification_state: "SATISFIED",
        required_competency_ids: [],
        required_competency_names: [],
      };

      await route.fulfill({
        response,
        json: {
          ...body,
          resources: [
            fakeResource,
            ...((body.resources || []) as Array<Record<string, unknown>>).filter((row) => row.id !== fakeResource.id),
          ],
          segments: [
            fakeSegment,
            ...((body.segments || []) as Array<Record<string, unknown>>).filter((row) => row.segment_id !== fakeSegment.segment_id),
          ],
          asset_types: [
            {
              id: "TYPE-575E",
              code: "TRUCK-575E",
              label: "Camion 575E",
              category: "VEHICLE",
              occupancy_policy: "EXCLUSIVE_DAY",
              active: true,
            },
          ],
          assets: [
            {
              id: "ASSET-575E",
              code: "TRUCK-575E-A",
              label: "Camion acceptation",
              asset_type_id: "TYPE-575E",
              active: true,
            },
          ],
          asset_requirements: [requestNeed],
          asset_allocations: [],
          asset_unavailability: [],
          asset_capacity: [],
          asset_diagnostics: [],
        },
      });
    });

    for (const [path, key] of [
      ["project-reservations", "PROJECT_DIRECT"],
      ["resource-period-reservations", "RESOURCE_PERIOD"],
      ["segment-reservations", "SEGMENT"],
    ] as const) {
      await page.route(`**/api/v1/assets/${path}`, async (route) => {
        if (route.request().method() !== "POST") {
          await route.continue();
          return;
        }
        captured[key] = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            operation: "CREATE",
            requirement_origin: key,
            planning_version: Number(captured[key].expected_planning_version ?? 0) + 1,
          }),
        });
      });
    }

    await navigateMain(page, "Planning opérationnel");
    const panel = page.locator(".asset-planning-panel");
    await expect(panel.getByRole("heading", { name: "Actifs et réservations" })).toBeVisible();

    const requestCard = panel.locator('.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]');
    await expect(requestCard).toContainText("Fenêtre autorisée");
    await expect(requestCard).toContainText("Budget d’usage 8 h");
    await requestCard.getByRole("button", { name: "Ouvrir la demande source" }).click();
    await expect(page.getByRole("dialog", { name: "Détail de la demande DMO-575E" })).toBeVisible();
    await page.getByRole("dialog", { name: "Détail de la demande DMO-575E" })
      .getByRole("button", { name: "Fermer" })
      .click();

    const form = panel.locator(".asset-direct-reservation-panel");
    const unitSelect = form.locator('label:has(> span:text-is("Unité")) select');
    await expect(form).toContainText("Sans demande ou quart artificiel");
    await chooseCombobox(form, "Projet", "575E", "P-575E");
    await form.getByLabel("Type d’actif").selectOption("TYPE-575E");
    await expect(unitSelect.locator('option[value="ASSET-575E"]')).toBeAttached();
    await unitSelect.selectOption("ASSET-575E");
    await reserveAndWaitForPlanningRefresh(page, form);
    expect(captured.PROJECT_DIRECT.project_id).toBe("P-575E");
    expect(captured.PROJECT_DIRECT.operator_resource_id).toBeNull();

    await form.getByLabel("Réserver pour").selectOption("RESOURCE_PERIOD");
    await chooseCombobox(form, "Ressource", "575E", "Ressource 575E");
    await form.getByLabel("Type d’actif").selectOption("TYPE-575E");
    await expect(unitSelect.locator('option[value="ASSET-575E"]')).toBeAttached();
    await unitSelect.selectOption("ASSET-575E");
    await reserveAndWaitForPlanningRefresh(page, form);
    expect(captured.RESOURCE_PERIOD.resource_id).toBe("R-575E");
    expect(captured.RESOURCE_PERIOD.project_id).toBeNull();

    await form.getByLabel("Réserver pour").selectOption("SEGMENT");
    await chooseCombobox(form, "Segment", "575E", "P-575E");
    await form.getByLabel("Type d’actif").selectOption("TYPE-575E");
    await expect(unitSelect.locator('option[value="ASSET-575E"]')).toBeAttached();
    await unitSelect.selectOption("ASSET-575E");
    await chooseCombobox(form, "Opérateur", "575E", "Ressource 575E");
    await reserveAndWaitForPlanningRefresh(page, form);
    expect(captured.SEGMENT.segment_id).toBe("SEG-575E");
    expect(captured.SEGMENT.operator_resource_id).toBe("R-575E");
  } finally {
    await closeContext(context);
  }
});
