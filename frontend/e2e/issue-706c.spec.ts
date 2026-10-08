import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function roleContext(browser: Browser, role: "TECHNICIAN" | "ADMIN" | "DELIVERY_TECHNICIAN") {
  return browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
}

test("706C — browser/API reject module details and indirect JSON for a technician", async ({ browser }) => {
  const context = await roleContext(browser, "TECHNICIAN");
  const page = await context.newPage();
  const forbiddenNetworkLoads: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/^\/api\/v1\/(?:demands|projects|work-packages|medium-term|delivery)(?:\/|$)/.test(path)) {
      forbiddenNetworkLoads.push(path);
    }
  });

  try {
    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("TECHNICIAN");
    await expect(page.getByRole("heading", { name: "Aujourd’hui", level: 1 })).toBeVisible();
    await expect(page.locator(".main-nav").getByRole("button", { name: "Mon horaire" })).toBeVisible();

    for (const view of ["demands", "projects", "medium-term", "delivery", "work-packages"]) {
      await page.goto("/?view=" + view);
      await expect(page.getByText("Accès refusé", { exact: true })).toBeVisible();
      await expect(page.locator(".projects-page, .demands-workspace, .medium-term-page, .delivery-page")).toHaveCount(0);
      expect(await page.locator("body").innerText()).not.toContain("Client E2E");
    }
    // Unauthorized tab entry must not trigger the underlying module requests.
    expect(forbiddenNetworkLoads).toEqual([]);

    // BrowserContext.request uses the same real FastAPI auth middleware as the page.
    for (const path of [
      "/api/v1/projects",
      "/api/v1/projects/P-251-ID/managers",
      "/api/v1/task-catalog",
      "/api/v1/demands",
      "/api/v1/demands/UNKNOWN/detail",
      "/api/v1/work-packages",
      "/api/v1/work-packages/UNKNOWN",
      "/api/v1/medium-term/budget",
      "/api/v1/delivery/plans/UNKNOWN",
      "/api/v1/verification/work-packages/UNKNOWN/documents/traceability.csv",
      "/api/v1/assets/requirements",
    ]) {
      for (const scope of [undefined, "mine", "global"]) {
        const query = new URLSearchParams({ source: "planning" });
        if (scope) query.set("scope", scope);
        const response = await context.request.get(path + "?" + query);
        expect(response.status(), path + " scope=" + scope).toBe(403);
        const responseBody = await response.text();
        const parsed = JSON.parse(responseBody) as { error: { code: string } };
        expect(parsed.error.code).toBe("permission_denied");
        for (const privateValue of ["Projet Playwright V2", "Client E2E", "P-251-ID"]) {
          expect(responseBody).not.toContain(privateValue);
        }
      }
    }
    const allowed = await context.request.get("/api/v1/me/schedule?start=2026-10-05&end=2026-10-11");
    expect(allowed.status()).toBe(200);
  } finally {
    await context.close();
  }
});

test("706C — mixed Delivery/Technician reads only explicitly granted catalogues", async ({ browser }) => {
  const context = await roleContext(browser, "DELIVERY_TECHNICIAN");
  const page = await context.newPage();
  try {
    const me = await context.request.get("/api/v1/auth/me");
    expect(me.status()).toBe(200);
    const identity = await me.json() as { permissions: string[] };
    expect(identity.permissions).toContain("read_projects");
    expect(identity.permissions).toContain("read_work_packages");
    expect(identity.permissions).not.toContain("read_demands");

    for (const path of ["/api/v1/projects?scope=mine", "/api/v1/work-packages?scope=mine"]) {
      const response = await context.request.get(path);
      expect(response.status(), path).toBe(200);
    }
    const demand = await context.request.get("/api/v1/demands?scope=mine");
    expect(demand.status()).toBe(403);
    expect((await demand.json()).error.code).toBe("permission_denied");

    await page.goto("/?view=projects");
    await expect(page.locator(".sidebar-footer")).toContainText("DELIVERY_CONTRIBUTOR");
    await expect(page.locator(".main-nav").getByRole("button", { name: "Projets" })).toBeVisible();
    await expect(page.locator(".main-nav").getByRole("button", { name: "Delivery" })).toBeVisible();
    await expect(page.locator(".main-nav").getByRole("button", { name: "Demandes" })).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("706C — switching from administrator to technician removes privileged React state", async ({ browser }) => {
  const context = await browser.newContext({ baseURL: BASE_URL, locale: "fr-CA" });
  const page = await context.newPage();
  try {
    await page.goto("/?view=projects");
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur bootstrap E2E");
    await expect(page.locator(".projects-page")).toBeVisible();
    await expect(page.getByText("Projet Playwright V2").first()).toBeVisible();

    const selector = page.getByLabel("Identité de test");
    await expect(selector).toBeVisible();
    const techOption = selector.locator("option").filter({ hasText: "Technicien Démo A" });
    const technicianId = await techOption.getAttribute("value");
    expect(technicianId).toBeTruthy();
    await selector.selectOption(technicianId!);

    await expect(page.locator(".sidebar-footer")).toContainText("Technicien Démo A");
    await expect(page.getByText("Accès refusé", { exact: true })).toBeVisible();
    await expect(page.locator(".projects-page, .demands-workspace, .demand-detail-modal")).toHaveCount(0);
    await expect(page.getByText("Projet Playwright V2")).toHaveCount(0);
    await expect(page.locator(".main-nav").getByRole("button", { name: "Demandes" })).toHaveCount(0);
    await expect(page.locator(".main-nav").getByRole("button", { name: "Projets" })).toHaveCount(0);

    await page.getByRole("button", { name: "Mon horaire" }).click();
    await expect(page.getByRole("heading", { name: "Aujourd’hui", level: 1 })).toBeVisible();
  } finally {
    await context.close();
  }
});
