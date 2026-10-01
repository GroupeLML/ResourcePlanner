import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openAs(browser: Browser, role: "ADMIN" | "COORDINATOR" | "PROJECT_MANAGER") {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  await page.goto("/");
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

function combobox(scope: Locator, label: string) {
  return scope.getByRole("combobox", { name: label, exact: true }).first();
}

async function chooseCombobox(
  scope: Locator,
  label: string,
  query: string,
  optionName: string,
) {
  const input = combobox(scope, label);
  await input.click();
  await input.fill(query);
  const listbox = scope.getByRole("listbox", { name: `${label} options`, exact: true }).first();
  const option = listbox.getByRole("option", { name: optionName, exact: false }).first();
  await expect(option).toBeVisible();
  await option.click();
  return input;
}

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
}

test("SearchableCombobox filtre sans casse et respecte clavier, Escape et identités stables", async ({ browser }) => {
  const { context, page } = await openAs(browser, "COORDINATOR");
  try {
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: "+ Quick Shift", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Créer un Quick Shift" });

    const project = combobox(dialog, "Projet");
    await project.fill("251");
    const projectList = dialog.getByRole("listbox", { name: "Projet options", exact: true });
    await expect(projectList.getByRole("option", { name: /P-251/ })).toBeVisible();
    await project.press("ArrowDown");
    await project.press("Enter");
    await expect(project).toHaveAttribute("data-combobox-value", "P-251-ID");
    await expect(project).toHaveValue(/P-251/);

    const technician = combobox(dialog, "Technicien");
    await technician.click();
    await technician.fill("PROGRAMMEUR");
    const technicianList = dialog.getByRole("listbox", { name: "Technicien options", exact: true });
    await expect(technicianList.getByRole("option")).toHaveCount(2);
    await technician.fill("programmeur");
    await expect(technicianList.getByRole("option")).toHaveCount(2);

    await technician.fill("ob");
    await expect(technicianList.getByRole("option", { name: /Bob/ })).toBeVisible();
    await technician.press("ArrowDown");
    await technician.press("Enter");
    await expect(technician).toHaveAttribute("data-combobox-value", "R-BOB");
    await expect(technician).toHaveValue(/Bob/);

    await technician.click();
    await technician.fill("LiC");
    await expect(technicianList.getByRole("option", { name: /Alice/ })).toBeVisible();
    await technician.press("Escape");
    await expect(technician).toHaveValue(/Bob/);
    await expect(technicianList).toBeHidden();

    await technician.click();
    await technician.fill("aucune-correspondance");
    await expect(technicianList).toContainText("Aucun résultat");
    await technician.press("Escape");
    await expect(technician).toHaveValue(/Bob/);

    await dialog.getByRole("button", { name: "Fermer" }).click();
  } finally {
    await closeContext(context);
  }
});

test("Demandes recherche les référentiels, clear sans submit et conserve les contrats backend", async ({ browser }) => {
  const { context, page } = await openAs(browser, "PROJECT_MANAGER");
  try {
    await navigateMain(page, "Demandes");

    const filter = combobox(page.locator(".demand-filter-bar"), "Filtre Projet");
    await filter.fill("251");
    const filterList = page.getByRole("listbox", { name: "Filtre Projet options", exact: true });
    await filterList.getByRole("option", { name: /P-251/ }).click();
    await expect(filter).toHaveAttribute("data-combobox-value", "P-251-ID");
    await page.getByRole("button", { name: "Effacer Filtre Projet" }).click();
    await expect(filter).toHaveValue("");

    await page.getByRole("button", { name: /Nouvelle demande/ }).click();
    const editor = page.locator(".demand-editor-form");
    await chooseCombobox(editor, "Projet", "251", "P-251");

    const task = await chooseCombobox(editor, "Tâche ERP", "autom", "210 — AUTOMATISATION E2E");
    await expect(task).toHaveAttribute("data-combobox-value", "TASK-P251-210");

    const resource = await chooseCombobox(editor, "Ressource proposée", "LiC", "Alice");
    await expect(resource).toHaveAttribute("data-combobox-value", "R-ALICE");
    await editor.getByRole("button", { name: "Effacer Ressource proposée" }).click();
    await expect(resource).toHaveValue("");
    await expect(editor.getByRole("heading", { name: "Nouvelle demande" })).toBeVisible();
    await chooseCombobox(editor, "Ressource proposée", "alice", "Alice");

    const requestPromise = page.waitForRequest((request) => (
      request.method() === "POST"
      && request.url().endsWith("/api/v1/demands")
    ));
    await editor.getByRole("button", { name: "Créer le brouillon" }).click();
    const request = await requestPromise;
    const payload = request.postDataJSON() as {
      project_number: string;
      task_code: string | null;
      proposed_technician: string | null;
    };
    expect(payload.project_number).toBe("P-251");
    expect(payload.task_code).toBe("210");
    expect(payload.proposed_technician).toBe("Alice");
    await expect(page.locator(".demand-notice")).toContainText("créée en brouillon");
  } finally {
    await closeContext(context);
  }
});

test("Demandes affiche une ressource historique sans la recréer comme option active", async ({ browser }) => {
  const admin = await openAs(browser, "ADMIN");
  const projectManager = await openAs(browser, "PROJECT_MANAGER");
  try {
    const historicalName = "Ressource historique E2E";
    const resourceCreate = await admin.page.request.post("/api/v1/resources", {
      data: {
        name: historicalName,
        resource_class: "PROGRAMMEUR",
        competencies: "SCADA",
        sort_order: 900,
      },
    });
    expect(resourceCreate.status(), await resourceCreate.text()).toBe(201);
    const { resource_id: resourceId } = await resourceCreate.json() as { resource_id: string };

    const demandCreate = await projectManager.page.request.post("/api/v1/demands", {
      headers: { "Idempotency-Key": "issue-546-historical-resource" },
      data: {
        project_number: "P-251",
        desired_start: localIso(new Date()),
        resource_count: 1,
        proposed_technician: historicalName,
        description: "Valeur historique pour combobox",
        submit: false,
      },
    });
    expect(demandCreate.status(), await demandCreate.text()).toBe(201);
    const { demand_number: demandNumber } = await demandCreate.json() as { demand_number: string };

    const deactivate = await admin.page.request.post(
      `/api/v1/resources/${encodeURIComponent(resourceId)}/deactivate`,
    );
    expect(deactivate.status(), await deactivate.text()).toBe(200);

    await navigateMain(projectManager.page, "Demandes");
    const card = projectManager.page.locator(".demand-card").filter({ hasText: demandNumber }).first();
    await expect(card).toBeVisible();
    await card.click();

    const editor = projectManager.page.locator(".demand-editor-form");
    const resource = combobox(editor, "Ressource proposée");
    await expect(resource).toHaveValue(`${historicalName} — inactive/non listée`);
    await expect(resource).toHaveAttribute("data-combobox-value", /historical:resource-name:/);

    await resource.click();
    const listbox = editor.getByRole("listbox", { name: "Ressource proposée options", exact: true });
    await expect(listbox.getByRole("option", { name: historicalName, exact: false })).toHaveCount(0);
    await resource.press("Escape");
    await expect(resource).toHaveValue(`${historicalName} — inactive/non listée`);
  } finally {
    await closeContext(projectManager.context);
    await closeContext(admin.context);
  }
});
