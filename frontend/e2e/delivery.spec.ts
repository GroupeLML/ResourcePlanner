import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

type Role = "PROJECT_MANAGER" | "TEAM_LEAD" | "DELIVERY_TECHNICIAN";

const DISPLAY_NAMES: Record<Role, string> = {
  PROJECT_MANAGER: "Chargé E2E",
  TEAM_LEAD: "Team Lead E2E",
  DELIVERY_TECHNICIAN: "Technicien Delivery E2E",
};

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

function deliveryDates() {
  const nextMonday = addDays(startOfWeek(new Date()), 7);
  return {
    start: localIso(nextMonday),
    end: localIso(addDays(nextMonday, 4)),
  };
}

async function openAs(browser: Browser, role: Role) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText(DISPLAY_NAMES[role]);
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

function labelled(scope: Locator, label: string, control: "select" | "input" | "textarea") {
  return scope.locator("label").filter({ hasText: label }).first().locator(control);
}

async function selectOptionContaining(select: Locator, text: string) {
  const option = select.locator("option", { hasText: text }).first();
  await expect(option).toBeAttached();
  const value = await option.getAttribute("value");
  expect(value).not.toBeNull();
  await select.selectOption(value!);
  return value!;
}

async function principal(page: Page) {
  const response = await page.request.get("/api/v1/auth/me");
  expect(response.ok(), await response.text()).toBeTruthy();
  return await response.json() as {
    local_user_id: string;
    roles: string[];
    permissions: string[];
  };
}

async function openDelivery(page: Page, workPackageCode: string, useGlobalScope = false) {
  await navigateMain(page, "Delivery");
  await expect(page.getByRole("heading", { name: "WorkPackages, Epics et Stories" })).toBeVisible();
  if (useGlobalScope) {
    const scope = page.locator(".view-scope-selector");
    await expect(scope).toBeVisible();
    await scope.getByRole("button", { name: "Vue globale" }).click();
  }
  const selectors = page.locator(".delivery-selector-panel");
  await labelled(selectors, "Projet", "select").selectOption("P-251");
  const selectedWorkPackageId = await selectOptionContaining(
    labelled(selectors, "WorkPackage", "select"),
    workPackageCode,
  );
  await expect(selectors.locator(".delivery-selected-reference")).toContainText(selectedWorkPackageId);
}

function storyCard(page: Page, title: string) {
  return page.locator(".delivery-story-card").filter({ hasText: title }).first();
}

function storyFact(card: Locator, name: string) {
  return card.locator(".delivery-story-facts > div").filter({ hasText: name }).first();
}

