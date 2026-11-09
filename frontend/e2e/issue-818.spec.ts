import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";
const FIXTURE_COUNT = 36;

test("818 garde les cartes Demandes lisibles et fait défiler la liste à fort volume", async ({ browser }) => {
  test.setTimeout(120_000);
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    viewport: { width: 1440, height: 900 },
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();

  try {
    const seedListResponse = await page.request.get("/api/v1/demands?scope=global");
    expect(seedListResponse.ok(), await seedListResponse.text()).toBeTruthy();
    const seedList = await seedListResponse.json() as Array<Record<string, any>>;
    expect(seedList.length).toBeGreaterThan(0);
    const seedDemand = seedList[0];

    const seedDetailResponse = await page.request.get(
      `/api/v1/demands/${encodeURIComponent(String(seedDemand.number))}/detail?scope=global`,
    );
    expect(seedDetailResponse.ok(), await seedDetailResponse.text()).toBeTruthy();
    const seedDetail = await seedDetailResponse.json() as Record<string, any>;

    const seedContactsResponse = await page.request.get(
      `/api/v1/demands/${encodeURIComponent(String(seedDemand.number))}/business-contacts`,
    );
    expect(seedContactsResponse.ok(), await seedContactsResponse.text()).toBeTruthy();
    const seedContacts = await seedContactsResponse.json();

    const fixtureDemands = Array.from({ length: FIXTURE_COUNT }, (_, index) => {
      const sequence = String(index + 1).padStart(2, "0");
      return {
        ...seedDemand,
        number: `DMO-818-${sequence}`,
        status: "Brouillon",
        effective_status: "Brouillon",
        terminal: false,
        project_number: `P-818-${sequence}`,
        project_name: `Projet volume 818 ${sequence}`,
        client: "Client volume 818",
        description: `Demande de volume #818 ${sequence}`,
        desired_start: "2026-11-09",
        desired_end: "2026-11-13",
        resource_count: index % 3 + 1,
        confirmation: index % 2 === 0 ? "Confirmée" : "Tentative",
        available_quick_actions: index === 0 ? ["submit"] : [],
        cancellation_state: null,
        created_at: new Date(Date.UTC(2026, 10, 9, 12, 0, index)).toISOString(),
        lines: [],
        line_mode: false,
      };
    });

    const fixtureByNumber = new Map(
      fixtureDemands.map((demand) => [String(demand.number), demand]),
    );

    await page.route("**/api/v1/demands?**", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(fixtureDemands),
      });
    });

    await page.route("**/api/v1/demands/*/detail**", async (route) => {
      const pathParts = new URL(route.request().url()).pathname.split("/");
      const demandNumber = decodeURIComponent(pathParts[pathParts.length - 2]);
      const demand = fixtureByNumber.get(demandNumber);
      if (!demand) {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...seedDetail,
          demand: {
            ...(seedDetail.demand ?? {}),
            ...demand,
          },
        }),
      });
    });

    await page.route("**/api/v1/demands/*/business-contacts", async (route) => {
      const pathParts = new URL(route.request().url()).pathname.split("/");
      const demandNumber = decodeURIComponent(pathParts[pathParts.length - 2]);
      if (!fixtureByNumber.has(demandNumber)) {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(seedContacts),
      });
    });

    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: "Demandes" }).click();
    await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();

    const listPanel = page.locator(".demand-list-panel");
    const heading = listPanel.locator(".demand-list-heading");
    const list = listPanel.locator(".demand-list");
    const cards = list.locator(".demand-card");

    await expect(cards).toHaveCount(FIXTURE_COUNT);
    await expect(heading.locator("strong")).toHaveText(`${FIXTURE_COUNT} demande(s)`);
    await expect(heading.locator("span")).toHaveText(`${FIXTURE_COUNT} au total`);

    const geometry = await list.evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
    }));
    expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);

    const representativeIndexes = [0, Math.floor(FIXTURE_COUNT / 2), FIXTURE_COUNT - 1];
    const heights: number[] = [];
    for (const index of representativeIndexes) {
      const card = cards.nth(index);
      const box = await card.boundingBox();
      expect(box).not.toBeNull();
      heights.push(box!.height);
      expect(box!.height).toBeGreaterThan(70);
      await expect(card.locator(".demand-card-topline strong")).toBeVisible();
      await expect(card.locator(".demand-project strong")).toBeVisible();
    }

    const quickActionCard = cards.filter({ hasText: "DMO-818-01" });
    await expect(quickActionCard.locator(".demand-card-quick-actions")).toBeVisible();
    const regularCardHeight = heights[1];
    const quickActionHeight = (await quickActionCard.boundingBox())!.height;
    expect(quickActionHeight).toBeGreaterThan(regularCardHeight);

    const baselineCardNumber = "DMO-818-19";
    const baselineCard = cards.filter({ hasText: baselineCardNumber });
    const highVolumeHeight = (await baselineCard.boundingBox())!.height;

    const search = page.getByLabel("Recherche");
    await search.fill(baselineCardNumber);
    await expect(cards).toHaveCount(1);
    await expect(heading.locator("strong")).toHaveText("1 demande(s)");
    await expect(heading.locator("span")).toHaveText(`${FIXTURE_COUNT} au total`);
    const naturalHeight = (await cards.first().boundingBox())!.height;
    expect(highVolumeHeight).toBeGreaterThanOrEqual(naturalHeight * 0.9);

    await search.fill("");
    await expect(cards).toHaveCount(FIXTURE_COUNT);

    const headingBeforeScroll = await heading.boundingBox();
    expect(headingBeforeScroll).not.toBeNull();
    await list.evaluate((node) => {
      node.scrollTop = node.scrollHeight;
    });
    await expect.poll(() => list.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
    const headingAfterScroll = await heading.boundingBox();
    expect(headingAfterScroll).not.toBeNull();
    expect(Math.abs(headingAfterScroll!.y - headingBeforeScroll!.y)).toBeLessThanOrEqual(1);
    await expect(heading).toBeVisible();

    const bottomNumber = "DMO-818-01";
    const bottomCard = cards.filter({ hasText: bottomNumber });
    await bottomCard.scrollIntoViewIfNeeded();
    await expect(bottomCard).toBeVisible();
    await expect(bottomCard).toContainText("P-818-01");
    await bottomCard.locator(".demand-card-select").click();
    await expect(bottomCard).toHaveClass(/selected/);
    await expect(page.locator(".demand-editor-heading h2")).toHaveText(bottomNumber);

    const selectedBox = await bottomCard.boundingBox();
    expect(selectedBox).not.toBeNull();
    expect(selectedBox!.height).toBeGreaterThan(70);
  } finally {
    await context.close();
  }
});
