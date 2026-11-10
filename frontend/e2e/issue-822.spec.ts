import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
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

async function navigateDemands(page: Page) {
  await page.locator(".main-nav").getByRole("button", { name: /Demandes/i }).click();
  await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();
}

async function createFixtureDemand(page: Page, description: string) {
  const day = localIso(new Date());
  const response = await page.request.post("/api/v1/demands", {
    data: {
      project_number: "P-251",
      task_code: "210",
      desired_start: day,
      desired_end: day,
      estimated_hours: 8,
      description,
      submit: false,
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return String((await response.json()).demand_number);
}

test("822 ouvre Demandes sans sélection puis charge uniquement après intention explicite", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  const suffix = Date.now().toString(36);
  const firstDemand = await createFixtureDemand(page, `#822 sélection explicite ${suffix}`);
  await createFixtureDemand(page, `#822 liste secondaire ${suffix}`);

  const detailRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") return;
    const pathname = new URL(request.url()).pathname;
    const match = pathname.match(/^\/api\/v1\/demands\/([^/]+)\/detail$/);
    if (match) detailRequests.push(decodeURIComponent(match[1]));
  });

  try {
    await navigateDemands(page);

    const list = page.locator(".demand-list");
    await expect(list.locator(".demand-card")).toHaveCount(2);
    await expect(
      page.getByText("Aucune demande actuellement sélectionnée", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Sélectionnez une demande dans la liste de gauche ou créez un nouveau brouillon.",
        { exact: true },
      ),
    ).toBeVisible();
    expect(detailRequests).toEqual([]);

    await page.getByLabel("Recherche").fill(suffix);
    await page.getByLabel("Tri").selectOption("oldest");
    await expect(list.locator(".demand-card")).toHaveCount(2);
    expect(detailRequests).toEqual([]);

    const firstCard = list.locator(".demand-card").filter({ hasText: firstDemand });
    await expect(firstCard).toHaveCount(1);
    await firstCard.locator(".demand-card-select").click();

    await expect(
      page.locator(`.demand-detail-context[data-demand-number="${firstDemand}"]`),
    ).toBeVisible();
    await expect.poll(() => detailRequests).toContain(firstDemand);

    const requestsAfterExplicitSelection = detailRequests.length;
    await page.getByRole("button", { name: /Nouvelle demande/ }).click();
    const editor = page.locator(".demand-editor-form");
    await expect(editor.getByRole("heading", { name: "Nouvelle demande" })).toBeVisible();
    await editor.getByRole("button", { name: "Annuler", exact: true }).click();

    await expect(
      page.getByText("Aucune demande actuellement sélectionnée", { exact: true }),
    ).toBeVisible();
    expect(detailRequests).toHaveLength(requestsAfterExplicitSelection);

    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await navigateDemands(page);
    await expect(
      page.getByText("Aucune demande actuellement sélectionnée", { exact: true }),
    ).toBeVisible();
    expect(detailRequests).toHaveLength(requestsAfterExplicitSelection);
  } finally {
    await context.close();
  }
});
