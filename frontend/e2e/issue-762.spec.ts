import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

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
  return { context, page };
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

async function selectUnit(page: Page, typeText: string, unitCode: string) {
  const catalog = page.locator(".asset-catalog-card");
  await catalog.locator(".asset-type-row").filter({ hasText: typeText }).click();
  await catalog.locator(".asset-unit-row").filter({ hasText: unitCode }).click();
  return catalog;
}

test("762 profile unité véhicule pilote le contrat continu #744", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openAdmin(browser);
  const today = localIso(new Date());
  const tomorrow = localIso(addDays(new Date(), 1));

  try {
    await navigateMain(page, "Ressources");

    // Non-vehicle units never expose the Attitré workflow.
    await selectUnit(page, "Nacelle", "NAC-01");
    await expect(page.getByTestId("asset-continuous-assignment-fields")).toHaveCount(0);

    // Create a current assignment from the physical vehicle profile.
    await selectUnit(page, "Véhicule attitrable E2E", "VEH-762-01");
    let editor = page.getByTestId("asset-continuous-assignment-fields");
    await expect(editor).toBeVisible();
    const assigned = editor.getByRole("checkbox", { name: "Attitré" });
    await expect(assigned).not.toBeChecked();
    await assigned.check();
    await editor.getByRole("combobox").selectOption("R-ALICE");
    await editor.getByLabel("Date effective").fill(today);
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();

    await expect(assigned).toBeChecked();
    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText("État : Actif");
    await expect(editor.getByRole("combobox")).toHaveValue("R-ALICE");

    // Changing the Resource uses the canonical atomic transfer and surfaces the
    // future transition without pretending it is already active.
    await editor.getByRole("combobox").selectOption("R-BOB");
    await editor.getByLabel("Date effective").fill(tomorrow);
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();

    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText("État : Actif");
    await expect(editor.getByTestId("asset-continuous-scheduled-transition")).toContainText(tomorrow);
    await expect(editor.getByTestId("asset-continuous-scheduled-transition")).toContainText("Bob");

    // Reload must project the same backend state; no Asset-local assignment cache.
    await page.reload();
    await navigateMain(page, "Ressources");
    await selectUnit(page, "Véhicule attitrable E2E", "VEH-762-01");
    editor = page.getByTestId("asset-continuous-assignment-fields");
    await expect(editor.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText("État : Actif");
    await expect(editor.getByTestId("asset-continuous-scheduled-transition")).toContainText("Bob");

    // A future-only assignment is displayed as SCHEDULED, never ACTIVE, and
    // unchecking it cancels the future assignment through the #744 command.
    await selectUnit(page, "Véhicule attitrable E2E", "VEH-762-02");
    editor = page.getByTestId("asset-continuous-assignment-fields");
    const scheduledCheck = editor.getByRole("checkbox", { name: "Attitré" });
    await scheduledCheck.check();
    await editor.getByRole("combobox").selectOption("R-ALICE");
    await editor.getByLabel("Date effective").fill(tomorrow);
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText(
      `État : Planifié pour ${tomorrow}`,
    );
    await scheduledCheck.uncheck();
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(editor.getByRole("checkbox", { name: "Attitré" })).not.toBeChecked();

    // Unchecking an ACTIVE assignment performs a dated revocation. Since the
    // vehicle remains assigned through today, the authoritative reload remains checked.
    await selectUnit(page, "Véhicule attitrable E2E", "VEH-762-03");
    editor = page.getByTestId("asset-continuous-assignment-fields");
    const revokeCheck = editor.getByRole("checkbox", { name: "Attitré" });
    await revokeCheck.check();
    await editor.getByRole("combobox").selectOption("R-ALICE");
    await editor.getByLabel("Date effective").fill(today);
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText("État : Actif");

    await editor.getByLabel("Date effective").fill(tomorrow);
    await revokeCheck.uncheck();
    await page.getByRole("button", { name: "Enregistrer l’unité" }).click();
    await expect(editor.getByRole("checkbox", { name: "Attitré" })).toBeChecked();
    await expect(editor.getByTestId("asset-continuous-assignment-state")).toContainText(
      `Fin prévue : ${today}`,
    );

    // #795: Planning consumes the backend-projected per-window unit visibility.
    await navigateMain(page, "Planning");
    const currentWeekHeading = page.getByRole("heading", { level: 1 });
    await expect(currentWeekHeading).toContainText("Semaine du");
    const currentWeekText = await currentWeekHeading.innerText();

    const filtersToggle = page.getByRole("button", { name: /^Filtres/ });
    await filtersToggle.click();
    const search = page.getByLabel("Recherche");
    await search.fill("Alice");

    const occupancyPanel = page.getByTestId("asset-unit-occupancy-panel");
    const occupancyContent = page.getByTestId("asset-unit-occupancy-content");
    await expect(occupancyContent).toContainText("NAC-01");
    await expect(occupancyContent).not.toContainText("VEH-762-03");

    const occupancyToggle = page.getByTestId("asset-unit-occupancy-toggle");
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "true");
    await occupancyToggle.click();
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "false");
    await expect(occupancyContent).toHaveCount(0);
    await expect(search).toHaveValue("Alice");
    await expect(currentWeekHeading).toHaveText(currentWeekText);

    await occupancyToggle.click();
    await expect(occupancyToggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("asset-unit-occupancy-content")).toContainText("NAC-01");
    await expect(search).toHaveValue("Alice");
    await expect(currentWeekHeading).toHaveText(currentWeekText);

    // The dated revocation above ends VEH-762-03 after today. In the next
    // Planning week, no continuous interval overlaps the viewed window and the
    // same canonical unit is projected back into Occupation par unité.
    await page.getByRole("button", { name: /Suivante/ }).click();
    await expect(currentWeekHeading).not.toHaveText(currentWeekText);
    await expect(page.getByLabel("Recherche")).toHaveValue("Alice");
    await expect(page.getByTestId("asset-unit-occupancy-content")).toContainText("VEH-762-03");
  } finally {
    await context.close();
  }
});
