import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openProjectManager(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "PROJECT_MANAGER" },
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
}

async function createDraft(page: Page) {
  await navigateMain(page, "Demandes");
  await page.getByRole("button", { name: /Nouvelle demande/ }).click();

  const editor = page.locator(".demand-editor-form");
  await chooseCombobox(editor, "Projet", "251", "P-251");
  await chooseCombobox(editor, "Tâche ERP", "autom", "210 — AUTOMATISATION E2E");
  await chooseCombobox(editor, "Classe de ressource", "prog", "PROGRAMMEUR");
  await editor.getByLabel("Heures estimées totales").fill("8");
  await editor.getByRole("button", { name: "Créer le brouillon" }).click();

  await expect(page.locator(".demand-notice")).toContainText("créée en brouillon");
  const demandNumber = (await page.locator(".demand-editor-heading h2").textContent())?.trim() || "";
  expect(demandNumber).toMatch(/^DMO-/);
  return demandNumber;
}

test("570 expose Soumettre dans le header et rafraîchit immédiatement après succès", async ({ browser }) => {
  const { context, page } = await openProjectManager(browser);
  try {
    await createDraft(page);

    const header = page.locator(".demand-editor-heading");
    const headerActions = header.getByTestId("demand-header-actions");
    const submit = headerActions.getByRole("button", { name: "Soumettre", exact: true });
    await expect(submit).toBeVisible();

    const workflowDetail = page.getByTestId("demand-workflow-section");
    await expect(workflowDetail.getByRole("button", { name: "Soumettre", exact: true })).toHaveCount(0);

    await submit.click();
    await expect(header.locator(".eyebrow")).toContainText("Soumise");
    await expect(headerActions.getByRole("button", { name: "Soumettre", exact: true })).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("570 permet Annuler la demande depuis le header avec la confirmation existante", async ({ browser }) => {
  const { context, page } = await openProjectManager(browser);
  try {
    await createDraft(page);

    const header = page.locator(".demand-editor-heading");
    const headerActions = header.getByTestId("demand-header-actions");
    const cancel = headerActions.getByRole("button", { name: "Annuler la demande", exact: true });
    await expect(cancel).toBeVisible();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      await dialog.accept();
    });
    await cancel.click();

    await expect(header.locator(".eyebrow")).toContainText("Annul");
    await expect(headerActions.getByRole("button", { name: "Annuler la demande", exact: true })).toHaveCount(0);
  } finally {
    await context.close();
  }
});
