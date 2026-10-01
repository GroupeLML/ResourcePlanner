import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

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

async function openCoordinator(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

async function resourceNames(group: ReturnType<Page["locator"]>) {
  return group.locator(".resource-identity > strong").allTextContents();
}

test("Planning mémorise semaine, tri et classes repliées tout en utilisant la capacité projetée", async ({ browser }) => {
  test.setTimeout(120_000);
  const currentWeekStart = startOfWeek(new Date());
  const targetWeekStart = addDays(currentWeekStart, 28);
  const targetDay = localIso(targetWeekStart);
  const rememberedWeekStart = addDays(targetWeekStart, 7);
  const rememberedDay = localIso(rememberedWeekStart);
  const { context, page } = await openCoordinator(browser);

  try {
    await navigateMain(page, "Planning opérationnel");
    for (let index = 0; index < 4; index += 1) {
      await page.getByRole("button", { name: /Suivante/ }).click();
    }

    const programmeurGroup = page.locator(".resource-group").filter({ hasText: "PROGRAMMEUR" }).first();
    const groupHeading = programmeurGroup.locator(".resource-group-heading");
    await expect(groupHeading).toHaveAttribute("aria-expanded", "true");
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Alice", "Bob"]);

    await page.getByRole("button", { name: "+ Quick Shift", exact: true }).click();
    const quickShift = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await quickShift.getByLabel("Projet").selectOption("P-251");
    await quickShift.getByLabel("Technicien").selectOption("Alice");
    await quickShift.getByLabel("Date").fill(targetDay);
    await quickShift.getByLabel("Heures").fill("8");
    await quickShift.getByRole("button", { name: "Créer le Quick Shift" }).click();
    await expect(quickShift).toBeHidden();

    const sort = page.getByLabel("Ordre des ressources");
    await expect(sort).toHaveValue("manual");
    await sort.selectOption("availability");
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Bob", "Alice"]);
    await sort.selectOption("alphabetical");
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Alice", "Bob"]);
    await sort.selectOption("manual");
    await expect.poll(() => resourceNames(programmeurGroup)).toEqual(["Alice", "Bob"]);

    await groupHeading.click();
    await expect(groupHeading).toHaveAttribute("aria-expanded", "false");
    await expect(programmeurGroup.locator(".resource-row")).toHaveCount(0);
    await expect(page.locator(".planning-board-toolbar")).toContainText("2 ressource(s)");

    await sort.selectOption("availability");
    await page.getByRole("button", { name: /Suivante/ }).click();
    const rememberedHeading = await page.getByRole("heading", { level: 1 }).innerText();
    await navigateMain(page, "Projets");
    await navigateMain(page, "Planning opérationnel");

    const restoredGroup = page.locator(".resource-group").filter({ hasText: "PROGRAMMEUR" }).first();
    const restoredHeading = restoredGroup.locator(".resource-group-heading");
    await expect(page.getByLabel("Ordre des ressources")).toHaveValue("availability");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(rememberedHeading);
    await expect(restoredHeading).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator(`.planning-grid`)).toBeVisible();

    await restoredHeading.click();
    await expect(restoredHeading).toHaveAttribute("aria-expanded", "true");
    await expect(restoredGroup.locator(`.planning-drop-day[data-day="${rememberedDay}"]`).first()).toBeVisible();

    await page.getByRole("button", { name: "Aujourd’hui", exact: true }).click();
    const currentDay = localIso(currentWeekStart);
    await expect(restoredGroup.locator(`.planning-drop-day[data-day="${currentDay}"]`).first()).toBeVisible();
    const currentHeading = await page.getByRole("heading", { level: 1 }).innerText();
    await navigateMain(page, "Projets");
    await navigateMain(page, "Planning opérationnel");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(currentHeading);
  } finally {
    await closeContext(context);
  }
});

test("Planning ignore des préférences navigateur corrompues", async ({ browser }) => {
  const currentWeekStart = startOfWeek(new Date());
  const { context, page } = await openCoordinator(browser);

  try {
    const response = await page.request.get("/api/v1/auth/me");
    expect(response.ok()).toBeTruthy();
    const principal = await response.json() as { local_user_id: string | null };
    const owner = encodeURIComponent(principal.local_user_id || "application");
    await page.evaluate(({ ownerKey }) => {
      localStorage.setItem(`resourceplanner:planning:${ownerKey}:resource-sort`, "banana");
      localStorage.setItem(`resourceplanner:planning:${ownerKey}:week-start`, "not-a-date");
      localStorage.setItem(`resourceplanner:planning:${ownerKey}:collapsed-classes`, "{bad-json");
    }, { ownerKey: owner });

    await navigateMain(page, "Planning opérationnel");
    await expect(page.getByLabel("Ordre des ressources")).toHaveValue("manual");
    await expect(page.locator(`.planning-drop-day[data-day="${localIso(currentWeekStart)}"]`).first()).toBeVisible();
    await expect(page.locator(".resource-group-heading").first()).toHaveAttribute("aria-expanded", "true");
  } finally {
    await closeContext(context);
  }
});
