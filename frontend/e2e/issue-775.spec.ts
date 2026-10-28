import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
}

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

async function openAdmin(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
  return { context, page };
}

async function selectVehicleType(page: Page) {
  const catalog = page.locator(".asset-catalog-card");
  await catalog.locator(".asset-type-row").filter({ hasText: "Véhicule attitrable E2E" }).click();
  return catalog;
}

async function selectVehicle(page: Page, code: string) {
  const catalog = await selectVehicleType(page);
  await catalog.locator(".asset-unit-row").filter({ hasText: code }).click();
  await expect(catalog.locator(".asset-unit-editor")).toBeVisible();
  return catalog;
}

async function catalogPayload(page: Page) {
  const response = await page.request.get("/api/v1/assets/catalog");
  if (!response.ok()) throw new Error(await response.text());
  return response.json() as Promise<{
    types: Array<{ id: string; code: string; category: string }>;
    assets: Array<{ id: string; code: string; label: string; asset_type_id: string }>;
    planning_version: number;
  }>;
}

async function createVehicleByApi(page: Page, code: string, label: string) {
  const catalog = await catalogPayload(page);
  const type = catalog.types.find((row) => row.code === "VEH-744");
  expect(type).toBeTruthy();
  const response = await page.request.post("/api/v1/assets", {
    data: {
      code,
      label,
      asset_type_id: type!.id,
    },
  });
  if (response.status() !== 201) throw new Error(await response.text());
  const created = await response.json() as { id: string };
  await page.reload();
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
  return { assetId: created.id, assetTypeId: type!.id };
}

async function unitState(page: Page, assetId: string, asOf?: string) {
  const suffix = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  const response = await page.request.get(
    `/api/v1/assets/continuous-vehicle-units/${encodeURIComponent(assetId)}${suffix}`,
  );
  if (!response.ok()) throw new Error(await response.text());
  return response.json() as Promise<{
    has_continuous_assignment: boolean;
    assignment: null | {
      requirement_id: string;
      resource_period_mode: string;
      effective_state: string;
      start_date: string;
      end_date: string | null;
      operator_resource_id: string | null;
    };
    has_ended_history: boolean;
    planning_version: number;
  }>;
}

async function continuousCount(page: Page, resourceId: string, assetId: string) {
  const response = await page.request.get(
    `/api/v1/assets/continuous-resource-assignments?resource_id=${encodeURIComponent(resourceId)}`,
  );
  if (!response.ok()) throw new Error(await response.text());
  const payload = await response.json() as {
    assignments: Array<{ asset_id: string; resource_period_mode: string }>;
  };
  return payload.assignments.filter(
    (row) => row.asset_id === assetId && row.resource_period_mode === "CONTINUOUS",
  ).length;
}