test("362F Delivery traverse PM, Team Lead et technicien sans élargir Planning", async ({ browser }) => {
  test.setTimeout(180_000);
  const workPackageCode = "WP-DELIVERY-362F";
  const epicTitle = "Epic acceptation 362F";
  const storyTitle = "Story acceptation 362F";
  const { start, end } = deliveryDates();

  const leadIdentity = await openAs(browser, "TEAM_LEAD");
  const leadPrincipal = await principal(leadIdentity.page);
  expect(leadPrincipal.roles).toContain("DELIVERY_CONTRIBUTOR");
  expect(leadPrincipal.permissions).toContain("contribute_delivery");
  expect(leadPrincipal.permissions).not.toContain("manage_planning");
  await closeContext(leadIdentity.context);

  const techIdentity = await openAs(browser, "DELIVERY_TECHNICIAN");
  const techPrincipal = await principal(techIdentity.page);
  expect(techPrincipal.roles).toContain("TECHNICIAN");
  expect(techPrincipal.roles).toContain("DELIVERY_CONTRIBUTOR");
  expect(techPrincipal.permissions).toContain("contribute_delivery");
  expect(techPrincipal.permissions).not.toContain("manage_planning");
  await closeContext(techIdentity.context);

  const projectManager = await openAs(browser, "PROJECT_MANAGER");
  await navigateMain(projectManager.page, "Moyen terme");
  await projectManager.page.getByRole("button", { name: /WorkPackage/ }).click();
  const dialog = projectManager.page.getByRole("dialog", { name: "Créer un lot" });
  await labelled(dialog, "Projet", "select").selectOption("P-251");
  await labelled(dialog, "Code", "input").fill(workPackageCode);
  await labelled(dialog, "Nom", "input").fill("WP-DELIVERY-362F — Lot Delivery acceptation");
  await labelled(dialog, "Début", "input").fill(start);
  await labelled(dialog, "Fin", "input").fill(end);
  await labelled(dialog, "Heures prévues", "input").fill("40");
  await labelled(dialog, "Description", "textarea").fill("Validation transversale ADR-008");
  await dialog.getByRole("button", { name: "Créer le WorkPackage" }).click();
  await expect(dialog).toBeHidden();

  await openDelivery(projectManager.page, workPackageCode);
  await expect(projectManager.page.getByText("Aucun DeliveryPlan", { exact: false })).toBeVisible();
  await projectManager.page.getByRole("button", { name: "Créer le DeliveryPlan" }).click();
  await expect(projectManager.page.locator(".delivery-plan-toolbar")).toContainText("DRAFT");

  const toolbar = projectManager.page.locator(".delivery-plan-toolbar");
  await labelled(toolbar, "Team Lead AppUser", "input").fill(leadPrincipal.local_user_id);
  await toolbar.getByRole("button", { name: "Enregistrer le lead" }).click();
  await expect(toolbar).toContainText("Version Delivery 2");
  await toolbar.getByRole("button", { name: "Activer" }).click();
  await expect(toolbar).toContainText("ACTIVE");
  await expect(toolbar).toContainText("Version Delivery 3");

  const leadA = await openAs(browser, "TEAM_LEAD");
  await openDelivery(leadA.page, workPackageCode);
  const createForm = leadA.page.locator(".delivery-create-item");
  await expect(createForm).toBeVisible();

  await labelled(createForm, "Type", "select").selectOption("EPIC");
  await labelled(createForm, "Titre", "input").fill(epicTitle);
  await labelled(createForm, "Description", "textarea").fill("Regroupement technique 362F");
  await createForm.getByRole("button", { name: "Ajouter l'Epic" }).click();
  await expect(leadA.page.locator(".delivery-epic-card").filter({ hasText: epicTitle })).toBeVisible();

  await labelled(createForm, "Type", "select").selectOption("STORY");
  await labelled(createForm, "Titre", "input").fill(storyTitle);
  await selectOptionContaining(labelled(createForm, "Epic parent", "select"), epicTitle);
  await labelled(createForm, "AppUser assigné", "input").fill(techPrincipal.local_user_id);
  await labelled(createForm, "Estimation (h)", "input").fill("8");
  await labelled(createForm, "Restant (h)", "input").fill("8");
  await labelled(createForm, "Description", "textarea").fill("Story traversant frontend et backend");
  const storyCreateResponse = leadA.page.waitForResponse(
    (response) => response.request().method() === "POST"
      && /\/api\/v1\/delivery\/plans\/[^/]+\/items$/.test(new URL(response.url()).pathname),
  );
  await createForm.getByRole("button", { name: "Ajouter la Story" }).click();
  const storyCreated = await storyCreateResponse;
  expect(storyCreated.status(), await storyCreated.text()).toBe(201);

  let leadStory = storyCard(leadA.page, storyTitle);
  await expect(leadStory).toBeVisible();
  await labelled(leadStory, "Statut", "select").selectOption("TODO");
  await leadStory.getByRole("button", { name: "Enregistrer" }).click();
  await expect(storyFact(leadStory, "Référence")).toContainText("8 h");
  await expect(leadA.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 6");

  const leadPlanning = await leadA.page.request.post("/api/v1/planning/rebuild");
  expect(leadPlanning.status()).toBe(403);
  const leadPlanningBody = await leadPlanning.json();
  expect(leadPlanningBody.error.context.required_permission).toBe("manage_planning");

  const leadB = await openAs(browser, "TEAM_LEAD");
  await openDelivery(leadB.page, workPackageCode);
  const leadBStory = storyCard(leadB.page, storyTitle);
  await labelled(leadBStory, "Restant (h)", "input").fill("6");
  await leadBStory.getByRole("button", { name: "Enregistrer" }).click();
  await expect(leadB.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 7");

  leadStory = storyCard(leadA.page, storyTitle);
  await labelled(leadStory, "Statut", "select").selectOption("IN_PROGRESS");
  await leadStory.getByRole("button", { name: "Enregistrer" }).click();
  await expect(leadA.page.locator(".error-panel")).toContainText("delivery_version_conflict");
  await expect(leadA.page.locator(".error-panel")).toContainText("rechargé avec la version courante");
  await expect(leadA.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 7");
  leadStory = storyCard(leadA.page, storyTitle);
  await expect(labelled(leadStory, "Statut", "select")).toHaveValue("TODO");
  await expect(labelled(leadStory, "Restant (h)", "input")).toHaveValue("6");

  await labelled(leadStory, "Statut", "select").selectOption("IN_PROGRESS");
  await labelled(leadStory, "Estimation (h)", "input").fill("12");
  await labelled(leadStory, "Restant (h)", "input").fill("5");
  await leadStory.getByRole("button", { name: "Enregistrer" }).click();
  await expect(storyFact(leadStory, "Référence")).toContainText("8 h");
  await expect(storyFact(leadStory, "Estimation")).toContainText("12 h");
  await expect(storyFact(leadStory, "Restant")).toContainText("5 h");
  await expect(leadA.page.locator(".delivery-summary").locator("article").filter({ hasText: "Progression" }).locator("strong")).toHaveText("0 %");
  await expect(leadA.page.locator(".delivery-summary").locator("article").filter({ hasText: "Travail restant" }).locator("strong")).toHaveText("5 h");
  await expect(leadA.page.locator(".delivery-summary").locator("article").filter({ hasText: "Capacité réservée" }).locator("strong")).toHaveText("0 h");

  const technician = await openAs(browser, "DELIVERY_TECHNICIAN");
  await openDelivery(technician.page, workPackageCode, true);
  let techStory = storyCard(technician.page, storyTitle);
  await expect(labelled(techStory, "AppUser assigné", "input")).toHaveCount(0);
  await expect(labelled(techStory, "Estimation (h)", "input")).toHaveCount(0);
  await labelled(techStory, "Statut", "select").selectOption("BLOCKED");
  await labelled(techStory, "Restant (h)", "input").fill("3");
  await techStory.getByRole("button", { name: "Enregistrer" }).click();

  techStory = storyCard(technician.page, storyTitle);
  await expect(labelled(techStory, "Note de blocage", "textarea")).toBeVisible();
  await labelled(techStory, "Note de blocage", "textarea").fill("Dépendance externe 362F");
  await techStory.getByRole("button", { name: "Ajouter la note" }).click();
  await expect(technician.page.locator(".delivery-summary").locator("article").filter({ hasText: "Travail restant" }).locator("strong")).toHaveText("3 h");

  const forbiddenPlanning = await technician.page.request.post("/api/v1/planning/rebuild");
  expect(forbiddenPlanning.status()).toBe(403);
  const forbiddenBody = await forbiddenPlanning.json();
  expect(forbiddenBody.error.context.required_permission).toBe("manage_planning");

  techStory = storyCard(technician.page, storyTitle);
  await labelled(techStory, "Statut", "select").selectOption("DONE");
  await labelled(techStory, "Restant (h)", "input").fill("0");
  await techStory.getByRole("button", { name: "Enregistrer" }).click();
  await expect(technician.page.locator(".delivery-summary").locator("article").filter({ hasText: "Progression" }).locator("strong")).toHaveText("100 %");
  await expect(technician.page.locator(".delivery-summary").locator("article").filter({ hasText: "Travail restant" }).locator("strong")).toHaveText("0 h");
  await expect(storyFact(storyCard(technician.page, storyTitle), "Référence")).toContainText("8 h");

  await closeContext(technician.context);
  await closeContext(leadB.context);
  await closeContext(leadA.context);

  await expect(projectManager.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 3");
  await projectManager.page.locator(".delivery-plan-toolbar").getByRole("button", { name: "Archiver" }).click();
  await expect(projectManager.page.locator(".error-panel")).toContainText("delivery_version_conflict");
  await expect(projectManager.page.locator(".error-panel")).toContainText("rechargé avec la version courante");
  await expect(projectManager.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 11");
  await projectManager.page.locator(".delivery-plan-toolbar").getByRole("button", { name: "Archiver" }).click();
  await expect(projectManager.page.locator(".delivery-plan-toolbar")).toContainText("ARCHIVED");
  await expect(projectManager.page.locator(".delivery-plan-toolbar")).toContainText("Version Delivery 12");
  await expect(storyCard(projectManager.page, storyTitle)).toBeVisible();

  await closeContext(projectManager.context);
});
