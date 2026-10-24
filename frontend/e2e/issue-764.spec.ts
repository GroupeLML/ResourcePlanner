import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function addDaysIso(start: string, days: number) {
  const value = new Date(`${start}T12:00:00`);
  value.setDate(value.getDate() + days);
  return value.toISOString().slice(0, 10);
}

test("764 affiche les fériés globaux, ciblés et individuels dans Planning et la fenêtre détachée", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  let holidayWeek: string | null = null;

  await context.route("**/api/v1/resource-classes", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { code: "INSTALL_ELEC", label: "Installation électrique", active: true },
        { code: "AUTOMATION", label: "Automation", active: true },
      ]),
    });
  });

  await context.route("**/api/v1/planning/actions?**", (route) => route.fulfill({ json: [] }));

  await context.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const template = body.resources?.[0];
    if (!template) throw new Error("764 requires one resource template");
    await route.fulfill({
      response,
      json: {
        ...body,
        resources: [
          {
            ...template,
            id: "R-764-ELEC",
            name: "Électricien 764",
            resource_class: "INSTALL_ELEC",
            planning_subgroup_code: "COMPANION",
            planning_subgroup_label: "Compagnons électriciens",
          },
          {
            ...template,
            id: "R-764-AUTO",
            name: "Automation 764",
            resource_class: "AUTOMATION",
            planning_subgroup_code: null,
            planning_subgroup_label: null,
          },
        ],
        shifts: [],
        segments: [],
        pending_loads: [],
      },
    });
  });

  await context.route("**/api/v1/planning/capacity-grid?**", async (route) => {
    const url = new URL(route.request().url());
    const start = url.searchParams.get("start");
    const end = url.searchParams.get("end");
    if (!start || !end) throw new Error("764 requires bounded Planning week");
    if (holidayWeek === null) holidayWeek = start;
    const activeHolidayWeek = start === holidayWeek;

    const day = (offset: number, kind: "ELEC" | "AUTO") => {
      const iso = addDaysIso(start, offset);
      if (!activeHolidayWeek) {
        return {
          day: iso,
          capacity_hours: 8,
          confirmed_hours: 0,
          tentative_hours: 0,
          outside_standard_hours: 0,
          total_hours: 0,
          prudent_free: 8,
          available: true,
          overloaded: false,
          reason: null,
          reason_code: null,
          reason_label: null,
          holiday_scope: null,
        };
      }
      if (offset === 0) {
        return {
          day: iso,
          capacity_hours: 0,
          confirmed_hours: 0,
          tentative_hours: 0,
          outside_standard_hours: 0,
          total_hours: 0,
          prudent_free: 0,
          available: false,
          overloaded: false,
          reason: "Jour férié",
          reason_code: "HOLIDAY",
          reason_label: "Action de grâce",
          holiday_scope: "GLOBAL",
        };
      }
      if (offset === 1 && kind === "ELEC") {
        return {
          day: iso,
          capacity_hours: 0,
          confirmed_hours: 0,
          tentative_hours: 0,
          outside_standard_hours: 0,
          total_hours: 0,
          prudent_free: 0,
          available: false,
          overloaded: false,
          reason: "Jour férié",
          reason_code: "HOLIDAY",
          reason_label: "Férié installation",
          holiday_scope: "RESOURCE_CLASS",
        };
      }
      if (offset === 2 && kind === "AUTO") {
        return {
          day: iso,
          capacity_hours: 0,
          confirmed_hours: 0,
          tentative_hours: 0,
          outside_standard_hours: 0,
          total_hours: 0,
          prudent_free: 0,
          available: false,
          overloaded: false,
          reason: "Jour férié",
          reason_code: "HOLIDAY",
          reason_label: "Jour férié",
          holiday_scope: "RESOURCE",
        };
      }
      if (offset === 3 && kind === "AUTO") {
        return {
          day: iso,
          capacity_hours: 0,
          confirmed_hours: 0,
          tentative_hours: 0,
          outside_standard_hours: 0,
          total_hours: 0,
          prudent_free: 0,
          available: false,
          overloaded: false,
          reason: "Vacances",
          reason_code: "VACATION",
          reason_label: "Vacances",
          holiday_scope: null,
        };
      }
      return {
        day: iso,
        capacity_hours: 8,
        confirmed_hours: 0,
        tentative_hours: 0,
        outside_standard_hours: 0,
        total_hours: 0,
        prudent_free: 8,
        available: true,
        overloaded: false,
        reason: null,
        reason_code: null,
        reason_label: null,
        holiday_scope: null,
      };
    };

    const resource = (id: string, name: string, resourceClass: string, kind: "ELEC" | "AUTO") => {
      const days = Array.from({ length: 7 }, (_, index) => day(index, kind));
      return {
        resource_id: id,
        resource_name: name,
        resource_class: resourceClass,
        capacity_hours: days.reduce((sum, row) => sum + row.capacity_hours, 0),
        confirmed_hours: 0,
        tentative_hours: 0,
        outside_standard_hours: 0,
        prudent_free: days.reduce((sum, row) => sum + row.prudent_free, 0),
        overloaded: false,
        days,
      };
    };

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        start,
        end,
        resources: [
          resource("R-764-ELEC", "Électricien 764", "INSTALL_ELEC", "ELEC"),
          resource("R-764-AUTO", "Automation 764", "AUTOMATION", "AUTO"),
        ],
        segment_diagnostics: [],
      }),
    });
  });

  try {
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();

    const globalHeader = page.locator('.day-header[data-holiday-scope="GLOBAL"]');
    await expect(globalHeader).toHaveCount(1);
    await expect(globalHeader).toContainText("Jour férié");
    await expect(globalHeader).toContainText("Action de grâce");

    const elec = page.locator('.resource-row').filter({ hasText: "Électricien 764" }).first();
    const auto = page.locator('.resource-row').filter({ hasText: "Automation 764" }).first();
    const week = holidayWeek;
    if (!week) throw new Error("holiday week was not captured");

    const globalDay = week;
    const classDay = addDaysIso(week, 1);
    const individualDay = addDaysIso(week, 2);
    const vacationDay = addDaysIso(week, 3);

    await expect(elec.locator(`.planning-day-cell[data-day="${globalDay}"] .holiday-cell-badge`)).toContainText("Action de grâce");
    await expect(auto.locator(`.planning-day-cell[data-day="${globalDay}"] .holiday-cell-badge`)).toContainText("Action de grâce");

    await expect(elec.locator(`.planning-day-cell[data-day="${classDay}"] .holiday-cell-badge`)).toContainText("Férié installation");
    await expect(auto.locator(`.planning-day-cell[data-day="${classDay}"] .holiday-cell-badge`)).toHaveCount(0);

    await expect(auto.locator(`.planning-day-cell[data-day="${individualDay}"] .holiday-cell-badge`)).toContainText("Jour férié");
    await expect(elec.locator(`.planning-day-cell[data-day="${individualDay}"] .holiday-cell-badge`)).toHaveCount(0);

    await expect(auto.locator(`.planning-day-cell[data-day="${vacationDay}"]`)).toContainText("Vacances");
    await expect(auto.locator(`.planning-day-cell[data-day="${vacationDay}"] .holiday-cell-badge`)).toHaveCount(0);

    const [detached] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
    ]);
    await detached.waitForLoadState("domcontentloaded");
    await expect(detached.locator('.day-header[data-holiday-scope="GLOBAL"]')).toContainText("Action de grâce");
    await expect(
      detached
        .locator('.resource-row')
        .filter({ hasText: "Électricien 764" })
        .first()
        .locator(`.planning-day-cell[data-day="${classDay}"] .holiday-cell-badge`),
    ).toContainText("Férié installation");

    await page.getByRole("button", { name: /Suivante/ }).click();
    await expect(page.locator('.day-header[data-holiday-scope="GLOBAL"]')).toHaveCount(0);
    await page.getByRole("button", { name: /Précédente/ }).click();
    await expect(page.locator('.day-header[data-holiday-scope="GLOBAL"]')).toContainText("Action de grâce");

    await detached.close();
  } finally {
    await context.close();
  }
});
