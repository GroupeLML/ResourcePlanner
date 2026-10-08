import { Browser, BrowserContext, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openRole(browser: Browser, role: "TECHNICIAN" | "ADMIN") {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  return { context, page };
}

test("706B — technicien : navigation restreinte et aucun montage des modules interdits", async ({ browser }) => {
  const { context, page } = await openRole(browser, "TECHNICIAN");
  const forbiddenRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/^\\/api\\/v1\\/(?:demands|projects|work-packages|medium-term)(?:\\/|$)/.test(url.pathname)) {
      forbiddenRequests.push(url.pathname);
    }
  });

  try {
    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("TECHNICIAN");
    await expect(page.locator(".main-nav").getByRole("button", { name: "Mon horaire" })).toBeVisible();
    for (const label of ["Demandes", "Projets", "Moyen terme", "Delivery"]) {
      await expect(page.locator(".main-nav").getByRole("button", { name: label, exact: true })).toHaveCount(0);
    }

    // Planning reste utilisable, mais aucun lien ne doit monter le détail Demandes.
    await page.locator(".main-nav").getByRole("button", { name: "Planning opérationnel" }).click();
    await expect(page.getByRole("button", { name: "Voir la demande" })).toHaveCount(0);

    for (const view of ["demands", "projects", "medium-term", "delivery"]) {
      await page.goto(`/?view=${view}`);
      await expect(page.getByText("Accès refusé", { exact: true })).toBeVisible();
      await expect(page.locator(".demand-detail-modal, .demands-workspace, .projects-page, .medium-term-page, .delivery-page")).toHaveCount(0);
    }
    await page.goto("/#/work-packages");
    await expect(page.getByText("Accès refusé", { exact: true })).toBeVisible();
    expect(forbiddenRequests).toEqual([]);
  } finally {
    await context.close();
  }
});

test("706B — les permissions du serveur conservent les modules des administrateurs", async ({ browser }) => {
  const { context, page } = await openRole(browser, "ADMIN");
  try {
    await page.goto("/?view=demands");
    await expect(page.locator(".sidebar-footer")).toContainText("ADMIN");
    for (const label of ["Demandes", "Projets", "Moyen terme", "Delivery"]) {
      await expect(page.locator(".main-nav").getByRole("button", { name: label, exact: true })).toBeVisible();
    }
    await expect(page.getByText("Accès refusé", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();
  } finally {
    await context.close();
  }
});
