import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

test("#840 normal new demand starts without implicit project or confirmation", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();

  async function openDemands() {
    await page.goto("/");
    await page
      .locator(".main-nav")
      .getByRole("button", { name: /Demandes/i })
      .click();
    await expect(
      page.getByRole("heading", { name: "Demandes", level: 1 }),
    ).toBeVisible();
  }

  async function startNormalDraft() {
    await page.getByRole("button", { name: "+ Nouvelle demande" }).click();
    await expect(page.getByRole("combobox", { name: "Projet", exact: true })).toBeVisible();
  }

  function confirmationSelect() {
    return page
      .locator("label")
      .filter({ has: page.getByText("Confirmation", { exact: true }) })
      .locator("select");
  }

  try {
    await openDemands();
    await startNormalDraft();

    const project = page.getByRole("combobox", { name: "Projet", exact: true });
    await expect(project).toHaveAttribute("data-combobox-value", "");
    await expect(project).toHaveValue("");
    await expect(confirmationSelect()).toHaveValue("");

    const managerValue = page
      .locator(".project-master-card > div")
      .filter({ hasText: "Responsable projet" })
      .locator("strong");
    await expect(managerValue).toHaveText("");

    // Existing draft validation remains project-first; the required
    // combobox is blocked by native form validation before React save runs.
    await page.getByRole("button", { name: "Créer le brouillon" }).click();
    await expect(project).toBeInvalid();
    await expect(page.locator(".demand-notice")).toHaveCount(0);

    const candidatesResponse = await page.request.get(
      "/api/v1/demand-project-candidates",
    );
    expect(candidatesResponse.ok(), await candidatesResponse.text()).toBeTruthy();
    const candidates = await candidatesResponse.json() as Array<{
      id: string;
      number: string;
      name: string;
      project_manager?: string | null;
    }>;
    expect(candidates.length).toBeGreaterThan(0);
    const candidate = candidates.find((row) => Boolean(row.project_manager)) ?? candidates[0];

    await project.fill(candidate.number);
    await page
      .getByRole("listbox", { name: "Projet options" })
      .getByRole("option")
      .filter({ hasText: candidate.number })
      .first()
      .click();
    await expect(project).not.toHaveAttribute("data-combobox-value", "");
    await expect(managerValue).toHaveText(candidate.project_manager || "Non défini");

    await confirmationSelect().selectOption("Confirmée");
    await page.getByRole("button", { name: "Créer le brouillon" }).click();
    await expect(page.locator(".demand-notice")).toContainText("créée en brouillon");

    // Re-entering normal creation must not reuse the just-created values.
    await startNormalDraft();
    await expect(project).toHaveAttribute("data-combobox-value", "");
    await expect(project).toHaveValue("");
    await expect(confirmationSelect()).toHaveValue("");
    await expect(managerValue).toHaveText("");
  } finally {
    await context.close();
  }
});
