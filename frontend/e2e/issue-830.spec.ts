import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

test("#830 chantier contact create/edit/reopen/clear uses request-owned fields", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  try {
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Demandes/i }).click();
    await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();
    const candidatesResponse = await page.request.get("/api/v1/demand-project-candidates");
    expect(candidatesResponse.ok(), await candidatesResponse.text()).toBeTruthy();
    const candidates = await candidatesResponse.json() as Array<{ number: string }>;
    expect(candidates.length).toBeGreaterThan(0);

    await page.getByRole("button", { name: "+ Nouvelle demande" }).click();
    const project = page.getByRole("combobox", { name: "Projet", exact: true });
    await project.fill(candidates[0].number);
    await page.getByRole("listbox", { name: "Projet options" })
      .getByRole("option").filter({ hasText: candidates[0].number }).first().click();
    const name = page.getByRole("textbox", { name: "Contact chantier — Nom" });
    const phone = page.getByRole("textbox", { name: "Contact chantier — Téléphone" });
    await name.fill("François Émond");
    await phone.fill("450-555-0180");
    const createdResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/demands")
        && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Créer le brouillon" }).click();
    const createdResponse = await createdResponsePromise;
    expect(createdResponse.ok(), await createdResponse.text()).toBeTruthy();
    const { demand_number: demandNumber } = await createdResponse.json() as { demand_number: string };
    await expect(page.locator(".demand-editor-heading")).toContainText(demandNumber);
    await expect(name).toHaveValue("François Émond");
    await expect(phone).toHaveValue("450-555-0180");

    // Reopen from the list after a new-draft transition to prove SQL read fidelity.
    await page.getByRole("button", { name: "+ Nouvelle demande" }).click();
    await page.locator(".demand-card").filter({ hasText: demandNumber })
      .locator(".demand-card-select").click();
    await expect(name).toHaveValue("François Émond");
    await expect(phone).toHaveValue("450-555-0180");

    await name.fill("Marie Lavoie");
    const updatedResponsePromise = page.waitForResponse(
      (response) => response.url().includes("/api/v1/demands/")
        && response.request().method() === "PATCH",
    );
    await page.getByRole("button", { name: "Enregistrer les modifications" }).click();
    const updatedResponse = await updatedResponsePromise;
    expect(updatedResponse.ok(), await updatedResponse.text()).toBeTruthy();
    await expect(page.locator(".demand-notice")).toContainText("Modification enregistrée");
    await expect(name).toHaveValue("Marie Lavoie");
    await expect(phone).toHaveValue("450-555-0180");

    await name.fill("");
    await phone.fill("");
    const clearResponsePromise = page.waitForResponse(
      (response) => response.url().includes("/api/v1/demands/")
        && response.request().method() === "PATCH",
    );
    await page.getByRole("button", { name: "Enregistrer les modifications" }).click();
    const clearedResponse = await clearResponsePromise;
    expect(clearedResponse.ok(), await clearedResponse.text()).toBeTruthy();
    await expect(name).toHaveValue("");
    await expect(phone).toHaveValue("");

    const detail = await page.request.get(`/api/v1/demands/${encodeURIComponent(demandNumber)}`);
    expect(detail.ok(), await detail.text()).toBeTruthy();
    const body = await detail.json() as any;
    const demand = body.demand ?? body;
    expect(demand.site_contact_name).toBeNull();
    expect(demand.site_contact_phone).toBeNull();
  } finally {
    await context.close();
  }
});
