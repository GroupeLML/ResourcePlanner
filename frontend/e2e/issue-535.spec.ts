import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openAs(browser: Browser, role: "PROJECT_MANAGER") {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  await page.goto("/");
  return { context, page };
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

function combobox(scope: Locator, label: string) {
  return scope.getByRole("combobox", { name: label, exact: true }).first();
}

async function chooseCombobox(scope: Locator, label: string, query: string, optionName: string) {
  const input = combobox(scope, label);
  await input.click();
  await input.fill(query);
  const listbox = scope.getByRole("listbox", { name: `${label} options`, exact: true }).first();
  const option = listbox.getByRole("option", { name: optionName, exact: false }).first();
  await expect(option).toBeVisible();
  await option.click();
  return input;
}

test("besoin simple conserve la classe canonique et expose Soumettre immédiatement", async ({ browser }) => {
  const { context, page } = await openAs(browser, "PROJECT_MANAGER");
  try {
    await navigateMain(page, "Demandes");
    await page.getByRole("button", { name: /Nouvelle demande/ }).click();

    const editor = page.locator(".demand-editor-form");
    await chooseCombobox(editor, "Projet", "251", "P-251");
    await chooseCombobox(editor, "Tâche ERP", "autom", "210 — AUTOMATISATION E2E");

    const resourceClass = combobox(editor, "Classe de ressource");
    await expect(resourceClass).toHaveAttribute("data-combobox-value", "PROGRAMMEUR");
    await expect(resourceClass).toHaveValue(/PROGRAMMEUR/);

    await editor.getByLabel("Heures estimées totales").fill("8");

    const createRequest = page.waitForRequest((request) => (
      request.method() === "POST" && request.url().endsWith("/api/v1/demands")
    ));
    await editor.getByRole("button", { name: "Créer le brouillon" }).click();
    const request = await createRequest;
    const payload = request.postDataJSON() as { required_resource_class?: string | null };
    expect(payload.required_resource_class).toBe("PROGRAMMEUR");

    await expect(page.locator(".demand-notice")).toContainText("créée en brouillon");
    const heading = page.locator(".demand-editor-heading h2");
    const demandNumber = (await heading.textContent())?.trim() || "";
    expect(demandNumber).toMatch(/^DMO-/);

    const detailResponse = await page.request.get(`/api/v1/demands/${encodeURIComponent(demandNumber)}`);
    expect(detailResponse.status(), await detailResponse.text()).toBe(200);
    const demand = await detailResponse.json() as {
      lines: Array<{ required_resource_class: string | null }>;
    };
    expect(demand.lines[0]?.required_resource_class).toBe("PROGRAMMEUR");

    await expect(combobox(editor, "Classe de ressource")).toHaveAttribute(
      "data-combobox-value",
      "PROGRAMMEUR",
    );

    const card = page.locator(".demand-card").filter({ hasText: demandNumber }).first();
    await expect(card.locator(".demand-card-quick-actions").getByRole("button", { name: "Soumettre" })).toBeVisible();
    await card.locator(".demand-card-quick-actions").getByRole("button", { name: "Soumettre" }).click();

    const primary = page.getByTestId("primary-demand-actions");
    await expect(primary.getByRole("button", { name: "Soumettre" })).toBeVisible();
    await primary.getByRole("button", { name: "Soumettre" }).click();
    await expect(page.locator(".demand-editor-heading .eyebrow")).toContainText("Soumise");
  } finally {
    await context.close();
  }
});

test("classe simple explicite devient la classe de la ligne lors du passage multi-besoins", async ({ browser }) => {
  const { context, page } = await openAs(browser, "PROJECT_MANAGER");
  try {
    await navigateMain(page, "Demandes");
    await page.getByRole("button", { name: /Nouvelle demande/ }).click();
    const editor = page.locator(".demand-editor-form");
    await chooseCombobox(editor, "Projet", "251", "P-251");

    const resourceClass = await chooseCombobox(editor, "Classe de ressource", "prog", "PROGRAMMEUR");
    await expect(resourceClass).toHaveAttribute("data-combobox-value", "PROGRAMMEUR");

    await editor.getByRole("button", { name: "Passer aux lignes multiples" }).click();
    const lineClass = combobox(editor.locator(".request-lines-editor"), "Classe de ressource");
    await expect(lineClass).toHaveAttribute("data-combobox-value", "PROGRAMMEUR");
  } finally {
    await context.close();
  }
});
