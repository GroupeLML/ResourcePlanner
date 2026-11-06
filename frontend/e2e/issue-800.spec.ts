import { Browser, Page, expect, test } from "@playwright/test";

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

async function openManager(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "MANAGER" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Gestionnaire E2E");
  return { context, page };
}

async function openPlanning(page: Page) {
  const nav = page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i });
  await expect(nav).toBeVisible();
  await nav.click();
  await expect(page.locator(".planning-board-panel")).toBeVisible();
  await expect(page.locator(".planning-board-scroll")).not.toHaveClass(/is-loading/);
}

async function assertReadOnlyPlanning(page: Page) {
  await expect(page.getByRole("button", { name: "+ Quart manuel", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Quick Shift", exact: true })).toHaveCount(0);
  await expect(page.locator(".shift-asset-action")).toHaveCount(0);
  await expect(page.locator(".planning-action-card.is-draggable")).toHaveCount(0);
  await expect(page.locator(".shift-card[draggable='true']")).toHaveCount(0);
  await expect(page.locator(".shift-card-main:enabled")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Trouver une ressource", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Modifier le segment", exact: true })).toHaveCount(0);
}

test("800 Gestionnaire consulte Planning en lecture seule et le backend refuse un Quick Shift direct", async ({ browser }) => {
  const { context, page } = await openManager(browser);
  const weekStart = startOfWeek(new Date());
  const start = localIso(weekStart);
  const end = localIso(addDays(weekStart, 6));

  try {
    const principal = await page.request.get("/api/v1/auth/me");
    expect(principal.status(), await principal.text()).toBe(200);
    const principalBody = await principal.json() as { permissions: string[] };
    expect(principalBody.permissions).toContain("read");
    expect(principalBody.permissions).toContain("read_demands");
    expect(principalBody.permissions).toContain("read_projects");
    expect(principalBody.permissions).toContain("read_work_packages");
    expect(principalBody.permissions).toContain("approve_demands");
    expect(principalBody.permissions).not.toContain("manage_planning");

    const policy = await page.request.get("/api/v1/me/planning-policy");
    expect(policy.status(), await policy.text()).toBe(200);
    expect(await policy.json()).toEqual({
      available_scopes: ["mine", "global"],
      default_scope: "global",
    });

    await openPlanning(page);

    await expect(page.locator(".resource-row").first()).toBeVisible();
    await expect(page.locator(".shift-card").first()).toBeVisible();
    await expect(page.locator(".planning-work-queue")).toBeVisible();
    await expect(page.getByRole("button", { name: "Vue globale" })).toHaveClass(/active/);
    await assertReadOnlyPlanning(page);

    const before = await page.request.get(
      `/api/v1/planning/snapshot?start=${start}&end=${end}&scope=global`,
    );
    expect(before.status(), await before.text()).toBe(200);
    const beforeBody = await before.json();

    const denied = await page.request.post("/api/v1/quick-shifts", {
      headers: { "Idempotency-Key": "issue-800-browser-denied" },
      data: {
        project_number: "P-251",
        project_name: "Projet Playwright V2",
        technician: "Alice",
        day: start,
        hours: 1,
        note: "Issue 800 must remain read-only",
      },
    });
    expect(denied.status(), await denied.text()).toBe(403);
    expect((await denied.json()).error.code).toBe("permission_denied");

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Gestionnaire E2E");
    await openPlanning(page);
    await assertReadOnlyPlanning(page);

    const after = await page.request.get(
      `/api/v1/planning/snapshot?start=${start}&end=${end}&scope=global`,
    );
    expect(after.status(), await after.text()).toBe(200);
    expect(await after.json()).toEqual(beforeBody);

    const [detached] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await detached.waitForLoadState("domcontentloaded");
    await expect(detached).toHaveTitle("Ressources et quarts — ResourcePlanner");
    await expect(detached.locator(".planning-board-panel")).toBeVisible();
    await expect(detached.locator(".resource-row").first()).toBeVisible();
    await assertReadOnlyPlanning(detached);
    await detached.close();
  } finally {
    await context.close();
  }
});
