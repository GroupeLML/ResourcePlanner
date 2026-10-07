import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openAdmin(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
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

test("653 — détail projet, contacts recherchables et défaut horaire 07:00–15:00", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  try {
    const historicalSchedule = await page.request.patch(
      "/api/v1/availability-rules/E2E-STD-ALICE",
      {
        headers: { "X-E2E-Role": "ADMIN" },
        data: {
          start_time: "07:00:00",
          end_time: "15:30:00",
        },
      },
    );
    expect(historicalSchedule.status(), await historicalSchedule.text()).toBe(200);

    await navigateMain(page, "Projets");
    await page.getByRole("button", { name: "Ouvrir", exact: true }).first().click();

    const projectDetail = page.locator(".project-contact-admin");
    await expect(projectDetail).toBeVisible();
    await expect.poll(async () => projectDetail.evaluate((node) => document.activeElement === node)).toBe(true);
    await expect.poll(async () => projectDetail.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return rect.top >= 0 && rect.top < window.innerHeight;
    })).toBe(true);

    await navigateMain(page, "Ressources");

    const coordinator = page.getByRole("combobox", { name: "Coordonnateur", exact: true });
    await expect(coordinator).toBeVisible();
    await coordinator.click();
    await coordinator.fill("CHARGÉ");
    const coordinatorOptions = page.getByRole("listbox", { name: "Coordonnateur options", exact: true });
    await expect(coordinatorOptions.getByRole("option", { name: /Chargé de projet Démo/i })).toBeVisible();
    await coordinator.fill("projet");
    await expect(coordinatorOptions.getByRole("option", { name: /Chargé de projet Démo/i })).toBeVisible();
    await coordinator.press("Escape");

    const availabilityCard = page.locator(".admin-card").filter({ hasText: "Horaire & absences" });
    await availabilityCard.getByRole("button", { name: "+ Ajouter", exact: true }).click();

    const newRuleEditor = availabilityCard.locator(".admin-editor");
    await expect(newRuleEditor.getByLabel("Début", { exact: true })).toHaveValue("07:00");
    await expect(newRuleEditor.getByLabel("Fin", { exact: true })).toHaveValue("15:00");
    await newRuleEditor.getByRole("button", { name: "Fermer", exact: true }).click();

    const historicalRule = availabilityCard.locator(".availability-rule").filter({ hasText: "07:00 → 15:30" });
    await expect(historicalRule).toBeVisible();
    await historicalRule.getByRole("button", { name: "Modifier", exact: true }).click();

    const historicalEditor = availabilityCard.locator(".admin-editor");
    await expect(historicalEditor.getByLabel("Début", { exact: true })).toHaveValue("07:00");
    await expect(historicalEditor.getByLabel("Fin", { exact: true })).toHaveValue("15:30");
  } finally {
    await closeContext(context);
  }
});
