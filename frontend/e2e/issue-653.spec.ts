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

async function openResourcesForAlice(page: Page) {
  await navigateMain(page, "Ressources");
  const alice = page.locator(".resource-list").getByRole("button", { name: /Alice/i }).first();
  await expect(alice).toBeVisible();
  await alice.click();
  await expect(page.locator(".resource-profile-card")).toContainText("Alice");
}

async function patchAliceSchedule(page: Page, startTime: string, endTime: string) {
  return page.request.patch(
    "/api/v1/availability-rules/E2E-STD-ALICE",
    {
      headers: { "X-E2E-Role": "ADMIN" },
      data: {
        start_time: startTime,
        end_time: endTime,
      },
    },
  );
}

test("653 — Ouvrir amène au détail projet et conserve le focus", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  try {
    await navigateMain(page, "Projets");

    const open = page.getByRole("button", { name: "Ouvrir", exact: true }).first();
    await expect(open).toBeVisible();
    await open.click();

    const projectDetail = page.locator(".project-contact-admin");
    await expect(projectDetail).toBeVisible();
    await expect.poll(async () => projectDetail.evaluate((node) => document.activeElement === node)).toBe(true);
    await expect.poll(async () => projectDetail.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return rect.top >= 0 && rect.top < window.innerHeight;
    })).toBe(true);
  } finally {
    await closeContext(context);
  }
});

test("653 — le coordonnateur de ressource est recherchable sans casse", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  try {
    await openResourcesForAlice(page);

    const coordinator = page.getByRole("combobox", { name: "Coordonnateur", exact: true });
    await expect(coordinator).toBeVisible();
    await coordinator.click();
    await coordinator.fill("CHARGÉ");

    const coordinatorOptions = page.getByRole("listbox", { name: "Coordonnateur options", exact: true });
    const projectManager = coordinatorOptions.getByRole("option", { name: /Chargé de projet Démo/i });
    await expect(projectManager).toBeVisible();

    await coordinator.fill("projet");
    await expect(projectManager).toBeVisible();
    await coordinator.press("Escape");
    await expect(coordinatorOptions).toBeHidden();
  } finally {
    await closeContext(context);
  }
});

test("653 — nouvelle règle 07:00–15:00 et règle historique 15:30 préservée", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  try {
    const historicalSchedule = await patchAliceSchedule(page, "07:00:00", "15:30:00");
    expect(historicalSchedule.status(), await historicalSchedule.text()).toBe(200);

    await openResourcesForAlice(page);

    const availabilityCard = page.locator(".admin-card").filter({ hasText: "Horaire & absences" });
    const addRule = availabilityCard.getByRole("button", { name: "+ Ajouter", exact: true });
    await expect(addRule).toBeVisible();
    await addRule.click();

    const newRuleEditor = availabilityCard.locator(".admin-editor");
    await expect(newRuleEditor.getByLabel("Début", { exact: true })).toHaveValue("07:00");
    await expect(newRuleEditor.getByLabel("Fin", { exact: true })).toHaveValue("15:00");
    await newRuleEditor.getByRole("button", { name: "Fermer", exact: true }).click();

    const historicalRule = availabilityCard.locator(".availability-rule").filter({ hasText: "07:00 → 15:30" });
    await expect(historicalRule).toBeVisible();
    const editHistorical = historicalRule.getByRole("button", { name: "Modifier", exact: true });
    await expect(editHistorical).toBeVisible();
    await editHistorical.click();

    const historicalEditor = availabilityCard.locator(".admin-editor");
    await expect(historicalEditor.getByLabel("Début", { exact: true })).toHaveValue("07:00");
    await expect(historicalEditor.getByLabel("Fin", { exact: true })).toHaveValue("15:30");
  } finally {
    await patchAliceSchedule(page, "08:00:00", "16:00:00");
    await closeContext(context);
  }
});