test("775 restaure une attribution CONTINUOUS dans le formulaire Unité sans doublon", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await openAdmin(browser);
  const today = localIso(new Date());

  try {
    const catalog = await selectVehicleType(page);
    await catalog.getByRole("button", { name: "+ Unité" }).click();

    const unitEditor = catalog.locator(".asset-unit-editor");
    await expect(unitEditor).toBeVisible();
    await unitEditor.getByLabel("Code").fill("VEH-775-01");
    await unitEditor.getByLabel("Libellé").fill("Camion E2E 775");
    await expect(unitEditor.getByText("Attribution continue", { exact: true })).toHaveCount(0);
    await expect(unitEditor.getByRole("button", { name: "Enregistrer l’attribution" })).toHaveCount(0);

    const fields = unitEditor.getByTestId("asset-continuous-assignment-fields");
    await expect(fields).toBeVisible();
    await fields.getByRole("checkbox", { name: "Attitré" }).check();
    await fields.getByRole("combobox").selectOption("R-775");
    await fields.getByLabel("Date effective").fill(today);
    await unitEditor.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(catalog.locator(".asset-catalog-notice")).toContainText(
      "Unité physique créée et attribution continue enregistrée",
    );

    const afterCreate = await catalogPayload(page);
    const asset = afterCreate.assets.find((row) => row.code === "VEH-775-01");
    expect(asset).toBeTruthy();

    let canonical = await unitState(page, asset!.id);
    expect(canonical.has_continuous_assignment).toBeTruthy();
    expect(canonical.assignment?.resource_period_mode).toBe("CONTINUOUS");
    expect(canonical.assignment?.effective_state).toBe("ACTIVE");
    expect(canonical.assignment?.operator_resource_id).toBe("R-775");
    expect(canonical.assignment?.start_date).toBe(today);
    expect(await continuousCount(page, "R-775", asset!.id)).toBe(1);

    // Leave the unit and return: the draft must be rebuilt from canonical backend state.
    await catalog.locator(".asset-unit-row").filter({ hasText: "VEH-744-01" }).click();
    await catalog.locator(".asset-unit-row").filter({ hasText: "VEH-775-01" }).click();

    let restored = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(restored.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(restored.getByRole("combobox")).toHaveValue("R-775");
    await expect(restored.getByLabel("Date effective")).toHaveValue(today);

    // Reload/re-enter must restore the original start date rather than today/as_of inference.
    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    const reloadedCatalog = await selectVehicle(page, "VEH-775-01");
    restored = reloadedCatalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(restored.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(restored.getByRole("combobox")).toHaveValue("R-775");
    await expect(restored.getByLabel("Date effective")).toHaveValue(today);

    // Saving unchanged state must not issue a duplicate continuous create.
    await reloadedCatalog
      .locator(".asset-unit-editor")
      .getByRole("button", { name: "Enregistrer l’unité" })
      .click();
    expect(await continuousCount(page, "R-775", asset!.id)).toBe(1);

  } finally {
    await context.close();
  }
});

test("775 garde Attitré lié au mode CONTINUOUS et non à SCHEDULED/ACTIVE", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openAdmin(browser);
  const today = localIso(new Date());
  const future = localIso(addDays(new Date(), 7));
  const futureEnd = localIso(addDays(new Date(), 9));

  try {
    // Future CONTINUOUS is still Attitré even though its temporal state is SCHEDULED.
    const futureVehicle = await createVehicleByApi(page, "VEH-775-FUTURE", "Camion E2E 775 futur");
    const requirements = await page.request.get("/api/v1/assets/requirements");
    const requirementPayload = await requirements.json() as { planning_version: number };
    const continuous = await page.request.post("/api/v1/assets/continuous-resource-assignments", {
      headers: { "Idempotency-Key": "775-e2e-future-continuous" },
      data: {
        resource_id: "R-ALICE",
        asset_id: futureVehicle.assetId,
        start_date: future,
        expected_planning_version: requirementPayload.planning_version,
      },
    });
    expect(continuous.status(), await continuous.text()).toBe(201);

    let catalog = await selectVehicle(page, "VEH-775-FUTURE");
    let fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(fields.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(fields.getByRole("combobox")).toHaveValue("R-ALICE");
    await expect(fields.getByLabel("Date effective")).toHaveValue(future);
    await expect(fields.getByTestId("asset-continuous-assignment-state")).toContainText(
      `État : Planifié pour ${future}`,
    );

    // Resource change uses the canonical atomic transfer command; because the
    // assignment has not started yet, the replacement remains CONTINUOUS/SCHEDULED.
    await fields.getByRole("combobox").selectOption("R-BOB");
    await catalog.locator(".asset-unit-editor").getByRole(
      "button",
      { name: "Enregistrer l’unité" },
    ).click();
    // Wait for the asynchronous save/transfer and canonical refresh before
    // asserting the persisted resource; a click alone does not await a mutation.
    await expect(catalog.locator(".asset-catalog-notice")).toContainText("Unité enregistrée.");
    let futureState = await unitState(page, futureVehicle.assetId);
    expect(futureState.has_continuous_assignment).toBeTruthy();
    expect(futureState.assignment?.operator_resource_id).toBe("R-BOB");
    expect(futureState.assignment?.resource_period_mode).toBe("CONTINUOUS");
    expect(futureState.assignment?.effective_state).toBe("SCHEDULED");

    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    catalog = await selectVehicle(page, "VEH-775-FUTURE");
    fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(fields.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(fields.getByRole("combobox")).toHaveValue("R-BOB");
    await expect(fields.getByLabel("Date effective")).toHaveValue(future);

    // Unchecking a future CONTINUOUS cancels that #744 assignment.
    await fields.getByRole("checkbox", { name: "Attitré" }).uncheck();
    await catalog.locator(".asset-unit-editor").getByRole(
      "button",
      { name: "Enregistrer l’unité" },
    ).click();
    expect((await unitState(page, futureVehicle.assetId)).has_continuous_assignment).toBeFalsy();

    // Generic bounded RESOURCE_PERIOD, future SCHEDULED, must remain unchecked.
    const boundedScheduled = await createVehicleByApi(
      page,
      "VEH-775-BOUNDED-F",
      "Camion E2E 775 bounded futur",
    );
    let state = await page.request.get("/api/v1/assets/requirements");
    let version = (await state.json() as { planning_version: number }).planning_version;
    const boundedFuture = await page.request.post("/api/v1/assets/resource-period-reservations", {
      headers: { "Idempotency-Key": "775-e2e-bounded-future" },
      data: {
        resource_id: "R-ALICE",
        asset_type_id: boundedScheduled.assetTypeId,
        asset_id: boundedScheduled.assetId,
        start_date: future,
        end_date: futureEnd,
        expected_planning_version: version,
      },
    });
    expect(boundedFuture.status(), await boundedFuture.text()).toBe(201);

    catalog = await selectVehicle(page, "VEH-775-BOUNDED-F");
    fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(fields.getByRole("checkbox", { name: "Attitré" })).not.toBeChecked();
    expect((await unitState(page, boundedScheduled.assetId)).has_continuous_assignment).toBeFalsy();

    // Generic bounded reservation already ACTIVE today also stays unchecked.
    const boundedActive = await createVehicleByApi(
      page,
      "VEH-775-BOUNDED-A",
      "Camion E2E 775 bounded actif",
    );
    state = await page.request.get("/api/v1/assets/requirements");
    version = (await state.json() as { planning_version: number }).planning_version;
    const boundedToday = await page.request.post("/api/v1/assets/resource-period-reservations", {
      headers: { "Idempotency-Key": "775-e2e-bounded-active" },
      data: {
        resource_id: "R-BOB",
        asset_type_id: boundedActive.assetTypeId,
        asset_id: boundedActive.assetId,
        start_date: today,
        end_date: localIso(addDays(new Date(), 2)),
        expected_planning_version: version,
      },
    });
    expect(boundedToday.status(), await boundedToday.text()).toBe(201);

    catalog = await selectVehicle(page, "VEH-775-BOUNDED-A");
    fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(fields.getByRole("checkbox", { name: "Attitré" })).not.toBeChecked();
    expect((await unitState(page, boundedActive.assetId)).has_continuous_assignment).toBeFalsy();

    // ACTIVE CONTINUOUS uncheck uses canonical revocation and preserves history.
    const revokeVehicle = await createVehicleByApi(
      page,
      "VEH-775-REVOKE",
      "Camion E2E 775 révocation",
    );
    state = await page.request.get("/api/v1/assets/requirements");
    version = (await state.json() as { planning_version: number }).planning_version;
    const activeContinuous = await page.request.post(
      "/api/v1/assets/continuous-resource-assignments",
      {
        headers: { "Idempotency-Key": "775-e2e-active-revoke" },
        data: {
          resource_id: "R-775",
          asset_id: revokeVehicle.assetId,
          start_date: today,
          expected_planning_version: version,
        },
      },
    );
    expect(activeContinuous.status(), await activeContinuous.text()).toBe(201);

    catalog = await selectVehicle(page, "VEH-775-REVOKE");
    fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    await expect(fields.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await fields.getByRole("checkbox", { name: "Attitré" }).uncheck();
    const revokeDate = localIso(addDays(new Date(), 1));
    await expect(fields.getByLabel("Date effective de révocation")).toHaveValue(revokeDate);
    await catalog.locator(".asset-unit-editor").getByRole(
      "button",
      { name: "Enregistrer l’unité" },
    ).click();

    // A future-dated revocation leaves the contract effective through today.
    const currentRevokedState = await unitState(page, revokeVehicle.assetId);
    expect(currentRevokedState.has_continuous_assignment).toBeTruthy();
    expect(currentRevokedState.assignment?.end_date).toBe(today);
    await expect(
      catalog
        .locator(".asset-unit-editor")
        .getByTestId("asset-continuous-assignment-fields")
        .getByRole("checkbox", { name: "Attitré" }),
    ).toBeChecked();

    // On the first non-applicable day, the same canonical history is no longer Attitré.
    const endedState = await unitState(page, revokeVehicle.assetId, revokeDate);
    expect(endedState.has_continuous_assignment).toBeFalsy();
    expect(endedState.assignment).toBeNull();
    expect(endedState.has_ended_history).toBeTruthy();
  } finally {
    await context.close();
  }
});

test("775 conserve l’état continu caché et signale un succès partiel catalogue", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openAdmin(browser);
  const today = localIso(new Date());

  try {
    // Redacted CONTINUOUS presence remains checked and non-editable.
    const hidden = await createVehicleByApi(page, "VEH-775-HIDDEN", "Camion E2E 775 caché");
    await page.route(
      `**/api/v1/assets/continuous-vehicle-units/${hidden.assetId}`,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            asset_id: hidden.assetId,
            asset_category: "VEHICLE",
            as_of: today,
            has_continuous_assignment: true,
            assignment: {
              requirement_id: "REQ-775-HIDDEN",
              allocation_id: "ALLOC-775-HIDDEN",
              asset_id: hidden.assetId,
              asset_code: "VEH-775-HIDDEN",
              asset_label: "Camion E2E 775 caché",
              asset_type_id: hidden.assetTypeId,
              asset_type_code: "VEH-744",
              asset_type_label: "Véhicule attitrable E2E",
              resource_period_mode: "CONTINUOUS",
              effective_state: "ACTIVE",
              start_date: today,
              end_date: null,
              operator_resource_id: null,
              qualification_state: "SATISFIED",
            },
            scheduled_assignment: null,
            assignment_details_visible: false,
            scheduled_assignment_details_visible: false,
            has_ended_history: false,
            assignment_authority: {
              allowed: false,
              can_assign: false,
              status: "NOT_AUTHORIZED",
              code: "asset_assignment_authority_required",
              message: "Admissible opérationnellement, mais vous n’êtes pas autorisé à attribuer cette unité.",
              diagnostics: [],
            },
            planning_version: 775,
          }),
        });
      },
    );

    let catalog = await selectVehicle(page, "VEH-775-HIDDEN");
    let fields = catalog.locator(".asset-unit-editor").getByTestId(
      "asset-continuous-assignment-fields",
    );
    const hiddenCheck = fields.getByRole("checkbox", { name: "Attitré" });
    await expect(hiddenCheck).toBeChecked();
    await expect(hiddenCheck).toBeDisabled();
    await expect(fields).toContainText("ressource hors de votre périmètre");
    await expect(fields).not.toContainText("R-ALICE");
    await expect(fields).toContainText("vous n’êtes pas autorisé");
    await page.unroute(`**/api/v1/assets/continuous-vehicle-units/${hidden.assetId}`);

    // Catalogue update succeeds, then continuous assignment fails: report partial success.
    const partial = await createVehicleByApi(page, "VEH-775-PARTIAL", "Camion E2E 775 partiel");
    catalog = await selectVehicle(page, "VEH-775-PARTIAL");
    const editor = catalog.locator(".asset-unit-editor");
    await editor.getByLabel("Libellé").fill("Camion E2E 775 partiel modifié");
    fields = editor.getByTestId("asset-continuous-assignment-fields");
    await fields.getByRole("checkbox", { name: "Attitré" }).check();
    await fields.getByRole("combobox").selectOption("R-ALICE");
    await fields.getByLabel("Date effective").fill(today);

    await page.route("**/api/v1/assets/continuous-resource-assignments", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "asset_double_booking",
            message: "Actif déjà réservé.",
          },
        }),
      });
    });

    await editor.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(catalog.locator(".asset-catalog-notice")).toContainText(
      "L’unité a été enregistrée, mais l’attribution continue a échoué",
    );
    await page.unroute("**/api/v1/assets/continuous-resource-assignments");

    const refreshed = await catalogPayload(page);
    expect(refreshed.assets.find((row) => row.id === partial.assetId)?.label).toBe(
      "Camion E2E 775 partiel modifié",
    );
    expect((await unitState(page, partial.assetId)).has_continuous_assignment).toBeFalsy();
  } finally {
    await context.close();
  }
});
