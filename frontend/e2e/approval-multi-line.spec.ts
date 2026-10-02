import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

type Role = "ADMIN" | "PROJECT_MANAGER" | "COORDINATOR" | "MANAGER";

const DISPLAY_NAMES: Record<Role, string> = {
  ADMIN: "Administrateur E2E",
  PROJECT_MANAGER: "Chargé E2E",
  COORDINATOR: "Coordonnateur E2E",
  MANAGER: "Gestionnaire E2E",
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

async function openWorkflow(page: Page, demandNumber: string) {
  await navigateMain(page, "Demandes");
  const card = page.locator(".demand-card").filter({ hasText: demandNumber }).first();
  await expect(card).toBeVisible();
  await card.click();
  const detail = page.locator(`.demand-detail-context[data-demand-number="${demandNumber}"]`);
  await expect(detail).toBeVisible();
  const section = detail.locator(".demand-detail-section").filter({ hasText: "Workflow et impact" }).first();
  const isOpen = await section.evaluate((node) => (node as HTMLDetailsElement).open);
  if (!isOpen) await section.locator("summary").click();
  await expect(section.locator(".workflow-detail-panel")).toBeVisible();
  return section;
}

test("276D multi-métier : votes partiels puis quorum complet dans React", async ({ browser }) => {
  test.setTimeout(120_000);
  const nextMonday = addDays(startOfWeek(new Date()), 7);
  const d1 = localIso(nextMonday);
  const d2 = localIso(addDays(nextMonday, 1));

  const admin = await openAs(browser, "ADMIN");
  await navigateMain(admin.page, "Configuration");
  await expect(admin.page.getByRole("heading", { name: "Périmètres et approbateurs" })).toBeVisible();
  await expect(admin.page.getByTestId("approval-scope-AUTOMATION")).toBeVisible();
  await expect(admin.page.getByTestId("approval-scope-ELECTRICAL")).toBeVisible();
  await closeContext(admin.context);

  const projectManager = await openAs(browser, "PROJECT_MANAGER");
  const created = await projectManager.context.request.post("/api/v1/demands", {
    data: {
      project_number: "P-251",
      priority: "Normale",
      description: "276D multi-métier E2E",
      lines: [
        {
          position: 0,
          kind: "WORKFORCE",
          required_resource_class: "AUTOMATION",
          required_competency_ids: [],
          desired_start: d1,
          desired_end: d1,
          desired_active_days: 1,
          estimated_hours: 8,
          task_code: "210",
          confirmation: "Confirmée",
          description: "Automatisation",
        },
        {
          position: 1,
          kind: "WORKFORCE",
          required_resource_class: "ELECTRICAL",
          required_competency_ids: [],
          desired_start: d2,
          desired_end: d2,
          desired_active_days: 1,
          estimated_hours: 8,
          task_code: "110",
          confirmation: "Confirmée",
          description: "Installation électrique",
        },
      ],
      submit: true,
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const createdBody = await created.json();
  const demandNumber = String(createdBody.demand_number);
  await closeContext(projectManager.context);

  const coordinator = await openAs(browser, "COORDINATOR");
  const coordinatorWorkflow = await openWorkflow(coordinator.page, demandNumber);
  const coordinatorProgress = coordinatorWorkflow.getByTestId("approval-progress");
  await expect(coordinatorProgress).toContainText("0 / 2 satisfaites");
  await expect(coordinatorProgress).toContainText("210 — AUTOMATISATION E2E");
  await expect(coordinatorProgress).toContainText("110 — INSTALLATION ÉLECTRIQUE E2E");
  await coordinator.page.getByTestId("demand-header-actions").getByTestId("approval-action").click();
  await expect(coordinator.page.locator(".demand-notice")).toContainText(
    "Approbation enregistrée — 1 lignes sur 2 satisfaites",
  );
  await expect(coordinatorWorkflow.locator(".workflow-state-card").first()).toContainText("Soumise");
  await closeContext(coordinator.context);

  const manager = await openAs(browser, "MANAGER");
  const managerWorkflow = await openWorkflow(manager.page, demandNumber);
  const managerProgress = managerWorkflow.getByTestId("approval-progress");
  await expect(managerProgress).toContainText("1 / 2 satisfaites");
  await expect(managerProgress).toContainText("Coordonnateur Démo");
  await manager.page.getByTestId("demand-header-actions").getByTestId("approval-action").click();
  await expect(managerWorkflow.locator(".workflow-state-card").first()).toContainText("En planification");
  await expect(manager.page.locator(".demand-notice")).toContainText("Demande approuvée — quorum complet");
  await expect(managerWorkflow.getByTestId("approval-progress")).toContainText("2 / 2 satisfaites");
  await closeContext(manager.context);
});
