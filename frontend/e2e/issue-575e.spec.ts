import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function createCoordinator(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
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
  const { context, page } = await createCoordinator(browser);
  const captured: Record<string, Record<string, unknown>> = {};
  let requestReserved = false;
  let planningSnapshotRequests = 0;
  let requirementReservationMutations = 0;

  try {
    await page.route("**/api/v1/projects?**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "P-575E",
            number: "P-575E",
            name: "Projet acceptation 575E",
            client: "Client 575E",
            status: "Actif",
          },
        ]),
      });
    });

    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      planningSnapshotRequests += 1;
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
        status: requestReserved ? "Réservé" : "À affecter",
        allocation_id: requestReserved ? "ALLOC-REQUEST-575E" : null,
        asset_id: requestReserved ? "ASSET-575E" : null,
        asset_code: requestReserved ? "TRUCK-575E-A" : null,
        asset_label: requestReserved ? "Camion acceptation" : null,
        allocation_start_date: requestReserved ? start : null,
        allocation_end_date: requestReserved ? end : null,
        allocation_locked: false,
        operator_resource_id: null,
        operator_resource_name: null,
        qualification_state: "SATISFIED",
        required_competency_ids: [],
        required_competency_names: [],
      };
      const reservedNeed = {
        ...requestNeed,
        requirement_id: "AREQ-RESERVED-575E",
        origin: "RESOURCE_PERIOD",
        request_id: null,
        demand_number: null,
        source_request_line_id: null,
        approval_revision_id: null,
        approved_entry_key: null,
        context_resource_id: "R-575E",
        resource_period_mode: "BOUNDED",
        status: "Réservé",
        allocation_id: "ALLOC-RESERVED-575E",
        asset_id: "ASSET-575E",
        asset_code: "TRUCK-575E-A",
        asset_label: "Camion acceptation",
        allocation_start_date: start,
        allocation_end_date: end,
        operator_resource_id: "R-575E",
        operator_resource_name: "Ressource 575E",
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
          asset_requirements: [requestNeed, reservedNeed],
          asset_allocations: [],
          asset_unavailability: [],
          asset_capacity: [],
          asset_diagnostics: [],
        },
      });
    });

    await page.route(
      "**/api/v1/assets/requirements/AREQ-REQUEST-575E/reservation",
      async (route) => {
        if (route.request().method() !== "PUT") {
          await route.continue();
          return;
        }
        requirementReservationMutations += 1;
        const payload = route.request().postDataJSON() as {
          asset_id: string | null;
          expected_planning_version: number;
        };
        requestReserved = Boolean(payload.asset_id);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            planning_version: payload.expected_planning_version + 1,
            allocation_id: requestReserved ? "ALLOC-REQUEST-575E" : null,
            changed: true,
          }),
        });
      },
    );

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

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");

    await navigateMain(page, "Planning opérationnel");
    const panel = page.locator(".asset-planning-panel");
    await expect(panel.getByRole("heading", { name: "Actifs et réservations" })).toBeVisible();

    const approvedPanel = panel.getByTestId("approved-asset-requirements");
    const parentToggle = panel.getByTestId("approved-asset-requirements-toggle");
    const unreservedGroup = panel.getByTestId("unreserved-asset-requirements");
    const reservedGroup = panel.getByTestId("reserved-asset-requirements");
    const unreservedToggle = panel.getByTestId("unreserved-asset-requirements-toggle");
    const reservedToggle = panel.getByTestId("reserved-asset-requirements-toggle");

    await expect(approvedPanel).toContainText("Besoins approuvés");
    await expect(unreservedGroup).toContainText("À réserver (1)");
    await expect(reservedGroup).toContainText("Réservé (1)");
    await expect(
      unreservedGroup.locator('.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]'),
    ).toHaveCount(1);
    await expect(
      reservedGroup.locator('.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]'),
    ).toHaveCount(0);
    await expect(
      reservedGroup.locator('.asset-requirement-card[data-requirement-id="AREQ-RESERVED-575E"]'),
    ).toHaveCount(1);

    const beforeCollapseUrl = page.url();
    const beforeCollapseSnapshots = planningSnapshotRequests;
    const beforeCollapseMutations = requirementReservationMutations;

    await reservedToggle.click();
    await expect(reservedToggle).toHaveAttribute("aria-expanded", "false");
    await expect(panel.getByTestId("reserved-asset-requirements-content")).toHaveCount(0);

    await parentToggle.click();
    await expect(parentToggle).toHaveAttribute("aria-expanded", "false");
    await expect(panel.getByTestId("approved-asset-requirements-content")).toHaveCount(0);

    await parentToggle.click();
    await expect(parentToggle).toHaveAttribute("aria-expanded", "true");
    await expect(panel.getByTestId("unreserved-asset-requirements-content")).toBeVisible();
    await expect(panel.getByTestId("reserved-asset-requirements-content")).toHaveCount(0);
    await expect(reservedToggle).toHaveAttribute("aria-expanded", "false");

    expect(page.url()).toBe(beforeCollapseUrl);
    expect(planningSnapshotRequests).toBe(beforeCollapseSnapshots);
    expect(requirementReservationMutations).toBe(beforeCollapseMutations);

    await reservedToggle.click();
    await expect(panel.getByTestId("reserved-asset-requirements-content")).toBeVisible();

    const requestCard = unreservedGroup.locator(
      '.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]',
    );
    await expect(requestCard).toContainText("Fenêtre autorisée");
    await expect(requestCard).toContainText("Budget d’usage 8 h");
    await requestCard.getByRole("button", { name: "Ouvrir la demande source" }).click();
    await expect(page.getByRole("dialog", { name: "Détail de la demande DMO-575E" })).toBeVisible();
    await page.getByRole("dialog", { name: "Détail de la demande DMO-575E" })
      .getByRole("button", { name: "Fermer" })
      .click();

    const requestUnit = requestCard.locator('label:has(> span:text-is("Unité")) select');
    await requestUnit.selectOption("ASSET-575E");
    const [reserveMutation] = await Promise.all([
      page.waitForResponse((response) => (
        response.request().method() === "PUT"
        && response.url().includes(
          "/api/v1/assets/requirements/AREQ-REQUEST-575E/reservation",
        )
      )),
      requestCard.getByRole("button", { name: "Enregistrer la réservation" }).click(),
    ]);
    expect(reserveMutation.status()).toBe(200);
    await expect(unreservedGroup).toContainText("À réserver (0)");
    await expect(reservedGroup).toContainText("Réservé (2)");
    await expect(panel.getByTestId("unreserved-asset-requirements-content")).toContainText(
      "Aucun besoin à réserver",
    );
    const movedReservedCard = reservedGroup.locator(
      '.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]',
    );
    await expect(movedReservedCard).toHaveCount(1);
    await expect(unreservedGroup.locator(
      '.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]',
    )).toHaveCount(0);

    await movedReservedCard.locator('label:has(> span:text-is("Unité")) select').selectOption("");
    const [releaseMutation] = await Promise.all([
      page.waitForResponse((response) => (
        response.request().method() === "PUT"
        && response.url().includes(
          "/api/v1/assets/requirements/AREQ-REQUEST-575E/reservation",
        )
      )),
      movedReservedCard.getByRole("button", { name: "Libérer" }).click(),
    ]);
    expect(releaseMutation.status()).toBe(200);
    await expect(unreservedGroup).toContainText("À réserver (1)");
    await expect(reservedGroup).toContainText("Réservé (1)");
    await expect(unreservedGroup.locator(
      '.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]',
    )).toHaveCount(1);
    await expect(reservedGroup.locator(
      '.asset-requirement-card[data-requirement-id="AREQ-REQUEST-575E"]',
    )).toHaveCount(0);

    await unreservedToggle.click();
    await expect(unreservedToggle).toHaveAttribute("aria-expanded", "false");
    await expect(panel.getByTestId("unreserved-asset-requirements-content")).toHaveCount(0);
    await unreservedToggle.click();
    await expect(unreservedToggle).toHaveAttribute("aria-expanded", "true");

    const occupancyToggle = panel.getByTestId("asset-unit-occupancy-toggle");
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "true");
    await occupancyToggle.click();
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "false");
    await occupancyToggle.click();
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "true");

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
    await expect(form.getByRole("combobox", { name: "Projet", exact: true })).toHaveCount(0);
    await chooseCombobox(form, "Ressource", "575E", "Ressource 575E");
    await form.getByLabel("Type d’actif").selectOption("TYPE-575E");
    await expect(unitSelect.locator('option[value="ASSET-575E"]')).toBeAttached();
    await unitSelect.selectOption("ASSET-575E");
    await reserveAndWaitForPlanningRefresh(page, form);
    expect(captured.RESOURCE_PERIOD.resource_id).toBe("R-575E");
    expect(captured.RESOURCE_PERIOD).not.toHaveProperty("project_id");

    await form.getByLabel("Réserver pour").selectOption("SEGMENT");
    await chooseCombobox(form, "Segment", "575E", "P-575E");
    await expect(form.getByLabel("Projet dérivé du segment")).toHaveValue(
      "P-575E — Projet acceptation 575E",
    );
    await expect(form.getByLabel("Projet dérivé du segment")).toBeDisabled();
    await form.getByLabel("Type d’actif").selectOption("TYPE-575E");
    await expect(unitSelect.locator('option[value="ASSET-575E"]')).toBeAttached();
    await unitSelect.selectOption("ASSET-575E");
    await reserveAndWaitForPlanningRefresh(page, form);
    expect(captured.SEGMENT.segment_id).toBe("SEG-575E");
    expect(captured.SEGMENT.operator_resource_id).toBeNull();
  } finally {
    await closeContext(context);
  }
});